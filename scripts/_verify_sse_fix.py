# -*- coding: utf-8 -*-
"""SSE 任务接力修复的快速验证（进程内起服 + monkeypatch 评分线程，不跑真实 LLM）。

验证点：
1. 讨论结束（finished=True, phase=idle, 末事件 done）后直接连 SSE → 重放历史后断开（原语义不破）
2. POST /api/score 后立即连 SSE（模拟前端 openStream 重连）→ 连接不被掐断，能收到 score + score_done
3. score 结束后会话恢复 finished=True / phase=idle（供刷新/清理正确判定）
"""
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "scripts")

import server  # noqa: E402

PORT = 0  # 随机端口，避免与残留服务冲突（Windows 上 SO_REUSEADDR 允许重复绑定导致请求打到旧服务）
BASE = "http://127.0.0.1:{port}"
FAILED = []


def check(cond, msg):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {msg}")
    if not cond:
        FAILED.append(msg)


def fake_run_score(sid, script):
    """模拟真实 _run_score 的 finally 行为（end_phase + finished=True + push score_done）。"""
    s = server.SESSIONS.get(sid)
    s.push({"type": "score", "name": "测试专家", "title": "测试",
            "score": 8.5, "reason": "测试理由", "text": "测试评分文本"})
    s.end_phase()
    s.finished = True
    s.push({"type": "score_done"})


server._run_score = fake_run_score  # 关键：替换评分线程为假实现

from werkzeug.serving import make_server  # noqa: E402

srv = make_server("127.0.0.1", PORT, server.app, threaded=True)
PORT = srv.socket.getsockname()[1]
BASE = BASE.format(port=PORT)
print(f"[info] 测试服务已启动，随机端口 {PORT}")
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
time.sleep(0.5)


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def read_events(sid, stop_types, max_wait=30):
    """连 SSE 读到 stop 类型事件或 EOF/超时，返回 (events, closed_by_server)。"""
    events = []
    req = urllib.request.Request(BASE + f"/api/stream/{sid}")
    t0 = time.time()
    closed = False
    with urllib.request.urlopen(req, timeout=10) as r:
        buf = b""
        while time.time() - t0 < max_wait:
            try:
                chunk = r.read(1)
            except Exception:
                closed = True
                break
            if not chunk:
                closed = True
                break
            buf += chunk
            if buf.endswith(b"\n\n"):
                text = buf.decode("utf-8").strip()
                buf = b""
                for line in text.splitlines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        m = json.loads(line[6:])
                    except Exception:
                        continue
                    events.append(m)
                    if m.get("type") in stop_types:
                        return events, closed
    return events, closed


try:
    # ===== 构造一个「讨论已结束」的会话 =====
    s = server.Session()
    s.sid = "test_sse_relay"
    s.script = "测试文稿"
    server.SESSIONS[s.sid] = s
    s.finished = True
    s.end_phase()
    s.push({"type": "done"})
    print(f"[debug] server module file={server.__file__}  id(SESSIONS)={id(server.SESSIONS)}  keys={list(server.SESSIONS.keys())}")
    print("== 场景 1：已结束会话直接连 SSE（应重放后断开）==")
    evs1, closed1 = read_events(s.sid, {"__never__"}, max_wait=5)
    t1 = [e.get("type") for e in evs1]
    print(f"  事件类型: {t1}")
    for e in evs1[:5]:
        print(f"  [debug] {str(e)[:200]}")
    check("done" in t1, "重放了历史（含 done）")
    check(closed1, "连接被服务端正常断开（原语义不破）")

    # ===== 场景 2：POST /api/score 后立即连 SSE（模拟前端 openStream 重连）=====
    print("== 场景 2：评分任务接力（核心修复验证）==")
    r = post("/api/score", {"sid": s.sid, "script": "测试文稿"})
    check(r.get("ok") is True, "POST /api/score 返回 ok")
    check(server.SESSIONS[s.sid].finished is False, "handler 已同步重置 finished=False（竞态窗口关闭）")
    evs2, closed2 = read_events(s.sid, {"score_done"}, max_wait=30)
    t2 = [e.get("type") for e in evs2]
    score_events = [e for e in evs2 if e.get("type") == "score"]
    print(f"  收到事件类型: {t2}")
    check("score" in t2, f"收到评分事件（{len(score_events)} 条，测试桩为 1 条）")
    check("score_done" in t2, "收到 score_done 收尾事件")
    check(not closed2, "连接在收到 score_done 前未被服务端掐断")

    # ===== 场景 3：评分结束后会话状态 =====
    print("== 场景 3：评分结束状态恢复 ==")
    st = server.SESSIONS[s.sid]
    check(st.finished is True, "finished 恢复为 True（刷新/清理可正确判定）")
    check(st.phase == "idle", "phase 恢复为 idle")
    check(st.history[-1].get("type") == "score_done", "末事件为 score_done")

    # ===== 场景 4：再接力一次评论（走 handler 内 try_begin 路径）=====
    print("== 场景 4：评分后再接评论（连续接力）==")
    server._run_comment = None  # 不替换，用真实评论会调 LLM —— 改为直接模拟 handler 前置状态
    # 直接验证 try_begin 在任务开始后重置 finished
    st.try_begin("comment")
    check(st.finished is False, "try_begin 成功后 finished=False")
    st.end_phase()
    st.finished = True
    st.push({"type": "comment_done"})
    check(st.finished is True and st.phase == "idle", "评论结束后状态恢复")

    # ===== 场景 5：重新讨论（新会话）不受影响 =====
    s2 = server.Session()
    s2.sid = "test_sse_relay_2"
    server.SESSIONS[s2.sid] = s2
    check(s2.finished is False, "新会话初始 finished=False")

finally:
    srv.shutdown()
    # 清理测试会话与磁盘归档
    server.SESSIONS.pop("test_sse_relay", None)
    server.SESSIONS.pop("test_sse_relay_2", None)
    for f in ("test_sse_relay.json", "test_sse_relay_2.json"):
        p = os.path.join("output", "sessions", f)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：")
    for m in FAILED:
        print(f"  - {m}")
    sys.exit(1)
print("全部验证通过 ✅")
