# -*- coding: utf-8 -*-
"""端到端验证：六位专家（含阿证·事实核查派）完整群聊讨论"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"

SCRIPT = """家人们，今天跟大家聊聊"为什么有的人越努力越穷"。
我干了8年命理，告诉你一个铁律：八字里"财星被劫"的人，这辈子注定发不了大财，再怎么努力都是白费。
我有个客户，八字财星被劫，我给她做了个法事，她三个月后就升职加薪了。心理学研究表明，80%的人财运不好都是因为原生家庭。所以赶紧来找我，我保证帮你逆天改命，不灵不要钱！"""


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def stream(sid, timeout=900):
    req = urllib.request.Request(BASE + f"/api/stream/{sid}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        events = []
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            events.append(ev)
            if ev.get("type") in ("done", "review_done", "score_done", "learn_done"):
                break
        return events


def main():
    # 1. 发起讨论
    r = post("/api/start", {"script": SCRIPT})
    assert r["ok"], r
    sid = r["sid"]
    members = [m["name"] for m in r["members"]]
    print("群成员:", members)
    assert "阿证" in members, "阿证不在成员列表!"
    assert len(members) == 9, f"应 7 专家 + 阿记 + 阿数 = 9 人，实际 {len(members)}"

    # 2. 拉取完整讨论流
    print("拉取讨论流（六专家 × 3 轮，预计几分钟）...")
    t0 = time.time()
    events = stream(sid)
    print(f"流结束，耗时 {time.time()-t0:.0f}s，事件数 {len(events)}")

    # 3. 统计发言
    from collections import Counter
    msgs = Counter()
    finals = Counter()
    for ev in events:
        if ev.get("type") == "message":
            msgs[ev.get("name")] += 1
        elif ev.get("type") == "final":
            finals[ev.get("name")] += 1
    print("\n--- 发言统计（Round1+Round2 各 1 条）---")
    for n in members:
        print(f"  {n}: 讨论发言 {msgs.get(n, 0)} 条 / 终稿 {finals.get(n, 0)} 份")

    # 4. 断言八专家全部参与
    for n in ["阿沁", "老周", "阿爆", "小黄", "爆哥", "阿骨", "阿证", "阿导"]:
        assert msgs.get(n, 0) >= 2, f"{n} 发言不足!"
        assert finals.get(n, 0) == 1, f"{n} 终稿缺失!"
    assert msgs.get("阿记", 0) == 0, "阿记不应参与讨论发言"
    assert msgs.get("阿数", 0) == 0, "阿数不应参与讨论发言"

    # 5. 阿证的发言内容检查（是否真的在做事实核查）
    azheng = [ev for ev in events if ev.get("name") == "阿证"]
    all_text = " ".join(ev.get("text", "") for ev in azheng)
    print("\n--- 阿证发言片段检查 ---")
    for kw in ["✅", "⚠", "❌", "核查", "依据", "处置", "红线", "改写"]:
        if kw in all_text:
            print(f"  命中关键词: {kw}")
    assert any(k in all_text for k in ["核查", "依据", "处置"]), "阿证没有执行事实核查!"

    # 6. 校验存档
    import glob
    files = sorted(glob.glob("output/discussion_*.md"), reverse=True)
    latest = files[0]
    md = open(latest, encoding="utf-8").read()
    print(f"\n存档文件: {latest}（{len(md)} 字符）")
    assert "阿证（事实核查派）" in md, "存档中没有阿证!"
    print("\n✅ 讨论流程端到端验证通过")


if __name__ == "__main__":
    main()
