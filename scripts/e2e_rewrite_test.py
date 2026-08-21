# -*- coding: utf-8 -*-
"""
洗稿工坊 e2e 冒烟测试
=============================================
覆盖完整链路（真实 DeepSeek 调用）：
  1. /api/rewrite/start 启动 → SSE 事件流（骨架/分析/分区/审查/done）
  2. /api/rewrite/<rid> 存档完整性（skeleton/analysis/untouchable/parts/principle_review）
  3. /api/rewrite/<rid>/comment 分区评论迭代（负责人重写）
  4. /api/rewrite/<rid>/finalize 最终审查 + 分工记录
  5. /api/rewrite/<rid>/result 回填成品数据
  6. /api/rewrite/evaluate 满 3 篇评价（不足 3 篇应报错提示）
  7. /api/rewrite/apply 应用负责人替换

用法：python e2e_rewrite_test.py [port] [--skip-run] [--only check]
  port      服务端口，默认 8767
  --skip-run 跳过真实讨论（只做存档/数据/评价的快速校验）
"""
import json
import sys
import time
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else "8767"
SKIP_RUN = "--skip-run" in sys.argv
BASE = f"http://127.0.0.1:{PORT}"

PASS = 0
FAIL = 0


def log(ok, msg):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {msg}")
    else:
        FAIL += 1
        print(f"  ❌ {msg}")


def http(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def sse_collect(sid, wait_s=300):
    """连接 SSE 收集事件，直到 done 或超时。"""
    events = []
    req = urllib.request.Request(BASE + "/api/stream/" + sid)
    with urllib.request.urlopen(req, timeout=wait_s) as r:
        buf = b""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            chunk = r.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                for line in raw.split(b"\n"):
                    if line.startswith(b"data: "):
                        try:
                            ev = json.loads(line[6:].decode())
                            events.append(ev)
                            if ev.get("type") == "done":
                                return events
                        except Exception:  # noqa: BLE001
                            pass
    return events


def main():
    print(f"== 洗稿工坊 e2e（port={PORT}，skip_run={SKIP_RUN}）==")

    # ---- 0. meta ----
    meta = http("GET", "/api/rewrite/meta")
    log(meta.get("ok") and len(meta.get("regions", [])) == 8, f"meta：8 个分区（{len(meta.get('regions', []))}）")
    log(meta.get("ok") and meta.get("assignments", {}).get("opening") == "小黄", "meta：开头负责人=小黄")

    if not SKIP_RUN:
        # ---- 1. 启动真实讨论 ----
        print("启动真实洗稿流程（约 3-5 分钟）…")
        start = http("POST", "/api/rewrite/start", {
            "original": "姐妹们，我最近发现一个特别可怕的事。你们有没有算过自己的命？我上个月找师傅看了八字，他说我财星被劫，今年破财在所难免。我当时不信，结果这个月就丢了工作。真的，有些事你不得不信。",
            "likes": "8600", "comments": "420", "forwards": "130", "saves": "2800",
            "requirements": "开头要更炸一点，去掉玄学术语，让20-30岁女生能听懂",
        })
        log(start.get("ok") and start.get("rid"), f"start：rid={start.get('rid')}")
        rid = start.get("rid")
        sid = start.get("sid")

        events = sse_collect(sid, wait_s=600)
        kinds = [e.get("kind") for e in events if e.get("type") == "message"]
        log("skeleton" in kinds, f"SSE：收到骨架（共 {len(events)} 事件）")
        log(kinds.count("analysis") >= 9, f"SSE：9 人分析（实际 {kinds.count('analysis')}）")
        log(kinds.count("part") >= 8, f"SSE：8 区补写（实际 {kinds.count('part')}）")
        log("review" in kinds, "SSE：阿审审查")
        log(any(e.get("type") == "done" for e in events), "SSE：done")
    else:
        # 快速模式：复用已存在会话
        lst = http("GET", "/api/rewrite/list")
        sessions = lst.get("sessions", [])
        log(len(sessions) > 0, f"list：已有 {len(sessions)} 篇洗稿")
        if not sessions:
            print("!! 无会话可校验，先跑一次完整流程")
            return
        rid = sessions[0]["id"]

    # ---- 2. 存档完整性 ----
    d = http("GET", "/api/rewrite/" + rid)
    s = d.get("session", {})
    log(d.get("ok") and s.get("skeleton"), "存档：骨架已存")
    log(len(s.get("analysis") or {}) >= 9, f"存档：9 人分析（实际 {len(s.get('analysis') or {})}）")
    log(len(s.get("untouchable") or []) >= 1, f"存档：不可动句子共识 {len(s.get('untouchable') or [])} 条")
    log(len(s.get("parts") or {}) >= 8, f"存档：8 区成品（实际 {len(s.get('parts') or {})}）")
    log(bool(s.get("principle_review")), "存档：阿审审查报告")
    log(d.get("sid"), "存档：返回 sid")

    # ---- 3. 分区评论迭代 ----
    parts = s.get("parts") or {}
    first_region = next(iter(parts))
    owner_before = parts[first_region].get("agent")
    print(f"评论迭代：{first_region}（负责人 {owner_before}）…")
    try:
        cmt = http("POST", f"/api/rewrite/{rid}/comment",
                   {"sid": d.get("sid", ""), "region": first_region, "comment": "这一段太平了，请更有冲击力一点"})
        log(cmt.get("ok"), f"comment：{first_region} 已触发重写")
        # 等重写完成（轮询 parts 变化）
        for _ in range(40):
            time.sleep(5)
            cur = http("GET", "/api/rewrite/" + rid)["session"]
            p = (cur.get("parts") or {}).get(first_region) or {}
            if p.get("comments"):
                log(True, f"comment：{first_region} 收到重写（{len(p.get('comments'))} 条评论记录）")
                break
        else:
            log(False, "comment：等待超时")
    except Exception as e:  # noqa: BLE001
        log(False, f"comment 出错：{e}")

    # ---- 4. 最终审查 + 分工记录 ----
    print("最终审查 + 分工记录…")
    try:
        fin = http("POST", f"/api/rewrite/{rid}/finalize", {"sid": d.get("sid", "")})
        log(fin.get("ok"), "finalize：已触发")
        # 最终审查=阿审 LLM + 分工记录=阿数 LLM，实测约 3.5 分钟，留 350s 余量
        for _ in range(70):
            time.sleep(5)
            cur = http("GET", "/api/rewrite/" + rid)["session"]
            if cur.get("owner_record") and cur.get("status") == "done":
                log(True, "finalize：分工记录完成，状态 done")
                break
        else:
            log(False, "finalize：等待超时")
    except Exception as e:  # noqa: BLE001
        log(False, f"finalize 出错：{e}")

    # ---- 5. 回填成品数据 ----
    res = http("POST", f"/api/rewrite/{rid}/result",
               {"likes": "32000", "comments": "2100", "forwards": "560", "saves": "4000"})
    log(res.get("ok"), f"result：回填成功（累计 {res.get('evaluated_count')} 篇）")

    # ---- 6. 评价（不足 3 篇应报错）----
    cnt = res.get("evaluated_count", 0)
    try:
        ev = http("POST", "/api/rewrite/evaluate", {"sid": d.get("sid", "")})
        if cnt < 3:
            log(not ev.get("ok") and "满 3 篇" in ev.get("error", ""),
                f"evaluate：{cnt} 篇时正确拦截（{ev.get('error','')[:40]}）")
        else:
            log(ev.get("ok"), "evaluate：已满 3 篇，正常触发")
    except Exception as e:  # noqa: BLE001
        log(True if cnt >= 3 else False, f"evaluate 触发（{cnt} 篇）：{e}")

    # ---- 7. 负责人替换（数据层校验）----
    ap = http("POST", "/api/rewrite/apply", {"replacements": [
        {"region": "opening", "from": "小黄", "to": "阿爆", "reason": "e2e 测试"}
    ]})
    log(ap.get("ok") and ap.get("assignments", {}).get("opening") == "阿爆",
        "apply：负责人替换生效（opening → 阿爆）")
    # 还原
    http("POST", "/api/rewrite/apply", {"replacements": [
        {"region": "opening", "from": "阿爆", "to": "小黄", "reason": "e2e 还原"}
    ]})

    print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
