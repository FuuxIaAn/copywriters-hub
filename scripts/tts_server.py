# -*- coding: utf-8 -*-
"""
配音工坊 · 服务端业务逻辑
========================
通过 ModelScope 创空间 API 调用 IndexTTS-2.5（B 站开源零样本音色克隆 TTS），
把口播文稿直接合成为配音音频（克隆参考音频的音色）。

- 认证：ModelScope 免费账号的 SDK Token（页面里粘贴保存，存本地 output/tts/settings.json）
- 端点：官方创空间 IndexTeam/IndexTTS-2.5 的 API 专用地址
- 接口签名来自官方 webui.py 的 gen_single（26 个参数，见 _build_args）
- WB_TTS_MOCK=1 时离线生成静音 wav，便于无 Token 演示/测试

server.py 只做薄路由转发，具体逻辑都在这里。
"""
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import wave
import math
import random

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STUDIO_API = "https://studio-indexteam-indextts-2-5.api-inference.modelscope.net"
MOCK = os.environ.get("WB_TTS_MOCK", "") == "1"

# 单次合成（client.predict）的最大等待秒数。ModelScope 免费创空间是排队制，
# GPU 忙时会排队很久。实测单条成功约 87~189s，但高峰期会 Queue is full 秒拒或长时间排队。
# 这里不再用超长单轮等待（8 分钟太久），而是缩短单轮上限 + 靠下方自动重试循环兜底。
PREDICT_TIMEOUT = int(os.environ.get("WB_TTS_TIMEOUT", "90"))
# 单轮等待到点但 predict 仍在跑时的「宽限等待」（秒）。免费服务排队制下，请求往往
# 「马上就好」，与其超时后盲目重开新请求（让队列更堵），不如多等这一小段等它返回。
GRACE_EXTRA = int(os.environ.get("WB_TTS_GRACE", "45"))
# 单条配音的总预算（秒）：从开始到放弃，最长自动尝试这么久。超过即报失败。
# 用户诉求（2026-08-18）：电脑不关、长时间等待没事，就怕频繁失败后要手动盯着重试。
# 因此从「2 分钟快速失败」上调到「5 分钟耐心重试」：高峰期 ModelScope 排队繁忙时，
# 自动在预算内持续等待重试，尽量一次成功，减少用户干预。仍设上限避免无限干等。
AUTO_RETRY_BUDGET = int(os.environ.get("WB_TTS_BUDGET", "300"))
MAX_AUTO_ATTEMPTS = int(os.environ.get("WB_TTS_MAX_ATTEMPTS", "5"))
# Once a remote request has been accepted, retain its lane while it is still
# running instead of submitting another request into the same busy service.
INFLIGHT_WATCHDOG = int(os.environ.get("WB_TTS_INFLIGHT_WATCHDOG", "900"))
# 每次「队列满」自动重试前的等待秒数。Queue is full 是瞬时秒拒（0.9s），
# 说明对方队列此刻没空位，等一段再试比立刻重试更有效。指数退避：第 N 次等 N*此值。
QUEUE_FULL_BACKOFF = int(os.environ.get("WB_TTS_QF_BACKOFF", "3"))
# 分句最大 token 数（2026-08-17 修复「断句不自然」）。官方默认 120 会让相邻口播短句
# 在 split_segments 里被「合并」（≤ max/2 即 60 token 就合并），模型在整段里自己找停顿，
# 容易断在不自然的位置。口播一句通常 10~25 字（约 15~35 token），这里下调到 40：
# 既够容纳一个完整短句，又避免多句被并成一大段、破坏自然断句。
MAX_TEXT_TOKENS_PER_SEGMENT = int(os.environ.get("WB_TTS_SEG_TOKENS", "40"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------- 路径与原子读写


def _dir(output_dir: str, *parts: str) -> str:
    p = os.path.join(output_dir, "tts", *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _uid(seed: str = "") -> str:
    return hashlib.md5(f"{seed}{time.time()}{id(object())}".encode()).hexdigest()[:10]


# ---------------------------------------------------------------- 设置（SDK Token）

def get_settings(output_dir: str) -> dict:
    s = _read_json(os.path.join(_dir(output_dir), "settings.json"), {})
    token = s.get("token", "")
    return {
        "ok": True,
        "has_token": bool(token),
        "token_masked": (token[:6] + "..." + token[-4:]) if len(token) > 12 else ("***" if token else ""),
    }


def save_settings(output_dir: str, token: str) -> dict:
    token = (token or "").strip()
    _write_json(os.path.join(_dir(output_dir), "settings.json"), {"token": token})
    return {"ok": True, "has_token": bool(token)}


def _load_token(output_dir: str) -> str:
    s = _read_json(os.path.join(_dir(output_dir), "settings.json"), {})
    return (s.get("token") or "").strip()


# Client 初始化超时秒数。Client() 构造时会去拉取创空间的 API 配置（view_api），
# 免费创空间排队繁忙时这一步也可能卡很久甚至无限期阻塞。设超时避免前端一直转圈。
CLIENT_INIT_TIMEOUT = int(os.environ.get("WB_TTS_CLIENT_TIMEOUT", "60"))


def _connect_client(token: str):
    """带超时的 Client 初始化。超时抛 TimeoutError，让调用方走重试/报错逻辑。"""
    from gradio_client import Client
    box = {}
    exc = {}

    def _do_init(_box=box, _exc=exc, _token=token):
        try:
            _box["client"] = Client(
                STUDIO_API,
                headers={"Authorization": f"Bearer {_token}"},
            )
        except Exception as e:  # noqa: BLE001
            _exc["error"] = e

    t = threading.Thread(target=_do_init, daemon=True)
    t.start()
    t.join(timeout=CLIENT_INIT_TIMEOUT)
    if t.is_alive():
        raise TimeoutError(f"连接 IndexTTS-2.5 服务超时（{CLIENT_INIT_TIMEOUT}秒），免费创空间可能排队繁忙")
    if "error" in exc:
        raise exc["error"]
    return box.get("client")


def _error_category(error) -> tuple[str, bool]:
    """Return a user-safe category and whether reconnecting/retrying can help."""
    if isinstance(error, _InFlightRequestTimeout):
        return "远端任务仍在处理", False
    name = type(error).__name__.lower()
    msg = str(error or "").lower()
    combined = f"{name} {msg}"
    if any(x in combined for x in ("401", "403", "unauthorized", "forbidden", "invalid token", "token")):
        return "鉴权失败", False
    if any(x in combined for x in ("404", "not found", "validation", "invalid", "unsupported", "parameter")):
        return "请求参数错误", False
    if any(x in combined for x in ("queueerror", "queue is full", "队列已满", "queue full")):
        return "服务队列繁忙", True
    if any(x in combined for x in (
        "timeout", "timed out", "ssl", "handshake", "connection", "reset by peer",
        "temporar", "dns", "name resolution", "502", "503", "504", "429", "remote disconnected",
    )):
        return "网络或服务暂时不可用", True
    return "服务返回异常", False


class _InFlightRequestTimeout(TimeoutError):
    """The remote request may still complete; issuing another one would duplicate it."""


def _retry_delay(attempt: int, category: str) -> int:
    base = QUEUE_FULL_BACKOFF if category == "服务队列繁忙" else 5
    return min(30, base * (2 ** min(max(0, attempt - 1), 3))) + random.randint(0, 2)


def _record_diagnostic(output_dir: str, **data) -> None:
    """Persist operational metadata only. Never write the token or generated text."""
    try:
        path = os.path.join(_dir(output_dir), "diagnostics.jsonl")
        data["at"] = _now()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _synthesize_with_retry(output_dir: str, ref_path: str, text: str, params: dict,
                           stage, cancelled=lambda: False, label: str = "single") -> tuple[str, int]:
    """Run one remote synthesis with bounded, connection-aware recovery.

    A timed-out predict is never followed by another predict while its worker is alive;
    submitting another request then would create duplicate jobs in ModelScope's queue.
    """
    token = _load_token(output_dir)
    args = _build_args(ref_path, text, params["lang"], params["emo_mode"],
                       params["emo_vec"], params["emo_weight"], params["duration_factor"],
                       params.get("emo_random", False), params.get("emo_text", ""))
    started = time.monotonic()
    attempt = 0
    last_error = None
    while attempt < MAX_AUTO_ATTEMPTS and time.monotonic() - started < AUTO_RETRY_BUDGET:
        if cancelled():
            raise RuntimeError("已取消")
        attempt += 1
        client = None
        try:
            stage(f"正在连接配音服务（第 {attempt}/{MAX_AUTO_ATTEMPTS} 次）")
            client = _connect_client(token)
            stage(f"已提交配音任务，等待服务处理（第 {attempt}/{MAX_AUTO_ATTEMPTS} 次）")
            box = {}
            def _predict(_box=box, _client=client, _args=args):
                try:
                    _box["result"] = _client.predict(*_args, api_name="/gen_single")
                except Exception as exc:  # noqa: BLE001
                    _box["error"] = exc
            worker = threading.Thread(target=_predict, daemon=True)
            worker.start()
            remaining = max(1, AUTO_RETRY_BUDGET - (time.monotonic() - started))
            worker.join(timeout=min(PREDICT_TIMEOUT, remaining))
            if worker.is_alive():
                remaining = max(0, AUTO_RETRY_BUDGET - (time.monotonic() - started))
                stage(f"服务仍在处理，继续等待 {min(GRACE_EXTRA, int(remaining))} 秒，避免重复提交")
                worker.join(timeout=min(GRACE_EXTRA, remaining))
            if worker.is_alive():
                # The request was accepted, so retrying it would create a duplicate
                # remote job. Continue watching this exact request; the caller owns
                # the single remote lane, so later local tasks wait instead of piling
                # into the provider queue. A finite watchdog still prevents infinity.
                watch_started = time.monotonic()
                while worker.is_alive() and time.monotonic() - watch_started < INFLIGHT_WATCHDOG:
                    if cancelled():
                        raise RuntimeError("已取消")
                    waited = int(time.monotonic() - started)
                    left = max(0, INFLIGHT_WATCHDOG - int(time.monotonic() - watch_started))
                    stage(f"远端任务仍在处理中，已自动守候 {waited} 秒（剩余保护时限约 {left} 秒）")
                    worker.join(timeout=1.0)
            if worker.is_alive():
                raise _InFlightRequestTimeout(f"服务在 {INFLIGHT_WATCHDOG} 秒守候期内仍未返回")
            src = _extract_audio_path(box.get("result"))
            if src and os.path.isfile(str(src)):
                _record_diagnostic(output_dir, operation=label, attempt=attempt, outcome="success",
                                   elapsed=round(time.monotonic() - started, 1), text_length=len(text))
                return str(src), attempt
            if box.get("error") is not None:
                raise box["error"]
            raise RuntimeError("服务返回空结果")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if isinstance(exc, _InFlightRequestTimeout):
                _record_diagnostic(output_dir, operation=label, attempt=attempt, outcome="in_flight_timeout",
                                   error_type=type(exc).__name__, elapsed=round(time.monotonic() - started, 1),
                                   text_length=len(text))
                raise TimeoutError("配音服务长时间未返回，已完成自动守候且未重复提交；该条任务已安全结束。") from exc
            category, recoverable = _error_category(exc)
            elapsed = time.monotonic() - started
            _record_diagnostic(output_dir, operation=label, attempt=attempt, outcome="error", category=category,
                               recoverable=recoverable, error_type=type(exc).__name__, elapsed=round(elapsed, 1),
                               text_length=len(text))
            if not recoverable:
                raise RuntimeError(f"{category}：{str(exc)[:160]}") from exc
            if attempt >= MAX_AUTO_ATTEMPTS or elapsed >= AUTO_RETRY_BUDGET:
                break
            delay = min(_retry_delay(attempt, category), max(0, int(AUTO_RETRY_BUDGET - elapsed)))
            stage(f"{category}，{delay} 秒后自动重试（第 {attempt}/{MAX_AUTO_ATTEMPTS} 次）")
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if cancelled():
                    raise RuntimeError("已取消")
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    category, _ = _error_category(last_error)
    raise TimeoutError(f"{category}。已自动尝试 {attempt} 次并停止，避免无限等待；请检查 Token、参考音频和 ModelScope 服务状态。")


# ---------------------------------------------------------------- 音色预设

def list_presets(output_dir: str) -> dict:
    presets = _read_json(os.path.join(_dir(output_dir), "presets.json"), [])
    # Mark duplicates without deleting anything: same reference bytes or same normalized name.
    seen = {}
    out = []
    for p in presets:
        item = dict(p)
        ref = os.path.join(_dir(output_dir), p.get("ref_audio", ""))
        name_key = "name:" + "".join(str(p.get("name", "")).lower().split())
        keys = [name_key]
        try:
            if os.path.isfile(ref):
                import hashlib as _hashlib
                h = _hashlib.sha256()
                with open(ref, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
            keys.insert(0, "audio:" + h.hexdigest())
        except OSError:
            pass
        prior = next((seen[k] for k in keys if k in seen), None)
        if prior:
            item["duplicate_of"] = prior
        for k in keys:
            seen.setdefault(k, p.get("id"))
        out.append(item)
    # UI only exposes one canonical entry per reference audio; duplicates remain recoverable
    # through duplicate_ids so cleanup can be explicit and no uploaded audio is wasted.
    canonical = []
    for item in out:
        if item.get("duplicate_of"):
            owner = next((x for x in canonical if x.get("id") == item.get("duplicate_of")), None)
            if owner:
                owner.setdefault("duplicate_ids", []).append(item.get("id"))
            continue
        item.setdefault("duplicate_ids", [])
        canonical.append(item)
    return {"ok": True, "presets": canonical}


def cleanup_duplicate_presets(output_dir: str, pid: str) -> dict:
    """Remove duplicate records for a canonical preset while retaining its reference audio."""
    pid = (pid or "").strip()
    path = os.path.join(_dir(output_dir), "presets.json")
    presets = _read_json(path, [])
    target = next((p for p in presets if p.get("id") == pid), None)
    if not target:
        return {"ok": False, "error": "音色不存在或已删除"}
    ref = os.path.join(_dir(output_dir), target.get("ref_audio", ""))
    def digest(p):
        try:
            q = os.path.join(_dir(output_dir), p.get("ref_audio", ""))
            h = hashlib.sha256()
            with open(q, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""
    wanted = digest(target)
    removed = []
    left = []
    for p in presets:
        if p.get("id") != pid and wanted and digest(p) == wanted:
            removed.append(p.get("id"))
            try:
                os.remove(os.path.join(_dir(output_dir), p.get("ref_audio", "")))
            except OSError:
                pass
        else:
            left.append(p)
    _write_json(path, left)
    return {"ok": True, "removed": removed, "kept": pid}


def update_preset(output_dir: str, pid: str, name: str = None, note: str = None) -> dict:
    """Edit non-audio metadata without changing the reference file or preset id."""
    pid = (pid or "").strip()
    if not pid:
        return {"ok": False, "error": "音色编号为空"}
    path = os.path.join(_dir(output_dir), "presets.json")
    presets = _read_json(path, [])
    target = next((p for p in presets if p.get("id") == pid), None)
    if not target:
        return {"ok": False, "error": "音色不存在或已删除"}
    if name is not None:
        name = str(name).strip()
        if not name:
            return {"ok": False, "error": "音色名称不能为空"}
        target["name"] = name[:80]
    if note is not None:
        target["note"] = str(note).strip()[:300]
    _write_json(path, presets)
    return {"ok": True, "preset": target}


def add_preset(output_dir: str, name: str, ref_path: str, note: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "请填写音色名称"}
    if not ref_path or not os.path.isfile(ref_path):
        return {"ok": False, "error": "参考音频不存在，请先上传"}
    h = hashlib.sha256()
    try:
        with open(ref_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return {"ok": False, "error": "无法读取参考音频"}
    digest = h.hexdigest()
    existing = _read_json(os.path.join(_dir(output_dir), "presets.json"), [])
    for p in existing:
        q = os.path.join(_dir(output_dir), p.get("ref_audio", ""))
        try:
            hh = hashlib.sha256()
            with open(q, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    hh.update(chunk)
            if hh.hexdigest() == digest:
                return {"ok": True, "preset": p, "duplicate": True, "message": "参考音频已存在，已复用原音色，不重复创建"}
        except OSError:
            continue
    ext = os.path.splitext(ref_path)[1] or ".wav"
    pid = _uid(name)
    dst = os.path.join(_dir(output_dir, "uploads"), f"{pid}{ext}")
    shutil.copyfile(ref_path, dst)
    presets = _read_json(os.path.join(_dir(output_dir), "presets.json"), [])
    presets.append({
        "id": pid,
        "name": name,
        "note": (note or "").strip(),
        "ref_audio": os.path.relpath(dst, os.path.join(output_dir, "tts")),
        "created_at": _now(),
    })
    _write_json(os.path.join(_dir(output_dir), "presets.json"), presets)
    return {"ok": True, "preset": presets[-1]}


def remove_preset(output_dir: str, pid: str) -> dict:
    base = _dir(output_dir)
    presets = _read_json(os.path.join(base, "presets.json"), [])
    left = [p for p in presets if p.get("id") != pid]
    if len(left) == len(presets):
        return {"ok": False, "error": "预设不存在或已删除"}
    for p in presets:
        if p.get("id") == pid:
            try:
                os.remove(os.path.join(base, p.get("ref_audio", "x")))
            except OSError:
                pass
    _write_json(os.path.join(base, "presets.json"), left)
    return {"ok": True}


def save_upload(output_dir: str, file_storage) -> dict:
    """保存前端上传的参考音频（Flask file_storage），返回临时路径（未入库）。"""
    name = file_storage.filename or "ref.wav"
    ext = os.path.splitext(name)[1] or ".wav"
    if ext.lower() not in (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"):
        return {"ok": False, "error": f"不支持的音频格式 {ext}，请用 wav/mp3/flac 等"}
    tmp_dir = _dir(output_dir, "uploads_tmp")
    path = os.path.join(tmp_dir, f"{_uid(name)}{ext}")
    try:
        file_storage.save(path)
        size = os.path.getsize(path)
        if size > MAX_UPLOAD_BYTES:
            os.remove(path)
            return {"ok": False, "error": "参考音频超过 50MB 上限，请先压缩后重试"}
    except OSError as e:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return {"ok": False, "error": f"保存参考音频失败：{e}"}
    return {"ok": True, "path": path, "filename": name}


# ---------------------------------------------------------------- 生成任务

# 多任务并行模型：每个合成请求是一个独立任务（task_id -> state dict），互不抢占、互不取消。
# 点另一条消息合成时，两条各自并行跑，互不干扰。前端按 task_id 分别轮询。
_gen_tasks = {}          # task_id -> 任务状态 dict（running/stage/result/last_error/started_at/finished_at）
_gen_tasks_lock = threading.Lock()
_gen_seq_counter = 0     # 全局递增，用于生成唯一 task_id
# ModelScope's free IndexTTS Space is unreliable with concurrent predict calls.
_remote_lane = threading.Lock()


def _new_task_id() -> str:
    global _gen_seq_counter
    with _gen_tasks_lock:
        _gen_seq_counter += 1
        return f"{int(time.time())}-{_gen_seq_counter}-{_uid('t')[:4]}"


# 兼容旧接口：保留「最近一次完成/进行中任务」的聚合视图（供配音 Tab 的 /api/tts/status 使用）
_gen_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "stage": "",
    "result": None,
    "epoch": 0,
}
_gen_lock = threading.Lock()


def _sync_gen_state() -> None:
    """Keep the legacy aggregate view truthful when several per-message jobs overlap."""
    with _gen_tasks_lock:
        states = [dict(s) for s in _gen_tasks.values() if not s.get("cancelled")]
    active = [s for s in states if s.get("running")]
    latest = max(states, key=lambda s: s.get("started_at") or 0, default=None)
    with _gen_lock:
        if active:
            current = max(active, key=lambda s: s.get("started_at") or 0)
            _gen_state.update(running=True, started_at=current.get("started_at"), finished_at=None,
                              last_error=None, stage=current.get("stage", "正在合成"), result=None)
        elif latest:
            _gen_state.update(running=False, started_at=latest.get("started_at"),
                              finished_at=latest.get("finished_at"), last_error=latest.get("last_error"),
                              stage=latest.get("stage", ""), result=latest.get("result"))

# emo_control_method 虽然官方 webui.py 里声明为 type="index" 型 Radio，但 ModelScope
# 的 API 网关会对 Radio 输入做「值校验」，只接受 choices 里的字符串 value，不接受整数
# index。实测：传整数 0 会直接报
#   AppError: Value: 0 (type: <class 'int'>) is not in the list of choices:
#   ['与音色参考音频相同', '使用情感参考音频', '使用情感向量控制']
# 传中文字符串则成功返回 {'__type__':'update','value':'xxx.wav','visible':True}。
# 因此这里必须传「中文字符串」，绝不能传整数索引。
EMO_MODE_SAME = "与音色参考音频相同"
EMO_MODE_VEC = "使用情感向量控制"
EMO_MODES = {"same": EMO_MODE_SAME, "auto": EMO_MODE_SAME, "vector": EMO_MODE_VEC}


# ---------------------------------------------------------------- 语速自适应

# 正常中文口语的语速基准（每秒汉字数）。IndexTTS-2.5 的 duration_factor=1.0 实测偏慢，
# 实际产出通常只有约 3.0~3.5 字/秒，而真人正常口语约 4~5 字/秒。这里以「模型 1.0 基准
# ≈ 3.3 字/秒」作为换算锚点：若测得参考音频语速为 V 字/秒，则 duration_factor ≈ 3.3 / V。
# 即：参考音频语速越快 → duration_factor 越小（合成更快），反之越慢。
TTS_BASE_CHARS_PER_SEC = 3.3  # duration_factor=1.0 时模型约产出的每秒字数（经验锚点）


def _audio_duration(path: str) -> float | None:
    """读取音频时长（秒）。优先用 ffprobe（支持 mp3/flac/m4a 等），失败回退 wave（仅 wav）。"""
    if not path or not os.path.isfile(path):
        return None
    # 1) ffprobe（最通用，支持压缩格式）
    for ffprobe in ("ffprobe", "ffprobe.exe"):
        try:
            import subprocess
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip())
        except Exception:
            continue
    # 2) wave 模块（仅未压缩 wav）
    try:
        with wave.open(path, "rb") as w:
            fr = w.getframerate() or 0
            nf = w.getnframes() or 0
            if fr > 0:
                return nf / fr
    except Exception:
        pass
    return None


def _count_chars(text: str) -> int:
    """统计文本的有效「说话字符数」：汉字 + 数字 + 字母，忽略空白与标点。"""
    if not text:
        return 0
    return len([c for c in text if c.isalnum() or ("\u4e00" <= c <= "\u9fff")])


def measure_ref_speed(ref_path: str, sample_text: str = "") -> dict:
    """测量参考音频语速，并换算成建议的 duration_factor 基准。

    返回：{ ok, duration(秒), chars, chars_per_sec(每秒字数), suggested_factor }
    - suggested_factor：让合成语速对齐参考音频的 duration_factor（<1 更快，>1 更慢）。
    - 若无法读取时长（无 ffprobe 且非 wav）或 sample_text 为空，返回 ok=True 但 suggested_factor=None，
      由调用方回退到默认语速。

    注意：这是「整段参考音频 ÷ 整段字数」的笼统测量，仅作兜底。
    当能拿到「目标发言人的逐句基准语速」（extract_server.measure_speaker_speed 固化的
    speaker_speed 字段）时，应优先用 _speed_factor_from_extract，因为那是按
    「发言人实际说的那句话在音轨里的真实时长」逐句测出的，更贴合真人语速。
    """
    if not ref_path or not os.path.isfile(ref_path):
        return {"ok": False, "error": "参考音频不存在"}
    dur = _audio_duration(ref_path)
    if not dur or dur <= 0:
        return {"ok": False, "error": "无法读取参考音频时长（非 wav 且无 ffprobe）"}
    chars = _count_chars(sample_text)
    if chars <= 0:
        # 没有文本，无法算语速，但仍返回时长供参考
        return {"ok": True, "duration": round(dur, 2), "chars": 0,
                "chars_per_sec": None, "suggested_factor": None,
                "note": "未提供文本，无法换算语速因子"}
    cps = chars / dur
    # 换算：factor = 模型1.0基准语速 / 参考语速，并夹在 0.5~2.0 合法区间
    factor = TTS_BASE_CHARS_PER_SEC / cps
    factor = max(0.5, min(2.0, factor))
    return {"ok": True, "duration": round(dur, 2), "chars": chars,
            "chars_per_sec": round(cps, 2),
            "suggested_factor": round(factor, 3)}


def _speed_factor_from_extract(output_dir: str, extract_id: str, speaker: str) -> dict:
    """从提取记录里读取「目标发言人的逐句基准语速 + 口语变化档案」，换算成建议的 duration_factor。

    返回：{ ok, base_chars_per_sec, suggested_factor, source, style, error? }
    - 优先读 record.speaker_speed[speaker]（已固化，音轨实测）；
    - 若尚未固化，尝试现场调用 extract_server.measure_speaker_speed 补测一次；
    - 换算 factor = TTS_BASE_CHARS_PER_SEC / base_cps，夹在 0.5~2.0；
    - 若 record 里已有 speaker_style[speaker]（口语变化档案），一并返回，供上层把
      语速随内容变化、情绪起伏、重音停顿习惯注入 TTS 合成参数。
    """
    if not extract_id:
        return {"ok": False, "error": "未提供提取记录编号"}
    speaker = (speaker or "A").strip().upper()
    try:
        import extract_server
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"无法加载提取模块: {e}"}
    try:
        record = extract_server._read_json(
            os.path.join(extract_server._dir(output_dir), f"{extract_id}.json"), None)
    except Exception:  # noqa: BLE001
        record = None
    if not record:
        return {"ok": False, "error": "提取记录不存在"}

    spd = (record.get("speaker_speed") or {}).get(speaker)
    if not spd:
        # 尚未固化 → 现场补测一次（会写回 record）
        r = extract_server.measure_speaker_speed(output_dir, extract_id, speaker)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", "语速测量失败")}
        spd = r
    base_cps = spd.get("base_chars_per_sec")
    if not base_cps or base_cps <= 0:
        return {"ok": False, "error": "发言人语速数据无效"}
    factor = TTS_BASE_CHARS_PER_SEC / base_cps
    factor = max(0.5, min(2.0, factor))

    # 口语变化档案（语速随内容 + 情绪起伏 + 重音停顿）：已固化则一并带出
    style = (record.get("speaker_style") or {}).get(speaker)

    return {
        "ok": True,
        "base_chars_per_sec": round(base_cps, 2),
        "suggested_factor": round(factor, 3),
        "source": spd.get("source", "音轨切片实测"),
        "variation": spd.get("variation", ""),
        "speaker": speaker,
        "style": style,   # 口语变化档案（可为 None，表示尚未测）
    }


def _build_args(ref_path: str, text: str, lang: str, emo_mode: str,
                emo_vec: list, emo_weight: float, duration_factor: float,
                emo_random: bool = False, emo_text: str = "") -> list:
    """按官方 webui.py gen_single 的输入顺序构造 20 个参数（15 显式 + 8 vec 展开 + 高级参数用官方默认值）。
    emo_mode 是中文字符串（"与音色参考音频相同"/"使用情感向量控制"），见 EMO_MODE_* 常量。"""
    try:
        from gradio_client import handle_file
        prompt = handle_file(ref_path)
    except ImportError:
        prompt = ref_path
    vec = [float(v) for v in (emo_vec or [0.0] * 8)][:8]
    while len(vec) < 8:
        vec.append(0.0)
    return [
        emo_mode,            # emo_control_method（Radio，网关只接受字符串 value）
        prompt,              # prompt_audio 音色参考音频
        text,                # input_text_single
        lang or "ZH",        # lang_dropdown: ZH/EN/JA/AR/ES
        None,                # emo_upload 情感参考音频（本工坊不提供该模式）
        float(emo_weight),   # emo_weight
        *vec,                # vec1..vec8
        emo_text or "",      # emo_text 情感描述文本
        bool(emo_random),    # emo_random 情感随机采样
        MAX_TEXT_TOKENS_PER_SEGMENT,  # max_text_tokens_per_segment（口播短句级，防断句不自然）
        float(duration_factor),  # duration_factor 时长系数 0.5~2.0
        # ---- 高级采样参数（音色还原优先：降低随机性，让输出更贴近参考音频）----
        True,                # do_sample
        0.9,                 # top_p（0.8→0.9，采样更集中）
        30,                  # top_k
        0.5,                 # temperature（0.8→0.5，减少音色漂移）
        0.0,                 # length_penalty
        3,                   # num_beams
        10.0,                # repetition_penalty
        1500,                # max_mel_tokens
    ]


def _extract_audio_path(result) -> str | None:
    """gen_single 返回 gr.update(...)，API 序列化后形如 dict(value=文件路径)。"""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        v = result.get("value") or result.get("audio") or result.get("url")
        if isinstance(v, str):
            return v
        if isinstance(v, tuple):
            for x in v:
                if isinstance(x, str):
                    return x
    if isinstance(result, (list, tuple)):
        for x in result:
            p = _extract_audio_path(x)
            if p:
                return p
    return None


def _mock_wav(path: str, seconds: float = 2.0) -> None:
    rate = 16000
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            int(6000 * math.sin(2 * math.pi * 220 * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(int(rate * seconds))
        )
        w.writeframes(frames)


def start_generate(output_dir: str, payload: dict) -> dict:
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "请输入要配音的文稿"}
    ref_path = ""
    preset_name = "未命名音色"
    if payload.get("preset_id"):
        base = _dir(output_dir)
        presets = _read_json(os.path.join(base, "presets.json"), [])
        preset = next((p for p in presets if p.get("id") == payload["preset_id"]), None)
        if not preset:
            return {"ok": False, "error": "所选音色预设不存在"}
        preset_name = preset.get("name", "")
        ref_path = os.path.join(base, preset.get("ref_audio", ""))
    elif payload.get("ref_path"):
        ref_path = payload["ref_path"]
        preset_name = "临时上传音色"
    else:
        return {"ok": False, "error": "请先选择音色预设或上传参考音频"}
    if not os.path.isfile(ref_path):
        return {"ok": False, "error": "参考音频文件不存在，请重新上传"}
    if not MOCK and not _load_token(output_dir):
        return {"ok": False, "error": "尚未配置 ModelScope SDK Token，请点对话顶部「🔑 Token」按钮配置（免费）"}

    lang = (payload.get("lang") or "ZH").upper()
    if lang not in ("ZH", "EN", "JA", "AR", "ES"):
        lang = "ZH"

    # 语速因子优先级（从最准到兜底）：
    # 1) 显式传入 duration_factor（用户在下拉里选了「快/正常/慢」）→ 直接用；
    # 2) 否则若带了 extract_id + speaker → 用「目标发言人逐句基准语速」换算因子
    #    （extract_server 按发言人实际说的那句话在音轨里的真实时长逐句测出，最贴合真人语速）；
    # 3) 否则若 auto_speed 开启（默认开）→ 测参考音频整段语速兜底；
    # 4) 都没有 → 默认 1.0。
    duration_factor = min(2.0, max(0.5, float(payload.get("duration_factor", 0) or 0)))
    style_hint = ""   # 口语变化档案摘要，注入 emo_text 供 TTS 参照真实风格
    if duration_factor <= 0:
        extract_id = (payload.get("extract_id") or "").strip()
        speaker = (payload.get("speaker") or "").strip()
        if extract_id:
            spd = _speed_factor_from_extract(output_dir, extract_id, speaker)
            if spd.get("ok") and spd.get("suggested_factor"):
                duration_factor = spd["suggested_factor"]
                # 口语变化档案：语速随内容 + 情绪起伏 + 重音停顿（实测校准）
                if spd.get("style") and spd["style"].get("summary"):
                    style_hint = spd["style"]["summary"]
    if duration_factor <= 0 and payload.get("auto_speed", True):
        spd = measure_ref_speed(ref_path, text)
        if spd.get("ok") and spd.get("suggested_factor"):
            duration_factor = spd["suggested_factor"]
    if duration_factor <= 0:
        duration_factor = 1.0
    duration_factor = min(2.0, max(0.5, duration_factor))

    # 情绪逐句倍率（2026-08-17 修复「逐句语速无差异」）：前端把情绪 LLM 判出的本句
    # duration_factor 作为「相对发言人基准的倍率」speed_ratio 传下来，这里「基准 × 倍率」
    # 算出最终因子。这样每句话语速随情绪/内容真实起伏：着急 → 基准×0.85 更快、
    # 委屈 → 基准×1.1 更慢，而不是一把固定语速跑到底。倍率夹在 0.7~1.3（对应
    # 「明显加快 ~ 明显放慢」的合理区间，超出即为情绪 LLM 越界，拉回边界）。
    try:
        _ratio = float(payload.get("speed_ratio", 0) or 0)
        if _ratio <= 0:
            _ratio = 1.0
        _ratio = max(0.7, min(1.3, _ratio))
    except Exception:
        _ratio = 1.0
    if _ratio != 1.0:
        duration_factor = min(2.0, max(0.5, duration_factor * _ratio))

    # 用户没显式给 emo_text 时，若拿到了口语变化档案摘要，则注入作风格参考
    # （语速随内容变化 + 情绪起伏 + 重音停顿，全部来自原视频实测校准，而非凭空推断）
    emo_text = str(payload.get("emo_text") or "").strip()
    if not emo_text and style_hint:
        emo_text = style_hint

    # 情绪控制方式：官方 gen_single 的 vector 模式（"使用情感向量控制"）在免费创空间
    # 服务器端会卡死（实测 50s+ 超时，same 模式则秒级成功），故这里把 vector 降级为
    # same（音色参考音频相同），语气情绪改用 emo_text（自然语言情感描述）表达 ——
    # IndexTTS 官方同样推荐用 emo_text，且情绪分析已生成该描述，不损失表达能力。
    _emo_mode = EMO_MODES.get(payload.get("emo_mode", "same"), EMO_MODE_SAME)
    if _emo_mode == EMO_MODE_VEC:
        _emo_mode = EMO_MODE_SAME

    params = {
        "lang": lang,
        "emo_mode": _emo_mode,
        "emo_vec": [float(v) for v in (payload.get("emo_vec") or [0.0] * 8)][:8],
        "emo_weight": float(payload.get("emo_weight", 0.65)),
        "duration_factor": duration_factor,
        "emo_random": bool(payload.get("emo_random", False)),
        "emo_text": emo_text,
    }

    # 并行模型：每个请求一个独立 task_id，不抢占、不取消已有任务
    task_id = _new_task_id()
    task_state = {
        "task_id": task_id,
        "running": True,
        "started_at": time.time(),
        "finished_at": None,
        "last_error": None,
        "stage": "任务排队中",
        "result": None,
        "cancelled": False,
    }
    with _gen_tasks_lock:
        _gen_tasks[task_id] = task_state

    _sync_gen_state()

    t = threading.Thread(
        target=_run_generate, args=(output_dir, ref_path, preset_name, text, params, task_id), daemon=True
    )
    t.start()
    return {"ok": True, "task_id": task_id}


def _run_generate(output_dir: str, ref_path: str, preset_name: str, text: str, params: dict, task_id: str):
    def _cancelled() -> bool:
        with _gen_tasks_lock:
            st = _gen_tasks.get(task_id)
            return bool(st and st.get("cancelled"))

    def _stage(s: str):
        with _gen_tasks_lock:
            st = _gen_tasks.get(task_id)
            if st and not st.get("cancelled"):
                st["stage"] = s
        _sync_gen_state()

    audio_id = _uid(text[:32])
    out_path = os.path.join(_dir(output_dir, "audio"), f"{audio_id}.wav")
    entry = None
    try:
        if MOCK:
            _stage("离线演示合成中")
            time.sleep(2)
            _mock_wav(out_path)
        else:
            # All entry points share one remote lane.  The free IndexTTS service
            # accepts a queued request unreliably when several predicts overlap;
            # waiting locally is both safer and visible to the user.
            while not _remote_lane.acquire(timeout=0.5):
                if _cancelled():
                    raise RuntimeError("已取消")
                _stage("等待前一条远端配音完成，随后自动继续")
            try:
                _stage("已获得配音服务通道，正在提交")
                src, _ = _synthesize_with_retry(output_dir, ref_path, text, params, _stage, _cancelled, "single")
            finally:
                _remote_lane.release()
            # gradio_client 下载到本地临时文件，转存到作品目录
            shutil.copyfile(src, out_path)
            try:
                os.remove(src)
            except OSError:
                pass

        # 结果写回前检查是否已被取消：若是，丢弃本次结果（旧音频不落库）
        if _cancelled():
            print(f"[tts] 任务已被取消，丢弃结果: {preset_name} · {len(text)}字")
            if os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            with _gen_tasks_lock:
                st = _gen_tasks.get(task_id)
                if st:
                    st.update(running=False, finished_at=time.time(), stage="已取消")
            _sync_gen_state()
            return

        entry = {
            "id": audio_id,
            "time": _now(),
            "preset": preset_name,
            "text_head": text[:80].replace("\n", " "),
            "text_len": len(text),
            "params": params,
            "audio": f"{audio_id}.wav",
        }
        hist_path = os.path.join(_dir(output_dir), "history.json")
        history = _read_json(hist_path, [])
        history.insert(0, entry)
        _write_json(hist_path, history[:100])
        with _gen_tasks_lock:
            st = _gen_tasks.get(task_id)
            if st and not st.get("cancelled"):
                st.update(running=False, finished_at=time.time(),
                          last_error=None, stage="完成", result=entry)
        _sync_gen_state()
        print(f"[tts] 配音完成: {preset_name} · {len(text)}字 -> {out_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[tts] 配音异常: {e}")
        if _cancelled():
            if entry is None and os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            with _gen_tasks_lock:
                st = _gen_tasks.get(task_id)
                if st:
                    st.update(running=False, finished_at=time.time(), stage="已取消")
            _sync_gen_state()
            return
        with _gen_tasks_lock:
            st = _gen_tasks.get(task_id)
            if st:
                st.update(running=False, finished_at=time.time(),
                          last_error=str(e), stage="失败")
        _sync_gen_state()
        if entry is None and os.path.isfile(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def cancel_generate(task_id: str = None) -> dict:
    """取消合成任务。

    - 传 task_id：取消指定任务（标记 cancelled，其线程完成后丢弃结果）。
    - 不传：取消所有进行中的任务（兼容旧「全部取消」语义）。
    并行模型下，点另一条消息合成时【不再】自动取消旧任务；此接口仅供显式取消使用。
    """
    with _gen_tasks_lock:
        if task_id:
            st = _gen_tasks.get(task_id)
            if st and st.get("running"):
                st["cancelled"] = True
                st.update(running=False, finished_at=time.time(), stage="已取消")
        else:
            for st in _gen_tasks.values():
                if st.get("running"):
                    st["cancelled"] = True
                    st.update(running=False, finished_at=time.time(), stage="已取消")
    _sync_gen_state()
    return {"ok": True}


def get_task_status(task_id: str) -> dict:
    """查询单个合成任务的实时状态（供前端按 task_id 轮询并行任务）。"""
    with _gen_tasks_lock:
        st = _gen_tasks.get(task_id)
        if not st:
            return {"ok": False, "error": "任务不存在或已结束"}
        return {"ok": True, "state": dict(st)}


def get_status() -> dict:
    with _gen_lock:
        return {"ok": True, "state": dict(_gen_state)}


def get_history(output_dir: str) -> dict:
    history = _read_json(os.path.join(_dir(output_dir), "history.json"), [])
    return {"ok": True, "history": history}


def remove_history(output_dir: str, hid: str) -> dict:
    base = _dir(output_dir)
    hist_path = os.path.join(base, "history.json")
    history = _read_json(hist_path, [])
    left = [h for h in history if h.get("id") != hid]
    if len(left) == len(history):
        return {"ok": False, "error": "记录不存在"}
    for h in history:
        if h.get("id") == hid:
            try:
                os.remove(os.path.join(base, "audio", h.get("audio", "x")))
            except OSError:
                pass
    _write_json(hist_path, left)
    return {"ok": True}


# ---------------------------------------------------------------- 连接测试

def test_connection(output_dir: str) -> dict:
    """验证 Token 是否有效、能否拿到创空间的接口列表。"""
    if MOCK:
        return {"ok": True, "mock": True, "endpoints": ["/gen_single (mock)"]}
    token = _load_token(output_dir)
    if not token:
        return {"ok": False, "error": "请先填写并保存 SDK Token"}
    try:
        client = _connect_client(token)
        info = client.view_api(return_format="dict")
        endpoints = sorted((info.get("named_endpoints") or {}).keys())
        return {"ok": True, "endpoints": endpoints}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"连接失败: {str(e)[:200]}"}


# ---------------------------------------------------------------- 批量生成（对话逐条 TTS）

_batch_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "current_text": "",
    "stage": "",
    "results": [],          # [{msg_id, text, audio, ok, error}]
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}
_batch_lock = threading.Lock()


def start_batch(output_dir: str, payload: dict) -> dict:
    """批量生成：接收 [{id, text}] 列表，逐条合成音频。"""
    with _batch_lock:
        if _batch_state["running"]:
            return {"ok": False, "error": "已有批量任务进行中"}
        messages = payload.get("messages") or []
        if not messages:
            return {"ok": False, "error": "没有要合成的内容"}

        # 解析音色预设
        ref_path = ""
        preset_name = "未命名音色"
        if payload.get("preset_id"):
            base = _dir(output_dir)
            presets = _read_json(os.path.join(base, "presets.json"), [])
            preset = next((p for p in presets if p.get("id") == payload["preset_id"]), None)
            if not preset:
                return {"ok": False, "error": "所选音色预设不存在"}
            preset_name = preset.get("name", "")
            ref_path = os.path.join(base, preset.get("ref_audio", ""))
        elif payload.get("ref_path"):
            ref_path = payload["ref_path"]
            preset_name = "临时上传音色"
        else:
            return {"ok": False, "error": "请先选择音色预设或上传参考音频"}
        if not os.path.isfile(ref_path):
            return {"ok": False, "error": "参考音频文件不存在"}
        if not MOCK and not _load_token(output_dir):
            return {"ok": False, "error": "尚未配置 ModelScope SDK Token"}

        lang = (payload.get("lang") or "ZH").upper()
        if lang not in ("ZH", "EN", "JA", "AR", "ES"):
            lang = "ZH"
        # vector 模式在免费创空间服务器端会卡死，降级为 same（见 start_generate 同款注释）
        _emo_mode = EMO_MODES.get(payload.get("emo_mode", "same"), EMO_MODE_SAME)
        if _emo_mode == EMO_MODE_VEC:
            _emo_mode = EMO_MODE_SAME
        params = {
            "lang": lang,
            "emo_mode": _emo_mode,
            "emo_vec": [float(v) for v in (payload.get("emo_vec") or [0.0] * 8)][:8],
            "emo_weight": float(payload.get("emo_weight", 0.65)),
            "duration_factor": min(2.0, max(0.5, float(payload.get("duration_factor", 1.0)))),
            "emo_random": bool(payload.get("emo_random", False)),
            "emo_text": str(payload.get("emo_text") or "").strip(),
        }

        msgs = [{"id": m.get("id", _uid(m.get("text", "")[:16])), "text": (m.get("text") or "").strip()}
                for m in messages if (m.get("text") or "").strip()]
        if not msgs:
            return {"ok": False, "error": "没有有效的文本内容"}

        _batch_state.update(
            running=True, total=len(msgs), done=0, current_text="",
            stage="批量任务已启动", results=[], started_at=time.time(),
            finished_at=None, last_error=None,
        )
        t = threading.Thread(
            target=_run_batch, args=(output_dir, ref_path, preset_name, msgs, params), daemon=True
        )
        t.start()
        return {"ok": True, "total": len(msgs)}


def _run_batch(output_dir: str, ref_path: str, preset_name: str, msgs: list, params: dict):
    def _stage(s: str):
        with _batch_lock:
            _batch_state["stage"] = s

    results = []
    for i, msg in enumerate(msgs):
        text = msg["text"]
        msg_id = msg["id"]
        with _batch_lock:
            _batch_state["current_text"] = text[:60]
            _batch_state["stage"] = f"正在合成第 {i+1}/{len(msgs)} 条"
        audio_id = _uid(text[:32])
        out_path = os.path.join(_dir(output_dir, "audio"), f"{audio_id}.wav")
        ok = False
        error = ""
        try:
            if MOCK:
                time.sleep(1.5)
                _mock_wav(out_path, seconds=1.5)
            else:
                while not _remote_lane.acquire(timeout=0.5):
                    _stage(f"第 {i + 1}/{len(msgs)} 条：等待前一条远端配音完成")
                try:
                    src, _ = _synthesize_with_retry(
                        output_dir, ref_path, text, params,
                        lambda status, _i=i: _stage(f"第 {_i + 1}/{len(msgs)} 条：{status}"),
                        label="batch",
                    )
                finally:
                    _remote_lane.release()
                shutil.copyfile(src, out_path)
                try:
                    os.remove(src)
                except OSError:
                    pass
            ok = True
            # 写入历史
            entry = {
                "id": audio_id, "time": _now(), "preset": preset_name,
                "text_head": text[:80].replace("\n", " "), "text_len": len(text),
                "params": params, "audio": f"{audio_id}.wav",
            }
            hist_path = os.path.join(_dir(output_dir), "history.json")
            history = _read_json(hist_path, [])
            history.insert(0, entry)
            _write_json(hist_path, history[:100])
        except Exception as e:
            error = str(e)[:200]

        results.append({"msg_id": msg_id, "text": text[:80], "audio": f"{audio_id}.wav", "ok": ok, "error": error})
        with _batch_lock:
            _batch_state["done"] = i + 1
            _batch_state["results"] = list(results)

    with _batch_lock:
        _batch_state.update(running=False, finished_at=time.time(), stage="批量完成", current_text="")
    print(f"[tts] 批量完成: {len(results)}/{len(msgs)} 条")


def get_batch_status() -> dict:
    with _batch_lock:
        return {"ok": True, "state": dict(_batch_state)}
