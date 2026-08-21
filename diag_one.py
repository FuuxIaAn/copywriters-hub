# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, 'scripts')
from extract_server import _get_cookie, _build_kwargs, _fetch_detail
import asyncio

aweme_id = sys.argv[1] if len(sys.argv)>1 else "7672652530145713649"
cookie = _get_cookie()
print("cookie len:", len(cookie or ""))
if not cookie:
    print("无cookie，退出"); sys.exit(0)
kwargs = _build_kwargs(cookie)
raw = asyncio.run(_fetch_detail(kwargs, aweme_id))
detail = raw.get("aweme_detail") or raw
video = detail.get("video") or {}
print("video keys:", list(video.keys()))
addr = video.get("play_addr") or {}
print("play_addr url_list:", (addr.get("url_list") or [])[:2])
low = video.get("play_addr_lowbr") or {}
print("lowbr url_list:", (low.get("url_list") or [])[:2])
music = detail.get("music") or {}
print("music play_url:", (music.get("play_url") or {}).get("url_list", [])[:1])
print("desc:", str(detail.get("desc"))[:60])
