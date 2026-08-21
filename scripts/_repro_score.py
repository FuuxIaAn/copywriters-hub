# -*- coding: utf-8 -*-
"""最小复现：start → done → score → dump 所有事件（含 error 文本）。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
SCRIPT = "家人们，今天给大家推荐一款榨汁杯。199块，一年省下两千多。每天一杯果汁，健康又省钱。"


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def stream_until(sid, stop_types, timeout=900):
    events = []
    req = urllib.request.Request(BASE + f"/api/stream/{sid}")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        buf = b""
        while time.time() - t0 < timeout:
            try:
                chunk = r.read(1)
            except Exception as e:
                print(f"[stream] read exception: {e!r}")
                break
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


r = post("/api/start", {"script": SCRIPT})
print("start:", r["ok"], r.get("sid"))
sid = r["sid"]
evs = stream_until(sid, {"done"})
types = {}
for e in evs:
    types[e.get("type")] = types.get(e.get("type"), 0) + 1
print("discussion events:", types)

r = post("/api/score", {"sid": sid, "script": SCRIPT})
print("score post:", r)
evs2 = stream_until(sid, {"score_done"})
print("== score phase events ==")
for e in evs2:
    t = e.get("type")
    if t in ("error", "system"):
        print(f"  [{t}] {e.get('text', '')[:300]}")
    else:
        print(f"  [{t}] name={e.get('name','')} score={e.get('score')}")
print("DONE")
