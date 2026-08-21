# -*- coding: utf-8 -*-
"""
对标监控 · 服务端业务逻辑
==========================
账号管理（增删查）、后台抓取任务、自动轮询、状态查询、报告读取、数据变化提醒。
server.py 只做薄路由转发，具体逻辑都在这里，便于独立测试。

两套抓取模式：
  - 手动抓取：`start_fetch` 一次性抓全量榜单（高赞榜 + 账号一览）
  - 自动轮询：`start_poll` 后台守护线程按固定间隔（默认 5 分钟）持续抓取，
              相对上一次快照逐视频算增量，数据异常（暴涨/新视频/涨粉）产生提醒
"""
import asyncio
import os
import sys
import threading
import time

# 让 monitor 包可导入（项目根目录；打包后 exe 解压目录同样生效）
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import monitor.fetch as mfetch  # noqa: E402
import monitor.store as mstore  # noqa: E402
import monitor.topics as mtopics  # noqa: E402
import monitor.realtime_store as rstore  # noqa: E402

# ---------------------------------------------------------------- 全局抓取状态

_lock = threading.Lock()
_fetch_state = {
    "running": False,
    "progress": {"done": 0, "total": 0, "current": ""},
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "report": None,  # 最近一次成功报告（内存缓存）
}


def _reset_state(total: int):
    _fetch_state["running"] = True
    _fetch_state["progress"] = {"done": 0, "total": total, "current": ""}
    _fetch_state["started_at"] = time.time()
    _fetch_state["finished_at"] = None
    _fetch_state["last_error"] = None
    _fetch_state["report"] = None


def _finish_state(error: str | None = None):
    _fetch_state["running"] = False
    _fetch_state["finished_at"] = time.time()
    _fetch_state["last_error"] = error
    if _fetch_state["progress"]["total"]:
        _fetch_state["progress"]["done"] = _fetch_state["progress"]["total"]


# ---------------------------------------------------------------- 账号管理

def _validate_url(home_url: str) -> str | None:
    """校验主页链接，合法返回 None，否则返回错误信息。

    用户常整段粘贴抖音「复制主页链接」分享文本（标题+链接+文字），
    这里复用 extract_server._extract_share_url 从整段里抠出 URL 再校验。
    """
    home_url = (home_url or "").strip()
    if not home_url:
        return "请输入抖音主页链接"
    # 从整段分享文本里提取出真正的 URL
    try:
        from extract_server import _extract_share_url
        url = _extract_share_url(home_url)
    except Exception:
        url = home_url
    if not url or ("douyin.com" not in url and "v.douyin.com" not in url):
        return "链接不像抖音主页，请粘贴复制到的主页链接（含 douyin.com）"
    return None


def get_accounts(data_dir: str, output_dir: str) -> dict:
    """返回账号列表，并附带每个账号「距上次抓取更新 X 条视频」的快照信息。"""
    accounts = mstore.load_accounts(data_dir)
    for a in accounts:
        info = mstore.load_account_new_count(output_dir, a.get("id", ""))
        a["new_count"] = info["new_count"]
        a["has_base"] = info["has_base"]
        a["last_fetched_at"] = info["fetched_at"]
    return {"ok": True, "accounts": accounts}


def resolve_account(home_url: str) -> dict:
    """粘贴链接时先抓取资料预览（昵称/粉丝数/作品数），供前端确认后添加。"""
    err = _validate_url(home_url)
    if err:
        return {"ok": False, "error": err}
    r = mfetch.fetch_user_videos(home_url, count=1)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "抓取失败")}
    acc = r.get("account") or {}
    return {"ok": True, "account": acc}


def add_account_with_fetch(data_dir: str, home_url: str, note: str = "") -> dict:
    """添加账号：抓取资料回填（昵称/粉丝/抖音号/sec_user_id），并做三重去重。"""
    err = _validate_url(home_url)
    if err:
        return {"ok": False, "error": err}
    r = mfetch.fetch_user_videos(home_url, count=1)
    if not r.get("ok"):
        # 抓资料失败时仍允许先加入列表（去重），等「立即抓取」时再回填
        return mstore.add_account(data_dir, home_url, note)
    acc = r.get("account") or {}
    return mstore.add_account(
        data_dir,
        home_url,
        note,
        douyin_id=acc.get("douyin_id", "") or acc.get("uid", ""),
        sec_user_id=acc.get("sec_user_id", ""),
        meta={
            "nickname": acc.get("nickname", ""),
            "follower_count": acc.get("follower_count", 0),
            "aweme_count": acc.get("aweme_count", 0),
            "signature": acc.get("signature", ""),
        },
    )


def update_account(data_dir: str, account_id: str, note: str) -> dict:
    return mstore.update_account(data_dir, account_id, note)


def remove_account(data_dir: str, account_id: str) -> dict:
    ok = mstore.remove_account(data_dir, account_id)
    return {"ok": ok, "error": None if ok else "账号不存在或已删除"}


# ---------------------------------------------------------------- 抓取任务

def start_fetch(data_dir: str, output_dir: str, force: bool = False) -> dict:
    with _lock:
        if _fetch_state["running"] and not force:
            return {"ok": False, "error": "已有抓取任务进行中，请稍候"}
        accounts = mstore.load_accounts(data_dir)
        if not accounts:
            return {"ok": False, "error": "监控列表为空，请先添加对标账号"}
        _reset_state(len(accounts))
        t = threading.Thread(
            target=_run_fetch_job, args=(data_dir, output_dir, list(accounts)), daemon=True
        )
        t.start()
        return {"ok": True, "total": len(accounts)}


def _run_fetch_job(data_dir: str, output_dir: str, accounts: list):
    def on_progress(done: int, total: int, current: str):
        with _lock:
            _fetch_state["progress"] = {"done": done, "total": total, "current": current}

    try:
        results = mfetch.fetch_accounts_videos(accounts, on_progress=on_progress)
        # 逐账号：回填资料 + 读上一份快照 -> 对比 -> 落新快照（供「更新X条视频」）
        for r in results:
            acc_id = r.get("_account_id") or ""
            if not r.get("ok"):
                continue
            acc_data = r.get("account") or {}
            if acc_id:
                mstore.update_account_meta(data_dir, acc_id, acc_data)
                prev = mstore.load_latest_snapshot(output_dir, acc_id)
                snap = mstore.build_compare(
                    acc_id, acc_data, r.get("videos") or [], prev
                )
                mstore.save_account_snapshot(output_dir, acc_id, snap)
        report = mtopics.build_report(results)
        # 预拉榜单视频字幕，点开弹窗即可直接显示（无需再异步拉取）
        try:
            n_sub = preload_subtitles(output_dir, report, {})
            if n_sub:
                print(f"[monitor] 已预存 {n_sub} 条视频字幕")
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 预存字幕失败（不影响主流程）: {e}")
        md = mtopics.build_markdown(report)
        path = mstore.save_report(output_dir, report, md)
        _fetch_state["report"] = report
        _finish_state()
        print(f"[monitor] 抓取完成: {report['account_count']} 账号, "
              f"{report['total_videos']} 视频 -> {path}")
    except Exception as e:  # noqa: BLE001
        print(f"[monitor] 抓取异常: {e}")
        _finish_state(error=str(e))


def get_status() -> dict:
    with _lock:
        return {"ok": True, "state": dict(_fetch_state, progress=dict(_fetch_state["progress"]))}


def get_report(output_dir: str) -> dict:
    report = _fetch_state.get("report") or mstore.load_latest_report(output_dir)
    if report is None:
        return {"ok": True, "report": None}
    return {"ok": True, "report": report}


# ---------------------------------------------------------------- 视频文案（逐字稿）

def get_video_transcript(output_dir: str, aweme_id: str, api_config: dict) -> dict:
    """按 aweme_id 获取单视频完整口播文案（逐字稿）。

    优先级：① 视频自带字幕（subtitle，快）→ ② ASR 逐字稿（下载视频抽音轨转写）。
    返回 {ok, text, source, aweme_id, video_info}；text 可直接拿去编辑/洗稿。
    """
    aweme_id = (aweme_id or "").strip()
    if not aweme_id:
        return {"ok": False, "error": "缺少 aweme_id"}

    import extract_server as ex
    import asr_server as asr

    if ex.MOCK or asr.MOCK or os.environ.get("WB_MONITOR_MOCK", "") == "1":
        return _mock_transcript(output_dir, aweme_id)

    if not ex.F2_AVAILABLE:
        return {"ok": False, "error": "抓取组件未安装（f2 库不可用）"}

    cookie = ex._get_cookie()
    if not cookie:
        return {"ok": False, "error": "未找到可用 cookie（请先在 Chrome/Edge 登录抖音）"}
    kwargs = ex._build_kwargs(cookie)

    # ① 拉视频详情原始 JSON
    try:
        raw = asyncio.run(ex._fetch_detail(kwargs, aweme_id))
    except Exception as e:
        return {"ok": False, "error": f"视频详情获取失败: {e}"}

    detail = raw.get("aweme_detail") or raw
    if not detail:
        return {"ok": False, "error": "视频详情为空（视频可能已删除）"}

    # 组装 video_info
    stats = detail.get("statistics") or {}
    video_info = {
        "aweme_id": aweme_id,
        "desc": (detail.get("desc") or "")[:200],
        "nickname": ((detail.get("author") or {}).get("nickname") or ""),
        "digg_count": stats.get("digg_count") or 0,
        "comment_count": stats.get("comment_count") or 0,
        "create_time": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(detail.get("create_time") or 0)
        ),
    }

    # ② 优先字幕
    sub_url = ex._extract_subtitle_url(raw)
    if sub_url:
        cues = ex._download_subtitle(sub_url)
        text = ex._cues_to_text(cues)
        if text and len(text) > 10:
            return {"ok": True, "text": text, "source": "字幕", **video_info}

    # ③ 无字幕 → ASR：下载视频 → 抽音轨 → 转写
    asr_key = asr._load_key(output_dir)
    if not asr_key:
        return {
            "ok": False,
            "error": "该视频没有字幕，需要硅基流动 API Key 才能转写（请在「配音工坊 → ASR 设置」粘贴 Key）",
        }
    urls = asr._pick_video_urls(raw)
    if not urls:
        return {"ok": False, "error": "未找到可下载的视频地址"}

    video_path, _sz, derr = asr._download_video(output_dir, urls, aweme_id)
    if not video_path:
        return {"ok": False, "error": f"视频下载失败: {derr}"}
    try:
        wav_path, aerr = asr._extract_audio(video_path)
        if not wav_path:
            return {"ok": False, "error": aerr}
        text, serr = asr._call_asr(asr_key, wav_path)
        if not text:
            return {"ok": False, "error": serr}
        # LLM 二次纠错（命理专名/同音字/补标点），失败静默回退原文
        try:
            text = asr._llm_correct_text(text, api_config or {})
        except Exception:
            pass
    finally:
        # 中转文件用完即删（视频 + 抽出的 wav）
        for p in (video_path, os.path.splitext(video_path)[0] + ".wav"):
            try:
                if p and os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
    return {"ok": True, "text": text, "source": "语音转写", **video_info}


def preload_subtitles(output_dir: str, report: dict, api_config: dict) -> int:
    """抓取完成后，为榜单里的每条视频预拉一次详情、提取「视频自带字幕」。

    只做字幕（接口现成、快，不下载视频、不做 ASR 转写），把字幕文本塞进
    report 里各视频对象的 `transcript` 字段，前端点开弹窗即可直接显示，
    无需再走「正在获取…」的异步拉取。无字幕的视频不塞（前端会兜底转写）。

    返回成功预存字幕的视频条数。
    """
    import extract_server as ex

    if ex.MOCK or os.environ.get("WB_MONITOR_MOCK", "") == "1":
        return 0
    if not ex.F2_AVAILABLE:
        return 0

    cookie = ex._get_cookie()
    if not cookie:
        return 0
    kwargs = ex._build_kwargs(cookie)

    # 收集需要预拉的视频（高赞榜 + 各账号 top），按 aweme_id 去重
    videos_by_id = {}
    for v in report.get("top_videos", []) or []:
        vid = str(v.get("aweme_id", ""))
        if vid and vid not in videos_by_id:
            videos_by_id[vid] = v
    for b in report.get("account_top", []) or []:
        for v in b.get("top", []) or []:
            vid = str(v.get("aweme_id", ""))
            if vid and vid not in videos_by_id:
                videos_by_id[vid] = v

    loaded = 0
    for vid, v in videos_by_id.items():
        try:
            raw = asyncio.run(ex._fetch_detail(kwargs, vid))
        except Exception:
            continue
        sub_url = ex._extract_subtitle_url(raw)
        if not sub_url:
            continue
        cues = ex._download_subtitle(sub_url)
        text = ex._cues_to_text(cues)
        if text and len(text) > 10:
            v["transcript"] = {"text": text, "source": "字幕"}
            loaded += 1
    return loaded


def _mock_transcript(output_dir: str, aweme_id: str) -> dict:
    """mock 模式：返回一段内置口播逐字稿，便于离线演示查看/编辑/洗稿。"""
    text = (
        "大家好，今天给大家讲一个命理小知识。\n"
        "很多人问我，为什么自己明明很努力，却总是存不住钱？\n"
        "从八字来看，这叫「财库漏」，也就是财星被比劫夺了。\n"
        "这种人不是没财运，是钱一到手就有人来分。\n"
        "解决办法很简单，少跟人合伙，钱自己攥紧。\n"
        "想了解自己的命盘，评论区扣个「想看」。"
    )
    return {
        "ok": True,
        "text": text,
        "source": "mock 文案",
        "aweme_id": aweme_id,
        "desc": "命理小知识：为什么存不住钱",
        "nickname": "命理师老张",
        "digg_count": 89000,
        "comment_count": 3200,
        "create_time": "2026-08-10 12:00:00",
    }


# ---------------------------------------------------------------- 自动轮询

_poll_lock = threading.Lock()

_poll_state = {
    "running": False,        # 后台轮询线程是否在跑
    "started_at": None,      # 本轮监控启动时间戳
    "interval": rstore.DEFAULT_POLL_INTERVAL,  # 轮询间隔（秒）
    "poll_count": 0,         # 累计已完成轮询次数
    "last_poll_at": None,    # 上次轮询完成时间戳
    "next_poll_at": None,    # 下次计划轮询时间戳
    "last_error": None,      # 最近一次轮询错误
    "report": None,          # 最近一次成功报告（内存缓存）
    "unread_alerts": 0,      # 未读提醒数
}

_poll_stop_event = threading.Event()
_poll_thread = None


def start_poll(data_dir: str, output_dir: str, interval: int | None = None,
               count: int = 10, force: bool = False) -> dict:
    """启动后台自动轮询线程。

    返回 {ok, interval, account_count} 或 {ok:False, error}。
    """
    global _poll_thread
    with _poll_lock:
        if _poll_state["running"] and not force:
            return {"ok": False, "error": "自动监控已在运行中"}

        accounts = mstore.load_accounts(data_dir)
        if not accounts:
            return {"ok": False, "error": "监控列表为空，请先添加对标账号"}

        if interval is not None and interval > 0:
            _poll_state["interval"] = interval
        else:
            _poll_state["interval"] = rstore.DEFAULT_POLL_INTERVAL

        _poll_state["running"] = True
        _poll_state["started_at"] = time.time()
        _poll_state["poll_count"] = 0
        _poll_state["last_poll_at"] = None
        _poll_state["next_poll_at"] = time.time()  # 立即开始第一轮
        _poll_state["last_error"] = None
        _poll_stop_event.clear()

        _poll_thread = threading.Thread(
            target=_poll_loop, args=(data_dir, output_dir, count), daemon=True
        )
        _poll_thread.start()
        return {
            "ok": True,
            "interval": _poll_state["interval"],
            "account_count": len(accounts),
        }


def stop_poll() -> dict:
    """停止后台自动轮询线程。"""
    global _poll_thread
    with _poll_lock:
        was_running = _poll_state["running"]
        _poll_stop_event.set()
        _poll_state["running"] = False
        _poll_state["next_poll_at"] = None
    if was_running:
        print("[monitor] 自动监控已停止")
    return {"ok": True, "was_running": was_running}


def _poll_loop(data_dir: str, output_dir: str, count: int):
    """后台轮询主循环：按 interval 间隔持续抓取。"""
    while not _poll_stop_event.is_set():
        # 等待到计划时间，期间可被 stop 中断
        while not _poll_stop_event.is_set():
            with _poll_lock:
                target = _poll_state["next_poll_at"] or time.time()
                interval = _poll_state["interval"]
            now = time.time()
            if now >= target:
                break
            time.sleep(min(1.0, max(0.1, target - now)))

        if _poll_stop_event.is_set():
            break

        # 执行一轮抓取
        _run_one_poll(data_dir, output_dir, count)

        # 计算下一轮时间
        with _poll_lock:
            _poll_state["poll_count"] += 1
            _poll_state["last_poll_at"] = time.time()
            _poll_state["next_poll_at"] = time.time() + _poll_state["interval"]


def _run_one_poll(data_dir: str, output_dir: str, count: int):
    """单轮抓取：读账号 -> 抓数据 -> 逐账号对比 -> 落快照 -> 汇总 -> 生成提醒。"""
    accounts = mstore.load_accounts(data_dir)
    if not accounts:
        return

    try:
        results = mfetch.fetch_accounts_videos(accounts, count=count)
        # 逐账号：回填资料 + 读上一份快照 -> 对比 -> 落新快照
        for r in results:
            acc_id = r.get("_account_id") or r.get("home_url", "")
            if not r.get("ok"):
                continue
            acc_data = r.get("account") or {}
            if acc_id:
                mstore.update_account_meta(data_dir, acc_id, acc_data)
                prev = rstore.load_latest_snapshot(output_dir, acc_id) if acc_id else None
                snap = rstore.build_compare(
                    acc_id, acc_data, r.get("videos") or [], prev
                )
                rstore.save_account_snapshot(output_dir, acc_id, snap)

        # 汇总报告
        account_ids = [a.get("id") for a in accounts]
        report = rstore.build_report(output_dir, account_ids)
        md = rstore.build_markdown(report)
        rstore.save_report(output_dir, report, md)

        # 生成数据变化提醒
        new_alerts = rstore.build_alerts(report)
        all_alerts = rstore.save_new_alerts(output_dir, new_alerts)
        unread = rstore.count_unread(all_alerts)

        with _poll_lock:
            _poll_state["report"] = report
            _poll_state["unread_alerts"] = unread
            _poll_state["last_error"] = None
        print(f"[monitor] 自动轮询完成: {report['account_count']} 账号, "
              f"{report['total_videos']} 视频, 新增 {report['new_videos']}, "
              f"提醒 {len(new_alerts)} 条(未读 {unread})")
    except Exception as e:  # noqa: BLE001
        with _poll_lock:
            _poll_state["last_error"] = str(e)
        print(f"[monitor] 自动轮询异常: {e}")


def get_poll_status() -> dict:
    with _poll_lock:
        return {
            "ok": True,
            "state": {
                "running": _poll_state["running"],
                "started_at": _poll_state["started_at"],
                "interval": _poll_state["interval"],
                "poll_count": _poll_state["poll_count"],
                "last_poll_at": _poll_state["last_poll_at"],
                "next_poll_at": _poll_state["next_poll_at"],
                "last_error": _poll_state["last_error"],
                "unread_alerts": _poll_state["unread_alerts"],
            },
        }


def get_poll_report(output_dir: str) -> dict:
    with _poll_lock:
        report = _poll_state["report"] or rstore.load_latest_report(output_dir)
    if report is None:
        return {"ok": True, "report": None}
    return {"ok": True, "report": report}


def get_alerts(output_dir: str) -> dict:
    alerts = rstore.load_alerts(output_dir)
    unread = rstore.count_unread(alerts)
    with _poll_lock:
        _poll_state["unread_alerts"] = unread
    return {"ok": True, "alerts": alerts, "unread": unread}


def mark_alerts_read(output_dir: str, alert_id: str | None = None) -> dict:
    alerts = rstore.mark_alerts_read(output_dir, alert_id)
    unread = rstore.count_unread(alerts)
    with _poll_lock:
        _poll_state["unread_alerts"] = unread
    return {"ok": True, "unread": unread}
