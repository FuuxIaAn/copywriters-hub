# -*- coding: utf-8 -*-
import sys

# Windows 默认控制台可能是 GBK；测试输出不应因为一个符号导致误报失败。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
"""口播工坊 · 后端 API 冒烟测试（不调用真实 AI）"""
import os
import sys
import tempfile
import shutil

TMP = tempfile.mkdtemp(prefix="wb_works_test_")
os.environ["WB_DATA_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import server  # noqa: E402

client = server.app.test_client()
passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} {extra}")


print("== 1. 页面与基础接口 ==")
r = client.get("/")
check("GET / 200", r.status_code == 200 and ("靓仔文案工作台" in r.get_data(as_text=True) or "口播工坊" in r.get_data(as_text=True)))
r = client.get("/api/status")
check("GET /api/status ok", r.get_json().get("ok") is True)
r = client.get("/api/context")
check("GET /api/context ok", r.get_json().get("ok") is True)

print("== 2. 作品库 CRUD ==")
r = client.post("/api/works", json={"title": "测试作品A", "draft": "这是一段测试初稿内容，讲桃花运的。"})
check("POST /api/works 创建", r.status_code == 200 and r.get_json().get("ok"))
wid_a = r.get_json()["work"]["id"]
r = client.post("/api/works", json={"title": "", "draft": "空标题自动命名"})
check("空标题自动命名", r.get_json()["work"]["title"] != "")
r = client.post("/api/works", json={"draft": ""})
check("空内容拒绝 400", r.status_code == 400)

r = client.get("/api/works")
check("GET /api/works 列表", r.get_json().get("ok") and len(r.get_json()["works"]) >= 2)
check("列表含 counts", "counts" in r.get_json())

r = client.get("/api/works/" + wid_a)
check("GET /api/works/<id> 详情", r.get_json()["work"]["id"] == wid_a)

print("== 3. 采纳 + 撤销 ==")
s = server.Session()
s.script = "测试文稿"
s.work_id = wid_a
server.SESSIONS["t_sid"] = s
r = client.post("/api/adopt", json={"sid": "t_sid", "name": "阿沁", "snippet": "开头改成反常识钩子", "note": "试试"})
d = r.get_json()
check("POST /api/adopt 采纳", d.get("ok") and d.get("work_id") == wid_a and d.get("adopt_no") == 1)
r = client.get("/api/works/" + wid_a)
adopts = r.get_json()["work"]["adoptions"]
check("作品挂上采纳记录", len(adopts) == 1 and adopts[0]["revoked"] is False)
check("采纳后状态→待采纳", r.get_json()["work"]["status"] == "to_adopt")

r = client.post("/api/undo-adopt", json={"work_id": wid_a, "no": 1, "reason": "点错了"})
check("POST /api/undo-adopt 撤销", r.get_json().get("ok") is True)
r = client.get("/api/works/" + wid_a)
a = r.get_json()["work"]["adoptions"][0]
check("采纳记录保留但标记撤销", a["revoked"] is True and a["revoked_at"])
r = client.post("/api/undo-adopt", json={"work_id": wid_a, "no": 1})
check("重复撤销被拒", r.status_code == 400)

print("== 4. PATCH 状态+数据 ==")
r = client.patch("/api/works/" + wid_a, json={"status": "published", "metrics": {"plays": 12000, "completion": 35.5, "likes": 430}})
w = r.get_json()["work"]
check("PATCH 状态+数据", w["status"] == "published" and w["metrics"]["plays"] == 12000)

print("== 5. 会话恢复 ==")
r = client.get("/api/session/t_sid")
check("GET /api/session 含 work_id", r.get_json().get("ok") and r.get_json().get("work_id") == wid_a)

print("== 6. 归档/恢复 ==")
r = client.post(f"/api/works/{wid_a}/archive")
check("归档", r.get_json()["work"]["status"] == "archived")
r = client.post(f"/api/works/{wid_a}/restore")
check("恢复", r.get_json()["work"]["status"] in ("draft", "to_adopt", "published"))

print("== 7. 看板 ==")
r = client.get("/api/overview")
d = r.get_json()
check("GET /api/overview ok", d.get("ok"))
check("overview 含 counts", "counts" in d and "timeline" in d and "experts" in d)
check("overview 含 rank_text", "rank_text" in d and "score_accuracy" in d)

print("== 8. 学习档案 ==")
r = client.get("/api/learnings")
d = r.get_json()
check("GET /api/learnings ok", d.get("ok") and isinstance(d.get("experts"), list) and len(d["experts"]) >= 6)

print("== 9. 清空（二次确认） ==")
r = client.post("/api/wipe", json={"kind": "works", "confirm": "NO"})
check("未确认被拒", r.status_code == 400)
r = client.post("/api/wipe", json={"kind": "works", "confirm": "DELETE"})
check("确认后清空作品", r.get_json().get("ok") is True)
r = client.get("/api/works")
check("清空后列表为空", len(r.get_json()["works"]) == 0)
r = client.post("/api/wipe", json={"kind": "stats", "confirm": "DELETE"})
check("清空统计", r.get_json().get("ok") is True)
r = client.post("/api/wipe", json={"kind": "lessons", "confirm": "DELETE"})
check("清空学习", r.get_json().get("ok") is True)

print(f"\n结果: {passed} 通过 / {failed} 失败")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
