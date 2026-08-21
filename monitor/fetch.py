# -*- coding: utf-8 -*-
"""
对标账号抓取层（基于 f2 库）
=================================
职责：
  1. cookie 获取：优先自动读本机浏览器（Chrome/Edge/Firefox）的 douyin.com cookie，
     失败时允许从外部传入手动 cookie；实在没有则返回 mock 提示，不硬失败。
  2. 账号解析：主页链接 -> sec_user_id（支持短链自动重定向）。
  3. 用户信息：昵称 / 粉丝数 / 作品数。
  4. 视频列表：最近 N 条（默认 10），提取 aweme_id / 标题 desc / 发布时间 /
     点赞 digg_count / 评论 comment_count / 播放 play_count / 封面 / 时长。
  5. 限速：每个账号之间随机延时 1.5~3.5s，降低风控压力。

设计说明：
  - f2 0.0.1.7 的 API：DouyinCrawler(kwargs).fetch_user_post(UserPost(...))，
    返回 UserPostFilter，点赞数不在 filter 属性里，需要从原始 JSON
    _data['aweme_list'][*]['statistics'] 提取。
  - 若没有可用 cookie，或者发生网络/反爬错误，所有函数都返回带
    `error` 标记的结构，由上层决定是否使用 mock 数据。
"""
import asyncio
import hashlib
import itertools
import logging
import os
import random
import re
import time

logger = logging.getLogger("monitor.fetch")

# f2 相关导入（延迟到模块内部，避免无 f2 环境直接崩）
try:
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.filter import UserPostFilter, UserProfileFilter
    from f2.apps.douyin.model import UserPost, UserProfile
    from f2.apps.douyin.utils import SecUserIdFetcher
    from f2.utils.utils import get_cookie_from_browser, split_dict_cookie

    F2_AVAILABLE = True
except Exception as e:  # pragma: no cover
    F2_AVAILABLE = False
    F2_IMPORT_ERROR = str(e)
    logger.warning("f2 不可用: %s", e)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BROWSER_ORDER = ("chrome", "edge", "firefox")

# 环境变量：WB_MONITOR_MOCK=1 时强制走 mock 数据（用于无网络/演示）
MOCK_MODE = os.environ.get("WB_MONITOR_MOCK", "").lower() in ("1", "true", "yes")

_mock_counter = itertools.count()
_mock_fetch_count = {}  # home_url -> 模拟抓取次数（每次递增，用于 mock 数据增长）


class FetchError(Exception):
    """抓取层统一错误（带用户可读中文信息）"""


def get_douyin_cookie() -> str | None:
    """从本机浏览器读取 douyin.com 的 cookie，返回 'k=v; k2=v2' 字符串。"""
    if not F2_AVAILABLE:
        return None
    for browser in BROWSER_ORDER:
        try:
            c = get_cookie_from_browser(browser, "douyin.com")
            if c:
                return split_dict_cookie(c)
        except Exception as e:
            logger.debug("browser %s cookie 读取失败: %s", browser, e)
    return None


def _build_kwargs(cookie: str, timeout: int = 15) -> dict:
    return {
        "cookie": cookie,
        "headers": {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.douyin.com/",
        },
        "timeout": timeout,
        "max_retries": 2,
        "max_connections": 5,
    }


# ---------------------------------------------------------------- 异步核心

async def _resolve_sec_user_id(home_url: str) -> str:
    """主页链接/短链 -> sec_user_id"""
    try:
        return await SecUserIdFetcher.get_sec_user_id(home_url)
    except Exception as e:
        raise FetchError(f"无法解析主页链接中的用户ID: {e}")


async def _fetch_user_profile(kwargs: dict, sec_user_id: str) -> dict:
    """用户资料：昵称/粉丝数/作品数/签名"""
    async with DouyinCrawler(kwargs) as crawler:
        params = UserProfile(sec_user_id=sec_user_id)
        resp = await crawler.fetch_user_profile(params)
    f = UserProfileFilter(resp)
    if f.nickname is None:
        raise FetchError("用户资料接口返回异常，请检查 cookie 是否有效")
    return {
        "nickname": f.nickname,
        "signature": f.signature or "",
        "follower_count": f.follower_count or 0,
        "aweme_count": f.aweme_count or 0,
        "total_favorited": f.total_favorited or 0,
        "sec_user_id": f.sec_user_id or sec_user_id,
        "uid": f.uid or "",
    }


async def _fetch_user_posts(kwargs: dict, sec_user_id: str, count: int = 10) -> list:
    """最近 count 条作品，含点赞/评论/播放统计。"""
    async with DouyinCrawler(kwargs) as crawler:
        params = UserPost(max_cursor=0, count=count, sec_user_id=sec_user_id)
        resp = await crawler.fetch_user_post(params)
    f = UserPostFilter(resp)
    # 抖音作品接口 status_code 为 0 表示成功；非 0 或缺失才是异常。
    if f.status_code not in (0, None):
        raise FetchError(f"作品接口返回异常状态码: {f.status_code}")

    raw = resp.get("aweme_list") or []
    items = []
    aweme_ids = f.aweme_id or []
    descs = f.desc or []
    times = f.create_time or []
    covers = f.cover or []
    durations = f.video_duration or []

    for i, aweme_id in enumerate(aweme_ids):
        stats = (raw[i].get("statistics") or {}) if i < len(raw) else {}
        items.append(
            {
                "aweme_id": str(aweme_id),
                "desc": (descs[i] if i < len(descs) else "") or "",
                "create_time": (times[i] if i < len(times) else "") or "",
                "cover": (covers[i] if i < len(covers) else "") or "",
                "duration_ms": (durations[i] if i < len(durations) else 0) or 0,
                "digg_count": stats.get("digg_count") or 0,
                "comment_count": stats.get("comment_count") or 0,
                "play_count": stats.get("play_count") or 0,
                "share_count": stats.get("share_count") or 0,
                # 视频直链（供 ASR 兜底转录口播，免二次抓详情）
                "video_url": _extract_video_url(raw[i] if i < len(raw) else {}),
                # 全部可下载候选（多来源兜底：列表接口 play_addr 可能过期/为空，
                # 但 bit_rate / download_addr 等仍可能有值，保存下来供重扒时多次尝试）
                "video_urls": _extract_video_urls(raw[i] if i < len(raw) else {}),
            }
        )
    return items


def _extract_video_url(aweme: dict) -> str:
    """从 aweme_list 单条原始数据里取第一个可下载视频地址。

    优先级：music.play_url（独立音轨，最小且必含人声）→
    video.play_addr.url_list[0]（混流，含音轨）→ 其余来源。拿不到返回空串。
    """
    urls = _extract_video_urls(aweme)
    return urls[0] if urls else ""


def _extract_video_urls(aweme: dict) -> list:
    """收集单条 aweme 里所有可下载视频地址（多来源，去重保序）。

    覆盖抖音列表接口常见的 url 字段：
      1. music.play_url.url_list        —— 独立音轨（最小且必含人声）
      2. video.play_addr.url_list       —— 常规混流（含音轨）
      3. video.play_addr_lowbr.url_list —— 低清混流
      4. video.download_addr.url_list   —— 下载直链
      5. video.bit_rate[].play_addr     —— 多档码率（升序，低档可能纯视频流）
    列表接口的 url 常带过期签名，重扒时会再次用这些候选逐个尝试下载。
    """
    if not aweme:
        return []
    music = aweme.get("music") or {}
    video = aweme.get("video") or {}
    candidates = []

    def _push(container):
        for u in container.get("url_list") or []:
            if isinstance(u, str) and u.startswith("http"):
                candidates.append(u)

    _push(music.get("play_url") or {})
    _push(video.get("play_addr") or {})
    _push(video.get("play_addr_lowbr") or {})
    _push(video.get("download_addr") or {})
    gears = []
    for br in video.get("bit_rate") or []:
        rate = br.get("bit_rate") or 0
        for u in (br.get("play_addr") or {}).get("url_list") or []:
            if isinstance(u, str) and u.startswith("http"):
                gears.append((rate, u))
    gears.sort(key=lambda x: x[0])
    candidates.extend(u for _, u in gears)

    seen, uniq = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ---------------------------------------------------------------- 同步入口

def fetch_user_videos(home_url: str, count: int = 10, cookie: str | None = None) -> dict:
    """抓取单个账号最近视频。
    返回: {ok, account:{nickname,...}, videos:[...]} 或 {ok:False, error:...}
    """
    if MOCK_MODE:
        return _mock_account(home_url)

    if not F2_AVAILABLE:
        return {"ok": False, "error": "抓取组件未安装"}
    cookie = cookie or get_douyin_cookie()
    if not cookie:
        return {"ok": False, "error": "未找到可用 cookie（请先在 Chrome/Edge/Firefox 任一浏览器登录抖音）"}

    kwargs = _build_kwargs(cookie)

    async def _run():
        sec = await _resolve_sec_user_id(home_url)
        profile = await _fetch_user_profile(kwargs, sec)
        videos = await _fetch_user_posts(kwargs, sec, count)
        return {"sec_user_id": sec, "profile": profile, "videos": videos}

    try:
        result = asyncio.run(_run())
        return {
            "ok": True,
            "account": result["profile"],
            "videos": result["videos"],
        }
    except FetchError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"抓取失败: {e}"}


# ---------------------------------------------------------------- 微博（weibo.com ajax 接口）

_WEIBO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _get_weibo_cookie() -> str | None:
    """从本机浏览器读取 weibo.com 的登录 cookie（关键字段 SUB），返回 'k=v; k2=v2' 字符串。

    微博现在游客态接口（m.weibo.cn / weibo.com/ajax）都被风控拦截（432 / ok:-100），
    必须带登录态 cookie。复用 browser_cookie3 读 Chrome/Edge/Firefox。
    """
    try:
        import browser_cookie3
    except Exception:
        return None
    for loader in (browser_cookie3.chrome, browser_cookie3.edge, browser_cookie3.firefox):
        try:
            cj = loader(domain_name="weibo.com")
            cookies = {c.name: c.value for c in cj if c.value}
            if cookies.get("SUB"):
                return "; ".join(f"{k}={v}" for k, v in cookies.items())
        except Exception:
            continue
    return None


def _weibo_extract_uid(home_url: str) -> str:
    """从微博主页链接提取 uid。

    支持形式：
      - https://weibo.com/u/1669879400            （uid 直链）
      - https://weibo.com/1669879400              （同 uid）
      - https://weibo.com/n/某某昵称              （自定义域名，需接口解析）
      - https://m.weibo.cn/u/1669879400
      - https://m.weibo.cn/profile/1669879400
    """
    low = (home_url or "").strip().lower()
    # uid 直链：/u/<数字> 或 /profile/<数字>
    m = re.search(r"(?:weibo\.(?:com|cn))/(?:u|profile)/(\d+)", low)
    if m:
        return m.group(1)
    m = re.search(r"weibo\.com/(\d+)", low)
    if m:
        return m.group(1)
    return ""


def _weibo_resolve_uid_by_nick(home_url: str, cookie: str) -> str:
    """自定义域名主页（weibo.com/n/<nick>）→ 通过 profile 接口解析 uid。"""
    import httpx
    headers = {
        "User-Agent": _WEIBO_UA,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }
    # 从 /n/<nick> 里抠昵称
    m = re.search(r"weibo\.com/n/([^/?\s]+)", (home_url or "").lower())
    if not m:
        return ""
    nick = m.group(1)
    try:
        url = f"https://weibo.com/ajax/profile/info?custom={nick}"
        r = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
        d = r.json()
        user = (d.get("data") or {}).get("user") or {}
        return str(user.get("id") or "")
    except Exception:
        return ""


def _weibo_fetch_user_info(uid: str, cookie: str) -> dict:
    """微博用户资料：昵称/简介/粉丝数/微博数。"""
    import httpx
    headers = {
        "User-Agent": _WEIBO_UA,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }
    try:
        url = f"https://weibo.com/ajax/profile/info?uid={uid}"
        r = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
        d = r.json()
        user = (d.get("data") or {}).get("user") or {}
        if not user:
            return {}

        def _to_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        return {
            "nickname": user.get("screen_name") or "",
            "signature": user.get("description") or "",
            "follower_count": _to_int(user.get("followers_count")),
            "aweme_count": _to_int(user.get("statuses_count")),
            "uid": uid,
        }
    except Exception as e:
        logger.warning("微博用户资料抓取失败 uid=%s: %s", uid, e)
        return {"uid": uid}


def _weibo_fetch_videos(uid: str, cookie: str, count: int = 10) -> list:
    """抓微博主页的视频微博列表（视频 Tab，接口 getWaterFallContent 分页）。

    返回结构：data.list 为微博卡片数组，每条 mblog 含 page_info（视频信息）与 text 正文。
    视频直链在 page_info.media_info.stream_url / stream_url_hd / mp4_hd_url。
    游标字段为 data.next_cursor。
    """
    import httpx
    headers = {
        "User-Agent": _WEIBO_UA,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }
    items = []
    cursor = None
    while len(items) < count:
        url = f"https://weibo.com/ajax/profile/getWaterFallContent?uid={uid}"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            r = httpx.get(url, headers=headers, timeout=25, follow_redirects=True)
            d = r.json()
        except Exception as e:
            logger.warning("微博视频列表抓取失败: %s", e)
            break
        if d.get("ok") != 1:
            # ok != 1 通常意味着 cookie 失效或接口变更
            if not items:
                logger.warning("微博视频接口返回 ok=%s: %s", d.get("ok"), d.get("msg", ""))
            break
        data = d.get("data") or {}
        feed = data.get("list") or data.get("cards") or []
        if not feed:
            break
        for card in feed:
            mblog = card.get("mblog") or card
            if not mblog:
                continue
            # 只保留视频微博（page_info.type == 'video' 且含 media_info）
            page_info = mblog.get("page_info") or {}
            media = page_info.get("media_info") or {}
            if not media and not page_info.get("type") == "video":
                continue
            mid = str(mblog.get("mid") or mblog.get("id") or "")
            text_raw = mblog.get("text_raw") or mblog.get("text") or ""
            # 去掉 <...> 标签（微博正文带 emoji/话题标签的 HTML）
            text = re.sub(r"<[^>]+>", "", text_raw)
            # 视频直链：stream_url_hd > stream_url > mp4_hd_url > mp4_sd_url
            video_url = (
                media.get("stream_url_hd")
                or media.get("stream_url")
                or media.get("mp4_hd_url")
                or media.get("mp4_sd_url")
                or ""
            )
            items.append(
                {
                    "aweme_id": mid,
                    "desc": text.strip(),
                    "create_time": mblog.get("created_at") or "",
                    "cover": (page_info.get("page_pic") or {}).get("url")
                    if isinstance(page_info.get("page_pic"), dict)
                    else page_info.get("page_pic") or "",
                    "duration_ms": (media.get("duration") or 0),
                    "digg_count": mblog.get("attitudes_count") or 0,
                    "comment_count": mblog.get("comments_count") or 0,
                    "play_count": (page_info.get("play_count") or 0),
                    "share_count": mblog.get("reposts_count") or 0,
                    "video_url": video_url,
                    "_platform": "weibo",
                }
            )
            if len(items) >= count:
                break
        # 取下一页游标
        cursor = data.get("next_cursor") or None
        if not cursor:
            break
    return items[:count]


def fetch_user_videos_weibo(home_url: str, count: int = 10, cookie: str | None = None) -> dict:
    """抓取单个微博账号主页的视频微博列表。

    返回结构与 fetch_user_videos 一致：{ok, account:{nickname,...}, videos:[...]}
    或 {ok:False, error:...}。依赖浏览器登录态 cookie（SUB）。
    """
    import httpx
    try:
        import browser_cookie3  # noqa: F401
    except Exception:
        return {"ok": False, "error": "缺少 browser_cookie3 组件，无法读取浏览器微博登录态"}

    cookie = cookie or _get_weibo_cookie()
    if not cookie:
        return {"ok": False, "error": "未找到微博登录态（请先在 Chrome/Edge/Firefox 任一浏览器登录 weibo.com）"}

    uid = _weibo_extract_uid(home_url)
    if not uid:
        uid = _weibo_resolve_uid_by_nick(home_url, cookie)
    if not uid:
        return {"ok": False, "error": "无法从链接解析微博用户 ID（支持 weibo.com/u/数字 或 weibo.com/n/昵称 形式）"}

    try:
        account = _weibo_fetch_user_info(uid, cookie)
        videos = _weibo_fetch_videos(uid, cookie, count)
        if not account.get("nickname"):
            # 资料拿不到但视频能拿时，至少给个 uid 兜底
            account["nickname"] = f"微博用户{uid}"
        return {
            "ok": True,
            "account": account,
            "videos": videos,
        }
    except Exception as e:
        return {"ok": False, "error": f"微博抓取失败: {e}"}


def fetch_accounts_videos(
    accounts: list[dict], count: int = 10, on_progress=None
) -> list[dict]:
    """批量抓取多个账号，带限速与进度回调。
    accounts: [{home_url, note, douyin_id}...]
    返回 [{home_url, note, ok, account, videos, error}...]
    """
    results = []
    total = len(accounts)
    for idx, acc in enumerate(accounts):
        start = time.time()
        if on_progress:
            on_progress(idx, total, acc.get("note") or acc.get("home_url", ""))
        r = fetch_user_videos(acc.get("home_url", ""), count=count)
        r["note"] = acc.get("note", "")
        r["douyin_id"] = acc.get("douyin_id", "")
        r["home_url"] = acc.get("home_url", "")
        r["_account_id"] = acc.get("id", "")  # 供「我的账号」按账号归档快照
        results.append(r)
        # 限速：真实抓取时每个账号间隔随机 1.5~3.5s（mock 不延时）
        if not MOCK_MODE and idx < total - 1:
            time.sleep(random.uniform(1.5, 3.5))
        logger.info("account %s done in %.1fs", acc.get("home_url"), time.time() - start)
    return results


# ---------------------------------------------------------------- mock（演示/离线）

_MOCK_POOL = [
    {
        "nickname": "情感观察员",
        "signature": "讲情感故事，聊人间百态",
        "videos": [
            ("看完这个视频，我决定原谅他了", 128000, 3200, 890000),
            ("30岁以后才明白的道理，句句扎心", 96500, 2100, 660000),
            ("女人这辈子，最怕的不是穷", 85200, 1800, 540000),
            ("当你熬过那段最难的日子", 61000, 1200, 380000),
            ("成年人的崩溃，都是无声的", 52000, 990, 310000),
        ],
    },
    {
        "nickname": "财商老K",
        "signature": "聊聊赚钱那些事",
        "videos": [
            ("普通人翻身的3个机会，2026年还有效", 152000, 4100, 980000),
            ("存款多少才算合格？看完沉默了", 118000, 2600, 720000),
            ("为什么越努力越穷？这3个陷阱要避开", 88000, 1900, 500000),
            ("30岁还不懂理财，等于白打工", 74000, 1500, 430000),
            ("穷人和富人的区别，就差在这5点", 66000, 1300, 390000),
        ],
    },
    {
        "nickname": "成长笔记",
        "signature": "记录我的成长方法论",
        "videos": [
            ("看完这个视频，我决定原谅他了", 98000, 2500, 700000),
            ("自律的真相：不是靠意志力", 76000, 1600, 460000),
            ("高手都在用的3个思维方式", 58000, 1100, 330000),
            ("如何一年读完100本书", 45000, 800, 260000),
            ("拒绝内耗，从这3件小事开始", 39000, 700, 220000),
        ],
    },
]


def _mock_account(home_url: str) -> dict:
    # 按主页链接稳定分配 mock 池：同一账号多次抓取内容一致，
    # 「我的账号」的涨跌对比/新增标记演示才成立（真实抓取 aweme_id 本身稳定）。
    idx = int(hashlib.md5(home_url.encode()).hexdigest(), 16) % len(_MOCK_POOL)
    src = _MOCK_POOL[idx]
    # 每次抓取模拟数据增长（第 0 次为基准，之后递增），让 delta/提醒可演示
    _mock_fetch_count[home_url] = _mock_fetch_count.get(home_url, 0) + 1
    growth = _mock_fetch_count[home_url]  # 1, 2, 3, ...
    videos = []
    for i, (desc, digg, comment, play) in enumerate(src["videos"]):
        videos.append(
            {
                # aweme_id 含 URL 哈希：不同账号各自独立视频（同题不同视频）
                "aweme_id": f"mock{idx}{i}{int(hashlib.md5(home_url.encode()).hexdigest(), 16) % 100000}",
                "desc": desc,
                "create_time": "2026-08-10 12:00:00",
                "cover": "",
                "duration_ms": 45000,
                "digg_count": digg + (digg // 20) * (growth - 1),      # 每次增长 ~5%
                "comment_count": comment + (comment // 20) * (growth - 1),
                "play_count": play + (play // 20) * (growth - 1),
                "share_count": (digg + (digg // 20) * (growth - 1)) // 50,
            }
        )
    return {
        "ok": True,
        "account": {
            "nickname": src["nickname"],
            "signature": src["signature"],
            "follower_count": 120000 + idx * 30000 + (growth - 1) * 87,   # 每次涨 87 粉
            "aweme_count": 88 + idx * 12,
            "total_favorited": 500000 + idx * 100000 + (growth - 1) * 1200,  # 每次涨 1200 获赞
            "sec_user_id": f"mock_sec_{idx}",
        },
        "videos": videos,
    }
