# -*- coding: utf-8 -*-
"""
对标账号存储 + 报告落盘
========================
账号列表: DATA_DIR/monitor_accounts.json
报告快照: OUTPUT_DIR/monitor/latest.json + latest.md
历史快照: OUTPUT_DIR/monitor/accounts/<account_id>/snapshot_*.json + latest.json
"""
import datetime
import json
import os
import uuid

DEFAULT_ACCOUNTS_FILE = "monitor_accounts.json"


def accounts_path(data_dir: str) -> str:
    return os.path.join(data_dir, DEFAULT_ACCOUNTS_FILE)


def load_accounts(data_dir: str) -> list:
    p = accounts_path(data_dir)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_accounts(data_dir: str, accounts: list) -> None:
    p = accounts_path(data_dir)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


def _norm_url(url: str) -> str:
    """归一化主页链接：去首尾空白、去查询参数（便于去重比较）。"""
    u = (url or "").strip()
    if not u:
        return ""
    # 去掉 ? 后的查询串（抖音链接常带 share_token 等临时参数）
    if "?" in u:
        u = u.split("?")[0]
    return u.rstrip("/")


def _find_duplicate(accounts: list, home_url: str, douyin_id: str = "", sec_user_id: str = "") -> dict | None:
    """三重去重：主页链接 / 抖音号 / sec_user_id，命中其一即视为重复。"""
    norm = _norm_url(home_url)
    dy = (douyin_id or "").strip()
    sec = (sec_user_id or "").strip()
    for a in accounts:
        if norm and _norm_url(a.get("home_url", "")) == norm:
            return a
        if dy and (a.get("douyin_id") or "").strip() == dy:
            return a
        if sec and (a.get("sec_user_id") or "").strip() == sec:
            return a
    return None


def add_account(
    data_dir: str,
    home_url: str,
    note: str = "",
    douyin_id: str = "",
    sec_user_id: str = "",
    meta: dict | None = None,
) -> dict:
    accounts = load_accounts(data_dir)
    dup = _find_duplicate(accounts, home_url, douyin_id, sec_user_id)
    if dup:
        return {"ok": False, "error": "该账号已在监控列表中", "account": dup, "duplicate": True}
    acc = {
        "id": uuid.uuid4().hex[:8],
        "home_url": home_url.strip(),
        "note": note.strip() or "",
        "douyin_id": douyin_id.strip() or "",
        "sec_user_id": sec_user_id.strip() or "",
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 抓取回填的资料（昵称/粉丝数等），添加时如有则直接带上
    if meta:
        for k in ("nickname", "follower_count", "aweme_count", "signature"):
            if meta.get(k) is not None:
                acc[k] = meta[k]
    accounts.append(acc)
    save_accounts(data_dir, accounts)
    return {"ok": True, "account": acc}


def update_account(data_dir: str, account_id: str, note: str) -> dict:
    """修改账号备注名。"""
    accounts = load_accounts(data_dir)
    for a in accounts:
        if a.get("id") == account_id:
            a["note"] = (note or "").strip()
            save_accounts(data_dir, accounts)
            return {"ok": True, "account": a}
    return {"ok": False, "error": "账号不存在或已删除"}


def update_account_meta(data_dir: str, account_id: str, meta: dict) -> dict:
    """抓取后用真实资料回填账号（昵称/粉丝/抖音号/sec_user_id/作品数）。"""
    accounts = load_accounts(data_dir)
    for a in accounts:
        if a.get("id") != account_id:
            continue
        changed = False
        for k in ("nickname", "follower_count", "aweme_count", "signature"):
            if meta.get(k) is not None:
                a[k] = meta[k]
                changed = True
        if meta.get("sec_user_id"):
            a["sec_user_id"] = meta["sec_user_id"]
            changed = True
        if meta.get("douyin_id"):
            a["douyin_id"] = meta["douyin_id"]
            changed = True
        if changed:
            a["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_accounts(data_dir, accounts)
        return {"ok": True, "account": a}
    return {"ok": False, "error": "账号不存在或已删除"}


def remove_account(data_dir: str, account_id: str) -> bool:
    accounts = load_accounts(data_dir)
    before = len(accounts)
    accounts = [a for a in accounts if a.get("id") != account_id]
    if len(accounts) == before:
        return False
    save_accounts(data_dir, accounts)
    return True


# ---------------------------------------------------------------- 快照落盘（对标账号）
#
# 复用「我的账号」的快照对比思路：每次抓取为每个账号落一份时间戳快照 +
# latest.json，再与上一份对比算出「较上次更新 X 条视频」。

def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts_tag() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def account_dir(output_dir: str, account_id: str) -> str:
    d = os.path.join(output_dir, "monitor", "accounts", account_id)
    os.makedirs(d, exist_ok=True)
    return d


def save_account_snapshot(output_dir: str, account_id: str, snapshot: dict) -> str:
    """落一份时间戳快照 + 覆盖 latest.json，返回时间戳快照路径。"""
    d = account_dir(output_dir, account_id)
    ts = _ts_tag()
    path = os.path.join(d, f"snapshot_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return path


def load_latest_snapshot(output_dir: str, account_id: str) -> dict | None:
    p = os.path.join(account_dir(output_dir, account_id), "latest.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def build_compare(account_id: str, account: dict, videos: list, prev_snapshot: dict | None) -> dict:
    """构造单个账号的快照 + 对比数据（对标账号版，聚焦「新增视频」）。

    输出：
      {
        "fetched_at": ...,
        "account": {...资料...},
        "videos": [{...原始视频..., is_new}],
        "video_count": N,
        "new_count": 本次相对上次新增的视频数（无基准则为本次条数），
        "prev_fetched_at": "..." 或 None,
      }
    """
    prev_videos = {}
    if prev_snapshot:
        for v in prev_snapshot.get("videos", []):
            prev_videos[str(v.get("aweme_id", ""))] = v

    compared = []
    for v in videos:
        vid = str(v.get("aweme_id", ""))
        compared.append({**v, "is_new": vid not in prev_videos})

    has_base = prev_snapshot is not None
    new_count = sum(1 for v in compared if v.get("is_new")) if has_base else len(compared)

    return {
        "fetched_at": _now_str(),
        "account": account,
        "videos": compared,
        "video_count": len(compared),
        "new_count": new_count,
        "has_base": has_base,
        "prev_fetched_at": (prev_snapshot or {}).get("fetched_at"),
    }


def load_account_new_count(output_dir: str, account_id: str) -> dict:
    """读取某账号最新快照，返回 {new_count, has_base, fetched_at, fetched_count}。

    口径说明：
      - fetched_count = 最近一次抓取实际入库的视频条数（快照 video_count，即「已抓」分子）；
      - new_count    = 相对上一份快照新增的条数（无基准则等于 fetched_count）；
      - 没有快照（从未抓取）时 fetched_count/new_count 均为 None，前端显示「尚未抓取」。
    """
    snap = load_latest_snapshot(output_dir, account_id)
    if snap is None:
        return {"new_count": None, "has_base": False, "fetched_at": None, "fetched_count": None}
    return {
        "new_count": snap.get("new_count"),
        "has_base": snap.get("has_base", False),
        "fetched_at": snap.get("fetched_at"),
        "fetched_count": snap.get("video_count"),
    }


# ---------------------------------------------------------------- 报告快照

def report_dir(output_dir: str) -> str:
    d = os.path.join(output_dir, "monitor")
    os.makedirs(d, exist_ok=True)
    return d


def save_report(output_dir: str, report: dict, markdown: str) -> str:
    """保存最新报告快照，返回 html 文件名（前端展示用 JSON 即可）。"""
    d = report_dir(output_dir)
    with open(os.path.join(d, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "latest.md"), "w", encoding="utf-8") as f:
        f.write(markdown)
    # 按时间戳归档一份，方便回溯
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(d, f"report_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return os.path.join(d, "latest.json")


def load_latest_report(output_dir: str) -> dict | None:
    p = os.path.join(report_dir(output_dir), "latest.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None
