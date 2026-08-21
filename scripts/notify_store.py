# -*- coding: utf-8 -*-
"""
通知中心存储
============
轻量通知存储（output/notifications.json），原子读写 + 进程内锁。
通知类型：system / adopt / review / retention / rewrite / session / debate。
字段：{id, ts, type, title, body, read, link}
link 形如 {view, wid, rid}，前端点通知可跳转。
"""
import datetime
import json
import os
import threading
import uuid

FILENAME = "notifications.json"
MAX_KEEP = 200          # 最多保留条数（超出丢弃最旧已读）
_LOCK = threading.Lock()


def _path(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, FILENAME)


def _load(output_dir: str) -> list:
    p = _path(output_dir)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            print(f"[notify] 读取通知失败，已重置: {p}")
    return []


def _save(output_dir: str, items: list):
    p = _path(output_dir)
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        print(f"[notify] 保存通知失败: {e}")


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add(output_dir: str, ntype: str, title: str, body: str = "", link: dict | None = None) -> dict:
    """追加一条通知。返回通知 dict。"""
    item = {
        "id": "n_" + uuid.uuid4().hex[:10],
        "ts": _now(),
        "type": ntype,
        "title": (title or "")[:120],
        "body": (body or "")[:400],
        "read": False,
        "link": link or {},
    }
    with _LOCK:
        items = _load(output_dir)
        items.insert(0, item)
        if len(items) > MAX_KEEP:
            items = items[:MAX_KEEP]
        _save(output_dir, items)
    return item


def list_all(output_dir: str, limit: int = 60) -> dict:
    """返回 {items, unread}。"""
    items = _load(output_dir)[:limit]
    unread = sum(1 for n in items if not n.get("read"))
    return {"items": items, "unread": unread}


def mark_read(output_dir: str, nid: str | None = None) -> int:
    """标记单个/全部已读，返回本次标记数量。"""
    with _LOCK:
        items = _load(output_dir)
        cnt = 0
        if nid:
            for n in items:
                if n.get("id") == nid and not n.get("read"):
                    n["read"] = True
                    cnt += 1
        else:
            for n in items:
                if not n.get("read"):
                    n["read"] = True
                    cnt += 1
        if cnt:
            _save(output_dir, items)
        return cnt


def clear(output_dir: str) -> int:
    """清空全部通知，返回删除数量。"""
    with _LOCK:
        items = _load(output_dir)
        cnt = len(items)
        if cnt:
            _save(output_dir, [])
        return cnt
