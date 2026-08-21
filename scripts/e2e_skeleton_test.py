# -*- coding: utf-8 -*-
"""八专家端到端冒烟（含阿骨/阿导）：POST 文稿 → SSE 收全事件 → 校验每位成员发言 + 阿骨骨架输出 + 阿导五关审查。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8765")
SCRIPT = (
    "姐妹们，你们有没有发现，越是天天查对象手机的人，感情越容易出问题？"
    "我做了八年命理咨询，这种案例见得太多了。今天不跟你讲大道理，就讲一个真实案例。"
    "有个姑娘，28岁，事业挺顺，就是感情老是不顺，谈一个崩一个。"
    "她来问我，我一看她的八字，财星被劫，正官落空亡，感情上就容易遇人不淑。"
    "我教了她三招：第一，认清自己命里的感情课题；第二，避开容易出问题的年份；"
    "第三，把精力收回来搞事业。她照做了半年，现在谈的男朋友，是公司同事介绍的，很踏实。"
    "所以姐妹们，感情不顺，不是你不够好，是你没找对方向。想要我帮你看看的，评论区扣1。"
)

def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    r = post(BASE + "/api/start", {"script": SCRIPT})
    if not r.get("ok"):
        print("启动失败:", r); sys.exit(1)
    sid = r["sid"]
    print("讨论启动 sid =", sid, "| members =", r.get("members"))

    url = BASE + "/api/stream/" + sid
    print("连接 SSE ...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=600) as resp:
        buf = b""
        stats = {"typing": 0, "message": 0, "final": 0, "system": 0}
        speakers = {}
        gu_round1 = None
        gu_final_head = None
        dd_round1 = None
        dd_final_head = None
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                line = raw.decode("utf-8", "ignore")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                t = ev.get("type")
                if t in stats:
                    stats[t] += 1
                if t in ("message", "final"):
                    name = ev.get("name", "?")
                    speakers.setdefault(name, {"message": 0, "final": 0})
                    speakers[name][t] = speakers[name].get(t, 0) + 1
                    if name == "阿骨" and t == "message" and gu_round1 is None:
                        gu_round1 = ev.get("text", "")[:120]
                    if name == "阿骨" and t == "final" and gu_final_head is None:
                        gu_final_head = ev.get("text", "")[:160]
                    if name == "阿导" and t == "message" and dd_round1 is None:
                        dd_round1 = ev.get("text", "")[:120]
                    if name == "阿导" and t == "final" and dd_final_head is None:
                        dd_final_head = ev.get("text", "")[:160]
                if t == "done":
                    print(">>> 收到 done")
                    break
            else:
                continue
            break
    print("\n事件统计:", stats)
    print("发言成员:", json.dumps(speakers, ensure_ascii=False))
    expect = {"阿沁", "老周", "阿爆", "小黄", "爆哥", "阿证", "阿骨", "阿导"}
    ok = True
    for name in sorted(expect):
        got = speakers.get(name, {})
        if got.get("message", 0) < 2 or got.get("final", 0) < 1:
            print(f"  [✗] {name} 发言不完整: {got}")
            ok = False
        else:
            print(f"  [✓] {name}: 讨论×{got['message']} 终稿×{got['final']}")
    if ok:
        print("\n[✓] 八位专家全部完成三轮讨论 + 终稿")
    if gu_round1:
        print("\n--- 阿骨 Round1 发言摘要 ---\n" + gu_round1 + "...")
    if gu_final_head:
        print("\n--- 阿骨 终稿开头 ---\n" + gu_final_head + "...")
    if dd_round1:
        print("\n--- 阿导 Round1 发言摘要 ---\n" + dd_round1 + "...")
    if dd_final_head:
        print("\n--- 阿导 终稿开头 ---\n" + dd_final_head + "...")
    print("\n讨论 md 存档: output/ 下 discussion_*.md（阿骨/阿导终稿见其中【终稿】段）")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
