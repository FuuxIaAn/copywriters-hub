# -*- coding: utf-8 -*-
"""
实时监控 · 存储 + 快照 + 相邻轮询增量
========================================
独立于「对标监控」(monitor) 和「我的账号」(mine) 的第三套板块：
专注**自动轮询**自己的抖音号，捕捉分钟级数据变化。

- 账号列表:  DATA_DIR/realtime_accounts.json   （独立于 mine_accounts.json / monitor_accounts.json）
- 历史快照:  OUTPUT_DIR/realtime/accounts/<account_id>/snapshot_YYYYmmdd_HHMMSS.json
- 最新快照:  OUTPUT_DIR/realtime/accounts/<account_id>/latest.json
- 汇总报告:  OUTPUT_DIR/realtime/latest.json + latest.md
- 提醒记录:  OUTPUT_DIR/realtime/alerts.json

对比逻辑：**每次轮询相对上一次快照**（相邻两次），逐视频算增量，
新出现的视频打 is_new 标记。这是与「我的账号」的关键区别——
「我的账号」是手动触发快照，这里是持续自动轮询，每次间隔短（默认 5 分钟），
增量更有实时意义。
"""
import datetime
import json
import os
import uuid

DEFAULT_ACCOUNTS_FILE = "realtime_accounts.json"

# 轮询参数默认值（可被 server 层覆盖）
DEFAULT_POLL_INTERVAL = 300  # 秒（5 分钟）
DEFAULT_VIDEO_COUNT = 10  # 每个账号抓最近 N 条视频


# ---------------------------------------------------------------- 账号管理

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


def add_account(data_dir: str, home_url: str, note: str = "", douyin_id: str = "") -> dict:
    accounts = load_accounts(data_dir)
    for a in accounts:
        if a.get("home_url", "").strip() == home_url.strip():
            return {"ok": False, "error": "该主页链接已在监控列表中", "account": a}
    acc = {
        "id": uuid.uuid4().hex[:8],
        "home_url": home_url.strip(),
        "note": note.strip() or "",
        "douyin_id": douyin_id.strip() or "",
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    accounts.append(acc)
    save_accounts(data_dir, accounts)
    return {"ok": True, "account": acc}


def remove_account(data_dir: str, account_id: str) -> bool:
    accounts = load_accounts(data_dir)
    before = len(accounts)
    accounts = [a for a in accounts if a.get("id") != account_id]
    if len(accounts) == before:
        return False
    save_accounts(data_dir, accounts)
    return True


# ---------------------------------------------------------------- 快照落盘

def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts_tag() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def account_dir(output_dir: str, account_id: str) -> str:
    d = os.path.join(output_dir, "realtime", "accounts", account_id)
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


def list_account_snapshots(output_dir: str, account_id: str, limit: int = 30) -> list:
    """列出某账号的历史快照（时间倒序）。"""
    d = account_dir(output_dir, account_id)
    files = [f for f in os.listdir(d) if f.startswith("snapshot_") and f.endswith(".json")]
    files.sort(reverse=True)
    out = []
    for f in files[:limit]:
        p = os.path.join(d, f)
        try:
            with open(p, "r", encoding="utf-8") as fp:
                snap = json.load(fp)
            out.append(
                {
                    "file": f,
                    "fetched_at": snap.get("fetched_at", ""),
                    "follower_count": snap.get("account", {}).get("follower_count", 0),
                    "video_count": len(snap.get("videos", [])),
                }
            )
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 对比逻辑

def _delta(cur: int, prev: int | None) -> int | None:
    """有上一份数据则算增量，否则 None（表示无对比基准）。"""
    if prev is None:
        return None
    return (cur or 0) - (prev or 0)


def build_compare(account_id: str, account: dict, videos: list, prev_snapshot: dict | None) -> dict:
    """构造单个账号的快照 + 相邻对比数据。

    输入：
      account: fetch 返回的账号资料（nickname/follower_count/...）
      videos:  本次抓取的视频列表
      prev_snapshot: 上一份快照（相邻上一次轮询，可为 None）
    输出：
      {
        "fetched_at": ...,
        "account": {...原始资料...},
        "account_delta": {follower_delta, aweme_delta, favorited_delta},  # 无基准则 None
        "videos": [{...原始视频..., is_new, digg_delta, comment_delta, play_delta, share_delta}],
        "video_count": N,
        "new_count": 本次相对上次新增的视频数（无基准则为本次条数）,
        "has_base": 是否有对比基准,
        "prev_fetched_at": "..." 或 None,
      }
    """
    prev_videos = {}
    prev_meta = None
    if prev_snapshot:
        prev_meta = prev_snapshot.get("account") or {}
        for v in prev_snapshot.get("videos", []):
            prev_videos[str(v.get("aweme_id", ""))] = v

    account_delta = None
    if prev_meta:
        account_delta = {
            "follower_delta": _delta(account.get("follower_count"), prev_meta.get("follower_count")),
            "aweme_delta": _delta(account.get("aweme_count"), prev_meta.get("aweme_count")),
            "favorited_delta": _delta(account.get("total_favorited"), prev_meta.get("total_favorited")),
        }

    compared = []
    for v in videos:
        vid = str(v.get("aweme_id", ""))
        pv = prev_videos.get(vid)
        compared.append(
            {
                **v,
                "is_new": pv is None,
                "digg_delta": None if pv is None else _delta(v.get("digg_count"), pv.get("digg_count")),
                "comment_delta": None if pv is None else _delta(v.get("comment_count"), pv.get("comment_count")),
                "play_delta": None if pv is None else _delta(v.get("play_count"), pv.get("play_count")),
                "share_delta": None if pv is None else _delta(v.get("share_count"), pv.get("share_count")),
            }
        )

    has_base = prev_snapshot is not None
    new_count = sum(1 for v in compared if v.get("is_new")) if has_base else len(compared)

    return {
        "fetched_at": _now_str(),
        "account": account,
        "account_delta": account_delta,
        "videos": compared,
        "video_count": len(compared),
        "new_count": new_count,
        "has_base": has_base,
        "prev_fetched_at": (prev_snapshot or {}).get("fetched_at"),
    }


# ---------------------------------------------------------------- 汇总报告

def build_report(output_dir: str, account_ids: list[str]) -> dict:
    """汇总所有账号的最新快照，生成总览报告（给前端展示 + 落 latest.json/md）。"""
    accounts_meta = load_accounts_report_meta(output_dir, account_ids)
    total_videos = sum(a.get("video_count", 0) for a in accounts_meta)
    new_videos = sum(a.get("new_count", 0) for a in accounts_meta)
    return {
        "fetched_at": _now_str(),
        "account_count": len(accounts_meta),
        "total_videos": total_videos,
        "new_videos": new_videos,
        "accounts": accounts_meta,
    }


def load_accounts_report_meta(output_dir: str, account_ids: list[str]) -> list:
    out = []
    for aid in account_ids:
        snap = load_latest_snapshot(output_dir, aid)
        if snap is None:
            continue
        acc = snap.get("account") or {}
        videos = snap.get("videos") or []
        # 视频按点赞降序（自己的号，高赞放前面）
        videos = sorted(videos, key=lambda x: x.get("digg_count", 0), reverse=True)
        acc_meta = {
            "account_id": aid,
            "nickname": acc.get("nickname", "未命名账号"),
            "follower_count": acc.get("follower_count", 0),
            "aweme_count": acc.get("aweme_count", 0),
            "total_favorited": acc.get("total_favorited", 0),
            "account_delta": snap.get("account_delta"),
            "fetched_at": snap.get("fetched_at", ""),
            "prev_fetched_at": snap.get("prev_fetched_at"),
            "video_count": len(videos),
            "new_count": snap.get("new_count", 0),
            "has_base": snap.get("has_base", False),
            "videos": videos,
            "history": list_account_snapshots(output_dir, aid, limit=30),
        }
        out.append(acc_meta)
    return out


def report_dir(output_dir: str) -> str:
    d = os.path.join(output_dir, "realtime")
    os.makedirs(d, exist_ok=True)
    return d


def save_report(output_dir: str, report: dict, markdown: str) -> str:
    d = report_dir(output_dir)
    with open(os.path.join(d, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "latest.md"), "w", encoding="utf-8") as f:
        f.write(markdown)
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


# ---------------------------------------------------------------- Markdown

def fmt_num(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 10000:
        return f"{n / 10000:.1f}w"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_delta(d) -> str:
    """delta 显示：None -> '-'；>=0 -> '+x'；<0 -> '-x'"""
    if d is None:
        return "-"
    if d > 0:
        return f"+{d}"
    return str(d)


# ---------------------------------------------------------------- 数据变化提醒

_ALERTS_FILE = "alerts.json"


def alerts_path(output_dir: str) -> str:
    return os.path.join(report_dir(output_dir), _ALERTS_FILE)


def build_alerts(report: dict) -> list:
    """从报告中提取数据变化提醒，返回 alert 列表（新提醒，尚未落盘）。

    提醒类型（聚焦核心互动指标：点赞/评论/播放/转发）：
      - new_video: 新视频发布
      - spike:     某视频数据暴涨（点赞/评论/播放/转发增量超阈值）
      - follower:  粉丝数变化
    """
    alerts = []
    fetched_at = report.get("fetched_at", _now_str())

    for acc in report.get("accounts", []):
        nickname = acc.get("nickname", "未命名账号")
        account_id = acc.get("account_id", "")
        ad = acc.get("account_delta")

        # --- 账号级变化：粉丝数 ---
        if ad:
            fd = ad.get("follower_delta")
            if fd is not None and fd != 0:
                alerts.append({
                    "type": "follower",
                    "level": "hot" if abs(fd) >= 100 else "info",
                    "account": nickname,
                    "account_id": account_id,
                    "title": f"{'📈' if fd > 0 else '📉'} 粉丝{'增加' if fd > 0 else '减少'} {abs(fd)}",
                    "detail": f"当前粉丝 {fmt_num(acc.get('follower_count', 0))}",
                    "time": fetched_at,
                    "fetch_at": fetched_at,
                    "read": False,
                })

        # --- 视频级变化 ---
        for v in acc.get("videos", []):
            desc = (v.get("desc") or "（无文字）")[:30]

            # 新视频
            if v.get("is_new"):
                alerts.append({
                    "type": "new_video",
                    "level": "info",
                    "account": nickname,
                    "account_id": account_id,
                    "title": "🆕 发布新视频",
                    "detail": f"{desc} · 点赞 {fmt_num(v.get('digg_count'))} · 播放 {fmt_num(v.get('play_count'))}",
                    "time": fetched_at,
                    "fetch_at": fetched_at,
                    "read": False,
                })
                continue  # 新视频不再判定暴涨

            # 数据暴涨判定（绝对值 + 百分比双阈值），覆盖点赞/评论/播放/转发
            dd = v.get("digg_delta") or 0
            cd = v.get("comment_delta") or 0
            pd = v.get("play_delta") or 0
            sd = v.get("share_delta") or 0

            cur_digg = v.get("digg_count") or 0
            cur_cmt = v.get("comment_count") or 0
            cur_play = v.get("play_count") or 0
            cur_share = v.get("share_count") or 0

            digg_spike = dd >= 100 or (cur_digg > 0 and dd > 0 and dd / max(cur_digg - dd, 1) >= 0.2)
            cmt_spike = cd >= 50 or (cur_cmt > 0 and cd > 0 and cd / max(cur_cmt - cd, 1) >= 0.2)
            play_spike = pd >= 1000 or (cur_play > 0 and pd > 0 and pd / max(cur_play - pd, 1) >= 0.2)
            share_spike = sd >= 50 or (cur_share > 0 and sd > 0 and sd / max(cur_share - sd, 1) >= 0.2)

            if digg_spike or cmt_spike or play_spike or share_spike:
                parts = []
                if digg_spike:
                    parts.append(f"点赞 +{fmt_num(dd)}")
                if cmt_spike:
                    parts.append(f"评论 +{fmt_num(cd)}")
                if play_spike:
                    parts.append(f"播放 +{fmt_num(pd)}")
                if share_spike:
                    parts.append(f"转发 +{fmt_num(sd)}")
                alerts.append({
                    "type": "spike",
                    "level": "hot",
                    "account": nickname,
                    "account_id": account_id,
                    "title": "🔥 数据暴涨",
                    "detail": f"{desc} · {' · '.join(parts)}",
                    "time": fetched_at,
                    "fetch_at": fetched_at,
                    "read": False,
                })

    return alerts


def load_alerts(output_dir: str) -> list:
    p = alerts_path(output_dir)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f).get("alerts", [])
        except Exception:
            return []
    return []


def save_new_alerts(output_dir: str, new_alerts: list) -> list:
    """把本次轮询产生的新提醒追加到已有提醒列表（去重），返回完整列表。

    去重规则：同账号同 type+title+detail 且距上次 <2h 视为重复。
    """
    existing = load_alerts(output_dir)
    now = datetime.datetime.now()

    for na in new_alerts:
        is_dup = False
        for ex in existing:
            if (ex.get("account_id") == na.get("account_id")
                    and ex.get("type") == na.get("type")
                    and ex.get("title") == na.get("title")):
                try:
                    old_t = datetime.datetime.strptime(ex.get("fetch_at", ""), "%Y-%m-%d %H:%M:%S")
                    if (now - old_t).total_seconds() < 7200:
                        is_dup = True
                        break
                except Exception:
                    pass
        if not is_dup:
            na["id"] = uuid.uuid4().hex[:8]
            existing.insert(0, na)  # 最新的放前面

    # 限制最多 200 条
    existing = existing[:200]
    with open(alerts_path(output_dir), "w", encoding="utf-8") as f:
        json.dump({"alerts": existing, "updated_at": _now_str()}, f, ensure_ascii=False, indent=2)
    return existing


def mark_alerts_read(output_dir: str, alert_id: str | None = None) -> list:
    """标记提醒已读。alert_id=None 时全部标记已读。"""
    alerts = load_alerts(output_dir)
    for a in alerts:
        if alert_id is None or a.get("id") == alert_id:
            a["read"] = True
    with open(alerts_path(output_dir), "w", encoding="utf-8") as f:
        json.dump({"alerts": alerts, "updated_at": _now_str()}, f, ensure_ascii=False, indent=2)
    return alerts


def count_unread(alerts: list) -> int:
    return sum(1 for a in alerts if not a.get("read", False))


def build_markdown(report: dict) -> str:
    lines = [
        "# 实时监控数据报告",
        "",
        f"- 生成时间：{report['fetched_at']}",
        f"- 账号数：{report['account_count']} · 视频总数：{report['total_videos']} · 新增视频：{report['new_videos']}",
        "",
    ]
    for a in report.get("accounts", []):
        lines.append(f"## {a['nickname']}")
        lines.append("")
        d = a.get("account_delta") or {}
        fd = d.get("follower_delta")
        fd_txt = "（首次轮询）" if fd is None else f"（较上次 {_fmt_delta(fd)}）"
        lines.append(
            f"- 粉丝：{fmt_num(a['follower_count'])} {fd_txt}"
            f" · 作品：{a['aweme_count']} · 获赞：{fmt_num(a['total_favorited'])}"
        )
        lines.append(f"- 轮询时间：{a['fetched_at']}" + (f" · 上次：{a['prev_fetched_at']}" if a.get("prev_fetched_at") else " · 首次轮询"))
        lines.append("")
        lines.append("| 标题 | 点赞 | 评论 | 播放 | 转发 | 较上次(赞/评/播/转) |")
        lines.append("|------|------|------|------|------|------|")
        for v in a.get("videos", []):
            tag = "🆕 " if v.get("is_new") else ""
            lines.append(
                f"| {tag}{(v.get('desc') or '')[:24]} | {fmt_num(v.get('digg_count'))} | "
                f"{fmt_num(v.get('comment_count'))} | {fmt_num(v.get('play_count'))} | "
                f"{fmt_num(v.get('share_count'))} | "
                f"{_fmt_delta(v.get('digg_delta'))}/{_fmt_delta(v.get('comment_delta'))}/"
                f"{_fmt_delta(v.get('play_delta'))}/{_fmt_delta(v.get('share_delta'))} |"
            )
        lines.append("")
    return "\n".join(lines)
