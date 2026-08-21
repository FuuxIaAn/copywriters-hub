# -*- coding: utf-8 -*-
"""
作品库 / 模拟对话库 · 服务端业务逻辑
====================================
给 N 个账号主页，抓取账号下所有视频，批量扒文案做成「模拟对话库」，
配音工坊里点某条作品即可「导入对话」（复用已扒好的 segments，无需重新提取）。

流程（后台任务，前端轮询进度）：
  1. upsert 账号（platform + home_url + 资料 meta）
  2. fetch_user_videos 抓视频列表（复用 monitor/fetch.py，抖音）
  3. 逐视频提取文案：优先字幕/描述，再用 LLM 区分发言人（复用 extract_server）
  4. 写回 store（mark_extracted / mark_video_error）
  5. 进度实时更新（_state.progress）

- WB_WORKSLIB_MOCK / WB_MONITOR_MOCK=1 时走 mock 演示数据。
"""
import asyncio
import json
import os
import random
import sys
import threading
import time

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import monitor.fetch as mfetch  # noqa: E402
import works_library_store as wstore  # noqa: E402

# ---------------------------------------------------------------- 任务状态

_lock = threading.Lock()
_state = {
    "running": False,
    "task": "",          # 当前任务类型：fetch / extract
    "progress": {"done": 0, "total": 0, "current": ""},
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "result": None,      # 最近一次任务摘要
}

# ---------------------------------------------------------------- crawl 持久化 / 自动续跑
# 用户诉求（2026-08-18）：扒文案任务可能要跑几小时，用户电脑不关、长时间等待没事，
# 但怕「调整 / 关软件」导致任务中断前功尽弃。这里把 crawl 任务进度落到磁盘：
#   - 任务开始写「进行中」标记，正常结束（成功/失败）清除标记；
#   - 若进程被强杀 / 关软件，标记残留 → 下次启动自动续跑（增量跳过已完成视频）。
# 关键：增量逻辑（已 extracted 且有 segments 的视频自动跳过）保证续跑不重复、只补缺口。
_CRAWL_MARK = "workslib_crawl_resume.json"


def _crawl_mark_path(output_dir: str) -> str:
    return os.path.join(output_dir, _CRAWL_MARK)


def _mark_crawl_started(output_dir: str, accounts: list):
    try:
        data = {
            "running": True,
            "started_at": time.time(),
            "accounts": [a.get("id", "") for a in accounts],
            "count": 50,
        }
        with open(_crawl_mark_path(output_dir), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _clear_crawl_mark(output_dir: str):
    try:
        p = _crawl_mark_path(output_dir)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _read_crawl_mark(output_dir: str):
    try:
        p = _crawl_mark_path(output_dir)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def maybe_auto_resume_crawl(output_dir: str, api_config: dict | None = None) -> bool:
    """软件启动时调用：若上次 crawl 因关软件/进程被杀而中断（标记残留），自动续跑。

    利用增量跳过逻辑（已 extracted 且有 segments 的视频跳过），续跑只补剩余缺口，
    不重复处理已完成视频。返回是否触发了续跑。
    """
    mark = _read_crawl_mark(output_dir)
    if not mark or not mark.get("running"):
        return False
    with _lock:
        if _state["running"]:
            return False
    # 有未完成任务标记 → 自动重新触发 crawl（count 沿用上次，默认 50）
    try:
        print("[workslib] 检测到上次扒文案任务中断，自动续跑…")
        start_crawl(output_dir, count=int(mark.get("count", 50)), api_config=api_config or {})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[workslib] 自动续跑失败：{e}")
        return False


def _set_progress(task: str, done: int, total: int, current: str = ""):
    with _lock:
        _state["task"] = task
        _state["progress"] = {"done": done, "total": total, "current": current}


def get_status() -> dict:
    with _lock:
        return {
            "ok": True,
            "running": _state["running"],
            "task": _state["task"],
            "progress": _state["progress"],
            "started_at": _state["started_at"],
            "finished_at": _state["finished_at"],
            "last_error": _state["last_error"],
            "result": _state["result"],
        }


# ---------------------------------------------------------------- 账号管理

def _validate_home_url(url: str) -> str | None:
    """校验主页链接，从整段分享文本里抠 URL 再判断平台。"""
    url = (url or "").strip()
    if not url:
        return "请输入账号主页链接"
    try:
        from extract_server import _extract_share_url
        url = _extract_share_url(url)
    except Exception:
        pass
    if not url:
        return "链接解析失败，请粘贴账号主页链接"
    low = url.lower()
    # 抖音
    if "douyin.com" in low:
        return None
    # 小红书 / 微博：主页级抓取暂未接入，但微博单条视频可转录（走「语音转文字」）
    if "xiaohongshu.com" in low or "xhslink.com" in low:
        return "小红书「主页级」抓取暂未接入（仅支持抖音/微博主页）"
    if "weibo.com" in low or "weibo.cn" in low:
        return None
    if "t.cn" in low:
        return "微博短链请先展开成 weibo.com 主页链接再添加（短链无法直接定位主页）"
    return "暂不支持该平台，请粘贴抖音或微博主页链接"


def _detect_platform(url: str) -> str:
    low = (url or "").lower()
    if "xiaohongshu" in low or "xhslink" in low:
        return "xiaohongshu"
    if "weibo" in low:
        return "weibo"
    return "douyin"


def list_accounts(output_dir: str) -> dict:
    accounts = wstore.list_accounts(output_dir)
    # 附每个账号的「已扒文案数 / 视频总数」统计
    for a in accounts:
        vids = wstore.load_videos(output_dir, a.get("id", ""))
        a["extracted_count"] = sum(1 for v in vids if v.get("extracted"))
    return {"ok": True, "accounts": accounts}


def add_account(output_dir: str, platform: str, home_url: str) -> dict:
    """添加账号：抓资料回填 meta（昵称/粉丝/作品数）。"""
    err = _validate_home_url(home_url)
    if err:
        return {"ok": False, "error": err}
    platform = platform or _detect_platform(home_url)
    meta = {}
    try:
        if platform == "weibo":
            r = mfetch.fetch_user_videos_weibo(home_url, count=1)
            if r.get("ok"):
                acc = r.get("account") or {}
                meta = {
                    "nickname": acc.get("nickname", ""),
                    "signature": acc.get("signature", ""),
                    "follower_count": acc.get("follower_count", 0),
                    "aweme_count": acc.get("aweme_count", 0),
                    "uid": acc.get("uid", ""),
                }
        else:
            r = mfetch.fetch_user_videos(home_url, count=1)
            if r.get("ok"):
                acc = r.get("account") or {}
                meta = {
                    "nickname": acc.get("nickname", ""),
                    "signature": acc.get("signature", ""),
                    "follower_count": acc.get("follower_count", 0),
                    "aweme_count": acc.get("aweme_count", 0),
                    "sec_user_id": acc.get("sec_user_id", ""),
                    "douyin_id": acc.get("uid", "") or acc.get("douyin_id", ""),
                }
    except Exception as e:
        print(f"[workslib] 抓账号资料失败（仍添加）: {e}")
    acc = wstore.upsert_account(output_dir, platform, home_url, meta)
    return {"ok": True, "account": acc}


def remove_account(output_dir: str, account_id: str) -> dict:
    ok = wstore.remove_account(output_dir, account_id)
    return {"ok": ok, "error": None if ok else "账号不存在"}


# ---------------------------------------------------------------- 抓取 + 提取任务

def start_crawl(output_dir: str, count: int = 50, api_config: dict | None = None) -> dict:
    """启动批量抓取任务：遍历所有账号，抓视频列表并逐视频扒文案。
    count: 每个账号抓取的视频条数（默认 50）。
    """
    with _lock:
        if _state["running"]:
            return {"ok": False, "error": "已有抓取任务进行中，请稍候"}
        accounts = wstore.list_accounts(output_dir)
        if not accounts:
            return {"ok": False, "error": "作品库还没有账号，请先添加账号主页"}
        _state["running"] = True
        _state["task"] = "crawl"
        _state["started_at"] = time.time()
        _state["finished_at"] = None
        _state["last_error"] = None
        _state["result"] = None
        _state["progress"] = {"done": 0, "total": len(accounts), "current": ""}
        # 持久化「进行中」标记：若任务被关软件/进程强杀打断，下次启动据此自动续跑
        _mark_crawl_started(output_dir, accounts)
        t = threading.Thread(
            target=_run_crawl_job,
            args=(output_dir, list(accounts), count, api_config or {}),
            daemon=True,
        )
        t.start()
        return {"ok": True, "total_accounts": len(accounts)}


def _run_crawl_job(output_dir: str, accounts: list, count: int, api_config: dict):
    total_done = 0
    total_videos = 0
    total_extracted = 0
    errors = []
    # 预取 cookie，供逐视频抓详情复用（避免每条视频都读浏览器，慢且易被风控）
    cookie = None
    try:
        from extract_server import _get_cookie
        cookie = _get_cookie()
    except Exception:
        cookie = None
    try:
        for acc in accounts:
            aid = acc.get("id", "")
            home_url = acc.get("home_url", "")
            acc_name = acc.get("nickname") or home_url
            platform = acc.get("platform", "douyin")
            _set_progress("crawl", total_done, len(accounts), f"正在抓取账号「{acc_name}」的视频列表…")

            # 1) 抓视频列表（抖音/微博分支）
            if platform == "weibo":
                r = mfetch.fetch_user_videos_weibo(home_url, count=count)
            else:
                r = mfetch.fetch_user_videos(home_url, count=count)
            if not r.get("ok"):
                errors.append(f"{acc_name}: {r.get('error')}")
                total_done += 1
                _set_progress("crawl", total_done, len(accounts), f"账号「{acc_name}」抓取失败：{r.get('error')}")
                continue

            videos = r.get("videos") or []

            # 排除清单：过滤掉用户已删除（或已走完导入闭环删除）的视频，下次抓取不重复抓回
            excluded_ids = set(wstore.load_excluded(output_dir).get(aid, []))
            if excluded_ids:
                videos = [v for v in videos if str(v.get("aweme_id", "")) not in excluded_ids]

            # 时长过滤：< 60 秒的视频不抓（对话内容太少，扒不出有价值的文案）
            MIN_DURATION_MS = 60000
            short_count = sum(1 for v in videos if (v.get("duration_ms") or 0) < MIN_DURATION_MS)
            if short_count:
                videos = [v for v in videos if (v.get("duration_ms") or 0) >= MIN_DURATION_MS]
                print(f"[workslib] 账号「{acc_name}」过滤掉 {short_count} 条短视频（<60s）")

            total_videos += len(videos)

            # 回填账号资料 + 先落视频列表（未提取态）
            acc_data = r.get("account") or {}
            if acc_data:
                wstore.upsert_account(output_dir, acc.get("platform", "douyin"), home_url, {
                    "nickname": acc_data.get("nickname", ""),
                    "signature": acc_data.get("signature", ""),
                    "follower_count": acc_data.get("follower_count", 0),
                    "aweme_count": acc_data.get("aweme_count", 0),
                })
            for v in videos:
                v = dict(v)
                # 关键：不要用新抓取的列表把「已提取」状态重置成 False。
                # 新列表来自 fetch，不含 extracted/segments/text；若强行 setdefault(False)，
                # upsert 合并会把已有视频的 extracted 覆盖成 False，导致「增量跳过」永远失效，
                # 每次重启都要把整个账号重新扒一遍（违背自动续跑的初衷）。
                # 因此：已成功提取（extracted=True 且有 segments）的视频，保留其提取结果，
                # 只刷新抓取层面的字段（desc/统计/直链等）；仅对全新视频才初始化为未提取态。
                _prev = wstore.get_video(output_dir, aid, str(v.get("aweme_id", "")))
                if _prev and _prev.get("extracted") and (_prev.get("segments") or []):
                    # 保留提取结果，仅更新抓取字段（含可能补上的 video_url 直链）
                    for k in ("extracted", "extract_time", "text", "segments", "visitor_profile", "error"):
                        if k in _prev:
                            v[k] = _prev[k]
                else:
                    v.setdefault("extracted", False)
                    v.setdefault("error", "")
                wstore.upsert_video(output_dir, aid, v)

            # 2) 逐视频扒文案（区分发言人）——增量：已扒成功（extracted=True 且有 segments）的跳过
            for i, v in enumerate(videos):
                aweme_id = str(v.get("aweme_id", ""))
                desc = v.get("desc", "")
                # current 携带「账号名 + 视频序号 + 标题」，供前端进度条显示「目前在做什么」
                _set_progress(
                    "extract", i, len(videos),
                    f"{acc.get('nickname') or home_url} · 第{i + 1}/{len(videos)}条 · {desc[:20] or aweme_id}",
                )
                # 增量：之前已扒出对话的（extracted 且有 segments）直接跳过，不重复提取
                _existing = wstore.get_video(output_dir, aid, aweme_id)
                if _existing and _existing.get("extracted") and (_existing.get("segments") or []):
                    total_extracted += 1
                    if i < len(videos) - 1:
                        time.sleep(random.uniform(0.15, 0.3))
                    continue
                try:
                    if v.get("_platform") == "weibo":
                        text, segments, vp, err_reason = _extract_one_video_weibo(v, api_config, output_dir)
                    else:
                        text, segments, vp, err_reason = _extract_one_video(v, api_config, cookie, output_dir)
                    if text and segments:
                        wstore.mark_extracted(output_dir, aid, aweme_id, text, segments, vp)
                        total_extracted += 1
                    else:
                        wstore.mark_video_error(output_dir, aid, aweme_id, err_reason or "未提取到可用文案")
                except Exception as e:
                    wstore.mark_video_error(output_dir, aid, aweme_id, str(e))
                # 逐视频抓详情，加轻微限速降低风控
                if i < len(videos) - 1:
                    time.sleep(random.uniform(0.6, 1.2))

            wstore.touch_fetch(output_dir, aid, len(videos))
            total_done += 1
            _set_progress("crawl", total_done, len(accounts), "")
    except Exception as e:
        errors.append(f"任务异常: {e}")
    finally:
        # 任务正常结束（成功/失败）→ 清除「进行中」标记，下次启动不误判续跑
        _clear_crawl_mark(output_dir)
        with _lock:
            _state["running"] = False
            _state["finished_at"] = time.time()
            _state["progress"]["done"] = _state["progress"]["total"]
            _state["last_error"] = errors[0] if errors else None
            _state["result"] = {
                "accounts": total_done,
                "videos": total_videos,
                "extracted": total_extracted,
                "errors": errors,
            }


def _extract_one_video(video: dict, api_config: dict, cookie: str | None = None, output_dir: str = ""):
    """从单个视频条目提取文案 + 区分发言人 + 经历画像。

    提取优先级（精度优先：desc 只是标题，要扒出多段对话必须拿完整字幕/ASR）：
    1) 抓详情拿字幕（带句边界 cues，对话最完整）→ 失败或字幕太短
    2) 用 video_url 直接 ASR 转录完整口播（绕开详情 403，仍能拿全文）
    3) 都拿不到才退回 desc 当标题兜底

    区分发言人自适应分级：
    - 先跑文本路线（LLM 两轮标注）
    - 结果不合格（全A 或 <3 段）→ 有音频就用音频辅助路线（ffmpeg 静音切分说话轮次）
    - 没有音频但文本路线不合格 → 按需下载音频再跑音频辅助路线

    返回 (text, segments, visitor_profile, error_reason)。
    error_reason 为空串表示成功；非空串说明失败原因（供调用方写 error 字段）。
    """
    import extract_server as es
    from extract_server import (_detect_speakers, _extract_visitor_profile,
                                 _extract_text_from_detail, _needs_audio_retry)

    aweme_id = str(video.get("aweme_id") or "").strip()
    if not aweme_id:
        return "", [], None, "无视频ID"

    text = ""
    cues = []
    raw = None
    desc = (video.get("desc") or "").strip()
    wav_path = ""
    fail_reason = ""

    # 1) 优先抓详情字幕（详情接口偶发 403，失败不阻塞，继续走 ASR）
    if es.F2_AVAILABLE:
        try:
            cookie = cookie or es._get_cookie()
            if cookie:
                kwargs = es._build_kwargs(cookie)
                raw = asyncio.run(es._fetch_detail(kwargs, aweme_id))
                try:
                    f = es.PostDetailFilter(raw)
                except Exception:
                    f = raw
                detail_text, _cues = _extract_text_from_detail(raw, f)
                if detail_text and len(detail_text.strip()) >= 5:
                    text = detail_text.strip()
                    cues = _cues or []
            else:
                fail_reason = "无 cookie，无法抓详情字幕"
        except Exception as e:
            fail_reason = f"详情抓取失败: {e}"
            print(f"[workslib] 视频 {aweme_id} 详情抓取失败，转 ASR: {e}")
    else:
        fail_reason = "F2 抓取模块未就绪"

    # 2) 字幕没拿到（或过短）→ ASR 转录完整口播（keep_wav=True 保留音频供辅助区分）
    if len(text) < 5:
        text, wav_path = _extract_one_video_asr(video, raw, output_dir, keep_wav=True)
        if len(text) < 5:
            # ASR 也失败了，记录具体原因
            has_vurl = bool((video.get("video_url") or "").strip())
            has_vurls = bool([u for u in (video.get("video_urls") or []) if isinstance(u, str) and u.startswith("http")])
            if not has_vurl and not has_vurls and not raw:
                fail_reason = "无字幕、无视频直链，无法 ASR"
            else:
                # 有直链但 ASR 失败：可能是 ASR Key 未配或转写超时
                import asr_server
                has_key = bool(asr_server._load_key(output_dir)) if output_dir else False
                if not has_key:
                    fail_reason = "无字幕、ASR Key 未配置"
                else:
                    fail_reason = "无字幕、ASR 转写失败或超时"

    # 3) 兜底：desc 当标题（至少保留一条）
    if len(text) < 5 and len(desc) >= 5:
        text = desc
        fail_reason = "仅标题（字幕/ASR 均失败）"

    if len(text) < 5:
        if wav_path:
            try: os.remove(wav_path)
            except OSError: pass
        return "", [], None, fail_reason or "未提取到文案"

    # 区分发言人（自适应分级：文本路线不合格时自动降级到音频辅助路线）
    if cues:
        segments = _detect_speakers(text, api_config, cues=cues, audio_path=wav_path)
    else:
        segments = _detect_speakers(text, api_config, audio_path=wav_path)

    # 文本路线不合格（全A 或 <3 段）且当前没有音频 → 按需下载音频做音频辅助重试
    # 直接调 _detect_speakers_audio 避免重跑文本路线 LLM（省两轮 LLM 调用）
    if _needs_audio_retry(segments) and not wav_path:
        print(f"[workslib] 视频 {aweme_id} 文本路线不合格（段数={len(segments)}），按需下载音频做辅助区分…")
        wav_path = _download_audio_only(video, raw, output_dir)
        if wav_path:
            from extract_server import _detect_speakers_audio
            segments_audio = _detect_speakers_audio(text, api_config, cues, wav_path)
            if segments_audio and len(segments_audio) >= len(segments):
                segments = segments_audio

    # 清理临时音频文件
    if wav_path:
        try: os.remove(wav_path)
        except OSError: pass

    if not segments:
        return "", [], None, "区分发言人失败"
    if _needs_audio_retry(segments):
        # 即使音频辅助也没能区分，记录但不阻断（至少有文本可用）
        print(f"[workslib] 视频 {aweme_id} 最终仍为单一发言人或段数过少（段数={len(segments)}）")

    vp = None
    if api_config and api_config.get("api_key") and len(segments) > 1:
        vp = _extract_visitor_profile(segments, api_config)
    return text, segments, vp, ""


def _download_audio_only(video: dict, raw: dict | None, output_dir: str) -> str:
    """仅下载视频并抽音轨，不做 ASR 转写。供音频辅助区分发言人使用。

    当字幕/ASR 文本路线产出的 segments 不合格（全A 或 <3 段）时，
    用这个函数按需下载音频给 _detect_speakers 做音频辅助重试。
    返回 wav 路径（调用方负责清理），失败返回 ""。
    """
    import asr_server

    aweme_id = str(video.get("aweme_id") or "").strip()
    urls = []
    vurl = (video.get("video_url") or "").strip()
    if vurl.startswith("http"):
        urls.append(vurl)
    for u in (video.get("video_urls") or []) or []:
        if isinstance(u, str) and u.startswith("http") and u not in urls:
            urls.append(u)
    if raw:
        try:
            for u in asr_server._pick_video_urls(raw):
                if u not in urls:
                    urls.append(u)
        except Exception:
            pass
    if not urls:
        return ""
    video_path, size, dl_err = asr_server._download_video(output_dir or ".", urls, aweme_id)
    if dl_err:
        return ""
    wav_path, ex_err = asr_server._extract_audio(video_path)
    try:
        os.remove(video_path)
    except OSError:
        pass
    if ex_err:
        return ""
    return wav_path


def _extract_one_video_asr(video: dict, raw: dict | None, output_dir: str,
                           keep_wav: bool = False) -> tuple:
    """ASR 兜底：下载视频音轨 → 硅基流动转写 → 返回 (text, wav_path)。

    复用 asr_server 的下载/抽音轨/转写链路。视频直链优先级：
    1. video["video_url"]——作品列表接口直接返回的可下载地址（music.play_url /
       video.play_addr），最可靠，不依赖二次抓详情；
    2. raw 详情里的 play_addr/music.play_url（_pick_video_urls）。
    两条都拿不到才放弃。

    keep_wav=True 时保留 wav 文件并返回路径（供音频辅助区分发言人使用），
    调用方负责后续清理。
    """
    import asr_server

    aweme_id = str(video.get("aweme_id") or "").strip()

    # ① 优先用列表接口自带的 video_url / video_urls（免二次抓详情，规避详情接口风控）
    urls = []
    vurl = (video.get("video_url") or "").strip()
    if vurl.startswith("http"):
        urls.append(vurl)
    for u in (video.get("video_urls") or []) or []:
        if isinstance(u, str) and u.startswith("http") and u not in urls:
            urls.append(u)

    # ② raw 详情兜底（_pick_video_urls 按音轨优先排序）
    if raw:
        try:
            for u in asr_server._pick_video_urls(raw):
                if u not in urls:
                    urls.append(u)
        except Exception:
            pass
    if not urls:
        return "", ""

    # 未配置 ASR Key 时静默跳过（不做无谓下载）
    if output_dir:
        api_key = asr_server._load_key(output_dir)
        if not api_key:
            return "", ""
    else:
        api_key = ""
    # 下载（优先独立音轨 music.play_url，最省流量且必含人声）
    video_path, size, dl_err = asr_server._download_video(output_dir or ".", urls, aweme_id)
    if dl_err:
        print(f"[workslib] ASR 下载失败 {aweme_id}: {dl_err}")
        return "", ""
    # 抽音轨
    wav_path, ex_err = asr_server._extract_audio(video_path)
    try:
        os.remove(video_path)
    except OSError:
        pass
    if ex_err:
        print(f"[workslib] ASR 抽音轨失败 {aweme_id}: {ex_err}")
        return "", ""
    # 转写
    key = api_key or (asr_server._load_key(output_dir) if output_dir else "")
    if not key:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        return "", ""
    text, asr_err = asr_server._call_asr(key, wav_path)
    if asr_err or not text or len(text) < 5:
        print(f"[workslib] ASR 转写失败 {aweme_id}: {asr_err or '结果为空'}")
        try:
            os.remove(wav_path)
        except OSError:
            pass
        return "", ""
    if keep_wav:
        return text, wav_path
    try:
        os.remove(wav_path)
    except OSError:
        pass
    return text, ""


def _extract_one_video_weibo(video: dict, api_config: dict, output_dir: str = ""):
    """微博视频条目提取文案 + 区分发言人 + 经历画像。

    微博主页接口返回的视频列表里已带 video_url（stream_url/mp4），无需二次抓详情。
    逻辑：desc 正文作标题，若正文过短则下载视频抽音轨 ASR 转录口播。
    返回 (text, segments, visitor_profile, error_reason)。
    """
    import asr_server
    from extract_server import (_detect_speakers, _extract_visitor_profile,
                                 _needs_audio_retry)

    mid = str(video.get("aweme_id") or "").strip()
    if not mid:
        return "", [], None, "无视频ID"

    desc = (video.get("desc") or "").strip()
    text = desc
    wav_path = ""
    fail_reason = ""

    # 正文过短 → ASR 兜底转录口播（保留 wav 供音频辅助区分发言人）
    if len(text) < 5:
        video_url = video.get("video_url") or ""
        if not video_url:
            return "", [], None, "无正文、无视频直链"
        api_key = asr_server._load_key(output_dir) if output_dir else ""
        if not api_key:
            return "", [], None, "无正文、ASR Key 未配置"
        video_path, size, dl_err = asr_server._download_video(output_dir or ".", [video_url], mid)
        if dl_err:
            print(f"[workslib] 微博 ASR 下载失败 {mid}: {dl_err}")
            return "", [], None, f"ASR 下载失败: {dl_err}"
        wav_path, ex_err = asr_server._extract_audio(video_path)
        try:
            os.remove(video_path)
        except OSError:
            pass
        if ex_err:
            print(f"[workslib] 微博 ASR 抽音轨失败 {mid}: {ex_err}")
            return "", [], None, f"ASR 抽音轨失败: {ex_err}"
        text, asr_err = asr_server._call_asr(api_key, wav_path)
        if asr_err or not text or len(text) < 5:
            print(f"[workslib] 微博 ASR 转写失败 {mid}: {asr_err or '结果为空'}")
            try: os.remove(wav_path)
            except OSError: pass
            return "", [], None, f"ASR 转写失败: {asr_err or '结果为空'}"

    if len(text) < 5:
        if wav_path:
            try: os.remove(wav_path)
            except OSError: pass
        return "", [], None, fail_reason or "未提取到文案"

    segments = _detect_speakers(text, api_config, audio_path=wav_path)

    # 文本路线不合格且无音频 → 按需下载音频重试（直接调音频路线，省 LLM）
    if _needs_audio_retry(segments) and not wav_path:
        wav_path = _download_audio_only(video, None, output_dir)
        if wav_path:
            from extract_server import _detect_speakers_audio
            segments_audio = _detect_speakers_audio(text, api_config, None, wav_path)
            if segments_audio and len(segments_audio) >= len(segments):
                segments = segments_audio

    if wav_path:
        try: os.remove(wav_path)
        except OSError: pass

    if not segments:
        return "", [], None, "区分发言人失败"

    vp = None
    if api_config and api_config.get("api_key") and len(segments) > 1:
        vp = _extract_visitor_profile(segments, api_config)
    return text, segments, vp, ""


# ---------------------------------------------------------------- 读取

def get_videos(output_dir: str, account_id: str) -> dict:
    acc = wstore.get_account(output_dir, account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    videos = wstore.load_videos(output_dir, account_id)
    return {"ok": True, "account": acc, "videos": videos}


def get_video_detail(output_dir: str, account_id: str, aweme_id: str) -> dict:
    v = wstore.get_video(output_dir, account_id, aweme_id)
    if not v:
        return {"ok": False, "error": "作品不存在"}
    return {"ok": True, "video": v}


def delete_video(output_dir: str, account_id: str, aweme_id: str, exclude: bool = False) -> dict:
    ok = wstore.remove_video(output_dir, account_id, aweme_id)
    if ok and exclude:
        # 记入排除清单，下次「抓取全部账号」不再抓回这条
        wstore.add_excluded(output_dir, account_id, aweme_id)
    return {"ok": ok, "error": None if ok else "作品不存在"}


def delete_short_videos(output_dir: str, account_id: str = "", min_duration_ms: int = 60000) -> dict:
    """批量删除时长不足指定值的视频（默认 < 60 秒）。

    account_id 为空时扫描所有账号。返回删除数 + 各账号明细。
    同时把被删的视频记入排除清单，下次抓取不再抓回。
    """
    accounts = wstore.load_accounts(output_dir)
    if account_id:
        accounts = [a for a in accounts if a.get("id") == account_id]
    if not accounts:
        return {"ok": False, "error": "没有账号"}

    total_deleted = 0
    details = []
    for acc in accounts:
        aid = acc.get("id", "")
        videos = wstore.get_videos(output_dir, aid)
        for v in videos:
            dur = v.get("duration_ms") or 0
            if dur < min_duration_ms:
                aweme_id = str(v.get("aweme_id", ""))
                wstore.remove_video(output_dir, aid, aweme_id)
                wstore.add_excluded(output_dir, aid, aweme_id)
                total_deleted += 1
        # 更新账号抓取计数
        remaining = [v for v in videos if (v.get("duration_ms") or 0) >= min_duration_ms]
        wstore.touch_fetch(output_dir, aid, len(remaining))
        if total_deleted or remaining:
            details.append({"account_id": aid, "nickname": acc.get("nickname", ""),
                            "deleted": sum(1 for v in videos if (v.get("duration_ms") or 0) < min_duration_ms),
                            "remaining": len(remaining)})

    return {"ok": True, "deleted": total_deleted, "details": details,
            "msg": f"已删除 {total_deleted} 条短视频（<60秒）"}


def import_to_extract(output_dir: str, account_id: str, aweme_id: str) -> dict:
    """把作品库某条视频的对话「导入」配音工坊：落成一个 extract 记录，
    返回与 extract_from_link 同构的结果（含 extract_id/segments/visitor_profile），
    前端可直接复用现有「区分发言人 + 创建角色」流程，编辑也会正常落盘。"""
    from extract_server import _dir as _exdir, _write_json, _now, _uid, _save_latest

    v = wstore.get_video(output_dir, account_id, aweme_id)
    if not v:
        return {"ok": False, "error": "作品不存在"}
    segments = v.get("segments") or []
    if not segments:
        return {"ok": False, "error": "该作品还没扒到文案，请先抓取"}

    acc = wstore.get_account(output_dir, account_id) or {}
    text = v.get("text") or "\n".join(s.get("text", "") for s in segments)

    extract_id = _uid(f"workslib_{aweme_id}")
    record = {
        "id": extract_id,
        "time": _now(),
        "share_url": "",
        "aweme_id": aweme_id,
        "source": "workslib",
        "account_id": account_id,
        "video_info": {
            "aweme_id": aweme_id,
            "desc": v.get("desc", "")[:200],
            "nickname": acc.get("nickname", "") or v.get("desc", "")[:20],
            "duration": v.get("duration_ms", 0),
            "digg_count": v.get("digg_count", 0),
            "comment_count": v.get("comment_count", 0),
        },
        "raw_text": text,
        "segments": segments,
        "visitor_profile": v.get("visitor_profile") or {},
        "subtitle_cues": [],
        "audio_path": "",
        "speaker_speed": None,
        "speaker_style": None,
    }
    _write_json(os.path.join(_exdir(output_dir), f"{extract_id}.json"), record)
    _save_latest(output_dir, record)
    return {"ok": True, "extract_id": extract_id, **record}


def reextract_video(output_dir: str, account_id: str, aweme_id: str,
                    api_config: dict | None = None) -> dict:
    """重新扒某条视频的文案（强制，忽略已有 extracted 标记）。

    用于用户觉得某条扒得不对、想重新提取。走与批量抓取相同的提取优先级
    （desc → ASR → 详情 → ASR），结果写回 store。
    """
    v = wstore.get_video(output_dir, account_id, aweme_id)
    if not v:
        return {"ok": False, "error": "作品不存在"}

    # 预取 cookie，供逐视频抓详情复用
    cookie = None
    try:
        from extract_server import _get_cookie
        cookie = _get_cookie()
    except Exception:
        cookie = None

    api_config = api_config or {}
    try:
        if v.get("_platform") == "weibo":
            text, segments, vp, err_reason = _extract_one_video_weibo(v, api_config, output_dir)
        else:
            text, segments, vp, err_reason = _extract_one_video(v, api_config, cookie, output_dir)
        if text and segments:
            wstore.mark_extracted(output_dir, account_id, aweme_id, text, segments, vp)
            return {"ok": True, "segments": segments, "seg_count": len(segments)}
        else:
            reason = err_reason or "未提取到可用文案"
            wstore.mark_video_error(output_dir, account_id, aweme_id, f"重新提取失败：{reason}")
            return {"ok": False, "error": f"重新提取失败：{reason}（可尝试「语音转文字」单独转录）"}
    except Exception as e:
        wstore.mark_video_error(output_dir, account_id, aweme_id, str(e))
        return {"ok": False, "error": f"重新提取失败：{e}"}


def reextract_stale_videos(output_dir: str, account_id: str, api_config: dict | None = None,
                           max_segments: int = 3, only_errors: bool = False) -> dict:
    """批量重扒某账号下「段数过少或失败」的视频。

    - 默认条件：segments 数 < max_segments（默认 3） 或 error 非空
    - only_errors=True：只重扒有 error 的
    - 返回每个视频的 before/after 段数变化
    """
    acc = wstore.get_account(output_dir, account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    videos = wstore.load_videos(output_dir, account_id)
    if not videos:
        return {"ok": False, "error": "该账号下没有视频"}

    # 预取 cookie
    cookie = None
    try:
        from extract_server import _get_cookie
        cookie = _get_cookie()
    except Exception:
        cookie = None

    api_config = api_config or {}
    stale = []
    skipped_short = 0
    for v in videos:
        # 跳过短视频（< 60s），不值得重扒
        dur = v.get("duration_ms") or 0
        if dur and dur < 60000:
            skipped_short += 1
            continue
        segs = v.get("segments") or []
        err = v.get("error")
        if only_errors:
            if err:
                stale.append(v)
        else:
            if err or len(segs) < max_segments:
                stale.append(v)

    if not stale:
        return {"ok": True, "total": len(videos), "re_extracted": 0,
                "skipped": 0, "failed": 0, "details": [],
                "msg": f"无需重扒（共 {len(videos)} 条都已满足条件）"}

    details = []
    re_ok = 0
    re_fail = 0
    for v in stale:
        aweme_id = v.get("aweme_id", "")
        before_segs = len(v.get("segments") or [])
        before_err = v.get("error", "")
        try:
            if v.get("_platform") == "weibo":
                text, segments, vp, err_reason = _extract_one_video_weibo(v, api_config, output_dir)
            else:
                text, segments, vp, err_reason = _extract_one_video(v, api_config, cookie, output_dir)
            after_segs = len(segments or [])
            if text and segments:
                wstore.mark_extracted(output_dir, account_id, aweme_id, text, segments, vp)
                re_ok += 1
                details.append({"aweme_id": aweme_id, "before": before_segs,
                                "after": after_segs, "ok": True})
            else:
                reason = err_reason or "未提取到可用文案"
                wstore.mark_video_error(output_dir, account_id, aweme_id, f"批量重扒：{reason}")
                re_fail += 1
                details.append({"aweme_id": aweme_id, "before": before_segs,
                                "after": 0, "ok": False, "reason": reason})
        except Exception as e:
            wstore.mark_video_error(output_dir, account_id, aweme_id, f"批量重扒失败：{e}")
            re_fail += 1
            details.append({"aweme_id": aweme_id, "before": before_segs,
                            "after": 0, "ok": False, "reason": str(e)})
        # 限流：每条之间稍作停顿，避免 ASR/详情接口被风控
        time.sleep(random.uniform(0.5, 1.5))

    return {"ok": True, "total": len(videos), "stale_count": len(stale),
            "skipped_short": skipped_short,
            "re_extracted": re_ok, "failed": re_fail,
            "details": details}
