"""语音转文字（ASR）模块 — 配音工坊·链接提取扩展

目的：抖音视频没有字幕时，把完整口播音频自动转成逐字稿，
再走既有的「句级分段 → LLM 区分 A/B 发言人 → 求测者经历提取」链路。
识别结果允许识别错误，前端段落编辑器可逐句改字/换说话人/增删拆并。

- 服务商：硅基流动（siliconflow.cn）FunAudioLLM/SenseVoiceSmall（当前免费）
- 接口：OpenAI 兼容 POST https://api.siliconflow.cn/v1/audio/transcriptions
- 限制：文件 ≤50MB、时长 ≤1 小时 → 抖音口播视频优先用低码率地址（约 20-35MB）
- 认证：硅基流动 API Key（注册+实名后在控制台创建，页面里粘贴保存，
  存本地 output/extract/asr_settings.json）
- mock：WB_ASR_MOCK=1 时返回内置长逐字稿，便于无 Key 演示/测试
"""
import json
import os
import re
import sys
import threading
import time

import httpx

_THIS = os.path.dirname(os.path.abspath(__file__))

# 下载上限：视频只是中转（本地会抽成 wav 再上传），放宽；上传接口限 50MB 约束的是 wav
MAX_FILE_BYTES = 200 * 1024 * 1024
ASR_API = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"

# 命理/占卜领域专用名词热词表：SenseVoice 对低频专有名词常识别错（如同音字混淆），
# 热词可显著提升这些词的识别精准度。逗号分隔传给接口的 hotwords 参数。
METAPHYSICS_HOTWORDS = (
    "八字,命理,大运,流年,财官印,比劫,伤官,正印,偏财,正财,食神,七杀,"
    "财库,婚姻宫,事业宫,命宫,身强,身弱,喜用神,忌神,五行,金木水火土,"
    "天干,地支,子丑寅卯,辰巳午未,申酉戌亥,甲乙丙丁,戊己庚辛,壬癸,"
    "属兔,属龙,属马,贵人,桃花,文昌,华盖,驿马,天乙贵人,太岁,犯太岁,"
    "本命年,冲太岁,值太岁,刑太岁,破太岁,流月,流日,上等命,下等命,"
    "官杀,印绶,比肩,劫财,食伤,伤官见官,财多身弱,财库漏,过手财,"
    "转运,破财,聚财,旺财,财运,事业运,感情运,健康运,婚姻,离婚,复婚,"
    "八字合婚,看相,算命,占卜,求测,测算,紫微斗数,六爻,梅花易数,"
    "贵人星,小人,劫财,克夫,克妻,旺夫,旺妻,晚婚,早婚,闪婚,"
    # —— 高频同音/近音易错词对（SenseVoice 常把专名听成同音常见字）——
    "巳时,四时,癸水,鬼水,壬水,申金,身金,申时,巳火,午火,戌土,亥水,"
    "子水,卯木,寅木,辰土,丑土,未土,酉金,甲木,乙木,丙火,丁火,戊土,己土,"
    "庚金,辛金,壬癸,枭神,枭印,偏印,正官,七杀星,偏官,"
    "食神制杀,伤官佩印,财星,官星,印星,比劫夺财,"
    "日主,日元,日柱,月柱,年柱,时柱,大运流年,交运,换运,"
    "身旺,身弱,从格,从强,从弱,假从,真从,"
    "五行缺金,五行缺木,五行缺水,五行缺火,五行缺土,"
    "婚姻宫,子女宫,兄弟宫,父母宫,田宅宫,疾厄宫,财帛宫,"
    "八字排盘,排盘,起运,十神,藏干,地支藏干,"
    "伤官,食神,正财,偏财,正印,偏印,比肩,劫财,正官,七杀"
)


MOCK = os.environ.get("WB_ASR_MOCK", "") == "1"

# SenseVoice 输出前后的富文本标签（事件/情感 emoji），转写时去掉
_TAG_CHARS = "🎼👏😀😡😔😰🤢😮😭🧧✅❤️"

_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}

# ---------------------------------------------------------------- 后台任务状态（供前端轮询进度条）

_asr_lock = threading.Lock()
_asr_state = {
    "running": False,
    "stage": "",            # 当前阶段键：resolve/download/extract_audio/transcribe/analyze/done
    "stage_text": "",       # 阶段中文描述（给前端直接展示）
    "progress": {"done": 0, "total": 0},  # 下载字节进度（0~100%）
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "result": None,         # 成功后缓存完整 record
}


def _reset_state():
    _asr_state["running"] = True
    _asr_state["stage"] = "resolve"
    _asr_state["stage_text"] = "正在解析视频链接…"
    _asr_state["progress"] = {"done": 0, "total": 0}
    _asr_state["started_at"] = time.time()
    _asr_state["finished_at"] = None
    _asr_state["last_error"] = None
    _asr_state["result"] = None


def _set_stage(stage: str, text: str):
    with _asr_lock:
        _asr_state["stage"] = stage
        _asr_state["stage_text"] = text


def _set_progress(done: int, total: int):
    with _asr_lock:
        _asr_state["progress"] = {"done": done, "total": total}


def _finish_state(error: str | None = None):
    _asr_state["running"] = False
    _asr_state["finished_at"] = time.time()
    _asr_state["last_error"] = error
    if error:
        _asr_state["stage"] = "error"
    else:
        _asr_state["stage"] = "done"
        _asr_state["stage_text"] = "转录完成"
        _asr_state["progress"] = {"done": 1, "total": 1}


def start_transcribe(output_dir: str, share_url: str, api_config: dict) -> dict:
    """启动后台转录任务，立即返回（前端轮询 /api/extract/asr/status）。"""
    with _asr_lock:
        if _asr_state["running"]:
            return {"ok": False, "error": "已有转录任务进行中，请稍候"}
        _reset_state()
    t = threading.Thread(target=_run_transcribe, args=(output_dir, share_url, api_config), daemon=True)
    t.start()
    return {"ok": True}


def get_status() -> dict:
    with _asr_lock:
        return {
            "ok": True,
            "state": {
                "running": _asr_state["running"],
                "stage": _asr_state["stage"],
                "stage_text": _asr_state["stage_text"],
                "progress": dict(_asr_state["progress"]),
                "last_error": _asr_state["last_error"],
                "result": _asr_state["result"],
            },
        }


# ---------------------------------------------------------------- 设置（API Key）

def _settings_path(output_dir: str) -> str:
    p = os.path.join(output_dir, "extract", "asr_settings.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def get_asr_settings(output_dir: str) -> dict:
    try:
        with open(_settings_path(output_dir), encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    key = s.get("api_key", "")
    return {
        "ok": True,
        "has_key": bool(key),
        "key_masked": (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("***" if key else ""),
    }


def save_asr_settings(output_dir: str, api_key: str) -> dict:
    api_key = (api_key or "").strip()
    with open(_settings_path(output_dir), "w", encoding="utf-8") as f:
        json.dump({"api_key": api_key}, f, ensure_ascii=False, indent=2)
    return {"ok": True, "has_key": bool(api_key)}


def _load_key(output_dir: str) -> str:
    try:
        with open(_settings_path(output_dir), encoding="utf-8") as f:
            return (json.load(f).get("api_key") or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 工具

def _clean_asr_text(text: str) -> str:
    """去掉 SenseVoice 的情感/事件 emoji 标签，压缩多余空白。"""
    if not text:
        return ""
    text = text.strip()
    for ch in _TAG_CHARS:
        text = text.replace(ch, "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pick_video_urls(raw: dict) -> list:
    """取可下载地址，按「口播命中率 + 稳定性」排序。

    顺序：
    1. play_addr_lowbr / play_addr — 混流（通常含完整口播，最适合 ASR）
    2. music.play_url — 独立音轨（有时只是配乐，不一定有口播）
    3. bit_rate 多档位升序 — 最低档兜底
    """
    detail = raw.get("aweme_detail") or raw
    video = detail.get("video") or {}
    urls = []

    for key in ("play_addr_lowbr", "play_addr"):
        addr = video.get(key) or {}
        for u in addr.get("url_list") or []:
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)

    music = detail.get("music") or {}
    for u in (music.get("play_url") or {}).get("url_list") or []:
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)

    gears = []
    for br in video.get("bit_rate") or []:
        rate = br.get("bit_rate") or 0
        for u in (br.get("play_addr") or {}).get("url_list") or []:
            if isinstance(u, str) and u.startswith("http"):
                gears.append((rate, u))
    gears.sort(key=lambda x: x[0])
    urls.extend(u for _, u in gears)
    # 去重保序
    seen, uniq = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _resolve_weibo_video(share_url: str) -> tuple:
    """解析微博视频链接，返回 (video_url, title, error)。

    微博视频链接形如 http://t.cn/xxxx（短链）或 weibo.com/tv/show/... 或 video.weibo.com/show?fid=...。
    策略：
    1. 短链先重定向到真实微博页面（抓取最终 URL 和 HTML）
    2. 从 HTML 里提取视频直链：og:video meta > <video src> > 页面 JSON 里的 stream_url/video_src
    3. 微博视频直链通常是 mp4，可被 ffmpeg 直接抽音轨
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Referer": "https://weibo.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
            resp = client.get(share_url)
            final_url = str(resp.url)
            html = resp.text
    except Exception as e:
        return "", "", f"微博链接访问失败: {e}"

    title = ""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', html, re.I)
    if not m:
        m = re.search(r'<title[^>]*>([^<]*)</title>', html, re.I)
    if m:
        title = m.group(1).strip()

    video_url = ""
    # ① og:video meta（微博视频页通常有）
    m = re.search(r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        video_url = m.group(1)
    # ② og:video:url
    if not video_url:
        m = re.search(r'<meta[^>]+property=["\']og:video:url["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m:
            video_url = m.group(1)
    # ③ <video ... src="...">
    if not video_url:
        m = re.search(r'<video[^>]+src=["\']([^"\']+)["\']', html, re.I)
        if m:
            video_url = m.group(1)
    # ④ 页面内嵌 JSON：stream_url / video_src / mp4 地址
    if not video_url:
        for pat in (
            r'["\']stream_url["\']\s*:\s*["\']([^"\']+)["\']',
            r'["\']video_src["\']\s*:\s*["\']([^"\']+)["\']',
            r'["\']mp4["\']\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
            r'(https?://[^"\'\s]+\.mp4[^"\'\s]*)',
        ):
            m = re.search(pat, html, re.I)
            if m:
                video_url = m.group(1)
                break
    # ⑤ video.weibo.com/show?fid= 的情况：拼接视频直链 API 尝试
    if not video_url and "fid=" in final_url:
        fid = re.search(r'fid=(\d+:\d+)', final_url)
        if fid:
            try:
                api = f"https://weibo.com/tv/api/component?page=/tv/show/{fid.group(1)}"
                with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
                    r2 = client.get(api)
                data = r2.json()
                # 递归在 JSON 里找 mp4
                def _walk(o):
                    if isinstance(o, dict):
                        for k, v in o.items():
                            if k in ("url", "stream_url", "video_url", "mp4") and isinstance(v, str) and v.startswith("http"):
                                return v
                            r = _walk(v)
                            if r:
                                return r
                    elif isinstance(o, list):
                        for x in o:
                            r = _walk(x)
                            if r:
                                return r
                    return ""
                video_url = _walk(data)
            except Exception:
                pass

    video_url = (video_url or "").replace("&amp;", "&").replace("\\/", "/")
    if not video_url:
        return "", title, "未能从微博页面提取到视频直链（可能需登录或视频已删除）"
    return video_url, title, ""


def _download_video(output_dir: str, urls: list, aweme_id: str, on_progress=None) -> tuple:
    """流式下载到 output/extract/audio/<aweme_id>.mp4，返回 (path, size, error)。

    on_progress(done, total)：每下载一块回调一次字节进度，total 来自 Content-Length。
    """
    audio_dir = os.path.join(output_dir, "extract", "audio")
    os.makedirs(audio_dir, exist_ok=True)
    path = os.path.join(audio_dir, f"{aweme_id}.mp4")
    last_err = ""
    for url in urls:
        try:
            with httpx.stream("GET", url, headers=_DOWNLOAD_HEADERS, timeout=120, follow_redirects=True) as r:
                if r.status_code != 200:
                    last_err = f"下载失败 HTTP {r.status_code}"
                    continue
                total = 0
                try:
                    total = int(r.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    total = 0
                size = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_bytes(1024 * 256):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            f.close()
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                            if on_progress:
                                on_progress(size, total or size)
                            return "", 0, (
                                f"视频文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限。"
                                "超长视频暂不支持自动转录"
                            )
                        f.write(chunk)
                        if on_progress:
                            on_progress(size, total or size)
            if size == 0:
                last_err = "下载内容为空"
                continue
            return path, size, ""
        except Exception as e:
            last_err = f"下载失败: {e}"
    return "", 0, last_err or "没有可用的视频下载地址"


def _find_ffmpeg() -> str:
    """定位可用的 ffmpeg 可执行文件，按优先级返回路径或空串。

    顺序：① imageio_ffmpeg 自带静态二进制 → ② 系统 PATH 里的 ffmpeg →
    ③ conda 环境里的 ffmpeg。全部找不到返回空串。
    """
    # ① imageio_ffmpeg 自带（开发环境已装；打包后靠 --collect-all imageio_ffmpeg）
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass

    # ② 系统 PATH 里的 ffmpeg（shutil.which 兜底）
    try:
        import shutil
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    except Exception:
        pass

    # ③ conda 环境
    for cand in (
        os.path.join(sys.prefix, "Library", "bin", "ffmpeg.exe"),
        os.path.join(sys.prefix, "bin", "ffmpeg"),
    ):
        if os.path.isfile(cand):
            return cand

    return ""


def _extract_audio(video_path: str) -> tuple:
    """用 ffmpeg 从视频抽音轨，转 16k 单声道 wav。

    转写接口对 mp4 视频容器支持不稳（常 500），wav 稳定且体积适中。
    返回 (wav_path, error)。
    """
    wav_path = os.path.splitext(video_path)[0] + ".wav"
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return "", "缺少 ffmpeg（imageio-ffmpeg 组件未打包，且系统也未安装 ffmpeg），无法抽音轨"
    try:
        import subprocess

        r = subprocess.run(
            [
                ffmpeg, "-y", "-i", video_path,
                "-vn", "-ac", "1", "-ar", "16000",
                # 提升嘈杂视频识别效果：高通滤掉 100Hz 以下低频噪声（底噪/嗡嗡声），
                # 再用 loudnorm 做响度归一化，避免口播忽大忽小导致漏字。
                "-af", "highpass=f=100,loudnorm=I=-16:TP=-1.5:LRA=11",
                wav_path,
            ],
            capture_output=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0 or not os.path.exists(wav_path) or os.path.getsize(wav_path) < 1024:
            err = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
            return "", f"抽音轨失败: {err}"
        return wav_path, ""
    except Exception as e:
        return "", f"抽音轨失败: {e}"


def _call_asr(api_key: str, audio_path: str) -> tuple:
    """调用硅基流动转写接口，返回 (text, error)。"""
    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "video/mp4")}
            # hotwords 提升命理领域专有名词识别精准度（SenseVoice 支持热词）
            data = {"model": ASR_MODEL, "hotwords": METAPHYSICS_HOTWORDS}
            headers = {"Authorization": f"Bearer {api_key}"}
            with httpx.Client(timeout=600) as client:
                resp = client.post(ASR_API, headers=headers, files=files, data=data)
    except Exception as e:
        return "", f"转写请求失败: {e}"
    if resp.status_code != 200:
        try:
            detail = resp.json().get("message") or resp.text[:200]
        except Exception:
            detail = resp.text[:200]
        hint = ""
        if resp.status_code == 401:
            hint = "（API Key 无效或未实名认证）"
        elif resp.status_code == 413:
            hint = "（文件太大，超过 50MB 上限）"
        return "", f"转写服务返回 {resp.status_code}: {detail}{hint}"
    try:
        text = resp.json().get("text", "")
    except Exception:
        return "", "转写服务返回格式异常"
    if not text.strip():
        return "", "转写结果为空（视频可能没有人声）"
    return _clean_asr_text(text), ""


# ---------------------------------------------------------------- LLM 二次纠错

_LLM_CORRECT_PROMPT = (
    "你是中文语音转写稿的校对专家，特别熟悉命理/占卜/八字领域的专业术语。\n"
    "下面这段文字是语音识别（ASR）从抖音口播视频转写出来的原始结果，"
    "可能存在以下问题：\n"
    "1. 同音字/近音字错误（如「巳时」误成「四时」、「癸水」误成「鬼水」、"
    "「申金」误成「身金」、「比劫」误成「鼻姐」、「大运」误成「大云」等）\n"
    "2. 命理专有名词被拆散或写错（八字、流年、财库、婚姻宫、伤官、七杀等）\n"
    "3. 缺少标点、句子粘连、数字/年份被误写\n\n"
    "请你在【不改变原意、不增删内容、不改动说话顺序】的前提下，做最小化校对：\n"
    "- 只纠正明显的同音字/错别字，尤其把命理专有名词改正确\n"
    "- 补全缺失的标点符号，让句子通顺\n"
    "- 不要把口语改写成书面语，不要润色、不要扩写、不要合并或拆分句子\n\n"
    "要求：直接输出校对后的完整文本，不要加任何解释、说明或前后缀。\n\n"
    "原始转写稿：\n"
)


def _llm_correct_text(text: str, api_config: dict) -> str:
    """用 LLM 对 ASR 原始转写稿做二次校对（同音字/专名/补标点）。

    失败时静默回退到原文，绝不阻断转录主流程（纠错是加分项，不是硬依赖）。
    """
    if not text or not api_config or not api_config.get("api_key"):
        return text
    try:
        from openai import OpenAI
        client = OpenAI(base_url=api_config["base_url"], api_key=api_config["api_key"])
        resp = client.chat.completions.create(
            model=api_config.get("model", "deepseek-chat"),
            messages=[{"role": "user", "content": _LLM_CORRECT_PROMPT + text}],
            temperature=0.1,
            max_tokens=8192,
        )
        corrected = (resp.choices[0].message.content or "").strip()
        # 兜底：LLM 返回为空或异常短就丢弃，保留原文
        if corrected and len(corrected) >= len(text) * 0.5:
            return corrected
    except Exception as e:  # noqa: BLE001
        print(f"[asr] LLM 二次纠错失败，回退原文: {e}")
    return text


# ---------------------------------------------------------------- Mock 逐字稿（模拟一段完整口播）

_MOCK_TRANSCRIPT = """师傅在吗在吗，我看到你主页说能看事业是吧。
在的，你想看什么直接说。
我是这样的，我2021年跟前合伙人一起搞了个建材生意，做了两年，2023年行情不好亏了一波，把车都卖了。
嗯，你先别急，你八字发我。
我是1990年腊月生的，具体时辰我妈说是晚上十点多。
好，我看一下。你这个命啊，财是有的，就是聚不住，过手财。
师傅你这么一说我真信，我真存不下钱，赚一点就有事要花出去。
你这是财库的问题，命中财库漏了。
那怎么办啊师傅，你给我想想办法呗。
办法有，但你得先听我把话说完。你2023年那一劫，其实在你2021年动手的时候就埋下了。
哎哟你说的太对了，那时候要不是我非要扩张，也不至于亏那么多。
所以说你不是没本事，你是太急了。三十岁之前你已经在走下坡了，但是你看你现在，气色其实转过来了。
真的假的，我今年都三十五了，还来得及吗？
来得及，但是明年开春这个机会你要是再抓不住，那就真的没了。
那我得注意点什么，你跟我说说。
第一，别再跟人合伙了，你就自己干。第二，明年三月前后有个属兔的，会是你的贵人。
属兔的？我想想啊，我好像真认识一个属兔的，做装修的。
对，就是他。这个机会要从他手上过。
好嘞好嘞，师傅那我再问一句，我感情上怎么样，我2024年刚离的婚。
你这一问我就知道，你婚姻宫也冲了，离婚不怪你，是时机问题。
唉，说实话那段时间真的难，生意垮了，婚也散了。
都过去了。你记住，明年上半年之前别急着谈，先把事做起来，人对了自然就来了。
行，我记住了。师傅那你看我这身体呢，我最近老失眠。
失眠是心里有事。你这个不算病，事顺了自然就好了。
那我放心了。师傅你再帮我看看我妈，她身体最近也不太好。
你妈是哪年的。
1958年的。
你妈这个岁数，今年冬天要注意，尤其是腿脚和血压，开春就没事了。
哎哟，那得让我妈注意点。行，师傅，今天真是问透了，我心里亮堂多了。
记住我跟你说的，明年三月，属兔的，自己干，别合伙。
记住了记住了，谢谢师傅！"""


# ---------------------------------------------------------------- 主入口

def transcribe_video(output_dir: str, share_url: str, api_config: dict) -> dict:
    """同步版转录入口（供 e2e 测试 / 兼容旧调用直接取结果）。"""
    if MOCK:
        return _mock_transcribe(output_dir, share_url, api_config)
    # 复用后台任务逻辑，但同步等待结果返回
    _reset_state()
    try:
        result = _run_transcribe(output_dir, share_url, api_config)
        return result
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _run_transcribe(output_dir: str, share_url: str, api_config: dict):
    """后台任务主体：下载抖音视频 → 硅基流动转写 → 句级分段 + 区分发言人 + 重提经历画像。

    全程更新 _asr_state，成功后把完整 record 存到 _asr_state["result"] 并返回。
    返回与 /api/extract 相同结构的记录。
    """
    # 延迟导入避免循环依赖
    from extract_server import (
        _detect_speakers,
        _extract_share_url,
        _extract_visitor_profile,
        _fetch_detail,
        _get_cookie,
        _build_kwargs,
        _resolve_aweme_id,
        _read_json,
        _write_json,
        _dir,
        _uid,
        _now,
    )
    import asyncio

    if MOCK:
        _set_stage("transcribe", "正在转写（演示模式）…")
        try:
            result = _mock_transcribe(output_dir, share_url, api_config)
            with _asr_lock:
                _asr_state["result"] = result
            _finish_state()
            return result
        except Exception as e:  # noqa: BLE001
            _finish_state(error=str(e))
            return {"ok": False, "error": str(e)}

    def _dl_progress(done, total):
        _set_progress(done, total)

    try:
        share_url = _extract_share_url(share_url)
        if not share_url:
            raise ValueError("请输入视频分享链接")

        api_key = _load_key(output_dir)
        if not api_key:
            raise ValueError("还没配置硅基流动 API Key（语音转文字设置里粘贴保存）")

        is_weibo = bool(re.search(r"(weibo\.com|t\.cn|weibo\.cn)", share_url))
        is_douyin = bool(re.search(r"douyin\.com|iesdouyin\.com", share_url))
        if not is_weibo and not is_douyin:
            raise ValueError("链接格式不对，请粘贴抖音或微博视频分享链接")

        aweme_id = ""
        detail = {}
        video = {}
        stats = {}
        nickname = ""
        desc = ""
        duration = 0
        urls = []

        if is_weibo:
            # ----- 微博视频：短链重定向 → 抓页面 → 提取视频直链 -----
            _set_stage("resolve", "正在解析微博视频链接、获取视频直链…")
            video_url, title, wb_err = _resolve_weibo_video(share_url)
            if wb_err:
                raise ValueError(wb_err)
            aweme_id = _uid("weibo_" + share_url[-20:])
            desc = title
            urls = [video_url]
        else:
            # ----- 抖音视频：f2 解析（需本机浏览器登录过抖音）-----
            _set_stage("resolve", "正在解析抖音链接、获取视频信息…")
            try:
                cookie = _get_cookie()
                if not cookie:
                    raise ValueError("未找到可用 cookie（请先在 Chrome/Edge 登录抖音）")
                kwargs = _build_kwargs(cookie)

                async def _run():
                    aweme_id = await _resolve_aweme_id(share_url)
                    raw = await _fetch_detail(kwargs, aweme_id)
                    return aweme_id, raw

                aweme_id, raw = asyncio.run(_run())
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"链接解析失败: {e}")

            detail = raw.get("aweme_detail") or raw
            video = detail.get("video") or {}
            stats = detail.get("statistics") or {}
            nickname = ((detail.get("author") or {}).get("nickname")) or ""
            desc = (detail.get("desc") or "")[:200]
            duration = video.get("duration") or 0
            urls = _pick_video_urls(raw)

        if not urls:
            raise ValueError("没有拿到视频下载地址（视频可能被删除或限制下载）")

        # 2+3. 逐个地址尝试：下载 → 抽音轨（mp4 → wav）。
        # 有的地址是纯视频流（无音轨），抽不出来就换下一个。
        audio_path = ""
        size = 0
        last_err = ""
        for i, url in enumerate(urls[:8]):
            _set_stage("download", f"正在下载视频（第 {i + 1} 个地址）…")
            video_path, size, dl_err = _download_video(output_dir, [url], aweme_id, on_progress=_dl_progress)
            if dl_err:
                last_err = dl_err
                continue
            _set_stage("extract_audio", "正在从视频中提取音轨…")
            wav_path, ex_err = _extract_audio(video_path)
            try:  # 音轨抽完视频文件就没用了，删掉省磁盘
                os.remove(video_path)
            except OSError:
                pass
            if not ex_err:
                audio_path = wav_path
                break
            last_err = ex_err
        if not audio_path:
            raise ValueError(f"所有下载地址都拿不到音轨（{last_err}）")

        # 4. 转写
        _set_progress(0, 0)  # 下载阶段结束，清进度
        _set_stage("transcribe", "正在上传音频转写（视时长可能需要 1~3 分钟）…")
        text, err = _call_asr(api_key, audio_path)
        if err:
            raise ValueError(err)
        if not text or len(text) < 5:
            raise ValueError("转写结果为空")

        # 4.5. LLM 二次纠错：命理专名 + 同音字 + 补标点（失败自动回退原文，不阻断）
        _set_stage("analyze", "正在用 AI 校对转写稿（纠正同音字、补标点）…")
        raw_text = text
        text = _llm_correct_text(text, api_config)

        # 5. 分段 + 区分发言人 + 重提经历画像
        # ASR 转写没有字幕 cues，但保留 wav 路径供 _detect_speakers 音频辅助路线
        # （ffmpeg 静音切分说话轮次），所以 wav 删除推迟到区分完发言人之后。
        _set_stage("analyze", "正在区分发言人、提取求测者经历…")
        segments = _detect_speakers(text, api_config, cues=None, audio_path=audio_path)
        visitor_profile = _extract_visitor_profile(segments, api_config)

        # 区分发言人完毕，wav 文件已无用（视频文件在抽音轨时已删），清理临时音频
        try:
            os.remove(audio_path)
        except OSError:
            pass

        extract_id = _uid(("asr_" + aweme_id)[:40])
        record = {
            "id": extract_id,
            "extract_id": extract_id,
            "time": _now(),
            "share_url": share_url,
            "aweme_id": aweme_id,
            "source": "asr",
            "audio_size_mb": round(size / 1024 / 1024, 1),
            "video_info": {
                "aweme_id": aweme_id,
                "desc": desc,
                "nickname": nickname,
                "duration": duration,
                "digg_count": stats.get("digg_count") or 0,
                "comment_count": stats.get("comment_count") or 0,
            },
            "raw_text": text,
            "asr_raw_text": raw_text,
            "llm_corrected": bool(raw_text and raw_text != text),
            "segments": segments,
            "visitor_profile": visitor_profile,
        }
        _write_json(os.path.join(_dir(output_dir), f"{extract_id}.json"), record)
        with open(os.path.join(_dir(output_dir), "latest.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        with _asr_lock:
            _asr_state["result"] = record
        _finish_state()
        return {"ok": True, "extract_id": extract_id, "source": "asr", **record}
    except Exception as e:  # noqa: BLE001
        msg = str(e) or "转录失败"
        _finish_state(error=msg)
        return {"ok": False, "error": msg}


def _mock_transcribe(output_dir: str, share_url: str, api_config: dict) -> dict:
    from extract_server import (
        _detect_speakers,
        _extract_visitor_profile,
        _write_json,
        _dir,
        _uid,
        _now,
    )

    text = _MOCK_TRANSCRIPT
    segments = _detect_speakers(text, api_config)
    visitor_profile = _extract_visitor_profile(segments, api_config)
    extract_id = _uid("mock_asr")
    record = {
        "id": extract_id,
        "extract_id": extract_id,
        "time": _now(),
        "share_url": share_url or "https://v.douyin.com/mock_asr/",
        "aweme_id": "mock_aweme_asr",
        "source": "asr",
        "audio_size_mb": 28.6,
        "video_info": {
            "aweme_id": "mock_aweme_asr",
            "desc": "命理咨询完整逐字稿（ASR mock）",
            "nickname": "命理师老张",
            "duration": 486000,
            "digg_count": 120000,
            "comment_count": 8800,
        },
        "raw_text": text,
        "segments": segments,
        "visitor_profile": visitor_profile,
    }
    _write_json(os.path.join(_dir(output_dir), f"{extract_id}.json"), record)
    with open(os.path.join(_dir(output_dir), "latest.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return {"ok": True, "extract_id": extract_id, "source": "asr", **record}
