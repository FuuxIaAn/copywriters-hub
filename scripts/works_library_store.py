# -*- coding: utf-8 -*-
"""
作品库 / 模拟对话库 · 存储层
============================
职责：
  给 N 个账号主页（抖音等），抓取账号下所有视频，把文字扒下来做成「模拟对话库」，
  替代「手动逐条粘贴分享链接提取」的重复劳动。

数据模型（落盘 output/workslib/ 下）：
  accounts.json
    [{ id, platform, home_url, nickname, signature, follower_count,
       aweme_count, added_at, last_fetched_at, video_count }]
  <account_id>.json   —— 每个账号一份，含该账号下所有已抓视频的对话条目
    { account_id, videos: [ { aweme_id, desc, create_time, digg_count, ...,
        extracted: bool, error: str, extract_time: str,
        text: str, segments: [{speaker,text}], visitor_profile: {...} } ] }

「作品」= 一个视频的对话条目；「导入对话」= 把某视频的 segments 交给配音工坊
build_persona 创建角色（无需重新提取，直接复用已扒下来的文案）。

注意：本 store 只负责数据读写与结构，抓取/提取逻辑在 works_library_server.py。
"""
import hashlib
import json
import os
import time

# 环境变量：WB_WORKSLIB_MOCK=1 时抓取层走 mock（离线演示）
MOCK = os.environ.get("WB_WORKSLIB_MOCK", "") == "1" or os.environ.get("WB_MONITOR_MOCK", "") == "1"


def _dir(output_dir: str, *parts: str) -> str:
    p = os.path.join(output_dir, "workslib", *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _uid(seed: str = "") -> str:
    return hashlib.md5(f"{seed}{time.time()}".encode()).hexdigest()[:10]


# ---------------------------------------------------------------- 账号管理

def _accounts_path(output_dir: str) -> str:
    return os.path.join(_dir(output_dir), "accounts.json")


def list_accounts(output_dir: str) -> list:
    return _read_json(_accounts_path(output_dir), [])


def _save_accounts(output_dir: str, accounts: list) -> None:
    _write_json(_accounts_path(output_dir), accounts)


def get_account(output_dir: str, account_id: str) -> dict | None:
    for a in list_accounts(output_dir):
        if a.get("id") == account_id:
            return a
    return None


def upsert_account(output_dir: str, platform: str, home_url: str,
                   meta: dict | None = None) -> dict:
    """按 home_url 去重地添加/更新账号；返回账号 dict。"""
    accounts = list_accounts(output_dir)
    home_url = (home_url or "").strip()
    for a in accounts:
        if a.get("home_url") == home_url:
            if meta:
                a.update(meta)
            a["platform"] = platform or a.get("platform", "douyin")
            _save_accounts(output_dir, accounts)
            return a
    acc = {
        "id": _uid(home_url[:48]),
        "platform": platform or "douyin",
        "home_url": home_url,
        "added_at": _now(),
        "last_fetched_at": None,
        "video_count": 0,
    }
    if meta:
        acc.update(meta)
    accounts.append(acc)
    _save_accounts(output_dir, accounts)
    return acc


def remove_account(output_dir: str, account_id: str) -> bool:
    accounts = list_accounts(output_dir)
    remain = [a for a in accounts if a.get("id") != account_id]
    if len(remain) == len(accounts):
        return False
    _save_accounts(output_dir, remain)
    # 删除该账号的视频数据文件
    try:
        p = os.path.join(_dir(output_dir), f"{account_id}.json")
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- 视频/对话条目

def _videos_path(output_dir: str, account_id: str) -> str:
    return os.path.join(_dir(output_dir), f"{account_id}.json")


def load_videos(output_dir: str, account_id: str) -> list:
    data = _read_json(_videos_path(output_dir, account_id), {})
    return data.get("videos") or []


def _save_videos(output_dir: str, account_id: str, videos: list) -> None:
    _write_json(_videos_path(output_dir, account_id), {"account_id": account_id, "videos": videos})


def upsert_video(output_dir: str, account_id: str, video: dict) -> None:
    """按 aweme_id 去重地写入/更新一个视频条目。"""
    videos = load_videos(output_dir, account_id)
    aweme_id = video.get("aweme_id")
    for i, v in enumerate(videos):
        if v.get("aweme_id") == aweme_id:
            videos[i] = {**v, **video}
            _save_videos(output_dir, account_id, videos)
            return
    videos.append(video)
    _save_videos(output_dir, account_id, videos)


def get_video(output_dir: str, account_id: str, aweme_id: str) -> dict | None:
    for v in load_videos(output_dir, account_id):
        if v.get("aweme_id") == aweme_id:
            return v
    return None


def mark_extracted(output_dir: str, account_id: str, aweme_id: str,
                   text: str, segments: list, visitor_profile: dict | None) -> None:
    """把某视频的提取结果（文案 + 发言人分段 + 经历画像）写回条目。"""
    v = get_video(output_dir, account_id, aweme_id) or {}
    v.update({
        "extracted": True,
        "extract_time": _now(),
        "text": text or "",
        "segments": segments or [],
        "visitor_profile": visitor_profile or {},
        "error": "",
    })
    upsert_video(output_dir, account_id, v)


def mark_video_error(output_dir: str, account_id: str, aweme_id: str, error: str) -> None:
    v = get_video(output_dir, account_id, aweme_id) or {}
    v.update({"extracted": False, "error": error or "", "extract_time": _now()})
    upsert_video(output_dir, account_id, v)


def remove_video(output_dir: str, account_id: str, aweme_id: str) -> bool:
    videos = load_videos(output_dir, account_id)
    remain = [v for v in videos if v.get("aweme_id") != aweme_id]
    if len(remain) == len(videos):
        return False
    _save_videos(output_dir, account_id, remain)
    return True


# ---------------------------------------------------------------- 排除清单（已删除视频）

def _excluded_path(output_dir: str) -> str:
    return os.path.join(_dir(output_dir), "excluded.json")


def load_excluded(output_dir: str) -> dict:
    """返回 {account_id: [aweme_id, ...]}。"""
    data = _read_json(_excluded_path(output_dir), {})
    return data if isinstance(data, dict) else {}


def _save_excluded(output_dir: str, data: dict) -> None:
    _write_json(_excluded_path(output_dir), data)


def add_excluded(output_dir: str, account_id: str, aweme_id: str) -> None:
    """把某视频 aweme_id 记入排除清单，下次抓取时跳过。"""
    data = load_excluded(output_dir)
    ids = data.get(account_id, [])
    if aweme_id not in ids:
        ids.append(aweme_id)
        data[account_id] = ids
        _save_excluded(output_dir, data)


def is_excluded(output_dir: str, account_id: str, aweme_id: str) -> bool:
    return aweme_id in load_excluded(output_dir).get(account_id, [])


def touch_fetch(output_dir: str, account_id: str, video_count: int) -> None:
    """更新账号的抓取时间与视频数。"""
    accounts = list_accounts(output_dir)
    for a in accounts:
        if a.get("id") == account_id:
            a["last_fetched_at"] = _now()
            a["video_count"] = video_count
            _save_accounts(output_dir, accounts)
            return
