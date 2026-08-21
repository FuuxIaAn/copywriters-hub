# -*- coding: utf-8 -*-
"""端到端测试：爆款文案学习闭环（真实调用 DeepSeek）。
流程：POST /api/start 建会话 → POST /api/learn 发爆款文案
→ SSE 监听 learn/learn_done → 校验 lessons 落盘 + stats.json learn_history。
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8765"

ARTICLE = (
    "你以为你输在文案？不是，你输在开头那三秒钟。\n"
    "我一个学员，以前发视频，完播率从来没超过20%。后来他只改了一件事："
    "把开头第一句从「大家好，今天教大家……」改成「这可能是你今年最后一次看到这么便宜的课了」。\n"
    "完播率直接干到58%，一条视频涨粉3万。\n"
    "为什么？因为用户的手指头，比你的脚本还要快。前3秒你不给他一个留下来的理由，他就划走了。\n"
    "记住：开头不卖货，开头只负责让人停下来。后面你讲什么，才有机会被听到。"
)


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    # 1) 建会话（/api/start 会自动启动讨论，learn 需等讨论结束）
    d = post("/api/start", {"script": "示例口播文稿：今天教大家三个提升完播率的技巧……"})
    assert d.get("ok"), d
    sid = d["sid"]
    print("1) session:", sid)

    # 1.5) 等讨论完成（done / review_done / score_done）
    print("等待讨论完成…")
    req = urllib.request.Request(BASE + "/api/stream/" + sid)
    with urllib.request.urlopen(req, timeout=600) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data: "):
                    continue
                item = json.loads(line[6:])
                if item.get("type") in ("done", "review_done", "score_done"):
                    print("    讨论结束事件:", item.get("type"))
                    break
            else:
                continue
            break

    # 2) 发学习请求
    d = post("/api/learn", {"sid": sid, "article": ARTICLE})
    assert d.get("ok"), d
    print("2) /api/learn 已接受")

    # 3) SSE 监听直到 learn_done
    learn_events = []
    req = urllib.request.Request(BASE + "/api/stream/" + sid)
    with urllib.request.urlopen(req, timeout=300) as resp:
        buf = b""
        done = False
        while not done:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data: "):
                    continue
                item = json.loads(line[6:])
                if item.get("type") == "learn":
                    learn_events.append(item)
                    print(f"    [{item['name']}] 吸收 {len(item.get('added', []))} 条 / 丢弃 {item.get('rejected_count', 0)} 条")
                if item.get("type") == "learn_done":
                    print("3) learn_done 到达")
                    done = True
                    break
    print("    learn 事件数:", len(learn_events))

    # 4) 校验落盘
    import glob, os
    files = glob.glob("knowledge_digests/lessons/*_lessons.md")
    print("4) lessons 文件:", files)
    total = 0
    for f in files:
        txt = open(f, encoding="utf-8").read()
        n = txt.count("吸收 #")
        total += n
        print(f"    {os.path.basename(f)}: {n} 条")
        assert "原文摘录" in txt and "吸收知识点" in txt, f
    print("    共落盘", total, "条知识点")

    # 5) stats.json learn_history
    stats = json.load(open("output/stats.json", encoding="utf-8"))
    lh = stats.get("learn_history", [])
    assert lh, "learn_history 缺失"
    print("5) learn_history 已记录:", lh[-1]["per_expert"])

    print("\nE2E LEARN TEST PASSED")


if __name__ == "__main__":
    main()
