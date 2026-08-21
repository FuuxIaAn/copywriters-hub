# -*- coding: utf-8 -*-
"""端到端实测：讨论 -> 采纳 -> 评分 -> 复盘 -> 验证 stats/反馈档案。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"

SCRIPT = ("家人们，今天给大家推荐一款榨汁杯。这款榨汁杯只要199块，就能让你一年省下两千多块钱！"
          "每天一杯果汁，健康又省钱。它续航强、充电快、杯身小巧，出门带着特别方便。"
          "现在下单还有优惠，赶紧点击下方链接购买吧！")
FINAL = ("所有每天在楼下奶茶店花25块买水果糖水的上班族注意了：你一年喝掉9125块，喝的不是水果，是房租和代言费。"
         "今天这个东西199块，让你把这9000多块全省回来。续航强、充电快、杯子小巧，出门带着特别方便。"
         "链接就在下方，下单立减20。")

def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def stream_until(sid, stop_types, timeout=900):
    """读取 SSE 直到遇到 stop_types 中的事件类型，返回事件列表。"""
    events = []
    req = urllib.request.Request(BASE + f"/api/stream/{sid}")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        buf = b""
        while time.time() - t0 < timeout:
            chunk = r.read(1)
            if not chunk:
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
                        return events
    return events

def summary(events):
    from collections import Counter
    c = Counter(e.get("type") for e in events)
    return dict(c)

# 1) 发起讨论
print("== [1/6] 发起讨论 ==")
r = post("/api/start", {"script": SCRIPT})
assert r["ok"], r
sid = r["sid"]
print("sid:", sid, "| members:", [m["name"] for m in r["members"]])

evs = stream_until(sid, {"done"})
c = summary(evs)
print("事件统计:", c)
assert c.get("message", 0) == 16 and c.get("final", 0) == 8, "讨论轮次数量不符"
print("✔ 讨论完成：16 条发言 + 8 份终稿")
# 抽查发言格式（应含【改写】）
first_msgs = [e["text"] for e in evs if e["type"] == "message"]
assert any("【改写】" in t for t in first_msgs), "发言格式未包含【改写】"
print("✔ Round1 发言已采用【改写】+【理由】格式")
for t in first_msgs[:1]:
    print("--- 样例发言 ---")
    print(t[:400])
    print("----------------")

# 2) 采纳
print("\n== [2/6] 记录采纳 x2 ==")
a1 = post("/api/adopt", {"sid": sid, "name": "阿沁", "round": "Round 1",
                         "snippet": "情感共鸣式开头改写", "note": "想验证更抓人"})
a2 = post("/api/adopt", {"sid": sid, "name": "爆哥", "round": "Round 3 终稿",
                         "snippet": "账目对比式开场", "note": ""})
assert a1["ok"] and a2["ok"]
print("✔ 采纳 阿沁 + 爆哥 已记录")

# 3) 评分
print("\n== [3/6] 终稿评分 ==")
r = post("/api/score", {"sid": sid, "script": FINAL})
assert r["ok"], r
evs = stream_until(sid, {"score_done"})
sc = [e for e in evs if e["type"] == "score"]
print("评分条数:", len(sc))
for e in sc:
    print(f"  {e['name']}: {e['score']} 分 | {e['reason'][:40]}")
assert len(sc) == 8, "评分数量不符"
assert all(e["score"] is not None for e in sc), "有专家未给分"
print("✔ 各位专家评分完成")

# 4) 复盘
print("\n== [4/6] 复盘评估 ==")
r = post("/api/review", {"sid": sid, "data": "发布后24h：播放量1.8w，完播率42%（原稿28%），点赞率3.5%，转化率0.8%（较上一条低），评论区有人质疑价格虚高"})
assert r["ok"], r
evs = stream_until(sid, {"review_done"})
reviews = [e for e in evs if e["type"] == "review"]
print("复盘报告条数:", len(reviews))
for e in reviews:
    print(e["text"][:1200])
    print("…" if len(e["text"]) > 1200 else "")
sys_out = [e for e in evs if e["type"] == "system" and "反馈档案已更新" in e.get("text", "")]
if sys_out:
    print("\n反馈档案系统提示:", sys_out[0]["text"])
else:
    print("\n⚠ 未捕获到反馈档案更新提示")
assert reviews, "复盘报告为空"

# 5) 验证 stats.json
print("\n== [5/6] 验证 stats.json ==")
import os as _os
_stats_path = _os.path.join(_os.environ.get("WB_DATA_DIR", ""), "output", "stats.json") if _os.environ.get("WB_DATA_DIR") else "output/stats.json"
with open(_stats_path, "r", encoding="utf-8") as f:
    stats = json.load(f)
print("stats 路径:", _stats_path)
print(json.dumps(stats, ensure_ascii=False, indent=2)[:2000])
assert stats["experts"]["阿沁"]["suggestions"], "阿沁无评估记录"
assert stats["experts"]["阿沁"]["negative_feedback"] or stats["experts"]["阿沁"]["positive_feedback"], "阿沁无反馈档案"
assert stats["scores"], "无评分记录"
print("✔ stats.json 结构完整")

# 6) 验证反馈注入
print("\n== [6/6] 验证反馈档案注入 build_agents ==")
sys.path.insert(0, "scripts")
from server import build_agents
config = json.load(open("config.json", "r", encoding="utf-8"))
agents = build_agents(config)
for a in agents:
    if a.name in ("阿沁", "爆哥"):
        has_fb = "历史反馈档案" in a._system_prompt()
        print(f"  {a.name} 反馈档案已注入: {has_fb}")
        assert has_fb, f"{a.name} 未注入反馈档案"
print("✔ 反馈档案注入成功")

print("\n===== E2E ALL PASSED =====")
