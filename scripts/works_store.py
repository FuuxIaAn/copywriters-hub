# -*- coding: utf-8 -*-
"""
作品库管理（口播工坊核心数据）
================================
以「作品」为主线贯穿 写稿→讨论→采纳→发布→复盘→学习 全生命周期：

    草稿 draft → 讨论中 discussing → 待采纳 to_adopt → 已发布 published → 已复盘 reviewed
                                              ↘ 已归档 archived（软删除，不真删）

每个作品承载：初稿 / 终稿 / 采纳记录（可撤销）/ 评分记录 / 复盘结论 / 效果数据 / 关联会话。
所有数据持久化到 <data_dir>/output/works.json，原子读写 + 进程内锁，防并发覆盖。
"""
import datetime
import json
import os
import threading
import uuid

WORKS_FILENAME = "works.json"
STATUS_LABELS = {
    "draft": "草稿",
    "discussing": "讨论中",
    "to_adopt": "待采纳",
    "published": "已发布",
    "reviewed": "已复盘",
    "archived": "已归档",
}
# 允许的流转顺序（用于前端推进按钮；可按任意顺序直接跳转）
_STATUS_ORDER = ["draft", "discussing", "to_adopt", "published", "reviewed"]

_LOCK = threading.Lock()


def works_path(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, WORKS_FILENAME)


def load(output_dir: str) -> dict:
    path = works_path(output_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            print(f"[works] 读取作品库失败，已重置: {path}")
    return {"version": 2, "works": []}


def save(output_dir: str, data: dict):
    path = works_path(output_dir)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print(f"[works] 保存作品库失败: {e}")


def update(output_dir: str, fn):
    """原子「读-改-写」作品库：fn(data) 内完成修改。返回 (data, fn 返回值)。"""
    with _LOCK:
        data = load(output_dir)
        ret = fn(data)
        save(output_dir, data)
        return data, ret


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_work(wid: str, title: str, draft: str, session_id: str = "", note: str = "") -> dict:
    return {
        "id": wid,
        "title": title or "未命名作品",
        "created_at": _now(),
        "updated_at": _now(),
        "status": "discussing" if session_id else "draft",
        "draft": draft or "",
        "final": "",
        "session_id": session_id,
        "note": note or "",
        "adoptions": [],       # [{no,name,round,snippet,note,time,revoked,revoked_at}]
        "scores": [],          # [{name,score,reason,time}]
        "review": None,        # {time, actual_score, summary, conclusions}
        "metrics": {},         # {plays, completion, likes, comments, saved, ...}
    }


def create(output_dir: str, title: str, draft: str, session_id: str = "", note: str = "") -> dict:
    wid = "w_" + uuid.uuid4().hex[:10]
    work = _default_work(wid, title.strip(), draft, session_id, note)
    update(output_dir, lambda d: (d.setdefault("works", []).append(work) or None))
    return work


def get(output_dir: str, wid: str) -> dict | None:
    data = load(output_dir)
    for w in data.get("works", []):
        if w["id"] == wid:
            return w
    return None


def list_works(output_dir: str, include_archived: bool = True) -> list:
    data = load(output_dir)
    works = data.get("works", [])
    if not include_archived:
        works = [w for w in works if w.get("status") != "archived"]
    return sorted(works, key=lambda w: w.get("updated_at", ""), reverse=True)


def update_work(output_dir: str, wid: str, fn):
    """针对单个作品做原子修改。fn(work) 内改完自动更新 updated_at。返回 work 或 None。"""
    def _fn(data):
        for w in data.get("works", []):
            if w["id"] == wid:
                fn(w)
                w["updated_at"] = _now()
                return w
        return None
    data, ret = update(output_dir, _fn)
    return ret


def set_status(output_dir: str, wid: str, status: str) -> dict | None:
    if status not in STATUS_LABELS:
        return None
    return update_work(output_dir, wid, lambda w: w.update({"status": status}))


def set_final(output_dir: str, wid: str, final_text: str) -> dict | None:
    return update_work(output_dir, wid, lambda w: w.update({"final": final_text or w.get("final", ""), "status": "to_adopt"}))


def set_title(output_dir: str, wid: str, title: str) -> dict | None:
    title = (title or "").strip()
    if not title:
        return None
    return update_work(output_dir, wid, lambda w: w.update({"title": title}))


def add_adoption(output_dir: str, wid: str, adopt: dict) -> int | None:
    """把一条采纳挂到作品下；返回采纳编号（作品不存在返回 None）。"""
    def _fn(w):
        no = len(w.get("adoptions", [])) + 1
        w.setdefault("adoptions", []).append({
            "no": no,
            "name": adopt.get("name", ""),
            "round": adopt.get("round", "讨论"),
            "snippet": (adopt.get("snippet") or "")[:300],
            "note": (adopt.get("note") or "").strip(),
            "time": _now(),
            "revoked": False,
            "revoked_at": None,
        })
        # 有采纳说明进入了待采纳/已采纳阶段
        if w.get("status") in ("draft", "discussing"):
            w["status"] = "to_adopt"
        return no
    work = update_work(output_dir, wid, _fn)
    return work.get("adoptions")[-1]["no"] if work else None


def revoke_adoption(output_dir: str, wid: str, no: int, reason: str = "") -> bool:
    """撤销一条采纳（软撤销：保留记录但标记 revoked，不参与统计）。返回是否撤销成功。"""
    def _fn(data):
        for w in data.get("works", []):
            if w["id"] == wid:
                for a in w.get("adoptions", []):
                    if a.get("no") == no and not a.get("revoked"):
                        a["revoked"] = True
                        a["revoked_at"] = _now()
                        a["revoke_reason"] = reason
                        w["updated_at"] = _now()
                        return True
                return False
        return False
    _, ret = update(output_dir, _fn)
    return ret


def add_scores(output_dir: str, wid: str, scores: list) -> dict | None:
    """记录一次终稿评分（scores: [{name, score, reason}]）。"""
    def _fn(w):
        rec = {"time": _now(), "scores": scores}
        w.setdefault("scores", []).append(rec)
        if len(w["scores"]) > 20:
            del w["scores"][:-20]
    return update_work(output_dir, wid, _fn)


def set_review(output_dir: str, wid: str, metrics: dict, actual_score, summary: str = "") -> dict | None:
    """复盘落库：更新效果数据 + 复盘结论，状态 → 已复盘。"""
    def _fn(w):
        w["metrics"] = {**(w.get("metrics") or {}), **{k: v for k, v in (metrics or {}).items() if v is not None}}
        w["review"] = {
            "time": _now(),
            "actual_score": actual_score,
            "summary": (summary or "")[:500],
        }
        w["status"] = "reviewed"
    return update_work(output_dir, wid, _fn)


def save_metrics(output_dir: str, wid: str, metrics: dict) -> dict | None:
    """保存详细数据指标（2秒跳出率/5秒完播率/平均播放时长/平均播放占比/完播率/播放量等），不改状态。"""
    def _fn(w):
        w["metrics"] = {**(w.get("metrics") or {}), **{k: v for k, v in (metrics or {}).items() if v is not None and v != ""}}
    return update_work(output_dir, wid, _fn)


def archive(output_dir: str, wid: str) -> dict | None:
    """归档作品：先保存当前状态再标记为 archived，以便恢复时还原。"""
    def _fn(w):
        w["prev_status"] = w.get("status", "draft")
        w["status"] = "archived"
    update_work(output_dir, wid, _fn)
    return get(output_dir, wid)


def restore(output_dir: str, wid: str) -> dict | None:
    w = get(output_dir, wid)
    prev = (w or {}).get("prev_status") or ("to_adopt" if w and w.get("adoptions") else "draft")
    return update_work(output_dir, wid, lambda x: x.update({"status": prev}))


def delete(output_dir: str, wid: str) -> bool:
    """硬删除作品：从 works.json 中永久移除该作品。"""
    def _fn(data):
        works = data.get("works", [])
        before = len(works)
        data["works"] = [w for w in works if w.get("id") != wid]
        return len(data["works"]) < before
    _, removed = update(output_dir, _fn)
    return bool(removed)


def counts(output_dir: str) -> dict:
    data = load(output_dir)
    works = data.get("works", [])
    by_status = {s: 0 for s in STATUS_LABELS}
    adopt_total = 0
    for w in works:
        if w.get("status") in by_status:
            by_status[w["status"]] += 1
        adopt_total += sum(1 for a in w.get("adoptions", []) if not a.get("revoked"))
    return {
        "total": len(works),
        "by_status": by_status,
        "adopt_total": adopt_total,
        "reviewed_count": by_status.get("reviewed", 0),
    }


def expert_contribution(output_dir: str) -> list:
    """专家贡献榜：按「被有效采纳次数」聚合（不含已撤销），带关联作品的均播放/完播。"""
    data = load(output_dir)
    stat = {}
    for w in data.get("works", []):
        if w.get("status") == "archived":
            continue
        m = w.get("metrics") or {}
        plays = m.get("plays")
        completion = m.get("completion")
        for a in w.get("adoptions", []):
            if a.get("revoked"):
                continue
            e = stat.setdefault(a["name"], {"adopted": 0, "works": 0, "plays_sum": 0, "completion_sum": 0})
            e["adopted"] += 1
            e["works"] += 1
            if isinstance(plays, (int, float)):
                e["plays_sum"] += plays
            if isinstance(completion, (int, float)):
                e["completion_sum"] += completion
    rows = []
    for name, e in stat.items():
        rows.append({
            "name": name,
            "adopted": e["adopted"],
            "works": e["works"],
            "avg_plays": round(e["plays_sum"] / e["works"], 0) if e["plays_sum"] else None,
            "avg_completion": round(e["completion_sum"] / e["works"], 1) if e["completion_sum"] else None,
        })
    rows.sort(key=lambda r: (-r["adopted"], -(r["avg_plays"] or 0)))
    return rows


def timeline(output_dir: str, limit: int = 12) -> list:
    """最近动态时间线：作品创建 / 采纳 / 复盘 / 归档。"""
    data = load(output_dir)
    events = []
    for w in data.get("works", []):
        events.append({"ts": w.get("created_at", ""), "type": "create",
                       "title": w.get("title", ""), "wid": w["id"]})
        for a in w.get("adoptions", []):
            events.append({"ts": a.get("time", ""), "type": "adopt",
                           "title": f"采纳 {a.get('name','')} 的建议", "wid": w["id"],
                           "detail": (a.get("snippet") or "")[:40]})
        rv = w.get("review")
        if rv:
            events.append({"ts": rv.get("time", ""), "type": "review",
                           "title": "复盘完成", "wid": w["id"],
                           "detail": (rv.get("summary") or "")[:40]})
        if w.get("status") == "archived":
            events.append({"ts": w.get("updated_at", ""), "type": "archive",
                           "title": "已归档", "wid": w["id"]})
    events = [e for e in events if e["ts"]]
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


def overview(output_dir: str) -> dict:
    return {
        "counts": counts(output_dir),
        "timeline": timeline(output_dir),
        "experts": expert_contribution(output_dir),
    }


def wipe_works(output_dir: str) -> int:
    """清空全部作品（设置页危险操作），返回删除数量。原子操作避免并发丢数据。"""
    def _fn(d):
        count = len(d.get("works", []))
        d["works"] = []
        return count
    _, ret = update(output_dir, _fn)
    return ret
