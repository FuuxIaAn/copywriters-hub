# -*- coding: utf-8 -*-
"""
抖音短视频链接提取 + 发言人区分
================================
职责：
  1. 解析抖音分享链接/短链接 -> aweme_id（via f2 AwemeIdFetcher）
  2. 获取视频详情（via f2 fetch_post_detail）
  3. 提取文案/字幕：优先从原始 JSON 取 subtitle URL 下载字幕，
     无字幕时回退到 video desc / seo_ocr_content
  4. 用 LLM 自动区分发言人（通常两人对话），支持手动修正
  5. 用 LLM 提取求测者（来访者）的经历画像，支持重新提取/手动编辑
  6. 提取结果落盘 output/extract/，支持增删改查

- WB_EXTRACT_MOCK=1 时返回模拟数据（离线演示/测试）
"""
import asyncio
import hashlib
import json
import os
import re
import html as _html
import urllib.request
import urllib.parse
import sys
import time

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MOCK = os.environ.get("WB_EXTRACT_MOCK", "") == "1" or os.environ.get("WB_MONITOR_MOCK", "") == "1"

# f2 延迟导入
try:
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.filter import PostDetailFilter
    from f2.apps.douyin.model import PostDetail
    from f2.apps.douyin.utils import AwemeIdFetcher
    from f2.utils.utils import get_cookie_from_browser, split_dict_cookie
    F2_AVAILABLE = True
except Exception as _e:
    import traceback
    print("f2 不可用:", _e, flush=True)
    traceback.print_exc()
    F2_AVAILABLE = False

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BROWSER_ORDER = ("chrome", "edge", "firefox")


# ---------------------------------------------------------------- 路径与原子读写

def _dir(output_dir: str, *parts: str) -> str:
    p = os.path.join(output_dir, "extract", *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    # Windows 下目标文件可能被并发读取/杀毒软件瞬时占用，os.replace 会抛
    # PermissionError；重试几次，仍失败则放弃本次写入（不阻塞主流程）。
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    last_err = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(0.15)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last_err


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _uid(seed: str = "") -> str:
    return hashlib.md5(f"{seed}{time.time()}".encode()).hexdigest()[:10]


# ---------------------------------------------------------------- cookie

def _get_cookie() -> str | None:
    if not F2_AVAILABLE:
        return None
    for browser in BROWSER_ORDER:
        try:
            c = get_cookie_from_browser(browser, "douyin.com")
            if c:
                return split_dict_cookie(c)
        except Exception:
            pass
    return None


def _build_kwargs(cookie: str) -> dict:
    return {
        "cookie": cookie,
        "headers": {"User-Agent": DEFAULT_UA, "Referer": "https://www.douyin.com/"},
        "timeout": 15,
        "max_retries": 2,
        "max_connections": 5,
    }


# ---------------------------------------------------------------- 核心提取

_URL_RE = re.compile(r"https?://[\w\-./:@?&=#~+%]+")


def _extract_share_url(text: str) -> str:
    """
    从任意文本中提取第一个 http(s) URL。

    抖音「分享」复制出来是一大段文字，形如：
      "9.41 EHi:/ 03/05 d@A.gO :8pm 财太重了... https://v.douyin.com/xxxxx/ 复制此链接..."
    用户往往整段粘贴。这里提取出链接本身，供 f2 解析 aweme_id。
    若找不到 URL，返回原文本 strip 结果（允许上层按其它规则再判断）。
    """
    s = (text or "").strip()
    if not s:
        return s
    m = _URL_RE.search(s)
    if m:
        return m.group(0).rstrip("，。,.！!？?）)】]")
    return s


def _extract_xhs_from_link(output_dir: str, share_url: str, api_config: dict) -> dict:
    """Extract a public Xiaohongshu note from a single share URL.

    XHS shares commonly contain a long sentence plus a markdown link. The page
    exposes the note title/body and counters in meta/initial-state JSON; this
    branch intentionally stays independent from the Douyin/F2 path.
    """
    raw = _extract_share_url(share_url)
    if not re.search(r"(?:xiaohongshu\.com|xhslink\.com)", raw, re.I):
        return {"ok": False, "error": "不是有效的小红书分享链接"}
    try:
        req = urllib.request.Request(raw, headers={
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(4 * 1024 * 1024).decode("utf-8", "ignore")
            final_url = resp.geturl() or raw
    except Exception as exc:
        return {"ok": False, "error": f"小红书页面读取失败：{exc}。如果笔记仅登录可见，请先在浏览器登录小红书。"}

    def _meta(name: str) -> str:
        m = re.search(r'<meta[^>]+(?:name|property)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']*)', body, re.I)
        return _html.unescape(m.group(1)).strip() if m else ""

    title = _meta("og:title") or _meta("twitter:title")
    desc = _meta("og:description") or _meta("description")
    # XHS embeds initial state with note title/desc and interact_info counters.
    for key in ("title", "desc", "description"):
        if not title and key == "title":
            m = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', body)
            if m:
                try: title = json.loads('"' + m.group(1) + '"')
                except Exception: title = m.group(1)
        if not desc and key in ("desc", "description"):
            m = re.search(r'"(?:desc|description)"\s*:\s*"((?:\\.|[^"\\])*)"', body)
            if m:
                try: desc = json.loads('"' + m.group(1) + '"')
                except Exception: desc = m.group(1)
    text = (desc or title or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text or len(text) < 2:
        return {"ok": False, "error": "小红书页面未公开正文，无法提取文案"}

    def _count(*names):
        for name in names:
            m = re.search(r'"' + re.escape(name) + r'"\s*:\s*(?:"([0-9]+)"|([0-9]+))', body, re.I)
            if m: return int(m.group(1) or m.group(2))
        return 0
    video_info = {
        "source": "xiaohongshu", "desc": title[:200], "nickname": "",
        "digg_count": _count("liked_count", "liked", "like_count"),
        "comment_count": _count("comment_count", "comments"),
        "share_count": _count("share_count", "shared_count"),
        "collect_count": _count("collected_count", "collect_count", "收藏"),
        "share_url": final_url,
    }
    segments = _detect_speakers(text, api_config, [], "")
    visitor_profile = _extract_visitor_profile(segments, api_config)
    extract_id = _uid(final_url[:80])
    record = {
        "id": extract_id, "time": _now(), "share_url": final_url,
        "source": "xiaohongshu", "video_info": video_info,
        "raw_text": text, "segments": segments, "visitor_profile": visitor_profile,
        "subtitle_cues": [], "audio_path": "", "asr_source": "",
        "speaker_speed": None, "speaker_style": None,
    }
    _write_json(os.path.join(_dir(output_dir), f"{extract_id}.json"), record)
    _save_latest(output_dir, record)
    return {"ok": True, "extract_id": extract_id, "text": text,
            "segments": segments, "visitor_profile": visitor_profile,
            "video_info": video_info, "share_url": final_url}


async def _resolve_aweme_id(share_url: str) -> str:
    """分享链接/短链接 -> aweme_id"""
    return await AwemeIdFetcher.get_aweme_id(share_url)


async def _fetch_detail(kwargs: dict, aweme_id: str) -> dict:
    """获取视频详情原始 JSON"""
    async with DouyinCrawler(kwargs) as crawler:
        params = PostDetail(aweme_id=aweme_id)
        resp = await crawler.fetch_post_detail(params)
    return resp


def _extract_subtitle_url(raw: dict) -> str | None:
    """从原始 JSON 中尝试提取字幕 URL"""
    detail = raw.get("aweme_detail") or raw

    def _find_http_url(obj):
        if isinstance(obj, str):
            return obj if obj.startswith("http") else ""
        if isinstance(obj, dict):
            for key in ("url", "Url", "uri", "Uri", "download_url", "play_url", "src", "source_url"):
                v = obj.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in obj.values():
                found = _find_http_url(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = _find_http_url(item)
                if found:
                    return found
        return ""

    def _walk(obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                lk = str(key).lower()
                if any(token in lk for token in ("subtitle", "caption", "transcript")):
                    found = _find_http_url(val)
                    if found:
                        return found
                if isinstance(val, (dict, list)):
                    found = _walk(val)
                    if found:
                        return found
        elif isinstance(obj, list):
            for item in obj:
                found = _walk(item)
                if found:
                    return found
        return None

    return _walk(detail)



def _parse_timestamp(ts: str) -> float:
    """把字幕时间戳（HH:MM:SS,mmm 或 MM:SS,mmm 或 HH:MM:SS.mmm）解析为秒（float）。"""
    ts = (ts or "").strip().replace(",", ".")
    if not ts:
        return 0.0
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(ts)
    except (ValueError, TypeError):
        return 0.0


def _download_subtitle(url: str) -> list:
    """下载字幕文件并解析为句级带时间戳结构 [{start, end, text}]。

    SRT/VTT 都带「start --> end」时间轴，本函数不再丢弃时间轴，
    而是逐条保留，供后续「按目标发言人那句话的音轨时长测语速」使用。
    无时间轴的纯文本字幕则返回 [{start:0, end:0, text}]。
    """
    try:
        import requests
        resp = requests.get(url, timeout=15, headers={"User-Agent": DEFAULT_UA})
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return []

    # 先尝试解析 SRT / VTT 带时间轴的结构
    lines = text.strip().split("\n")
    cues = []          # 最终句级结果 [{start, end, text}]
    cur = {"start": 0.0, "end": 0.0, "text": ""}  # 当前累积的字幕句
    in_header = False

    def _flush():
        nonlocal cur
        t = " ".join(cur["text"].split()).strip()
        if t:
            cues.append({"start": cur["start"], "end": cur["end"], "text": t})
        cur = {"start": 0.0, "end": 0.0, "text": ""}

    time_re = re.compile(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[,.]\d{1,3})")

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("WEBVTT"):
            in_header = True
            continue
        if in_header:
            # WEBVTT 头（Kind/Language 等），遇到时间轴或空行即结束
            if "-->" in s or re.match(r"^\d{2}:\d{2}", s):
                in_header = False
            else:
                continue
        m = time_re.match(s)
        if m:
            _flush()  # 上一句结束
            cur["start"] = _parse_timestamp(m.group(1))
            cur["end"] = _parse_timestamp(m.group(2))
            continue
        if re.match(r"^\d+$", s):
            # SRT 序号行
            continue
        if s.startswith("NOTE"):
            continue
        # 文本行：追加到当前句
        if cur["text"]:
            cur["text"] += " " + s
        else:
            cur["text"] = s
    _flush()

    if cues:
        return cues

    # 无时间轴：纯文本字幕，退回逐行（start/end=0）
    result = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.match(r"^\d+$", s):
            continue
        result.append({"start": 0.0, "end": 0.0, "text": s})
    return result


def _cues_to_text(cues: list) -> str:
    """把带时间戳的字幕句列表拼接成纯文本（供区分发言人等下游使用）。"""
    return "\n".join(c["text"] for c in (cues or []) if c.get("text"))


def _extract_text_from_detail(raw: dict, f):
    """从视频详情中提取文案文本 + 句级时间戳。

    返回 (text, cues)：cues 为 [{start, end, text}]，无字幕时为 []。
    """
    def _normalize(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"\s+", "", s)
        s = re.sub(r"#\S+", "", s)
        return s

    def _looks_like_body_text(s: str) -> bool:
        s = (s or "").strip()
        if len(s) < 6:
            return False
        # 过滤抖音/小红书分享口令与引导语
        if re.search(r"复制此链接|打开Dou音|直接观看视频|打开小红书|复制打开", s):
            return False
        # 过滤纯话题/标签串（如 "#a #b #c"），保留带话题的真实短文案
        no_tags = re.sub(r"#\S+", "", s).strip()
        if not no_tags:
            return False
        # 话题占比过高、几乎没有中文内容的也过滤
        if len(no_tags) < 4 and len(s) > len(no_tags) + 4:
            return False
        return True

    # 1. 尝试字幕 URL
    sub_url = _extract_subtitle_url(raw)
    if sub_url:
        cues = _download_subtitle(sub_url)
        sub_text = _cues_to_text(cues)
        if sub_text and len(sub_text) > 10:
            return sub_text, cues

    # 2. 尝试 desc（视频描述文案）
    desc = ""
    try:
        desc = f.desc or ""
    except Exception:
        pass
    if not desc:
        try:
            desc = (raw.get("aweme_detail") or {}).get("desc") or ""
        except Exception:
            pass

    # 3. 尝试 seo_ocr_content（OCR 文字）
    ocr = ""
    try:
        ocr = f.seo_ocr_content or ""
    except Exception:
        pass

    # 4. 合并候选，但过滤明显只是标题/标签/口令的短文本
    candidates = []
    for part in (desc, ocr):
        part = (part or "").strip()
        if part and _looks_like_body_text(part):
            candidates.append(part)

    if not candidates:
        return "", []

    if len(candidates) == 1:
        return candidates[0], []

    if _normalize(candidates[0]) == _normalize(candidates[1]):
        return candidates[0], []

    return "\n".join(candidates), []


def _is_weak_plain_text(text: str) -> bool:
    """判断文本是否明显只是标题/简介/口令/标签，而不是可用正文。

    抖音短视频的口播文案经常就是一段短描述加话题标签（例如
    "这个日主多出高智商 #癸水 #癸水男 #癸水女"），这种应视为正文，
    不再因为带 # 或长度短而强制走 ASR 降级。
    """
    s = (text or "").strip()
    if not s:
        return True
    # 分享口令/引导语直接判弱
    if re.search(r"复制此链接|打开Dou音|直接观看视频|打开小红书|复制打开", s):
        return True
    # 去掉话题/标签后没有实质内容的判弱
    no_tags = re.sub(r"#\S+", "", s).strip()
    if not no_tags:
        return True
    # 极短（<5 字）仍判弱，留给用户在校对页补全；其余短文案不再因带 # 或长度被误判
    if len(no_tags) < 5:
        return True
    return False


def extract_from_link(output_dir: str, share_url: str, api_config: dict) -> dict:
    """主入口：从抖音分享链接提取文案 + 自动区分发言人。
    api_config: {base_url, api_key, model} 供 LLM 调用
    返回 {ok, extract_id, text, segments, video_info}
    """
    if MOCK:
        return _mock_extract(output_dir, share_url, api_config)

    # Xiaohongshu single-note shares use a separate public-page parser. Keep this
    # branch isolated from the Douyin/F2 crawler so the two workflows do not mix.
    normalized = _extract_share_url(share_url)
    if re.search(r"(?:xiaohongshu\.com|xhslink\.com)", normalized, re.I):
        return _extract_xhs_from_link(output_dir, normalized, api_config)

    if not F2_AVAILABLE:
        return {"ok": False, "error": "抓取组件未安装（f2 库不可用）"}

    share_url = _extract_share_url(share_url)
    if not share_url:
        return {"ok": False, "error": "请输入抖音分享链接"}
    if not re.search(r"douyin\.com", share_url):
        return {"ok": False, "error": "链接格式不对，请粘贴抖音分享链接"}

    cookie = _get_cookie()
    if not cookie:
        return {"ok": False, "error": "未找到可用 cookie（请先在 Chrome/Edge 登录抖音）"}

    kwargs = _build_kwargs(cookie)

    async def _run():
        aweme_id = await _resolve_aweme_id(share_url)
        raw = await _fetch_detail(kwargs, aweme_id)
        return aweme_id, raw

    try:
        aweme_id, raw = asyncio.run(_run())
    except Exception as e:
        return {"ok": False, "error": f"链接解析失败: {e}"}

    try:
        f = PostDetailFilter(raw)
    except Exception as e:
        return {"ok": False, "error": f"视频详情解析失败: {e}"}

    text, cues = _extract_text_from_detail(raw, f)

    video_info = {
        "aweme_id": aweme_id,
        "desc": (f.desc or "")[:200],
        "nickname": f.nickname or "",
        "duration": f.duration or 0,
        "digg_count": 0,
        "comment_count": 0,
        "share_count": 0,
        "collect_count": 0,
    }
    try:
        stats = (raw.get("aweme_detail") or {}).get("statistics") or {}
        video_info["digg_count"] = stats.get("digg_count") or 0
        video_info["comment_count"] = stats.get("comment_count") or 0
        video_info["share_count"] = stats.get("share_count") or 0
        video_info["collect_count"] = stats.get("collect_count") or 0
    except Exception:
        pass

    # 下载视频并落地（供 ASR 降级 + 语速测量 + 音频辅助区分发言人用）。
    # 音轨要保留到语速测完并固化进 record 之后才能删，删除逻辑见 measure_speaker_speed。
    audio_path = ""
    try:
        from asr_server import _pick_video_urls, _download_video
        urls = _pick_video_urls(raw)
        if urls:
            ap, _sz, _err = _download_video(output_dir, urls, aweme_id)
            audio_path = ap or ""
    except Exception as e:
        print(f"[extract] 视频下载失败（不影响文案提取）: {e}")
        audio_path = ""

    # === ASR 降级：无字幕、或当前文本明显只是标题/口令/标签时，
    # 自动转录完整口播音频，拿到完整对话文本后再区分发言人。
    # 这修复了「多段对话被弄成只有一段」以及「标题被当正文」的问题。===
    asr_source = ""
    asr_error = ""
    asr_key_ready = False
    base_text = text
    base_text_weak = _is_weak_plain_text(base_text)
    need_asr = (not cues) or base_text_weak
    if need_asr and audio_path:
        try:
            from asr_server import _extract_audio, _call_asr, _load_key, _llm_correct_text
            asr_key = _load_key(output_dir)
            asr_key_ready = bool(asr_key)
            if asr_key:
                print(f"[extract] 无字幕或弱文本（{len(base_text)}字），触发 ASR 降级转录完整口播…")
                wav_path, ex_err = _extract_audio(audio_path)
                if wav_path and not ex_err:
                    asr_text, asr_err = _call_asr(asr_key, wav_path)
                    if asr_text:
                        asr_text = asr_text.strip()
                        corrected = _llm_correct_text(asr_text, api_config)
                        candidate_text = corrected if (corrected and len(corrected) > 10) else asr_text
                        # 只要原文本明显只是标题/简介/标签，就优先接受 ASR 结果；
                        # 不再强依赖「ASR 必须比标题更长」，否则短标题样本会被误判为失败。
                        if base_text_weak or len(candidate_text) > len(base_text):
                            if candidate_text and len(candidate_text) > 10:
                                text = candidate_text
                                asr_source = "asr"
                                try:
                                    os.remove(audio_path)
                                except OSError:
                                    pass
                                audio_path = wav_path
                            else:
                                asr_error = "ASR 返回文本过短"
                                try:
                                    os.remove(wav_path)
                                except OSError:
                                    pass
                        else:
                            asr_error = asr_err or "ASR 未获得更长文本"
                            print(f"[extract] ASR 未获得更长文本（asr_err={asr_err}），保留原文")
                            try:
                                os.remove(wav_path)
                            except OSError:
                                pass
                    else:
                        asr_error = asr_err or "ASR 返回空文本"
                        print(f"[extract] ASR 返回空文本（asr_err={asr_err}）")
                        try:
                            os.remove(wav_path)
                        except OSError:
                            pass
                else:
                    asr_error = ex_err or "音轨提取失败"
                    print(f"[extract] ASR 抽音轨失败: {ex_err}")
            else:
                asr_error = "未配置 ASR Key"
                print("[extract] 无字幕且文本过短，但 ASR Key 未配置，跳过 ASR 降级")
        except Exception as e:
            asr_error = str(e)
            print(f"[extract] ASR 降级失败（不影响后续流程）: {e}")

    if _is_weak_plain_text(text) and not cues and not asr_source:
        if asr_error:
            if "401" in asr_error or "token is invalid" in asr_error.lower():
                return {"ok": False, "error": f"该视频没有可用字幕，且 ASR Key 无效：{asr_error}"}
            if not asr_key_ready:
                return {"ok": False, "error": "该视频没有可用字幕，且未配置 ASR Key。请先到「设置 → API 集中设置 → 语音转文字（硅基流动）」填写后重试"}
            return {"ok": False, "error": f"该视频没有可用字幕，ASR 转写失败：{asr_error}"}
        return {"ok": False, "error": "只提取到标题/简介，未拿到原稿正文；请补充 ASR Key 后重试"}

    if not text:
        return {"ok": False, "error": "未能从视频中提取到文案（该视频没有字幕和描述，且 ASR 转录失败或未配置）"}

    # LLM 区分发言人（传入 cues 时间戳 + 音轨路径，启用音频辅助降级路线）
    segments = _detect_speakers(text, api_config, cues, audio_path)

    # 句级时间戳对齐到 segments（供逐句测语速）
    segments = _align_timestamps(segments, cues)

    # LLM 提取求测者经历画像
    visitor_profile = _extract_visitor_profile(segments, api_config)

    extract_id = _uid(share_url[:32])
    record = {
        "id": extract_id,
        "time": _now(),
        "share_url": share_url,
        "aweme_id": aweme_id,
        "video_info": video_info,
        "raw_text": text,
        "segments": segments,
        "visitor_profile": visitor_profile,
        "subtitle_cues": cues,
        "audio_path": audio_path,   # 音轨路径；语速测完固化后可删，见 measure_speaker_speed
        "asr_source": asr_source,   # 标记是否走了 ASR 降级（"asr" 或 ""）
        "speaker_speed": None,      # 逐句语速数据（测完后填充）
        "speaker_style": None,      # 口语变化档案（语速随内容/情绪/重音/停顿，测完后填充）
    }
    _write_json(os.path.join(_dir(output_dir), f"{extract_id}.json"), record)

    # 提取完成后自动生成「口语变化档案」（语速随内容 + 情绪 + 重音停顿，实测校准）。
    # 优先测求测者 B（配音主要给来访者做 agent 模拟），失败不阻塞提取主流程。
    _auto_build_style(output_dir, extract_id, record, api_config)

    _save_latest(output_dir, record)
    return {"ok": True, "extract_id": extract_id, "text": text, **record}


def extract_from_text(output_dir: str, text: str, api_config: dict) -> dict:
    """从用户提交的纯文本（.txt 文件内容）直接提取文案 + 自动区分发言人。

    与 extract_from_link 的区别：跳过抖音抓取，直接把文本走「区分发言人 + 经历画像」流程，
    返回同 extract_from_link 一致的结构，前端可无缝复用后续「区分发言人/创建角色」流程。
    api_config: {base_url, api_key, model} 供 LLM 调用
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "文本内容为空，请提交有效的文本文件"}

    # 去掉可能的 BOM
    if text and text[0] == "\ufeff":
        text = text[1:]

    # LLM 区分发言人
    segments = _detect_speakers(text, api_config)

    # LLM 提取求测者经历画像
    visitor_profile = _extract_visitor_profile(segments, api_config)

    extract_id = _uid("textfile")
    record = {
        "id": extract_id,
        "time": _now(),
        "share_url": "",
        "aweme_id": "",
        "video_info": {"nickname": "文本文件", "desc": "本地提交的文本文件"},
        "raw_text": text,
        "segments": segments,
        "visitor_profile": visitor_profile,
        "subtitle_cues": [],
        "audio_path": "",      # 无音轨，语速测量不可用
        "speaker_speed": None,
        "speaker_style": None,
    }
    _write_json(os.path.join(_dir(output_dir), f"{extract_id}.json"), record)
    _save_latest(output_dir, record)
    return {"ok": True, "extract_id": extract_id, "text": text, **record}


def _auto_build_style(output_dir: str, extract_id: str, record: dict, api_config: dict) -> None:
    """提取后自动为求测者（B）构建口语变化档案；无音轨/无时间戳/无 LLM 时静默跳过。

    语速测量依赖音轨真实时长，口语档案依赖语速数据，因此这里串行：
    先 measure_speaker_speed 再 measure_speaker_style。任何一步失败都静默降级，
    不阻塞提取主流程（用户后续可手动触发 /api/extract/style）。
    """
    try:
        segments = record.get("segments") or []
        speakers = sorted({(s.get("speaker", "A") or "A").upper() for s in segments})
        # 求测者 B 优先；若只有单人则测 A
        order = [s for s in ("B", "A") if s in speakers]
        if not order:
            order = ["A"]
        # 只自动测主要发言人（求测者优先），避免提取变慢
        target = order[0]
        if record.get("audio_path") or record.get("subtitle_cues"):
            measure_speaker_style(output_dir, extract_id, target, api_config)
    except Exception as e:  # noqa: BLE001
        print(f"[extract] 自动构建口语档案失败（不阻塞提取）: {e}")


def _align_timestamps(segments: list, cues: list) -> list:
    """把字幕句级时间戳对齐到 segments（按文本内容匹配，字幕句与分段句可能不是一一对应）。

    策略：先按「句文本完全相同」匹配；匹配不到的用模糊包含匹配；
    都匹配不到则该句 start/end=0（该句无可用时间戳，测速时跳过）。
    """
    if not cues:
        return segments
    cue_texts = [(c.get("text") or "").strip() for c in cues]
    for seg in segments:
        t = (seg.get("text") or "").strip()
        seg["start"] = 0.0
        seg["end"] = 0.0
        if not t:
            continue
        # ① 完全相等
        hit = None
        for i, ct in enumerate(cue_texts):
            if ct == t:
                hit = i
                break
        # ② 包含匹配：字幕句里含这段文本，或这段文本含字幕句
        if hit is None:
            for i, ct in enumerate(cue_texts):
                if ct and (t in ct or ct in t):
                    hit = i
                    break
        if hit is not None:
            seg["start"] = cues[hit].get("start", 0.0)
            seg["end"] = cues[hit].get("end", 0.0)
    return segments


# ---------------------------------------------------------------- 逐句语速测量

def _find_ffmpeg_local() -> str:
    """定位 ffmpeg（复用 asr_server 的实现，避免重复）。"""
    try:
        from asr_server import _find_ffmpeg
        return _find_ffmpeg()
    except Exception:
        return ""


def _count_speak_chars(text: str) -> int:
    """统计有效说话字符数（汉字+数字+字母，忽略空白与标点）。"""
    if not text:
        return 0
    return len([c for c in text if c.isalnum() or ("\u4e00" <= c <= "\u9fff")])


def _clip_duration(ffmpeg: str, audio_path: str, start: float, end: float) -> float:
    """用 ffprobe 读取音轨在 [start, end] 片段的实际时长（秒）。

    优先直接用 ffprobe 读整段时长减去静音不现实，这里用 ffmpeg 截取该片段到临时 wav，
    再读其时长，保证「那句话」的真实时长准确（不含前后句、不含留白）。
    失败时用 end-start 兜底。
    """
    dur = max(0.0, end - start)
    if not ffmpeg or not audio_path or not os.path.isfile(audio_path) or dur <= 0:
        return dur
    import tempfile
    import subprocess
    tmp = os.path.join(tempfile.gettempdir(), f"spd_{_uid('clip')}.wav")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", tmp],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 512:
            # 读切片真实时长
            import wave as _wave
            with _wave.open(tmp, "rb") as w:
                fr = w.getframerate() or 0
                nf = w.getnframes() or 0
                if fr > 0:
                    return nf / fr
    except Exception:
        pass
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return dur


def measure_speaker_speed(output_dir: str, extract_id: str, speaker: str) -> dict:
    """按「目标发言人实际说的那句话」在音轨里的真实时长，测量其逐句语速。

    这是本模块的核心：不再笼统测整段上传音频，而是：
    1. 取 record 里该发言人的所有句子（含对齐好的句级时间戳 start/end）；
    2. 对每一句，用 ffmpeg 从音轨切出该句片段，读真实时长；
    3. 字数 ÷ 句时长 = 该句语速（字/秒）；
    4. 汇总得基准值（加权平均）+ 逐句分布 + 快慢变化（识别不同内容/情绪下的语速差异）。

    测完后把结果固化进 record 的 speaker_speed 字段；音轨文件若已无其他用途可删
    （但此处默认保留，删除由上层按「语速已固化」决定，见 cleanup 逻辑）。
    """
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}

    speaker = (speaker or "A").strip().upper()
    segments = record.get("segments") or []
    audio_path = record.get("audio_path") or ""

    # 该发言人的句子，且带有效时间戳
    target = [s for s in segments
              if s.get("speaker", "A").upper() == speaker
              and s.get("start", 0) > 0 and s.get("end", 0) > s.get("start", 0)
              and (s.get("text") or "").strip()]
    if not target:
        return {"ok": False, "error": f"发言人 {speaker} 没有带时间戳的句子，无法测语速"}

    ffmpeg = _find_ffmpeg_local()
    audio_ok = bool(audio_path) and os.path.isfile(audio_path)

    per_sentence = []
    total_chars = 0
    total_dur = 0.0
    for s in target:
        text = (s.get("text") or "").strip()
        chars = _count_speak_chars(text)
        start = s.get("start", 0.0)
        end = s.get("end", 0.0)
        if audio_ok and ffmpeg:
            dur = _clip_duration(ffmpeg, audio_path, start, end)
        else:
            # 无音轨：用字幕时间戳差做近似（不如真实时长准，但能兜底）
            dur = max(0.0, end - start)
        cps = (chars / dur) if dur > 0 else None
        per_sentence.append({
            "text": text[:60],
            "chars": chars,
            "duration": round(dur, 2),
            "chars_per_sec": round(cps, 2) if cps else None,
        })
        if cps:
            total_chars += chars
            total_dur += dur

    if total_dur <= 0 or total_chars <= 0:
        return {"ok": False, "error": "无法从句子中计算出有效语速"}

    # 基准值：加权平均（总字数 ÷ 总时长，比简单平均更贴合实际语速）
    base_cps = total_chars / total_dur
    cps_list = [p["chars_per_sec"] for p in per_sentence if p["chars_per_sec"]]
    cps_list.sort()
    median_cps = cps_list[len(cps_list) // 2] if cps_list else base_cps
    min_cps = cps_list[0] if cps_list else base_cps
    max_cps = cps_list[-1] if cps_list else base_cps

    # 快慢变化：识别该发言人不同句子的语速差异（情绪/内容导致的快慢波动）
    spread = max_cps - min_cps if cps_list else 0.0
    if spread >= 2.0:
        variation = "明显"      # 不同句子语速差异大，可能受内容/情绪影响
    elif spread >= 1.0:
        variation = "中等"
    else:
        variation = "平稳"

    result = {
        "speaker": speaker,
        "base_chars_per_sec": round(base_cps, 2),   # 基准语速（加权平均）
        "median_chars_per_sec": round(median_cps, 2),
        "min_chars_per_sec": round(min_cps, 2),
        "max_chars_per_sec": round(max_cps, 2),
        "spread": round(spread, 2),
        "variation": variation,                    # 明显/中等/平稳
        "sentence_count": len(per_sentence),
        "per_sentence": per_sentence,
        "source": "音轨切片实测" if (audio_ok and ffmpeg) else "字幕时间戳估算",
    }

    # 固化进 record
    spd = record.get("speaker_speed") or {}
    if not isinstance(spd, dict):
        spd = {}
    spd[speaker] = result
    record["speaker_speed"] = spd
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, **result}


def measure_speaker_style(output_dir: str, extract_id: str, speaker: str, api_config: dict) -> dict:
    """目标发言人「口语变化档案」：从原视频实测其语速随内容/话语的变化、
    情绪起伏、重音停顿习惯，供后续 TTS 合成时参照其真实表现（而非 LLM 凭空推断）。

    核心思路（用户要求「一切的一切都要参照真实表现去模拟」）：
    1. 取该发言人的所有句子（含句级时间戳）+ 已固化的 speaker_speed 逐句语速实测数据；
    2. 让 LLM 结合「句子文本 + 真实语速（字/秒）+ 前后语境」，逐句标注：
       - content_type：内容/话语类型（提问/陈述/抒情/强调/犹豫/安慰/下结论…）
       - emotion：该句情绪
       - stress：重音落点（最该重读的词/字）
       - pause：停顿习惯（句内/句间停顿倾向）
    3. 汇总成档案：
       - content_speed_map：不同内容类型下的语速倾向（快/中/慢，附实测 cps）
       - emotion_curve：情绪起伏轨迹（按时间序）
       - stress_pause_habits：重音与停顿习惯总结
       - summary：一段可注入 TTS prompt 的口语风格描述

    产出固化进 record["speaker_style"][speaker]。
    无 LLM 配置或调用失败时，退化为「纯实测统计」模式（只给语速分布，不给情绪/重音）。
    """
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}

    speaker = (speaker or "A").strip().upper()
    segments = record.get("segments") or []
    spd = record.get("speaker_speed") or {}
    spd_this = spd.get(speaker) if isinstance(spd, dict) else None

    # 若语速还没测，先补测（口语档案依赖逐句语速实测数据）
    if not spd_this:
        r = measure_speaker_speed(output_dir, extract_id, speaker)
        if not r.get("ok"):
            return {"ok": False, "error": f"测口语档案前需先测语速：{r.get('error')}"}
        spd_this = r

    per_sentence = spd_this.get("per_sentence") or []
    target = [s for s in segments if (s.get("speaker", "A") or "A").upper() == speaker]
    # 把 per_sentence 的逐句实测 cps 按顺序对齐到 target 文本（per_sentence 与 target 同序）
    sentence_meta = []
    for i, s in enumerate(target):
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        cps = None
        dur = None
        if i < len(per_sentence):
            cps = per_sentence[i].get("chars_per_sec")
            dur = per_sentence[i].get("duration")
        sentence_meta.append({"text": txt, "chars_per_sec": cps, "duration": dur})

    # 汇总实测统计（不依赖 LLM 也能给）
    base_cps = spd_this.get("base_chars_per_sec")
    variation = spd_this.get("variation", "平稳")
    cps_values = [m["chars_per_sec"] for m in sentence_meta if m["chars_per_sec"]]
    stats = {
        "base_chars_per_sec": base_cps,
        "variation": variation,
        "sentence_count": len(sentence_meta),
        "min_cps": round(min(cps_values), 2) if cps_values else None,
        "max_cps": round(max(cps_values), 2) if cps_values else None,
    }

    # 用 LLM 做内容分类 + 情绪 + 重音 + 停顿标注
    llm_out = None
    if api_config and api_config.get("api_key") and sentence_meta:
        llm_out = _llm_style_annotate(sentence_meta, speaker, api_config)

    # 组装档案
    content_speed_map = {}
    emotion_curve = []
    stress_pause_habits = {"stress": "", "pause": ""}
    summary = ""

    if llm_out:
        annotations = llm_out.get("annotations") or []
        content_speed_map = llm_out.get("content_speed_map") or {}
        stress_pause_habits = {
            "stress": llm_out.get("stress_habits", ""),
            "pause": llm_out.get("pause_habits", ""),
        }
        summary = llm_out.get("summary", "")
        # 按时间序构建情绪轨迹（对齐 sentence_meta 顺序）
        for i, m in enumerate(sentence_meta):
            ann = annotations[i] if i < len(annotations) else {}
            emotion_curve.append({
                "text": m["text"][:40],
                "emotion": ann.get("emotion", ""),
                "content_type": ann.get("content_type", ""),
                "stress": ann.get("stress", ""),
                "chars_per_sec": m["chars_per_sec"],
            })
    else:
        # 纯统计退化：情绪曲线只保留语速起伏，不编造情绪
        for m in sentence_meta:
            emotion_curve.append({
                "text": m["text"][:40],
                "emotion": "",
                "content_type": "",
                "stress": "",
                "chars_per_sec": m["chars_per_sec"],
            })
        summary = (
            f"该发言人整体语速 {base_cps} 字/秒，波动{variation}。"
            "（未取得 LLM 分析，情绪/重音/停顿需在 TTS 阶段按内容推断）"
        )

    result = {
        "speaker": speaker,
        "stats": stats,
        "content_speed_map": content_speed_map,
        "emotion_curve": emotion_curve,
        "stress_pause_habits": stress_pause_habits,
        "summary": summary,
        "source": "音轨+LLM 实测校准" if llm_out else "音轨实测（无 LLM）",
    }

    # 固化进 record
    st = record.get("speaker_style") or {}
    if not isinstance(st, dict):
        st = {}
    st[speaker] = result
    record["speaker_style"] = st
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, **result}


def _llm_style_annotate(sentence_meta: list, speaker: str, api_config: dict) -> dict:
    """让 LLM 结合逐句真实语速，标注目标发言人的内容类型/情绪/重音/停顿，
    并汇总其语速随内容、情绪起伏、重音停顿的规律。"""
    lines = []
    for i, m in enumerate(sentence_meta):
        cps = m["chars_per_sec"]
        cps_str = f"{cps} 字/秒" if cps else "未知"
        lines.append(f"{i + 1}. [{cps_str}] {m['text']}")
    numbered = "\n".join(lines)

    prompt = (
        "你是一个口语表达分析专家。下面是一位发言人（" + speaker + "）在视频里说的每一句话，"
        "括号里是**从音轨实测**出的该句真实语速（字/秒）。\n"
        "请你结合每句的**真实语速 + 文本内容 + 前后语境**，分析这位发言人真实的口语变化规律，"
        "供后续语音克隆时参照（不要凭空推断情绪，要以语速快慢和用词为准）。\n\n"
        "要求：只输出一个 JSON 对象，不要任何解释文字，格式如下：\n"
        '{\n'
        '  "annotations": [\n'
        '    {"content_type": "内容/话语类型(如:提问/陈述/抒情/强调/犹豫/安慰/下结论/报信息/客套)", '
        '"emotion": "该句情绪(平静/急切/期待/犹豫/安抚/笃定/惊讶/无奈等，没把握就写平静)", '
        '"stress": "最该重读的词或字(从原句里挑1-2个)", "pause": "停顿倾向(句间停顿长/短/快接/无明显)"},\n'
        '    ... 每句一个对象，顺序与上面一致\n'
        '  ],\n'
        '  "content_speed_map": {"内容类型": "语速倾向描述(如：提问时语速偏快约X字/秒)", ...},\n'
        '  "stress_habits": "这位发言人重音落点习惯的一句话总结",\n'
        '  "pause_habits": "这位发言人停顿习惯的一句话总结",\n'
        '  "summary": "一段 80 字内的口语风格总结，可直接作为 TTS 合成时的风格参考"\n'
        '}\n\n'
        f"句子列表（共 {len(sentence_meta)} 句）：\n{numbered}"
    )

    content = _llm_call(api_config, prompt, max_tokens=4096)
    if not content:
        return None
    m = re.search(r'\{.*\}', content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
        if isinstance(obj, dict) and obj.get("annotations"):
            return obj
    except Exception as e:
        print(f"[extract] 口语档案 LLM 结果解析失败: {e}")
    return None


def cleanup_audio(output_dir: str, extract_id: str) -> dict:
    """语速测完固化后，删除音轨/视频临时文件（释放磁盘）。

    删除前校验：仅当 record.speaker_speed 已填充（语速已固化）才删除；
    否则拒绝，避免「音轨删了但语速还没测」导致后续无法复测。
    """
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    spd = record.get("speaker_speed")
    if not spd:
        return {"ok": False, "error": "语速尚未测完固化，暂不能删除音轨（先测语速）"}
    audio_path = record.get("audio_path") or ""
    removed = []
    errors = []
    if audio_path and os.path.isfile(audio_path):
        try:
            os.remove(audio_path)
            removed.append(audio_path)
        except OSError as e:
            errors.append(str(e))
            print(f"[extract] 删除音轨失败: {e}")
    # 清空 record 里的 audio_path 引用（仅当文件确实删掉）
    if audio_path in removed:
        record["audio_path"] = ""
        _write_json(path, record)
        _save_latest(output_dir, record)
    return {"ok": True, "removed": removed, "errors": errors}


def _presegment_text(text: str) -> list:
    """按自然语句和换行预分割文本，返回短句列表。
    优先保留字幕本身的换行，再按中文/英文句末标点切分；
    对无标点的超长句，再按逗号/顿号/长度进一步切分，避免整段挤成一句。

    注意：阈值要保守——切太碎会导致 LLM 面对一堆碎片无法正确判断说话人，
    最终整段被标成 A（f2 视频通常 1 分钟就有 100+ 碎片，体验崩坏）。
    经验值：有句末标点的句子直接收尾；无标点超长句才按逗号切，标点跟随前句，
    剩余仍 >80 字才按 80 字硬切。
    """
    if not text:
        return []
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 合并连续空白，但保留换行用于先分行
    text = re.sub(r'[ \t]+', ' ', text)
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    result = []
    for line in lines:
        # 按句末标点切分，但把标点保留在前一句
        parts = re.split(r'([。！？；.!?;]+)', line)
        buf = ""
        for p in parts:
            buf += p
            if re.search(r'[。！？；.!?;]+$', p):
                if buf.strip():
                    result.append(buf.strip())
                buf = ""
        if buf.strip():
            result.append(buf.strip())
    # 二次细分：只对无句末标点的超长句（>50字）按逗号/顿号切，再按长度兜底
    # 调高阈值（24→50）后，普通 ASR 长句不再被切碎成 N 个逗号短语
    refined = []
    for s in result:
        if re.search(r'[。！？；.!?;]', s) or len(s) <= 50:
            refined.append(s)
            continue
        # 有逗号/顿号 → 在逗号/顿号处切（标点跟随前句），但每段至少保留 12 字避免碎片
        pieces = re.split(r'([，,、…]+)', s)
        buf = ""
        for p in pieces:
            buf += p
            if re.search(r'[，,、…]+$', p) and len(buf.strip()) >= 12:
                refined.append(buf.strip())
                buf = ""
        if buf.strip():
            # 剩余仍超长则按长度硬切（80 字一截，留足语义空间）
            rest = buf.strip()
            if len(rest) > 80:
                for i in range(0, len(rest), 80):
                    refined.append(rest[i:i + 80])
            else:
                refined.append(rest)
    # 过滤无意义过短碎片
    return [s for s in refined if len(s.strip()) >= 2]


def _llm_call(api_config: dict, prompt: str, max_tokens: int = 4096):
    """统一封装 LLM 调用，返回清洗后的文本内容；失败返回 None"""
    try:
        from openai import OpenAI
        client = OpenAI(base_url=api_config["base_url"], api_key=api_config["api_key"])
        resp = client.chat.completions.create(
            model=api_config.get("model", "deepseek-chat"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[extract] LLM 调用失败: {e}")
        return None


def _speaker_profile_prompt(text: str, cues: list | None = None) -> str:
    """第一轮：让 LLM 从整段对话中总结说话人的特征画像（称呼/口癖/语气/立场）。
    发言人只有两种情况：单人独白（全标A）或 A/B 双人对话。
    如果有 cues（句级时间戳），附加时间信息帮助 LLM 识别说话人切换。
    """
    # 如果有 cues，附带时间戳让 LLM 看到对话的交替节奏
    timed_text = ""
    if cues:
        timed_lines = []
        for c in cues:
            if c.get("text"):
                s = c.get("start", 0)
                e = c.get("end", 0)
                timed_lines.append(f"[{s:.1f}-{e:.1f}s] {c['text']}")
        timed_text = "\n".join(timed_lines)

    body = timed_text if timed_text else text

    return (
        "你是一个对话分析专家。下面是一段抖音短视频的口播/对话文本"
        + (f"，附带每句的时间戳（[开始-结束秒]）" if timed_text else "")
        + "。\n"
        "视频里要么是单人独白（一个人从头说到尾），要么是 A/B 两人对话。\n\n"
        "请先通读整段，判断是单人独白还是 A/B 双人对话，"
        "并总结每个说话人的**语言特征画像**，用于后续逐句判定归属。\n"
        "重点捕捉这些线索：\n"
        "- 各自的称呼/自称（比如有人说「我」「师傅」「你」「宝宝」）\n"
        "- 各自的口癖、语气词、固定句式\n"
        "- 各自的内容立场（提问/倾诉/报生辰/求指点 vs 解答/点评/下结论/给建议）\n"
        "- **对话交替模式**：注意哪些句子是在回应上一句，哪些句子在提问，"
        "这能帮助你区分不同说话人\n"
        + ("- **时间戳线索**：如果两句话之间有明显时间间隔（>1秒），"
           "可能是说话人切换的标志\n" if timed_text else "")
        + "\n"
        "要求：\n"
        "1. 只输出一个 JSON 对象，不要任何解释文字\n"
        "2. 格式：{\"speakers\": {\"A\": \"说话人A的语言特征描述\", \"B\": \"说话人B的语言特征描述\"},\n"
        "   \"count\": 1或2}\n"
        "   - 说话人只有 A 和 B 两种\n"
        "   - 如果明显是单人独白（从头到尾一个人在说，无问答交替），输出 {\"monologue\": true}\n"
        "3. **仔细检查是否有对话交替的迹象**：如果文本中有问答、回应、反驳等交替模式，"
        "即使语气相似也应当判定为 A/B 两人对话\n\n"
        f"文本内容：\n{body}"
    )


def _looks_like_title_only(text: str) -> bool:
    """判断文本是否只是视频标题/简介，没有实质口播正文。

    特征：过短、无句末标点、带话题标签/下划线/纯口号。
    这类文本不应该被当成「成功提取到口播正文」去区分发言人。
    """
    if not text:
        return True
    t = text.strip()
    if len(t) < 30 and not re.search(r"[。！？；.!?;]", t):
        return True
    # 极短且只有话题/下划线/空格/中英文数字
    if len(t) < 50 and (t.count("#") >= 1 or t.count("_") >= 1):
        return True
    return False


def _detect_speakers(text: str, api_config: dict, cues: list | None = None,
                     audio_path: str = "") -> list:
    """自适应分级区分发言人：
    第一轮（轻量文本路线）：LLM 两轮标注（画像 + 逐句）。
    如果结果不合格（全A 或 <3段），且有音频文件 → 自动降级到音频辅助路线
    （ffmpeg 静音切分说话轮次 → 给 LLM 带 hint 重新标注）。

    cues: 字幕句级时间戳 [{start,end,text}]
    audio_path: ASR 保留的 wav 路径，供音频辅助使用
    返回 [{speaker: "A", text: "..."}, ...]。
    """
    if not text:
        return [{"speaker": "A", "text": text}]
    # 极短文本（<5字）直接单段返回，不浪费 LLM 调用
    if len(text) < 5:
        return [{"speaker": "A", "text": text}]
    # 只有标题/简介时，也直接单段返回并标记，避免 UI 显示「✅ 1段对话」误导
    if _looks_like_title_only(text):
        return [{"speaker": "A", "text": text}]

    # ── 第一轮：纯文本 LLM 标注 ──
    segments = _detect_speakers_text(text, api_config, cues)

    # ── 自适应判定：结果是否合格？ ──
    if _needs_audio_retry(segments):
        if audio_path and os.path.isfile(audio_path):
            print(f"[extract] 文本路线不合格（段数={len(segments)}），降级到音频辅助路线")
            segments_audio = _detect_speakers_audio(text, api_config, cues, audio_path)
            if (segments_audio and len(segments_audio) >= len(segments)
                    and (len({x.get("speaker", "A") for x in segments_audio}) > 1
                         or not _looks_like_dialogue([x.get("text", "") for x in segments_audio]))):
                # 音频路线被采纳时同样要合并相邻同发言人，避免同一说话人连续多句被拆成多个气泡
                segments = _merge_consecutive_speakers(segments_audio)
                return segments
            print("[extract] 音频辅助仍为单一发言人，保留文本路线的对话兜底")
        else:
            print(f"[extract] 文本路线不合格（段数={len(segments)}），但无音频文件可降级")

    segments = _merge_consecutive_speakers(segments)
    return segments


def _merge_consecutive_speakers(segments: list) -> list:
    """合并相邻同发言人的段落，避免同一说话人连续多句被拆成多个气泡展示。"""
    if not segments:
        return segments
    merged = []
    for seg in segments:
        sp = str(seg.get("speaker") or "A").strip().upper() or "A"
        txt = str(seg.get("text") or "").strip()
        if not txt:
            continue
        if merged and merged[-1].get("speaker") == sp:
            merged[-1]["text"] = (merged[-1].get("text") or "").rstrip() + "\n" + txt
        else:
            merged.append({"speaker": sp, "text": txt})
    return merged


def _needs_audio_retry(segments: list) -> bool:
    """判定文本路线结果是否不合格，需要降级到音频路线。
    不合格条件：
    - 段数 < 2（至少要有两段才算对话）
    - 全部归同一个说话人（LLM 没能区分）
    注意：2 段不同说话人是合格的最小对话（一问一答），不再判为不合格。
    """
    if not segments or len(segments) < 2:
        return True
    speakers = set(s.get("speaker", "A") for s in segments)
    if len(speakers) <= 1:
        return True  # 全是同一个说话人 → LLM 没能区分
    return False


def _detect_speakers_text(text: str, api_config: dict, cues: list | None = None) -> list:
    """纯文本路线：LLM 两轮标注区分发言人。
    第一轮总结说话人特征画像，第二轮带着画像逐句标注。只有 A/B 两人或单人独白。
    """
    if not text or len(text) < 5:
        return [{"speaker": "A", "text": text}]

    # 有字幕 cues 时，直接用字幕自带的分句（每句一行，边界最准），再叠加兜底细分
    if cues:
        cue_text = _cues_to_text(cues)
        if cue_text and len(cue_text) >= 5:
            sentences = _presegment_text(cue_text)
        else:
            sentences = _presegment_text(text)
    else:
        sentences = _presegment_text(text)

    if not sentences:
        return [{"speaker": "A", "text": text}]

    # 单句文本也交给 LLM 判断（不直接全标 A，让更强模型决定是否独白）
    # if len(sentences) == 1: return [{"speaker": "A", "text": sentences[0]}]

    # 第一轮：总结说话人特征画像（传入 cues 让 LLM 看到时间戳线索）
    profile_text = _llm_call(api_config, _speaker_profile_prompt(text, cues), max_tokens=800)
    profile_hint = ""
    monologue = False
    if profile_text:
        m = re.search(r'\{.*\}', profile_text, re.DOTALL)
        if m:
            try:
                prof = json.loads(m.group())
                if isinstance(prof, dict) and prof.get("monologue"):
                    monologue = True
                else:
                    speakers = prof.get("speakers") or {}
                    if isinstance(speakers, dict):
                        descs = []
                        if speakers.get("A"):
                            descs.append(f"- A：{speakers['A']}")
                        if speakers.get("B"):
                            descs.append(f"- B：{speakers['B']}")
                        if descs:
                            profile_hint = (
                                "说话人特征画像（供参考，请据此判定）：\n"
                                + "\n".join(descs) + "\n"
                            )
            except Exception:
                profile_hint = ""

    # 画像只是提示，不能直接决定全段为 A。旧逻辑在这里提前返回，
    # 会把真实双人对话（尤其双方语气相近的咨询视频）全部吞成单人。
    if monologue:
        profile_hint += "第一轮画像倾向判断为单人独白，但这不是最终结论；请逐句复核问答、回应和称呼变化。\n"

    # 如果有 cues，把时间戳也附到句子编号后，让 LLM 利用时间间隔线索
    numbered = ""
    for i, s in enumerate(sentences):
        time_tag = ""
        if cues:
            # 找到与该句匹配的 cue 的时间
            for c in cues:
                ct = c.get("text", "")
                if s[:6] in ct or ct[:6] in s:
                    time_tag = f" [{c.get('start', 0):.1f}-{c.get('end', 0):.1f}s]"
                    break
        numbered += f"{i + 1}. {time_tag} {s}\n" if time_tag else f"{i + 1}. {s}\n"

    prompt = (
        "你是一个对话分析专家。下面是从抖音短视频字幕中提取的若干短句，已按语句边界预分割。\n"
        "请判断每一句分别是谁说的。\n\n"
        + profile_hint +
        "\n判断线索：\n"
        "- 提问、倾诉烦恼、报信息的一方通常是来访者/提问者\n"
        "- 解答、点评、下结论、给建议的一方通常是解答者/师傅\n"
        "- **对话交替模式**：注意句子之间是否有问答、回应、反驳等交替关系——\n"
        "  如果一句在提问、下一句在回答，它们属于不同说话人\n"
        "  如果一句在说「好的」「嗯」「对」等应答词，通常是回应方说的\n"
        "- **时间戳线索**（如有 [开始-结束秒] 标记）：\n"
        "  - 两句之间如果有 >1 秒的时间间隔，可能是说话人切换\n"
        "  - 但同一说话人也可能有短暂停顿，需结合语义判断\n"
        "- **重要**：不要因为句子太短就全标为同一个说话人。哪怕只有两三句，\n"
        "  只要存在问答/回应/交替模式，就应当区分到不同说话人\n\n"
        "要求：\n"
        "1. 只输出 JSON 数组，不要任何解释文字\n"
        "2. 数组长度必须等于下面的句子数量，每个元素对应一句话\n"
        '3. 每个元素格式：{"speaker": "A"或"B", "text": "原句内容，必须一字不改"}\n'
        "4. speaker 只能是 A 或 B（只有单人独白全标A，或双人对话 A/B 两种情况）\n"
        "5. 保持每句话的原文内容不变，只做发言人标记\n"
        "6. 如果明显是单人独白（从头到尾只有一个人在说，无问答交替），全部标为 A\n"
        "7. **优先保多段对话**：宁可多分几段也不要把多段对话合并成一段，\n"
        "  每个自然语义单元（一个完整的提问或一个完整的回答）都应单独成段\n\n"
        f"句子列表（共 {len(sentences)} 句）：\n{numbered}"
    )

    content = _llm_call(api_config, prompt, max_tokens=4096)
    if content:
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            try:
                segments = json.loads(match.group())
                valid = _normalize_labeled_segments(segments, sentences)
                if valid:
                    # LLM 偶尔会把整段双人问答全部贴成 A；有明确问答信号时
                    # 进行保守修复，避免无音轨文本路线永久丢失 B。
                    if len({x["speaker"] for x in valid}) == 1 and _looks_like_dialogue(sentences):
                        return [{"speaker": "A" if i % 2 == 0 else "B", "text": s}
                                for i, s in enumerate(sentences)]
                    return valid
            except Exception as e:
                print(f"[extract] 发言人标注结果解析失败: {e}")
    # LLM 调用失败时保留源句，不凭空把独白交替成双人。
    # 有明确问答标点/称呼/人称变化时才采用交替兜底，否则默认独白 A。
    dialogue_hint = _looks_like_dialogue(sentences)
    print(f"[extract] 文本路线 LLM 调用失败，使用{'交替' if dialogue_hint else '单人'} fallback")
    return [{"speaker": "A" if not dialogue_hint or i % 2 == 0 else "B", "text": s}
            for i, s in enumerate(sentences)]


def _normalize_labeled_segments(raw_segments: list, source_sentences: list) -> list:
    """把 LLM 标签严格投影回源句，禁止 LLM 合并、删改或重排文本。

    旧实现直接信任 LLM 返回的 text，导致多个源句被一个返回段吞掉。
    这里按顺序用规范化文本做包含匹配；一个返回段覆盖多个源句时，
    同一 speaker 标签会复制到这些源句，但源句数量和原文始终不变。
    """
    if not isinstance(raw_segments, list) or not source_sentences:
        return []

    def norm(value: str) -> str:
        return re.sub(r"[\s，。！？；：、“”‘’（）()\[\]{}<>…,.!?;:'\"-]+", "", str(value or "")).lower()

    result = []
    src_i = 0
    for item in raw_segments:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        speaker = str(item.get("speaker", "A")).strip().upper()
        if speaker not in ("A", "B"):
            speaker = "A"
        returned = norm(item.get("text"))
        if not returned:
            continue
        matched = []
        probe = ""
        j = src_i
        while j < len(source_sentences):
            candidate = norm(source_sentences[j])
            if not candidate:
                j += 1
                continue
            matched.append(j)
            probe += candidate
            # `probe in returned` 不能单独作为结束条件：返回段通常包含多个
            # 源句，第一句天然是它的子串，会导致后续句再次被错贴 speaker。
            if returned in probe or probe == returned or len(probe) >= len(returned) * 0.92:
                break
            j += 1
        if not matched:
            continue
        for idx in matched:
            result.append({"speaker": speaker, "text": source_sentences[idx]})
        src_i = matched[-1] + 1
        if src_i >= len(source_sentences):
            break

    # 只有覆盖率足够时才接受 LLM 结果；缺失源句按相邻标签补齐，仍不改原文。
    if not result:
        return []
    labels = [x["speaker"] for x in result]
    while len(result) < len(source_sentences):
        idx = len(result)
        labels_before = labels[-1] if labels else "A"
        result.append({"speaker": labels_before, "text": source_sentences[idx]})
        labels.append(labels_before)
    return result[:len(source_sentences)] if len(result) >= len(source_sentences) * 0.7 else []


def _looks_like_dialogue(sentences: list) -> bool:
    """Detect likely conversation without relying only on question marks.

    Short-video subtitles often omit punctuation, so requiring a literal ``?``
    caused real Q&A to be permanently flattened into speaker A.

    扩展：命理/情感/咨询类口播中，常见「师傅-求测者」对话模式，
    加入更多问诊/求测/回应特征词。
    """
    joined = "".join(sentences or [])
    if len(sentences or []) < 2:
        return False
    has_question = bool(re.search(r"[？?]|(吗|呢|怎么|为什么|多少|哪里|啥时候|能不能|是不是|帮我|请问|看看|算一下|想问问|问一下|想请教|您看|您说)", joined))
    has_response = bool(re.search(r"(是的|不是|因为|好的|嗯|对的|真的吗|然后呢|师傅|老师|您好|我说|你说|我觉得|其实|可以|这样|对|没错|嗯嗯|行|好|好吧)", joined))
    alternating_pronouns = bool(re.search(r"我.{0,18}(你|您)|你.{0,18}(我|他|她)", joined))
    # 命理咨询类常见对话触发词（师傅/求测者/八字/命盘/财运等）
    consultation_terms = bool(re.search(r"(师傅|老师|大师|先生|您好|求测|八字|命理|命盘|财运|姻缘|桃花|合婚|流年|大运|日主|五行|属相|星座|手相|面相|紫微|塔罗|占卜|卦|算命|测算)", joined))
    return (has_question and has_response) or alternating_pronouns or consultation_terms


# ── 音频辅助路线 ──

def _ffmpeg_silence_detect(audio_path: str, noise_db: int = -35,
                           min_dur: float = 0.4) -> list:
    """用 ffmpeg silencedetect 滤镜检测静音段。

    返回 [{start, end}, ...] 静音段列表（秒）。
    静音段之间的非静音段 = 一个"说话块"。
    """
    try:
        import subprocess
        ffmpeg = _find_ffmpeg_exe()
        if not ffmpeg:
            print("[extract] 音频辅助：找不到 ffmpeg，跳过")
            return [], 0.0
        cmd = [
            ffmpeg, "-i", audio_path,
            "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
            "-f", "null", "-"
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        starts, ends = [], []
        for line in (r.stderr or "").split("\n"):
            m = re.search(r"silence_start:\s*([\d.]+)", line)
            if m:
                starts.append(float(m.group(1)))
            m = re.search(r"silence_end:\s*([\d.]+)", line)
            if m:
                ends.append(float(m.group(1)))
        # 获取音频总时长
        dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or "")
        total = 0.0
        if dur_match:
            h, mi, s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
            total = h * 3600 + mi * 60 + s
        # 配对静音段
        silences = []
        for i in range(min(len(starts), len(ends))):
            silences.append({"start": starts[i], "end": ends[i]})
        return silences, total
    except Exception as e:
        print(f"[extract] ffmpeg 静音检测失败: {e}")
        return [], 0.0


def _find_ffmpeg_exe() -> str:
    """定位 ffmpeg 可执行文件（复用 asr_server 的逻辑，避免循环引用）。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    return ""


def _silence_to_talk_blocks(silences: list, total_duration: float) -> list:
    """把静音段转成说话块（静音之间的非静音段 = 说话块）。"""
    blocks = []
    prev_end = 0.0
    for s in silences:
        if s["start"] > prev_end + 0.15:
            blocks.append({"start": prev_end, "end": s["start"]})
        prev_end = s["end"]
    if total_duration > 0 and prev_end < total_duration - 0.15:
        blocks.append({"start": prev_end, "end": total_duration})
    elif total_duration <= 0 and prev_end < 9999:
        # 拿不到总时长时，最后一块到无穷大
        blocks.append({"start": prev_end, "end": 999999.0})
    return blocks


def _align_cues_to_blocks(cues: list, blocks: list) -> list:
    """把字幕 cues 按时间对齐到说话块，返回每条 cue 的块编号（0-based）。"""
    result = []
    for cue in cues:
        mid = (cue.get("start", 0) + cue.get("end", 0)) / 2
        block_idx = 0
        for i, b in enumerate(blocks):
            if b["start"] <= mid <= b["end"]:
                block_idx = i
                break
            elif mid < b["start"]:
                block_idx = max(0, i - 1)
                break
        else:
            block_idx = len(blocks) - 1 if blocks else 0
        result.append(block_idx)
    return result


def _detect_speakers_audio(text: str, api_config: dict, cues: list | None,
                           audio_path: str) -> list:
    """音频辅助路线：ffmpeg 静音切分说话轮次 → 给 LLM 带 hint 重新标注 A/B。

    流程：
    1. ffmpeg silencedetect 检测静音段（>0.4s 视为说话人轮换边界）
    2. 静音段 → 说话块列表
    3. 如果有 cues：按 cues.start/end 对齐到块 → 每条 cue 标块编号
       如果没有 cues：用 _presegment_text 切句 → 按等长时间粗估每句所属块
    4. 构建带音频块编号 hint 的 prompt → LLM 标注 A/B
    """
    if not audio_path or not os.path.isfile(audio_path):
        return []

    # 1) ffmpeg 静音检测
    silences, total_dur = _ffmpeg_silence_detect(audio_path)
    if not silences and total_dur <= 0:
        print("[extract] 音频辅助：未检测到静音段，可能音频过短或无对话间隙")
        return []

    # 2) 静音段 → 说话块
    blocks = _silence_to_talk_blocks(silences, total_dur)
    if len(blocks) < 2:
        print(f"[extract] 音频辅助：只切出 {len(blocks)} 个说话块，不足以区分说话人")
        return []

    print(f"[extract] 音频辅助：检测到 {len(silences)} 个静音段，切出 {len(blocks)} 个说话块")

    # 3) 准备句子列表 + 对齐到块
    if cues:
        cue_text = _cues_to_text(cues)
        sentences = _presegment_text(cue_text) if cue_text else _presegment_text(text)
        # 按 cues 的 start/end 对齐到块
        cue_blocks = _align_cues_to_blocks(cues, blocks)
        # sentences 和 cues 可能不是 1:1（_presegment 可能把多句 cue 合并），
        # 但 cues 句数 >= sentences 句数，取每句对应的第一个 cue 的块编号
        sent_blocks = []
        cue_idx = 0
        for s in sentences:
            # 找到第一个还没分配的 cue
            while cue_idx < len(cue_blocks) and cue_idx < len(cues):
                # 粗略匹配：句子是否是 cue 文本的子串
                cue_txt = cues[cue_idx].get("text", "")
                if s[:4] in cue_txt or cue_txt[:4] in s:
                    sent_blocks.append(cue_blocks[cue_idx])
                    cue_idx += 1
                    break
                cue_idx += 1
            else:
                sent_blocks.append(cue_blocks[-1] if cue_blocks else 0)
        # 兜底：如果没对齐上，按等长分配
        if len(sent_blocks) < len(sentences):
            while len(sent_blocks) < len(sentences):
                sent_blocks.append(sent_blocks[-1] if sent_blocks else 0)
    else:
        # 没有 cues：按等长时间粗估每句所属块
        sentences = _presegment_text(text)
        n = len(sentences)
        sent_blocks = []
        for i in range(n):
            mid = (i + 0.5) / n * (total_dur or 60.0)
            block_idx = 0
            for j, b in enumerate(blocks):
                if b["start"] <= mid <= b["end"]:
                    block_idx = j
                    break
                elif mid < b["start"]:
                    block_idx = max(0, j - 1)
                    break
            else:
                block_idx = len(blocks) - 1
            sent_blocks.append(block_idx)

    if not sentences:
        return []

    # 4) 构建带音频块编号 hint 的 prompt
    # 第一轮画像（复用文本路线的结果，传入 cues）
    profile_text = _llm_call(api_config, _speaker_profile_prompt(text, cues), max_tokens=800)
    profile_hint = ""
    if profile_text:
        m = re.search(r'\{.*\}', profile_text, re.DOTALL)
        if m:
            try:
                prof = json.loads(m.group())
                if isinstance(prof, dict) and not prof.get("monologue"):
                    speakers = prof.get("speakers") or {}
                    if isinstance(speakers, dict):
                        descs = []
                        if speakers.get("A"):
                            descs.append(f"- A：{speakers['A']}")
                        if speakers.get("B"):
                            descs.append(f"- B：{speakers['B']}")
                        if descs:
                            profile_hint = "说话人特征画像：\n" + "\n".join(descs) + "\n"
            except Exception:
                pass

    # 构建 numbered sentences with audio block hint + 时间戳
    numbered = ""
    for i, s in enumerate(sentences):
        blk = sent_blocks[i] if i < len(sent_blocks) else 0
        time_tag = ""
        if cues:
            for c in cues:
                ct = c.get("text", "")
                if s[:6] in ct or ct[:6] in s:
                    time_tag = f" [{c.get('start', 0):.1f}-{c.get('end', 0):.1f}s]"
                    break
        numbered += f"{i + 1}. [音频段{blk + 1}]{time_tag} {s}\n"

    n_blocks = len(blocks)
    prompt = (
        "你是一个对话分析专家。下面是从抖音短视频中提取的若干短句。\n"
        "每句话前面有 [音频段N] 标记，表示这句话属于音频中的第 N 个说话块"
        "（按静音间隙分隔，相邻块编号变化通常意味着说话人切换）。\n"
        f"音频共切出 {n_blocks} 个说话块。\n\n"
        + profile_hint +
        "\n判断规则：\n"
        "- 提问、倾诉烦恼、报信息的一方通常是来访者/提问者\n"
        "- 解答、点评、下结论、给建议的一方通常是解答者/师傅\n"
        "- **音频段编号变化是关键线索**：编号从 1→2 很可能是说话人切换\n"
        "- **时间戳线索**（如有 [开始-结束秒]）：两句间 >1 秒间隔可能是说话人切换\n"
        "- 但也要看文本语义：同一个人可能连续说多个块\n"
        "- **对话交替模式**：注意问答、回应、反驳等交替关系——\n"
        "  问句和答句属于不同说话人，「好的」「嗯」等应答词通常是回应方说的\n"
        "- **重要**：不要因为句子少就全标为同一个说话人，只要存在交替就应区分\n\n"
        "要求：\n"
        "1. 只输出 JSON 数组，不要任何解释文字\n"
        "2. 数组长度必须等于下面的句子数量\n"
        '3. 每个元素格式：{"speaker": "A"或"B", "text": "原句内容，必须一字不改"}\n'
        "4. speaker 只能是 A 或 B（只有单人独白全标A，或双人对话 A/B 两种情况）\n"
        "5. 保持每句话的原文内容不变\n"
        "6. 如果明显是单人独白（无问答交替，音频块编号只是一个人在说话），全部标为 A\n"
        "7. **优先保多段对话**：每个自然语义单元单独成段，不要合并\n\n"
        f"句子列表（共 {len(sentences)} 句）：\n{numbered}"
    )

    content = _llm_call(api_config, prompt, max_tokens=4096)
    if content:
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            try:
                segments = json.loads(match.group())
                valid = _normalize_labeled_segments(segments, sentences)
                if valid:
                    return valid
            except Exception as e:
                print(f"[extract] 音频辅助标注结果解析失败: {e}")

    # 音频路线也失败：交替 fallback
    print("[extract] 音频路线 LLM 调用失败，使用交替 fallback")
    _fallback = []
    for i, s in enumerate(sentences):
        _fallback.append({"speaker": "A" if i % 2 == 0 else "B", "text": s})
    return _fallback


def _extract_visitor_profile(segments: list, api_config: dict) -> dict:
    """用 LLM 从对话中提取求测者（来访者）的经历画像。
    返回 {summary, basics, experiences[], problems[], demands[], emotion}
    识别不到求测者时返回 {"summary": "", "not_found": True}
    """
    dialogue = "\n".join(f"{s.get('speaker', 'A')}：{s.get('text', '')}" for s in (segments or []))
    if not dialogue.strip():
        return {"summary": "", "not_found": True}

    prompt = (
        "你是一个访谈分析专家。下面是一段短视频中提取的两人对话（算命/占卜/咨询类场景）。\n"
        "对话里只有两个人：一方是【求测者/来访者】（主动求助、提问、倾诉烦恼的一方），"
        "另一方是师傅/解答者。\n"
        "请先判断哪位发言人（A 或 B）是求测者，"
        "然后从对话中**穷尽式**地提取求测者的经历画像。\n\n"
        "要求：\n"
        "1. 只输出一个 JSON 对象，不要任何解释文字\n"
        "2. 字段格式：\n"
        '   {"summary": "一句话概括这个人是谁、来问什么", '
        '"basics": "基本背景（性别/年龄/生辰/职业/婚姻等对话中提到的信息，没提到的不编造）", '
        '"experiences": ["经历事件1（按时间先后）", "经历事件2", ...], '
        '"problems": ["当前困扰1", ...], '
        '"demands": ["诉求1（想问什么/想要什么）", ...], '
        '"emotion": "当前情绪状态（如：焦虑/迷茫/急切/半信半疑）", '
        '"visitor_speaker": "求测者是哪个发言人（A 或 B）"}\n'
        "3. experiences（经历时间线）必须穷尽对话里提到的所有事件，特别注意：\n"
        "   - 凡是出现【年份】的事件（如 2024年/25年/前年/2020年），必须一条不漏全部列出，"
        "每条以年份开头，如：\"2024年：他背着她偷偷和别的女生聊了半年\"\n"
        "   - 多个年份就列多条，按时间先后排序\n"
        "   - 对话里提到的感情经历、工作变动、家庭变故等也要列出\n"
        "4. 所有内容必须来自对话原文，不要编造对话中没有的信息\n"
        "5. 如果这段对话里没有求测者（比如是单人独白或纯讲解），"
        '输出 {"not_found": true}\n\n'
        f"对话内容：\n{dialogue}"
    )

    try:
        from openai import OpenAI
        client = OpenAI(base_url=api_config["base_url"], api_key=api_config["api_key"])
        resp = client.chat.completions.create(
            model=api_config.get("model", "deepseek-chat"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1500,
        )
        content = (resp.choices[0].message.content or "").strip()
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            if data.get("not_found"):
                return {"summary": "", "not_found": True}
            profile = {
                "summary": str(data.get("summary", "")).strip(),
                "basics": str(data.get("basics", "")).strip(),
                "experiences": [str(x).strip() for x in (data.get("experiences") or []) if str(x).strip()],
                "problems": [str(x).strip() for x in (data.get("problems") or []) if str(x).strip()],
                "demands": [str(x).strip() for x in (data.get("demands") or []) if str(x).strip()],
                "emotion": str(data.get("emotion", "")).strip(),
            }
            vs = str(data.get("visitor_speaker", "")).strip().upper()
            if vs in ("A", "B"):
                profile["visitor_speaker"] = vs
            if profile["summary"] or profile["experiences"]:
                return profile
    except Exception as e:
        print(f"[extract] 求测者经历提取失败: {e}")

    return {"summary": "", "not_found": True}


def reextract_visitor_profile(output_dir: str, extract_id: str, api_config: dict) -> dict:
    """对已有提取记录重新提取求测者经历"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    profile = _extract_visitor_profile(record.get("segments") or [], api_config)
    record["visitor_profile"] = profile
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "visitor_profile": profile}


def update_visitor_profile(output_dir: str, extract_id: str, profile: dict) -> dict:
    """保存用户手动编辑的求测者经历"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    profile = profile if isinstance(profile, dict) else {}

    def _lines(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [ln.strip() for ln in str(v or "").split("\n") if ln.strip()]

    clean = {
        "summary": str(profile.get("summary", "")).strip(),
        "basics": str(profile.get("basics", "")).strip(),
        "experiences": _lines(profile.get("experiences")),
        "problems": _lines(profile.get("problems")),
        "demands": _lines(profile.get("demands")),
        "emotion": str(profile.get("emotion", "")).strip(),
    }
    vs = str(profile.get("visitor_speaker", "")).strip().upper()
    if vs in ("A", "B"):
        clean["visitor_speaker"] = vs
    if profile.get("not_found"):
        clean["not_found"] = True
    record["visitor_profile"] = clean
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "visitor_profile": clean}


def update_segment(output_dir: str, extract_id: str, seg_idx: int, speaker: str) -> dict:
    """手动修改某段文字的发言人"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    segments = record.get("segments") or []
    if seg_idx < 0 or seg_idx >= len(segments):
        return {"ok": False, "error": "段落索引越界"}
    sp = (speaker or "A").strip().upper()
    if sp not in ("A", "B"):
        sp = "A"
    segments[seg_idx]["speaker"] = sp
    record["segments"] = segments
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments}


def resegment_record(output_dir: str, extract_id: str, api_config: dict) -> dict:
    """按最新分段规则重新识别一条已有提取记录，不重新抓取视频。

    用于修复旧版本把双人对话识别成单人、或把多段合并成一段的历史结果。
    原始文本、字幕时间戳和音轨均复用，只有 segments/profile 被更新。
    """
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    text = (record.get("raw_text") or "").strip()
    if not text:
        return {"ok": False, "error": "记录没有原始文本，无法重新识别"}
    cues = record.get("subtitle_cues") or []
    audio_path = record.get("audio_path") or ""
    segments = _detect_speakers(text, api_config, cues, audio_path)
    # Final integrity guard: a dialogue-looking source must not be persisted as all A
    # merely because both remote classifiers were inconclusive.
    if (len(segments) >= 2 and len({s.get("speaker", "A") for s in segments}) <= 1
            and _looks_like_dialogue([s.get("text", "") for s in segments])):
        segments = [
            {**seg, "speaker": "A" if i % 2 == 0 else "B"}
            for i, seg in enumerate(segments)
        ]
    segments = _align_timestamps(segments, cues)
    if not segments:
        return {"ok": False, "error": "重新识别没有得到有效分段"}
    record["segments"] = segments
    record["visitor_profile"] = _extract_visitor_profile(segments, api_config)
    record["resegment_time"] = _now()
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments,
            "visitor_profile": record.get("visitor_profile") or {},
            "seg_count": len(segments)}


def edit_segment_text(output_dir: str, extract_id: str, seg_idx: int, text: str) -> dict:
    """手动修改某段文字的内容"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    segments = record.get("segments") or []
    if seg_idx < 0 or seg_idx >= len(segments):
        return {"ok": False, "error": "段落索引越界"}
    segments[seg_idx]["text"] = (text or "").strip()
    record["segments"] = segments
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments}


def add_segment(output_dir: str, extract_id: str, after_idx: int, speaker: str, text: str) -> dict:
    """在某段后面插入新段落"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    segments = record.get("segments") or []
    sp = (speaker or "A").strip().upper()
    if sp not in ("A", "B"):
        sp = "A"
    new_seg = {"speaker": sp, "text": (text or "").strip()}
    segments.insert(after_idx + 1, new_seg)
    record["segments"] = segments
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments}


def delete_segment(output_dir: str, extract_id: str, seg_idx: int) -> dict:
    """删除某段"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    segments = record.get("segments") or []
    if seg_idx < 0 or seg_idx >= len(segments):
        return {"ok": False, "error": "段落索引越界"}
    segments.pop(seg_idx)
    record["segments"] = segments
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments}


def merge_segments(output_dir: str, extract_id: str, seg_idx: int) -> dict:
    """将某段与下一段合并"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    segments = record.get("segments") or []
    if seg_idx < 0 or seg_idx >= len(segments) - 1:
        return {"ok": False, "error": "没有下一段可合并"}
    segments[seg_idx]["text"] = segments[seg_idx]["text"] + " " + segments[seg_idx + 1]["text"]
    segments.pop(seg_idx + 1)
    record["segments"] = segments
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments}


def split_segment(output_dir: str, extract_id: str, seg_idx: int, split_pos: int) -> dict:
    """在某段指定位置拆分成两段"""
    path = os.path.join(_dir(output_dir), f"{extract_id}.json")
    record = _read_json(path, None)
    if not record:
        return {"ok": False, "error": "提取记录不存在"}
    segments = record.get("segments") or []
    if seg_idx < 0 or seg_idx >= len(segments):
        return {"ok": False, "error": "段落索引越界"}
    text = segments[seg_idx]["text"]
    if split_pos <= 0 or split_pos >= len(text):
        return {"ok": False, "error": "拆分位置无效"}
    speaker = segments[seg_idx]["speaker"]
    first = text[:split_pos].strip()
    second = text[split_pos:].strip()
    segments[seg_idx]["text"] = first
    segments.insert(seg_idx + 1, {"speaker": speaker, "text": second})
    record["segments"] = segments
    _write_json(path, record)
    _save_latest(output_dir, record)
    return {"ok": True, "segments": segments}


def get_latest(output_dir: str) -> dict:
    """获取最近一次提取结果"""
    latest = _read_json(os.path.join(_dir(output_dir), "latest.json"), None)
    if not latest:
        return {"ok": False, "error": "还没有提取记录"}
    return {"ok": True, **latest}


def _save_latest(output_dir: str, record: dict) -> None:
    # latest.json 只是「最近一次提取」的缓存指针，写入失败不应让整个
    # 提取/洗稿请求 500（Windows 下可能被并发读取瞬时占用）。
    try:
        _write_json(os.path.join(_dir(output_dir), "latest.json"), record)
    except Exception as e:  # noqa: BLE001
        print(f"[extract] latest.json 写入失败（不影响提取结果）: {e}")


# ---------------------------------------------------------------- Mock

_MOCK_DIALOGUE = """师傅你好，我想找您算一卦。
你好，你想问什么？
我最近事业不太顺利，想看看有没有转机。
嗯，你把生辰八字发我看看。
1993年农历五月十二，午时。
你这个人啊，做事太急躁了。命中有个贵人，三十岁以后会转运。
真的吗？那我现在三十二了，是不是快了？
对，就在眼前了。但是你要记住，机会来了要抓住，不能再犹豫了。
好的好的，我记住了。谢谢师傅！
不客气，记住我说的话就行。"""


def _mock_extract(output_dir: str, share_url: str, api_config: dict) -> dict:
    text = _MOCK_DIALOGUE

    # 离线 mock 先构建模拟 cues，传给 _detect_speakers 让文本路线也能利用时间信息
    _mock_lines_pre = [ln.strip() for ln in text.split("\n") if ln.strip()]
    _mock_cues_pre = []
    _cursor_pre = 0.0
    for _ln in _mock_lines_pre:
        _n = _count_speak_chars(_ln)
        _dur = max(0.8, _n / 4.0)
        _mock_cues_pre.append({"start": round(_cursor_pre, 2), "end": round(_cursor_pre + _dur, 2), "text": _ln})
        _cursor_pre += _dur + 0.15
    segments = _detect_speakers(text, api_config, _mock_cues_pre)

    # 离线 mock 也补一份「句级时间戳字幕 + 音轨」，让「逐句语速测量」能端到端跑通：
    # 复用前面已构建的模拟 cues，对齐到 segments，并生成静音 wav 音轨供 _clip_duration 实测切片时长。
    _mock_cues = _mock_cues_pre
    segments = _align_timestamps(segments, _mock_cues)

    # 生成 mock 音轨（静音 wav），总时长覆盖到末句 end
    audio_path = os.path.join(_dir(output_dir, "audio"), "mock_aweme_001.wav")
    try:
        import wave as _wave
        _total = _mock_cues[-1]["end"] + 0.3
        _rate = 16000
        with _wave.open(audio_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_rate)
            w.writeframes(b"\x00\x00" * int(_rate * _total))
    except Exception as e:  # noqa: BLE001
        print(f"[extract] 生成 mock 音轨失败（不影响提取）: {e}")
        audio_path = ""

    visitor_profile = {
        "summary": "一位 1993 年出生的男性来访者，事业不顺，来找师傅算命问转机",
        "basics": "男，1993年农历五月十二午时出生，今年32岁，事业上遇到麻烦",
        "visitor_speaker": "B",
        "experiences": [
            "2022年：开始自己创业做生意，一直不温不火",
            "2024年：公司业务受阻，事业陷入低谷，几乎没有起色",
            "2025年：经朋友介绍第一次来找师傅算命，想知道转机",
            "最近：师傅指出他做事太急躁，命中有个贵人，三十岁以后会转运",
            "得知自己三十二岁、转运就在眼前，心情从焦虑转为期待",
        ],
        "problems": ["事业不顺利，迟迟没有转机", "做事太急躁，容易犹豫错失机会"],
        "demands": ["想知道事业什么时候有转机", "想请师傅指点接下来该怎么做"],
        "emotion": "焦虑中带着期待，半信半疑",
    }
    extract_id = _uid("mock")
    record = {
        "id": extract_id,
        "time": _now(),
        "share_url": share_url or "https://v.douyin.com/mock_link/",
        "aweme_id": "mock_aweme_001",
        "video_info": {
            "aweme_id": "mock_aweme_001",
            "desc": "算命师傅与来访者的对话",
            "nickname": "命理师老张",
            "duration": 45000,
            "digg_count": 89000,
            "comment_count": 3200,
        },
        "raw_text": text,
        "segments": segments,
        "visitor_profile": visitor_profile,
        "subtitle_cues": _mock_cues,
        "audio_path": audio_path,
        "speaker_speed": None,
        "speaker_style": None,
    }
    _write_json(os.path.join(_dir(output_dir), f"{extract_id}.json"), record)
    _auto_build_style(output_dir, extract_id, record, api_config)
    _save_latest(output_dir, record)
    return {"ok": True, "extract_id": extract_id, **record}
