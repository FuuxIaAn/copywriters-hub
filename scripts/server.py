# -*- coding: utf-8 -*-
"""
口播文稿专家群聊 · 实时 Web 服务
=================================
真微信群体验：打开页面 -> 把口播文稿粘贴进输入框 -> 点发送
-> 各位专家在群里实时"打字"发言（三轮讨论 + 各自终稿）。

架构:
  POST /api/start       接收文稿, 启动后台讨论线程, 返回 session id
  GET  /api/stream/<sid> SSE 长连接, 实时推送专家发言
  GET  /                 群聊页面

用法:
  python scripts/server.py
  浏览器打开 http://127.0.0.1:8765
"""
import datetime
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import urlparse

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(_SCRIPTS_DIR)


def _default_data_dir() -> str:
    """返回用户可写数据目录，避免直接运行 server 时污染源码目录。"""
    appdata = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or os.path.expanduser("~")
    )
    return os.path.join(appdata, "靓仔文案工作台")


# desktop_app 会在导入前设置该变量；直接运行此服务时也使用同一安全默认值。
os.environ.setdefault("WB_DATA_DIR", _default_data_dir())
sys.path.insert(0, _SCRIPTS_DIR)

from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402
from openai import OpenAI  # noqa: E402

import stats_store  # noqa: E402
import learn_store  # noqa: E402
import works_store  # noqa: E402
import skeleton_store  # noqa: E402
import data_insight_store  # noqa: E402
import skill_methods  # noqa: E402
import data_import  # noqa: E402
import monitor_server  # noqa: E402
import radar_server  # noqa: E402
import tts_server  # noqa: E402
import extract_server  # noqa: E402
import agent_chat_server  # noqa: E402
import rewrite_store  # noqa: E402
import rewrite_flow  # noqa: E402
import notify_store  # noqa: E402
import export_utils  # noqa: E402
import weekly_report  # noqa: E402
import asr_server  # noqa: E402
import api_settings_server  # noqa: E402
import works_library_server  # noqa: E402
from agents import Agent  # noqa: E402
from discussion import run_discussion_stream, save_output  # noqa: E402
from knowledge_loader import load_knowledge_dir  # noqa: E402
from render_chat import AVATAR_COLORS, DEFAULT_COLOR, _strip_name_prefix, render_md  # noqa: E402

# 可写数据目录：打包成 exe 后 BASE_DIR 是只读解压目录，
# 通过 WB_DATA_DIR 把 output / lessons 重定向到用户可写位置（默认 AppData）。
DATA_DIR = os.environ["WB_DATA_DIR"]
WEB_DIR = os.path.join(BASE_DIR, "web")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
DIGEST_DIR = os.path.join(DATA_DIR, "knowledge_digests")

app = Flask(__name__, static_folder=WEB_DIR)
# 本地桌面应用也应限制单次上传大小，避免误拖超大视频/音频导致进程失稳。
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# 模块级加载手动配置的微博 cookie（桌面/CLI 两种启动方式都会走到这里，
# 供作品库/对标监控抓微博主页使用；works_library_server 依赖已 import）
try:
    works_library_server.load_weibo_cookie(OUTPUT_DIR)
except Exception as e:  # noqa: BLE001
    print(f"  [weibo] 微博 Cookie 加载跳过：{e}")


@app.after_request
def _disable_cache(resp):
    """桌面端每次启动都强制拿最新前端资源，避免 webview 命中旧页面脚本。"""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
MAX_SCRIPT_CHARS = 100_000
MAX_EXTRACT_TEXT_CHARS = 200_000
_EXTRACT_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com")


def _friendly_error_text(value) -> str:
    """把底层网络异常转换为用户可执行的提示，避免泄露冗长 SSL 堆栈。"""
    text = str(value or "")
    lowered = text.lower()
    if any(k in lowered for k in (
        "_ssl.c", "ssl", "handshake", "timed out", "timeout",
        "connection reset", "connection aborted", "connection refused",
        "eof", "network is unreachable",
    )):
        return "网络连接模型服务超时，请检查网络/代理后点击重试"
    return text[:500]


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"ok": False, "error": "上传文件超过 50MB 上限，请先压缩或裁剪后重试"}), 413


@app.errorhandler(Exception)
def _unhandled_error(error):
    """任何接口未捕获异常都返回 JSON（前端能展示真实原因），
    并把完整堆栈写入 logs/server_error.log，避免 exe 无控制台时排障无据。"""
    try:
        log_dir = os.path.join(DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "server_error.log"), "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                    f"{request.method} {request.path} ---\n")
            f.write("".join(traceback.format_exception(error)) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": False, "error": f"服务内部错误：{error}"}), 500


def _valid_extract_url(value: str) -> bool:
    """仅接受抖音分享链接，避免本地服务成为任意地址下载器。"""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and any(
            host == suffix or host.endswith("." + suffix)
            for suffix in _EXTRACT_HOST_SUFFIXES
        )
    except ValueError:
        return False


def _member_model(api: dict, member_cfg: dict) -> str:
    """成员的模型：per-member 配置优先，缺省回退 api.model。
    config.json 的 model_pool 里可定义可用模型说明（label/trait）。"""
    return (member_cfg or {}).get("model") or api.get("model") or "deepseek-chat"


def _make_client(provider_cfg: dict) -> "OpenAI":
    """从 provider 配置创建 OpenAI 客户端。"""
    from openai import OpenAI
    return OpenAI(
        base_url=provider_cfg["base_url"],
        api_key=provider_cfg["api_key"],
        timeout=180.0,
    )


def _resolve_member_provider(config: dict, model: str) -> dict:
    """根据模型名前缀决定用哪个 provider。
    - deepseek-* → config.deepseek（若已配置 base_url + api_key）
    - 其他 → config.api
    这样主 API 走 DeepRouter 跑 gpt-5.4-high，专家走 DeepSeek 跑 deepseek-chat。"""
    if model.startswith("deepseek-"):
        ds = config.get("deepseek") or {}
        if ds.get("base_url") and ds.get("api_key"):
            return ds
    return config["api"]


def _merge_config(base, override):
    """合并用户配置覆盖层：空值不覆盖内置默认值，避免半截 config 让应用失效。"""
    if not isinstance(base, dict):
        return override if isinstance(override, dict) else base
    result = dict(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config(result[key], value)
        elif value not in (None, ""):
            result[key] = value
    return result


# ---------- 配置与 Agent 构建（与 main.py 一致） ----------

def load_config() -> dict:
    # 内置配置永远是完整基线。用户目录只保存覆盖项，且空字段不能把基线抹掉。
    base_path = os.path.join(BASE_DIR, "config.json")
    with open(base_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    override = os.path.join(DATA_DIR, "config.json")
    if DATA_DIR != BASE_DIR and os.path.exists(override):
        try:
            with open(override, "r", encoding="utf-8") as f:
                config = _merge_config(config, json.load(f))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[server] 忽略损坏的用户配置，已使用内置默认配置: {exc}")
    # 生产部署可通过环境变量注入密钥，避免把凭据写入源码或发布包。
    env_key = os.environ.get("WB_LLM_API_KEY", "").strip()
    if env_key:
        config.setdefault("api", {})["api_key"] = env_key
    return config


def build_agents(config: dict) -> list:
    api = config["api"]
    max_chars = config["discussion"].get("max_context_chars", 20000)
    stats = stats_store.load_stats(OUTPUT_DIR)
    ctx = config.get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
    methods = skill_methods.load_methods_text(BASE_DIR, config)
    agents = []
    for acfg in config["agents"]:
        digest_rel = acfg.get("digest_path") or ""
        digest_path = os.path.join(BASE_DIR, digest_rel) if digest_rel else ""
        if digest_path and os.path.exists(digest_path):
            with open(digest_path, "r", encoding="utf-8") as f:
                knowledge = f.read()
            knowledge_source = "深度研读内化的个人知识档案"
        else:
            raw_path = acfg.get("knowledge_path") or ""
            kb_path = raw_path if os.path.isabs(raw_path) else os.path.join(BASE_DIR, raw_path)
            knowledge = load_knowledge_dir(kb_path, max_chars)
            knowledge_source = "原始知识库（尚未做知识内化，建议先运行 knowledge_distill.py）"
        feedback = stats_store.feedback_archive_text(stats, acfg["name"])
        principles = data_insight_store.principles_text(OUTPUT_DIR)
        if principles:
            feedback = principles + ("\n\n" + feedback if feedback else "")
        lessons = learn_store.lessons_text(DIGEST_DIR, acfg["id"], 6000)
        model = _member_model(api, acfg)
        client = _make_client(_resolve_member_provider(config, model))
        agents.append(
            Agent(
                cfg=acfg,
                knowledge=knowledge,
                client=client,
                model=model,
                temperature=api["temperature"],
                knowledge_source=knowledge_source,
                feedback=feedback,
                lessons=lessons,
                context=context,
                methods=methods,
            )
        )
    return agents


def build_single_agent(config: dict, agent_name: str):
    """构建指定名称的单个 agent（用于评论迭代）。"""
    api = config["api"]
    max_chars = config["discussion"].get("max_context_chars", 20000)
    stats = stats_store.load_stats(OUTPUT_DIR)
    ctx = config.get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
    methods = skill_methods.load_methods_text(BASE_DIR, config)
    for acfg in config["agents"]:
        if acfg["name"] == agent_name:
            digest_rel = acfg.get("digest_path") or ""
            digest_path = os.path.join(BASE_DIR, digest_rel) if digest_rel else ""
            if digest_path and os.path.exists(digest_path):
                with open(digest_path, "r", encoding="utf-8") as f:
                    knowledge = f.read()
                knowledge_source = "深度研读内化的个人知识档案"
            else:
                raw_path = acfg.get("knowledge_path") or ""
                kb_path = raw_path if os.path.isabs(raw_path) else os.path.join(BASE_DIR, raw_path)
                knowledge = load_knowledge_dir(kb_path, max_chars)
                knowledge_source = "原始知识库"
            feedback = stats_store.feedback_archive_text(stats, acfg["name"])
            principles = data_insight_store.principles_text(OUTPUT_DIR)
            if principles:
                feedback = principles + ("\n\n" + feedback if feedback else "")
            lessons = learn_store.lessons_text(DIGEST_DIR, acfg["id"], 6000)
            model = _member_model(api, acfg)
            client = _make_client(_resolve_member_provider(config, model))
            return Agent(
                cfg=acfg, knowledge=knowledge, client=client,
                model=model, temperature=api["temperature"],
                knowledge_source=knowledge_source, feedback=feedback,
                lessons=lessons, context=context, methods=methods,
            )
    return None


# ---------- 会话管理 ----------

class Session:
    def __init__(self):
        self.queue = queue.Queue()
        self.history = []          # 已推送的全部事件（含 seq）
        self.members = []          # [{name,title,color}]
        self.finished = False
        self.lock = threading.Lock()
        self.seq = 0
        self.ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.script = ""
        self.md_path = ""          # 讨论记录 Markdown 存档路径（复盘时读取）
        self.adoptions = []        # 用户采纳记录 [{name,round,snippet,note,time}]
        self.merges = []           # 段落级采纳合并 [{para_idx, original, rewritten, agent_name, note, time}]
        self.work_id = ""          # 绑定的作品 id（口播工坊：讨论隶属于某个作品）
        self.work_title = ""       # 作品标题
        self.created_ts = time.time()
        self.phase = "idle"        # idle/discussion/score/review/learn（同一时刻只允许一个后台任务）
        self.phase_lock = threading.Lock()
        self.sid = ""              # 会话 id（创建后由 api_start 赋值）
        self.frozen = False        # True=从磁盘重建的「只读回顾」会话（无运行中的讨论线程）
        self.interrupted = False   # True=上次讨论进行中被中断（应用关闭），已恢复至中断点
        self.rw = {}               # 洗稿工坊会话专属状态（rid/original/metrics/requirements/status）

    def try_begin(self, phase: str) -> bool:
        """尝试占用会话执行指定任务；已有其他任务在跑则拒绝。"""
        with self.phase_lock:
            if self.phase not in ("idle",) and self.phase != phase:
                return False
            self.phase = phase
            self.finished = False   # 新任务开始即重开会话：SSE 保持接收，任务可接力（评分/复盘/留存…）
            return True

    def end_phase(self):
        with self.phase_lock:
            self.phase = "idle"

    def push(self, raw: dict):
        """推送事件：记录到 history + 入队（供 SSE 广播）。"""
        item = dict(raw)
        if item.get("type") == "error":
            item["text"] = _friendly_error_text(item.get("text"))
        if item.get("type") in ("message", "final", "review", "learn", "score", "retention", "score_roast", "debate", "principle_review"):
            name = item.get("name", "")
            item["html"] = render_md(_strip_name_prefix(item.get("text", ""), name))
        with self.lock:
            self.seq += 1
            item["seq"] = self.seq
            self.history.append(item)
        self.queue.put(item)
        _persist_session(self)


SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

SESSIONS_DIR = os.path.join(OUTPUT_DIR, "sessions")


def _ensure_members(session: "Session"):
    """给会话注入群成员信息（从配置读取，不重复构建 agents）。"""
    try:
        config = load_config()
        api = config.get("api") or {}
        for acfg in config["agents"]:
            session.members.append({
                "name": acfg["name"],
                "title": acfg["title"],
                "color": AVATAR_COLORS.get(acfg["name"], DEFAULT_COLOR),
                "model": _member_model(api, acfg),
                "desc": (acfg.get("desc") or acfg.get("title") or ""),
            })
        rcfg = config.get("recorder") or {}
        session.members.append({
            "name": rcfg.get("name", "记录员"),
            "title": rcfg.get("title", "记录员"),
            "color": rcfg.get("color", "#64748B"),
            "model": "",
            "desc": rcfg.get("desc") or rcfg.get("title") or "",
        })
    except Exception as e:  # noqa: BLE001
        print(f"[server] 读取成员信息失败: {e}")


def _new_standalone_session() -> "Session":
    """创建一个不绑定作品、不跑讨论的独立会话（供爆款拆解等独立操作承载讨论记录）。"""
    sid = uuid.uuid4().hex[:12]
    session = Session()
    session.sid = sid
    _ensure_members(session)
    with SESSIONS_LOCK:
        SESSIONS[sid] = session
    return session


def _session_path(sid: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{sid}.json")


def _persist_session(session: "Session"):
    """把会话现场持久化到磁盘（关闭窗口/重启后「继续讨论」可完整恢复，不再重新讨论）。"""
    if not getattr(session, "sid", ""):
        return
    try:
        with session.lock:
            snapshot = {
                "sid": session.sid,
                "members": list(session.members),
                "history": list(session.history),
                "finished": session.finished,
                "script": session.script,
                "work_id": session.work_id,
                "work_title": session.work_title,
                "ts": session.ts,
                "adoptions": list(session.adoptions),
                "merges": list(session.merges),
                "rw": getattr(session, "rw", {}),
                "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = _session_path(session.sid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print(f"[server] 会话持久化失败: {e}")


def _load_session_from_disk(sid: str):
    """从磁盘重建一个「只读回顾」会话（进程重启后，讨论线程已不存在）。"""
    path = _session_path(sid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[server] 读取会话存档失败: {e}")
        return None
    s = Session()
    s.sid = sid
    s.members = data.get("members", [])
    s.history = data.get("history", [])
    s.script = data.get("script", "")
    s.work_id = data.get("work_id", "")
    s.work_title = data.get("work_title", "")
    s.ts = data.get("ts", "")
    s.adoptions = data.get("adoptions", [])
    s.merges = data.get("merges", [])
    s.rw = data.get("rw", {})
    s.seq = len(s.history)
    was_finished = bool(data.get("finished", True))
    s.finished = True            # 重建后不再有运行中的讨论线程，视为「已结束」以停止 SSE 重连
    s.frozen = True
    s.interrupted = not was_finished
    return s


def _sse(item: dict) -> str:
    return f"data: {json.dumps(item, ensure_ascii=False)}\n\n"


def _build_recorder(config: dict) -> Agent:
    """构建记录员（不参与讨论，只在采纳/复盘时被调用）。"""
    api = config["api"]
    rcfg = config.get("recorder") or {}
    if not rcfg.get("id"):
        return None
    model = _member_model(api, rcfg)
    client = _make_client(_resolve_member_provider(config, model))
    ctx = config.get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
    return Agent(
        cfg=rcfg,
        knowledge="",
        client=client,
        model=model,
        temperature=0.4,
        name_prefix=False,
        context=context,
    )


def _build_data_analyst(config: dict) -> Agent:
    """构建数据专员（独立角色，做字幕稿 + 留存曲线的句子级分析）。"""
    api = config["api"]
    dcfg = config.get("data_analyst") or {}
    if not dcfg.get("id"):
        return None
    model = _member_model(api, dcfg)
    client = _make_client(_resolve_member_provider(config, model))
    ctx = config.get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
    return Agent(
        cfg=dcfg,
        knowledge="",
        client=client,
        model=model,
        temperature=0.4,
        name_prefix=False,
        context=context,
    )


def _build_principle_reviewer(config: dict) -> Agent:
    """构建原则审查员（独立角色，讨论结束后对所有终稿做原则审查）。"""
    api = config["api"]
    rcfg = config.get("principle_reviewer") or {}
    if not rcfg.get("id"):
        return None
    model = _member_model(api, rcfg)
    client = _make_client(_resolve_member_provider(config, model))
    ctx = config.get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
    return Agent(
        cfg=rcfg,
        knowledge="",
        client=client,
        model=model,
        temperature=0.3,
        name_prefix=False,
        context=context,
    )


def _run_auto_principle_review(session: "Session", script: str, config: dict):
    """三轮讨论结束后，自动对所有专家终稿做原则审查。
    注意：不要改名为 _run_principle_review，那个名字被「原则审视」（用户反馈打破原则）占用了。"""
    # 收集所有终稿
    finals = []
    for item in session.history:
        if item.get("type") == "final":
            finals.append({
                "name": item.get("name", ""),
                "title": item.get("title", ""),
                "text": item.get("text", ""),
            })
    if not finals:
        return

    # 获取全部原则
    principles = data_insight_store.principles_text(OUTPUT_DIR, max_chars=6000)
    if not principles:
        session.push({"type": "system", "text": "🔍 原则审查员阿审：当前暂无原则，跳过终稿审查"})
        return

    reviewer = _build_principle_reviewer(config)
    if not reviewer:
        print("[server] 原则审查员未配置（config.json 缺少 principle_reviewer 配置块）")
        return

    session.push({"type": "system", "text": "🔍 原则审查员阿审正在审查各专家终稿..."})
    session.push({"type": "typing", "name": "阿审", "title": "原则审查员"})

    try:
        # 组装审查 prompt
        finals_text = "\n\n".join(
            f"### {f['name']}（{f['title']}）终稿\n{f['text']}"
            for f in finals
        )
        prompt = (
            f"以下是原稿：\n{script}\n\n"
            f"以下是各专家的终稿（段落级改写建议）：\n\n{finals_text}\n\n"
            f"以下是全部原则（你必须逐条对照审查每位专家的终稿）：\n{principles}\n\n"
            "请按照你的审查规则，对每位专家的终稿逐条审查所有原则。"
        )
        review_text = reviewer.say([{"role": "user", "content": prompt}])
        session.push({
            "type": "principle_review",
            "name": "阿审",
            "title": "原则审查员",
            "text": review_text,
        })
        print("[server] 原则审查完成")
    except Exception as e:  # noqa: BLE001
        print(f"[server] 原则审查失败: {e}")
        session.push({"type": "system", "text": f"⚠ 原则审查失败：{e}"})


def _data_standards_text(config: dict) -> str:
    """把 config 里的 data_standards 渲染成数据专员的数据认知文本。"""
    ds = config.get("data_standards") or {}
    if not ds:
        return ""
    lines = ["【平台数据标准（你判断各项数据健康等级的依据）】"]
    for key, std in ds.items():
        label = std.get("label", key)
        lines.append(f"- {label}：{std.get('desc', '')}")
        for tier_key in ("hot", "excellent", "good", "pass", "warn", "danger"):
            tier = std.get(tier_key)
            if tier:
                lines.append(f"    · {tier_key}（{tier.get('range','')}）：{tier.get('verdict','')}")
    return "\n".join(lines)



def _save_adoptions(session: Session):
    """把采纳记录持久化到 output/adoptions_<ts>.json。"""
    try:
        path = os.path.join(OUTPUT_DIR, f"adoptions_{session.ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "session_ts": session.ts,
                "script": session.script,
                "adoptions": session.adoptions,
            }, f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:  # noqa: BLE001
        print(f"[server] 保存采纳记录失败: {e}")
        return ""


def _review_prompt(md: str, adoptions: list, data: str, stats: dict, context: str = "",
                   session_ts: str = "") -> str:
    """组装复盘分析 prompt：讨论记录 + 采纳记录 + 复盘数据 + 历史统计，要求 JSON 结构化输出。"""
    ctx_block = (context or "").strip() or "（无特别说明）"
    lines = ["你正在复盘一次口播文稿的专家讨论，请基于用户提供的实际效果数据，评估每一条被采纳的改动是否正确，并提炼反馈。"]
    lines.append(f"\n【创作背景（本账号的核心信息，评估效果时要结合它判断改动是否真的适合）】\n{ctx_block}")
    lines.append("\n【讨论记录（三轮发言 + 各专家终稿）】\n" + (md[:16000] if md else "（无存档）"))
    lines.append("\n【用户采纳记录】")
    if adoptions:
        for i, a in enumerate(adoptions, 1):
            note = f"；用户备注：{a['note']}" if a.get("note") else ""
            lines.append(f"{i}. 采纳【{a['name']}·{a['round']}】的内容：{a['snippet'][:200]}{note}")
    else:
        lines.append("（暂无采纳记录）")
    lines.append(f"\n【用户提供的复盘数据】\n{data}")
    lines.append("\n【历史正确率排名（仅作参考）】\n" + stats_store.rank_text(stats))
    last_scores = (stats.get("scores") or [])
    # 优先取同一会话的最近一次评分，避免与别的会话错配
    if last_scores and session_ts:
        same = [s for s in last_scores if s.get("session_ts") == session_ts]
        last = same[-1] if same else None
    else:
        last = last_scores[-1] if last_scores else None
    if last:
        sc = "、".join(f"{s['name']} {s.get('score')}分" for s in last.get("scores", []))
        lines.append("\n【最近一次终稿评分（各位专家打的分数）】\n"
                     f"被评终稿：{last.get('script', '')}\n评分：{sc}")
    else:
        lines.append("\n【最近一次终稿评分】\n（暂无评分记录）")
    lines.append("""
【任务】逐条评估每条被采纳的改动，并**只输出一个 JSON 对象**（不要输出任何其他文字、解释或 Markdown 代码块），格式严格如下：
{
  "summary": "100字以内的整体总结论：这次采纳决策整体做得怎么样，哪些类型的建议值得继续信任",
  "items": [
    {
      "name": "专家名",
      "round": "Round 1 或 Round 3 终稿等",
      "snippet": "被采纳内容的关键句（30字以内）",
      "conclusion": "有效 或 部分有效 或 无效",
      "reason": "用复盘数据里的具体数字（完播率/转化率/播放量/互动量等）说明依据，一句话",
      "next": "保留 或 微调（具体怎么调） 或 回退",
      "feedback_positive": "如果这次改动有效：提炼成一句话正面反馈，概括该专家这次做对的做法（留给他下次保持）；无效则填空字符串",
      "feedback_negative": "如果这次改动无效或部分有效：提炼成一句话负面反馈，指出该专家下次严禁再犯的具体做法；有效则填空字符串"
    }
  ],
  "actual_score": "根据实际数据给这份终稿的整体表现打一个满分10分的分数（允许小数），数据不足以打分则填 null",
  "score_accuracy": "如果存在最近一次评分记录：对比各专家打分与实际分的偏差，用一句话说谁评分最准、谁最不准；没有评分记录则填空字符串"
}
注意：conclusion 必须精确是「有效」「部分有效」「无效」三者之一；数据不足时在 reason 里明确说"现有数据不足以判断"，绝不编造因果。""")
    return "\n".join(lines)


def _parse_review_json(text: str) -> dict:
    """容错解析阿记返回的 JSON。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t).strip()
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            pass
    return None


def _parse_learn_json(text: str) -> dict:
    """容错解析专家学习输出（{items:[{quote,point,apply}]}）。

    三级容错：整体 JSON → 花括号提取 → 正则逐条提取。
    DeepSeek 偶发会在 JSON 字符串里混入未转义引号导致整体解析失败，
    正则逐条提取能把结构完整的条目抢救回来（宁缺毋滥：坏条目直接放弃）。
    """
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t).strip()
    t = re.sub(r"\s*```$", "", t).strip()
    # 1) 整体 JSON
    try:
        d = json.loads(t)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:  # noqa: BLE001
        pass
    # 2) 花括号提取
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                return d
        except Exception:  # noqa: BLE001
            pass
    # 3) 正则逐条提取（prompt 强制 quote→point→apply 顺序）
    pat = re.compile(r'"quote"\s*:\s*"(.*?)"\s*,\s*"point"\s*:\s*"(.*?)"\s*,\s*"apply"\s*:\s*"(.*?)"', re.S)
    items = [{"quote": m.group(1), "point": m.group(2), "apply": m.group(3)}
             for m in pat.finditer(t)]
    return {"items": items} if items else None


_VERDICT_ICON = {"有效": "✅", "部分有效": "⚠️", "无效": "❌"}


def _build_review_report(parsed: dict, res: dict, stats: dict) -> str:
    """把结构化复盘结果渲染成美观的 Markdown 报告。"""
    items = parsed.get("items") or []
    lines = [f"📊 **复盘报告** · 共评估 {len(items)} 条采纳"]
    if not items:
        lines.append("\n（本批次没有可评估的采纳记录）")
    for i, v in enumerate(items, 1):
        conclusion = v.get("conclusion", "无效")
        icon = _VERDICT_ICON.get(conclusion, "❓")
        name = v.get("name", "?")
        rnd = v.get("round", "")
        lines.append(f"\n{i}. {icon} **{name}**（{rnd}）· {conclusion}")
        lines.append(f"- 采纳内容：{v.get('snippet', '')}")
        if v.get("reason"):
            lines.append(f"- 依据：{v['reason']}")
        if v.get("next"):
            lines.append(f"- 下一步：{v['next']}")
        if v.get("feedback_negative"):
            lines.append(f"- 📌 负面反馈已写入 {name} 档案：{v['feedback_negative']}")
        if v.get("feedback_positive"):
            lines.append(f"- 👍 正面反馈已写入 {name} 档案：{v['feedback_positive']}")

    per = res.get("per_expert") or {}
    if per:
        lines.append("\n---\n\n📈 **历史正确率排名**（有效=1分 / 部分有效=0.5分 / 无效=0分）")
        lines.append(stats_store.rank_text(stats))

    acc = stats_store.score_accuracy_text(stats)
    if acc and "暂无" not in acc:
        lines.append("\n🎯 **评分准确性排名**")
        lines.append(acc)

    if parsed.get("summary"):
        lines.append(f"\n---\n\n🗣 **总结论**\n{parsed['summary']}")
    return "\n".join(lines)


def _feedback_summary(verdicts: list) -> str:
    """按专家汇总本次写入的正/负反馈，生成一条系统提示。"""
    by = {}
    for v in verdicts or []:
        name = (v.get("name") or "").strip()
        if not name:
            continue
        fb = by.setdefault(name, [])
        if v.get("feedback_negative"):
            fb.append(f"负面：{v['feedback_negative']}")
        if v.get("feedback_positive"):
            fb.append(f"正面：{v['feedback_positive']}")
    if not by:
        return ""
    parts = ["📁 反馈档案已更新（下次讨论自动生效）："]
    for name, fbs in by.items():
        parts.append(f"- {name}：{'；'.join(fbs)}")
    return "\n".join(parts)


def _run_review(sid: str, data: str):
    """后台线程：记录员基于采纳记录 + 复盘数据生成评估报告、提炼反馈、更新统计并推送。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("review"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发复盘。"})
        session.push({"type": "review_done"})
        return

    def push_typing():
        session.push({"type": "typing", "name": "阿记", "title": "记录员"})

    try:
        config = load_config()
        recorder = _build_recorder(config)
        if recorder is None:
            session.push({"type": "review", "name": "阿记", "title": "记录员",
                          "text": "⚠ 记录员未配置（config.json 缺少 recorder 配置块）。"})
            session.push({"type": "review_done"})
            return

        md = ""
        if session.md_path and os.path.exists(session.md_path):
            with open(session.md_path, "r", encoding="utf-8") as f:
                md = f.read()

        stats = stats_store.load_stats(OUTPUT_DIR)
        push_typing()
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
        prompt = _review_prompt(md, session.adoptions, data, stats, context, session.ts)
        report = recorder.say([{"role": "user", "content": prompt}])
        # 兜底：去掉可能残留的角色名前缀
        for prefix in ("角色名：阿记", "角色名: 阿记", "阿记：", "阿记:"):
            if report.startswith(prefix):
                report = report[len(prefix):].strip()
                break
        parsed = _parse_review_json(report)
        if parsed:
            verdicts = parsed.get("items") or []
            actual = parsed.get("actual_score")

            def _apply(s):
                res = stats_store.apply_verdicts(s, verdicts, session.ts)
                if actual is not None:
                    try:
                        stats_store.update_score_accuracy(s, float(actual), session.ts)
                    except (TypeError, ValueError):  # noqa: BLE001
                        pass
                return res

            stats, res = stats_store.update_stats(OUTPUT_DIR, _apply)
            # 口播工坊：复盘结论 + 效果数据落库到作品，状态自动推进为「已复盘」
            wid = session.work_id
            if wid and works_store.get(OUTPUT_DIR, wid):
                works_store.set_review(OUTPUT_DIR, wid,
                                       getattr(session, "review_metrics", None) or {},
                                       actual, parsed.get("summary") or "")
            session.push({"type": "review", "name": "阿记", "title": "记录员",
                          "text": _build_review_report(parsed, res, stats)})
            fb = _feedback_summary(verdicts)
            if fb:
                session.push({"type": "system", "text": fb})
        else:
            session.push({"type": "review", "name": "阿记", "title": "记录员",
                          "text": report + "\n\n（本次复盘未能结构化解析，反馈档案未更新；可重试）"})
    except Exception as e:  # noqa: BLE001
        print(f"[server] 复盘分析异常: {e}")
        session.push({"type": "review", "name": "阿记", "title": "记录员",
                      "text": f"⚠ 复盘分析失败：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "review_done"})
        print(f"[server] session {sid} 复盘完成")


# ---------- 数据专员：字幕稿 + 留存曲线句子级分析 ----------

def _retention_prompt(subtitle: str, retention: str, metrics: dict, adoptions: list,
                      context: str = "", standards: str = "", history_blacklist: str = "") -> str:
    """组装数据专员句子级留存分析 prompt。metrics: 各项数据指标字典；adoptions: 采纳记录。
    history_blacklist: 历史黑榜（同类句子记忆，用于对照判断有没有改善）。"""
    ctx_block = (context or "").strip() or "（无特别说明）"
    # 各项数据指标
    metric_lines = []
    if metrics:
        for k, v in metrics.items():
            if v is None or v == "":
                continue
            metric_lines.append(f"- {k}：{v}")
    metrics_text = "\n".join(metric_lines) if metric_lines else "（用户未提供具体数据指标）"

    # 采纳记录（用于判断黑榜句子是不是采纳了某位专家的建议）
    adopt_lines = []
    for i, a in enumerate(adoptions or [], 1):
        adopt_lines.append(f"{i}. {a.get('name','')}（{a.get('round','')}）：{a.get('snippet','')}")
    adopt_text = "\n".join(adopt_lines) if adopt_lines else "（暂无采纳记录）"

    standards_block = ("\n\n" + standards) if standards else ""

    history_block = ""
    if history_blacklist:
        history_block = (
            "五、历史黑榜（同类句子记忆 · 你过去判定过的问题句子和当时的改写方向）。"
            "本次字幕稿里如果出现了**同类型的句子**，必须对照这条历史结论："
            "这次这类句子有没有用上「上次改写方向」？留存表现相比上次有没有改善？"
            "把「同类句子改善追踪」作为第五部分输出。\n\n"
            "【历史黑榜开始】\n" + history_blacklist + "\n【历史黑榜结束】\n\n"
        )

    return (
        "创作背景（本账号的核心信息，分析留存表现时要结合目标受众判断）：\n"
        f"{ctx_block}\n\n"
        "【字数红线】整体文案应控制在 600 字以内（含标题）。\n\n"
        "这是用户用终稿口播后得到的真实数据反馈。你的最终目标：**找出哪些句子导致留存流失、并定位到是否某位专家的建议导致的**，从而帮助提高播放量。\n\n"
        "一、口播视频字幕稿（逐句，可能带时间戳，格式如「00:00:01,500 --> 00:00:02,133 放屁」——即「第几秒说了什么话」，请结合时间戳对照留存曲线定位是哪一秒、哪句话掉的留存）：\n\n"
        "【字幕稿开始】\n" + subtitle + "\n【字幕稿结束】\n\n"
        "二、观众留存数据（可能是数据点「0秒100%、5秒85%…」，也可能是趋势描述；若同时给了「同类作品留存曲线」，它就是**对标基准**，要拿来和「我的留存曲线」逐秒对比，判断哪一秒掉得比同类更狠）：\n\n"
        "【留存数据开始】\n" + (retention or "（用户未提供具体留存数据）") + "\n【留存数据结束】\n\n"
        "三、各项数据指标（2秒跳出率/5秒完播率/平均播放时长/平均播放占比/完播率/播放量等）：\n\n"
        "【数据指标开始】\n" + metrics_text + "\n【数据指标结束】\n\n"
        "四、本次终稿的采纳记录（判断黑榜句子是不是采纳了某位专家建议的依据）：\n\n"
        "【采纳记录开始】\n" + adopt_text + "\n【采纳记录结束】\n\n"
        + history_block
        + standards_block + "\n\n"
        "请按你 persona 里的三层职责输出：\n"
        "一、数据判级：逐项数据 → 健康等级（优秀/良好/预警/危险）→ 一句话结论\n"
        "二、播放量归因：这条视频播放量高/低，主要是哪个数据指标导致的，为什么\n"
        "三、句子级黑榜：按留存掉得最厉害排序，每句【第N句】/【原文】/【留存表现】/【诊断】/【是否某专家建议导致】/【改写方向】\n"
        "四、给专家的原则性建议：2-3 条「下次写这类句子必须遵守的原则」\n"
        + ("五、同类句子改善追踪：对照历史黑榜，指出本次出现的同类句子有没有改善（每句一句：改善了/没改善/不适用）\n" if history_block else "")
        + "数据不足时明确说「现有数据不足以判断」，绝不编造具体留存数字。"
    )


def _parse_blacklist(report: str) -> list:
    """从数据专员报告中容错提取黑榜句子（句子级），用于落盘。"""
    items = []
    # 只取「三、句子级黑榜」部分（截断到「四、」之前，避免越界吞掉原则）
    m_sec = re.search(r"(?:三|3)[、.．]\s*句子级黑榜\s*[:：]?\s*([\s\S]*?)(?=(?:四|4)[、.．]|\Z)", report)
    section = m_sec.group(1) if m_sec else report
    blocks = re.split(r"(?=【第\d+句】)", section)
    for blk in blocks:
        if not blk.strip().startswith("【第"):
            continue
        def _grab(tag):
            m = re.search(tag + r"\s*([^\n【]*(\n(?!【)[^\n【]*)*)", blk)
            return m.group(1).strip() if m else ""
        original = _grab("【原文】")
        diagnosis = _grab("【诊断】")
        agent_hit = _grab("【是否某专家建议导致】")
        rewrite = _grab("【改写方向】")
        if not original:
            continue
        # 从「是否某专家建议导致」里提取专家名（只要提到专家名就提取）
        agent = ""
        if agent_hit:
            m = re.search(r"(阿沁|老周|阿爆|小黄|爆哥|阿骨|阿导|阿证|阿数)", agent_hit)
            agent = m.group(1) if m else ""
        items.append({
            "sentence": original[:200],
            "agent": agent,
            "reason": diagnosis[:300],
            "rewrite": rewrite[:300],
        })
    return items


def _parse_principles(report: str) -> list:
    """从数据专员报告中提取原则性建议（第四部分或「原则性建议」标题后）。"""
    principles = []
    # 定位「原则性建议」之后的内容
    m = re.search(r"原则性建议\s*[:：]?\s*([\s\S]*)$", report)
    section = m.group(1) if m else ""
    if not section:
        return principles
    # 按行提取，去掉编号前缀，过滤过短行和标题行
    for line in section.splitlines():
        line = line.strip()
        if not line or line.startswith("【"):
            continue
        line = re.sub(r"^[0-9一二三四五六七八九十]+[、.．）)]\s*", "", line)
        line = re.sub(r"^[-•·]\s*", "", line)
        line = line.strip()
        if len(line) >= 4:
            principles.append(line[:200])
        if len(principles) >= 5:
            break
    return principles


def _parse_principle_actions(report: str) -> list:
    """从原则审视报告末尾的「处置清单」解析待处置建议。返回 [{action, old_text, new_text}]。"""
    actions = []
    m = re.search(r"处置清单\s*[:：]?\s*([\s\S]*)$", report)
    section = m.group(1) if m else ""
    if not section:
        return actions
    for line in section.splitlines():
        line = line.strip().strip("|").strip()
        if not line:
            continue
        if line in ("无", "无。", "无处置", "无需处置"):
            break
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        action = parts[0]
        old_text = parts[1]
        new_text = parts[2] if len(parts) > 2 else ""
        if action in ("修正", "fix", "FIX") and new_text:
            actions.append({"action": "fix", "old_text": old_text, "new_text": new_text})
        elif action in ("废除", "remove", "REMOVE", "删除") and old_text:
            actions.append({"action": "remove", "old_text": old_text, "new_text": ""})
    return actions


def _analyze_retention(subtitle, retention, metrics, adoptions, context, standards, video_title):
    """运行数据专员留存分析并落盘黑榜/原则/归因。返回 (report, blacklist, principles)。失败返回 (None, [], [])。"""
    config = load_config()
    analyst = _build_data_analyst(config)
    if analyst is None:
        return None, [], []
    # 历史黑榜 → 同类句子记忆（强关联：识别同类型句子并对照上次结论）
    history_blacklist = data_insight_store.blacklist_text(OUTPUT_DIR, max_chars=3000)
    prompt = _retention_prompt(subtitle, retention, metrics or {}, adoptions, context, standards,
                               history_blacklist=history_blacklist)
    report = analyst.say([{"role": "user", "content": prompt}])
    for prefix in (f"{analyst.name}：", f"{analyst.name}:"):
        if report.startswith(prefix):
            report = report[len(prefix):].strip()
            break
    blacklist = _parse_blacklist(report)
    principles = _parse_principles(report)
    if blacklist:
        data_insight_store.add_blacklist(OUTPUT_DIR, blacklist)
    if principles:
        data_insight_store.add_principles(OUTPUT_DIR, principles)
    plays = ""
    if metrics:
        for k, v in metrics.items():
            if "播放" in str(k) and v not in (None, ""):
                plays = str(v)
                break
    attribution = {"video_title": video_title or "未命名作品", "plays": plays,
                   "key_metric": "", "analysis": ""}
    m2 = re.search(r"(?:二|2)[、.．]\s*播放量归因\s*[:：]?\s*([\s\S]*?)(?=(?:三|3)[、.．]|\Z)", report)
    if m2:
        attribution["key_metric"] = m2.group(1).strip()[:200]
        attribution["analysis"] = m2.group(1).strip()[:500]
    if attribution["key_metric"] or attribution["analysis"]:
        data_insight_store.add_attribution(OUTPUT_DIR, attribution)
    # 同类句子改善追踪落盘
    for t in _parse_tracks(report):
        data_insight_store.add_track(OUTPUT_DIR, t)
    return report, blacklist, principles


def _parse_tracks(report: str) -> list:
    """从报告「五、同类句子改善追踪」部分提取跟踪记录，落盘用于闭环。"""
    m = re.search(r"(?:五|5)[、.．]\s*同类句子改善追踪\s*[:：]?\s*([\s\S]*?)(?=\n(?:一|二|三|四)[、.．]|\Z)", report)
    section = (m.group(1) if m else "").strip()
    if not section:
        return []
    tracks = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-•·\d]+[、.．）)]\s*", "", line).strip()
        if len(line) >= 4:
            tracks.append({"sentence_type": line[:100], "result": "对照历史黑榜", "old_method": "", "new_method": ""})
        if len(tracks) >= 10:
            break
    return tracks


def _adoptions_from_work_or_session(wid, session):
    """采纳记录：优先从作品库取，兜底从会话取。"""
    adoptions = []
    if wid:
        w = works_store.get(OUTPUT_DIR, wid)
        if w:
            adoptions = [{"name": a.get("name", ""), "round": a.get("round", ""),
                          "snippet": a.get("snippet", "")}
                         for a in (w.get("adoptions") or []) if not a.get("revoked")]
    if not adoptions and session is not None:
        adoptions = [{"name": a.get("name", ""), "round": a.get("round", ""),
                      "snippet": a.get("snippet", "")} for a in (session.adoptions or [])]
    return adoptions


def _retention_tail(blacklist, principles):
    if blacklist or principles:
        return ("\n\n---\n✅ 已自动落盘："
                + (f"黑榜 {len(blacklist)} 条" if blacklist else "")
                + (f"、原则性建议 {len(principles)} 条" if principles else "")
                + "。可点「🔔 黑榜讨论」让专家继续讨论。")
    return ""


def _run_retention(sid: str, subtitle: str, retention: str, metrics: dict):
    """后台线程：数据专员做句子级留存分析 + 数据判级 + 播放量归因，并落盘黑榜/原则。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("retention"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再做留存分析。"})
        session.push({"type": "retention_done"})
        return
    try:
        config = load_config()
        analyst = _build_data_analyst(config)
        if analyst is None:
            session.push({"type": "retention", "name": "阿数", "title": "数据专员",
                          "text": "⚠ 数据专员未配置（config.json 缺少 data_analyst 配置块）。"})
            session.push({"type": "retention_done"})
            return
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
        standards = _data_standards_text(config)
        adoptions = _adoptions_from_work_or_session(session.work_id, session)
        wid = session.work_id
        # 数据指标落库到作品，供「数据榜单」统计（补全中英文键）
        if wid and metrics:
            works_store.save_metrics(OUTPUT_DIR, wid, data_import.normalize_metrics(metrics))
        session.push({"type": "typing", "name": analyst.name, "title": analyst.title})
        report, blacklist, principles = _analyze_retention(
            subtitle, retention, metrics or {}, adoptions, context, standards,
            session.work_title or "未命名作品")
        if report is None:
            session.push({"type": "retention", "name": "阿数", "title": "数据专员",
                          "text": "⚠ 数据专员未配置，无法分析。"})
            session.push({"type": "retention_done"})
            return
        if wid:
            def _r(x):
                x["retention_report"] = report
            works_store.update_work(OUTPUT_DIR, wid, _r)
        session.push({"type": "retention", "name": analyst.name, "title": analyst.title,
                      "text": report + _retention_tail(blacklist, principles)})
    except Exception as e:  # noqa: BLE001
        print(f"[server] 留存分析异常: {e}")
        session.push({"type": "error", "text": f"留存分析出错：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "retention_done"})
        print(f"[server] session {sid} 留存分析完成")


def _run_import_analysis(wid: str):
    """文件导入后自动触发：数据专员直接开干，结果存到作品 + 落盘洞察；有活跃会话则推送。"""
    w = works_store.get(OUTPUT_DIR, wid)
    if not w:
        return
    subtitle = (w.get("subtitle") or "").strip()
    if not subtitle:
        return
    # 标记「分析中」
    def _begin(x):
        x["analyzing"] = True
        x["retention_analyzed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    works_store.update_work(OUTPUT_DIR, wid, _begin)
    try:
        config = load_config()
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
        standards = _data_standards_text(config)
        retention = (w.get("retention") or "").strip()
        metrics = {k: v for k, v in (w.get("metrics") or {}).items() if v not in (None, "")}
        adoptions = _adoptions_from_work_or_session(wid, None)
        report, blacklist, principles = _analyze_retention(
            subtitle, retention, metrics, adoptions, context, standards,
            w.get("title", "未命名作品"))
        if report is None:
            def _fail(x):
                x["analyzing"] = False
            works_store.update_work(OUTPUT_DIR, wid, _fail)
            return
        def _done(x):
            x["retention_report"] = report
            x["analyzing"] = False
        works_store.update_work(OUTPUT_DIR, wid, _done)
        # 若作品绑定的会话仍在且已结束，把结果推送到群里（避免打断进行中的讨论）
        sid = w.get("session_id")
        session = SESSIONS.get(sid) if sid else None
        if session and not getattr(session, "frozen", False) and session.finished:
            session.push({"type": "retention", "name": "阿数", "title": "数据专员",
                          "text": report + _retention_tail(blacklist, principles)})
            session.push({"type": "retention_done"})
        print(f"[server] 作品 {wid} 数据专员自动分析完成")
    except Exception as e:  # noqa: BLE001
        print(f"[server] 自动留存分析异常: {e}")
        def _err(x):
            x["analyzing"] = False
        works_store.update_work(OUTPUT_DIR, wid, _err)


# ---------- 黑榜反馈专家群讨论 + 投票表决 + 数据专员跟踪 ----------

def _debate_prompt(agent: Agent, blacklist_text: str, context: str = "") -> str:
    """让某位专家针对黑榜句子讨论：为什么掉留存、下次这类句子怎么改。"""
    return (
        f"你是「{agent.name}」，{agent.title}。数据专员刚做完一次真实留存分析，"
        f"发现下面这些句子导致了明显的留存流失（已列入黑榜）：\n\n"
        f"{blacklist_text}\n\n"
        f"请你以你的专业视角，针对这些黑榜句子做两件事：\n"
        f"1.【为什么掉】这些句子为什么会导致观众划走？结合你的专业领域指出病根。\n"
        f"2.【怎么改】下次遇到这类句子，具体应该怎么写？给出可执行的新写法（【改写】短格式，至少 1-2 条）。\n"
        f"注意：不要客套，直接给方法；整体文案控制在 600 字以内。"
    )


def _vote_prompt(agent: Agent, others_plans: str, blacklist_text: str, context: str = "") -> str:
    """让每位专家对其他专家的「怎么改」方案投票表决。"""
    return (
        f"你是「{agent.name}」，{agent.title}。针对黑榜句子，各位专家提出了以下改进方案：\n\n"
        f"{others_plans}\n\n"
        f"请投票表决：选出你认为最有效、最该被采纳的 1-2 条方案，"
        f"并说明为什么。最后补充一条你自己坚持的补充方案（如果别人已经说到了，就明确表示认同并投它）。"
    )


def _run_debate(sid: str):
    """后台线程：黑榜反馈到专家群 → 专家讨论为什么/怎么改 → 投票表决 → 数据专员总结提炼原则。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("debate"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发起讨论。"})
        session.push({"type": "debate_done"})
        return
    try:
        config = load_config()
        agents = build_agents(config)
        analyst = _build_data_analyst(config)
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()

        blacklist_text = data_insight_store.blacklist_text(OUTPUT_DIR)
        if not blacklist_text:
            session.push({"type": "system", "text": "⚠ 暂无黑榜数据，请先做一次「留存分析」让数据专员生成黑榜。"})
            session.push({"type": "debate_done"})
            return

        # 阶段1：把黑榜反馈到专家群，逐位专家讨论「为什么掉 + 怎么改」
        session.push({"type": "system", "text": "🔔 数据专员反馈黑榜到专家群，开始讨论留存流失句子…"})
        session.push({"type": "debate", "name": "阿数", "title": "数据专员", "text": blacklist_text})
        plans = {}
        for a in agents:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            text = a.say([{"role": "user", "content": _debate_prompt(a, blacklist_text, context)}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            plans[a.name] = text
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": text})

        # 阶段2：投票表决
        session.push({"type": "system", "text": "🗳️ 开始投票表决，各位专家选出最该采纳的方案…"})
        others_plans = "\n\n".join([f"【{n}】{t}" for n, t in plans.items()])
        votes = []
        for a in agents:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            others = "\n\n".join([f"【{n}】{t}" for n, t in plans.items() if n != a.name])
            text = a.say([{"role": "user", "content": _vote_prompt(a, others, blacklist_text, context)}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            votes.append(text)
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": "🗳️ " + text})

        # 阶段3：数据专员总结投票 + 提炼最终原则性建议
        session.push({"type": "typing", "name": analyst.name, "title": analyst.title})
        votes_text = "\n\n".join([f"{a.name}：" + v for a, v in zip(agents, votes)])
        summary_prompt = (
            f"你是数据专员「阿数」。针对黑榜句子的专家讨论与投票如下：\n\n"
            f"【专家方案】\n{others_plans}\n\n【投票表决】\n{votes_text}\n\n"
            f"请总结：\n"
            f"1. 投票共识：多数专家认可的最佳改法是什么？\n"
            f"2. 提炼「原则性建议」：把这次讨论的结论提炼成 3-5 条明确的、下次产出同类句子必须遵守的原则（每条一句话，可执行）。\n"
            f"3. 跟踪要求：说明下次遇到同类句子按新方法改后，需要看哪些数据指标来验证有没有提升。\n"
            f"直接输出，不要客套。"
        )
        summary = analyst.say([{"role": "user", "content": summary_prompt}])
        for prefix in (f"{analyst.name}：", f"{analyst.name}:"):
            if summary.startswith(prefix):
                summary = summary[len(prefix):].strip()
                break
        # 提炼原则性建议并落盘（从总结里抓取）
        principles = _parse_principles(summary)
        if principles:
            data_insight_store.add_principles(OUTPUT_DIR, principles)
        session.push({"type": "debate", "name": analyst.name, "title": analyst.title,
                      "text": "📌 讨论结论与原则性建议：\n" + summary})
        if principles:
            session.push({"type": "system", "text": f"✅ 已提炼 {len(principles)} 条原则性建议并落盘，下次所有专家产出前都会过一遍。"})
        session.push({"type": "system", "text": "🔁 闭环说明：下次遇到同类句子按新方法改后，再做一次「留存分析」，数据专员会对照数据判断有没有提升；没提升就继续发起讨论。"})
    except Exception as e:  # noqa: BLE001
        print(f"[server] 黑榜讨论异常: {e}")
        session.push({"type": "error", "text": f"黑榜讨论出错：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "debate_done"})
        print(f"[server] session {sid} 黑榜讨论完成")


# ---------- 数据榜单：每项数据 top1 / 垫底 ----------

# 榜单指标：key 对应前端留存分析弹窗的 metrics key，value 是「越高越好还是越低越好」
LEADERBOARD_METRICS = {
    "2秒跳出率": {"better": "lower", "label": "2秒跳出率", "unit": "%"},
    "5秒完播率": {"better": "higher", "label": "5秒完播率", "unit": "%"},
    "平均播放时长": {"better": "higher", "label": "平均播放时长", "unit": "秒"},
    "平均播放占比": {"better": "higher", "label": "平均播放占比", "unit": "%"},
    "完播率": {"better": "higher", "label": "完播率", "unit": "%"},
    "播放量": {"better": "higher", "label": "播放量", "unit": ""},
}


def _parse_num(v):
    """把指标值转成数字（处理「12000」「52%」「18秒」等）。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:  # noqa: BLE001
        return None


def _build_leaderboard() -> dict:
    """从作品库提取每项数据指标的榜单，返回 top1 和垫底。"""
    works = works_store.list_works(OUTPUT_DIR)
    result = {}
    for key, meta in LEADERBOARD_METRICS.items():
        better = meta["better"]
        # 收集有该指标的作品
        rows = []
        for w in works:
            m = (w.get("metrics") or {})
            v = _parse_num(m.get(key))
            if v is None:
                continue
            rows.append({"work_id": w["id"], "title": w.get("title", "未命名"),
                         "value": v, "raw": m.get(key)})
        if not rows:
            result[key] = {"label": meta["label"], "unit": meta["unit"], "better": better,
                           "top": None, "bottom": None, "count": 0}
            continue
        rows.sort(key=lambda x: x["value"], reverse=(better == "higher"))
        result[key] = {
            "label": meta["label"], "unit": meta["unit"], "better": better, "count": len(rows),
            "top": rows[0] if rows else None,
            "bottom": rows[-1] if rows else None,
            "all": rows,
        }
    return result


def _leaderboard_text(leaderboard: dict, mode: str = "top") -> str:
    """把榜单的 top1 或垫底渲染成文本，供讨论用。"""
    lines = []
    for key, lb in leaderboard.items():
        item = lb.get("top" if mode == "top" else "bottom")
        if not item:
            continue
        title = item["title"]
        val = item["raw"]
        unit = lb.get("unit", "")
        lines.append(f"- 【{lb.get('label', key)}】{title}：{val}{unit}")
    if not lines:
        return ""
    head = "以下作品在某项数据上拿下了【榜首/top1】：" if mode == "top" else "以下作品在某项数据上【垫底】："
    return head + "\n" + "\n".join(lines)


def _run_leaderboard_debate(sid: str, mode: str):
    """后台线程：针对榜单 top1（建议性）或垫底（禁止性）发起全员讨论+投票+原则提炼。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("leaderboard_debate"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发起讨论。"})
        session.push({"type": "debate_done"})
        return
    try:
        config = load_config()
        agents = build_agents(config)
        analyst = _build_data_analyst(config)
        leaderboard = _build_leaderboard()
        topic_text = _leaderboard_text(leaderboard, mode)
        if not topic_text:
            session.push({"type": "system", "text": "⚠ 暂无足够的数据榜单（需要先做留存分析并录入数据指标）。"})
            session.push({"type": "debate_done"})
            return

        participants = list(agents)
        if analyst is not None:
            participants.append(analyst)

        kind = "suggest" if mode == "top" else "forbid"
        kind_label = "建议性原则" if mode == "top" else "禁止性原则"
        mode_label = "榜首" if mode == "top" else "垫底"
        session.push({"type": "system", "text": f"📊 数据榜单「{mode_label}」反馈全员：讨论为什么这些作品这项数据特别{'好' if mode=='top' else '差'}，提炼{kind_label}…"})
        session.push({"type": "debate", "name": analyst.name, "title": analyst.title, "text": topic_text})

        opinions = {}
        for a in participants:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            prompt = (
                f"你是「{a.name}」，{a.title}。数据专员整理了各作品的数据榜单，"
                f"下面这些作品在某项数据上拿了【{mode_label}】：\n\n{topic_text}\n\n"
                f"请分析：这些作品为什么这项数据特别{'好' if mode=='top' else '差'}？"
                f"背后的做法/写法是什么？给出 2-3 条具体原因，并指出哪些值得沉淀成"
                f"「下次写稿{'要遵守' if mode=='top' else '必须避免'}的原则」。"
            )
            text = a.say([{"role": "user", "content": prompt}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            opinions[a.name] = text
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": text})

        session.push({"type": "system", "text": "🗳️ 开始投票表决，选出最该沉淀的原则…"})
        others_all = "\n\n".join([f"【{n}】{t}" for n, t in opinions.items()])
        votes = []
        for a in participants:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            others = "\n\n".join([f"【{n}】{t}" for n, t in opinions.items() if n != a.name])
            prompt = (
                f"你是「{a.name}」，{a.title}。针对数据榜单【{mode_label}】的各方分析：\n\n{others}\n\n"
                f"请投票表决：选出最该被沉淀成{kind_label}的 1-2 条观点，并说明为什么。"
            )
            text = a.say([{"role": "user", "content": prompt}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            votes.append(text)
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": "🗳️ " + text})

        session.push({"type": "typing", "name": analyst.name, "title": analyst.title})
        votes_text = "\n\n".join([f"{p.name}：" + v for p, v in zip(participants, votes)])
        summary_prompt = (
            f"你是数据专员「阿数」。针对数据榜单【{mode_label}】的全员分析与投票如下：\n\n"
            f"【各方分析】\n{others_all}\n\n【投票表决】\n{votes_text}\n\n"
            f"请总结并提炼「{kind_label}」（3-5 条，每条一句话，可执行）：\n"
            f"{'这些是下次写稿应该遵守的正面原则' if mode=='top' else '这些是下次写稿必须避免的负面原则（禁止性）'}\n"
            f"直接输出，不要客套。"
        )
        summary = analyst.say([{"role": "user", "content": summary_prompt}])
        for prefix in (f"{analyst.name}：", f"{analyst.name}:"):
            if summary.startswith(prefix):
                summary = summary[len(prefix):].strip()
                break
        principles = _parse_principles(summary)
        if principles:
            data_insight_store.add_principles(OUTPUT_DIR, principles, kind=kind)
        session.push({"type": "debate", "name": analyst.name, "title": analyst.title,
                      "text": f"📌 {kind_label}（{mode_label}）：\n" + summary})
        if principles:
            session.push({"type": "system", "text": f"✅ 已提炼 {len(principles)} 条{kind_label}并落盘，下次所有专家产出前都会过一遍。"})
    except Exception as e:  # noqa: BLE001
        print(f"[server] 榜单讨论异常: {e}")
        session.push({"type": "error", "text": f"榜单讨论出错：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "debate_done"})
        print(f"[server] session {sid} 榜单讨论完成")


# ---------- 原则审视：某篇文案打破原则时分析原因 ----------

def _run_principle_review(sid: str, note: str):
    """后台线程：用户说某篇文案打破了已有原则（或表现反常），
    数据专员 + 全员分析：之前那条原则是错的，还是别的原因？产出分析报告。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("principle_review"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发起审视。"})
        session.push({"type": "debate_done"})
        return
    try:
        config = load_config()
        agents = build_agents(config)
        analyst = _build_data_analyst(config)
        principles_text_all = data_insight_store.principles_text(OUTPUT_DIR, max_chars=4000)
        blacklist_text = data_insight_store.blacklist_text(OUTPUT_DIR, max_chars=2000)

        participants = list(agents)
        if analyst is not None:
            participants.append(analyst)

        session.push({"type": "system", "text": "🔍 用户反馈某篇文案打破了已有原则，全员审视：是原则错了，还是别的原因？"})
        review_note = note or "用户指出：某篇文案的表现违背了之前提炼的原则（例如遵守了建议性原则却扑街，或违反了禁止性原则却爆了）。"

        # 数据专员先给初步判断
        session.push({"type": "typing", "name": analyst.name, "title": analyst.title})
        analyst_prompt = (
            f"你是数据专员「阿数」。用户反馈：\n{review_note}\n\n"
            f"当前已有的原则（含建议性和禁止性）：\n{principles_text_all or '（暂无原则）'}\n\n"
            f"最近的句子级黑榜：\n{blacklist_text or '（暂无黑榜）'}\n\n"
            f"请你先给出初步判断：这条被打破的原则，更可能是「原则本身错了」还是「别的原因（如题材不同、受众变了、执行走样、数据波动等）」？"
            f"结合你能拿到的数据说话，不要和稀泥。"
        )
        analyst_view = analyst.say([{"role": "user", "content": analyst_prompt}])
        for prefix in (f"{analyst.name}：", f"{analyst.name}:"):
            if analyst_view.startswith(prefix):
                analyst_view = analyst_view[len(prefix):].strip()
                break
        session.push({"type": "debate", "name": analyst.name, "title": analyst.title, "text": analyst_view})

        # 各位专家审视
        opinions = {}
        for a in agents:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            prompt = (
                f"你是「{a.name}」，{a.title}。用户反馈某篇文案的表现打破了之前的原则：\n{review_note}\n\n"
                f"数据专员的初步判断：\n{analyst_view}\n\n"
                f"已有原则：\n{principles_text_all or '（暂无）'}\n\n"
                f"请你审视：这条原则到底是错了、还是别的原因？从你的专业角度给出判断，"
                f"并说明这条原则应该「保留」「修正」还是「废除」。"
            )
            text = a.say([{"role": "user", "content": prompt}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            opinions[a.name] = text
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": text})

        # 数据专员汇总成分析报告
        session.push({"type": "typing", "name": analyst.name, "title": analyst.title})
        opinions_text = "\n\n".join([f"【{n}】{t}" for n, t in opinions.items()])
        report_prompt = (
            f"你是数据专员「阿数」。针对「文案打破原则」的全员审视如下：\n\n"
            f"【你的初步判断】\n{analyst_view}\n\n【各位专家的审视】\n{opinions_text}\n\n"
            f"请产出一份【原则审视报告】，包括：\n"
            f"1. 结论：被打破的原则是「错误」还是「别的原因」（给出判断依据）\n"
            f"2. 该原则的处置：保留 / 修正（怎么修正）/ 废除\n"
            f"3. 如果有需要修正或废除的原则，给出修正后的新表述\n"
            f"直接输出报告，不要客套。\n\n"
            f"报告末尾必须附一个【处置清单】，每行一条，格式严格为（用半角竖线 | 分隔）：\n"
            f"修正|旧原则的原文|修正后的新表述\n"
            f"废除|旧原则的原文\n"
            f"其中「旧原则的原文」要尽量一字不差地引用上面已有原则里的原句；保留的原则不要列入；若无需处置就写「无」。"
        )
        report = analyst.say([{"role": "user", "content": report_prompt}])
        for prefix in (f"{analyst.name}：", f"{analyst.name}:"):
            if report.startswith(prefix):
                report = report[len(prefix):].strip()
                break
        # 解析处置清单并落盘待处置建议，供前端一键替换
        actions = _parse_principle_actions(report)
        if actions:
            data_insight_store.add_pending_actions(OUTPUT_DIR, actions)
        session.push({"type": "debate", "name": analyst.name, "title": analyst.title,
                      "text": "📋 原则审视报告：\n" + report})
        if actions:
            session.push({"type": "system", "text": f"📋 已生成 {len(actions)} 条待处置建议，去「数据复盘 → 全部原则」一键应用替换/废除。"})
        else:
            session.push({"type": "system", "text": "📋 原则审视报告已生成，本次结论为「保留」，无需处置。"})
    except Exception as e:  # noqa: BLE001
        print(f"[server] 原则审视异常: {e}")
        session.push({"type": "error", "text": f"原则审视出错：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "debate_done"})
        print(f"[server] session {sid} 原则审视完成")


# ---------- 爆款拆解（合并原「爆款讨论」+「爆款学习」） ----------
def _viral_debate_prompt(agent: Agent, article: str, context: str = "") -> str:
    """让某位专家/骨架师/数据专员分析一篇爆款文案为什么爆。"""
    return (
        f"你是「{agent.name}」，{agent.title}。用户给了下面这篇爆款文案，"
        f"想让你分析它为什么会爆：\n\n"
        f"【爆款文案开始】\n{article}\n【爆款文案结束】\n\n"
        f"请你以你的专业视角，给出这篇文案能爆的**核心原因**（至少 2-3 条，具体到句子/结构/情绪/钩子层面），"
        f"并指出其中哪些手法值得沉淀成「下次写稿也要用」的原则。\n"
        f"注意：不要客套，直接说干货；每一条要具体、可执行。"
    )


def _viral_vote_prompt(agent: Agent, others_opinions: str, article: str, context: str = "") -> str:
    """让每位专家对「这篇为什么爆」的各方意见投票。"""
    return (
        f"你是「{agent.name}」，{agent.title}。针对这篇爆款文案，各位给出了以下分析：\n\n"
        f"{others_opinions}\n\n"
        f"请投票表决：选出你认为最有价值、最该被提炼成原则的 1-2 条观点，并说明为什么。"
        f"最后补充一条你自己坚持、别人没充分说到的爆款要素。"
    )


def _run_viral_teardown(sid: str, article: str):
    """后台线程：爆款拆解 = ① 全员（专家+骨架师+数据专员）分析为什么爆 → ② 投票 → ③ 提炼原则性建议 → ④ 各专家吸收自身不足知识点。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("viral_teardown"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发起拆解。"})
        session.push({"type": "teardown_done"})
        return
    try:
        config = load_config()
        agents = build_agents(config)      # 全部文案专家（含骨架师阿骨、编导阿导）
        analyst = _build_data_analyst(config)  # 数据专员阿数
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()

        # 参与者 = 全部专家 + 数据专员
        participants = list(agents)
        if analyst is not None:
            participants.append(analyst)

        session.push({"type": "system", "text": "🧨 爆款拆解开始：先由全员分析这篇文案为什么爆，再投票提炼原则，最后各专家吸收自身不足的知识点…"})

        # ① 分析为什么爆
        session.push({"type": "system", "text": "🔥 全员分析爆款文案：各位专家、骨架师、数据专员逐位给出「为什么会爆」的意见…"})
        opinions = {}
        for a in participants:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            text = a.say([{"role": "user", "content": _viral_debate_prompt(a, article, context)}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            opinions[a.name] = text
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": text})

        # ② 投票
        session.push({"type": "system", "text": "🗳️ 开始投票表决，选出最该沉淀成原则的爆款要素…"})
        others_all = "\n\n".join([f"【{n}】{t}" for n, t in opinions.items()])
        votes = []
        for a in participants:
            session.push({"type": "typing", "name": a.name, "title": a.title})
            others = "\n\n".join([f"【{n}】{t}" for n, t in opinions.items() if n != a.name])
            text = a.say([{"role": "user", "content": _viral_vote_prompt(a, others, article, context)}])
            for prefix in (f"{a.name}：", f"{a.name}:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            votes.append(text)
            session.push({"type": "debate", "name": a.name, "title": a.title, "text": "🗳️ " + text})

        # ③ 数据专员总结 + 提炼原则
        session.push({"type": "typing", "name": analyst.name, "title": analyst.title})
        votes_text = "\n\n".join([f"{p.name}：" + v for p, v in zip(participants, votes)])
        summary_prompt = (
            f"你是数据专员「阿数」。针对这篇爆款文案的全员分析与投票如下：\n\n"
            f"【各方分析】\n{others_all}\n\n【投票表决】\n{votes_text}\n\n"
            f"请总结：\n"
            f"1. 这篇文案为什么爆——投票共识是什么？\n"
            f"2. 提炼「原则性建议」：把共识提炼成 3-5 条明确的、下次写稿应该遵守的原则（每条一句话，可执行）。\n"
            f"3. 特别点出：有没有哪条原则是「反直觉」或「容易踩坑」的，值得重点标注。\n"
            f"直接输出，不要客套。"
        )
        summary = analyst.say([{"role": "user", "content": summary_prompt}])
        for prefix in (f"{analyst.name}：", f"{analyst.name}:"):
            if summary.startswith(prefix):
                summary = summary[len(prefix):].strip()
                break
        principles = _parse_principles(summary)
        if principles:
            data_insight_store.add_principles(OUTPUT_DIR, principles, kind="suggest")
        session.push({"type": "debate", "name": analyst.name, "title": analyst.title,
                      "text": "📌 爆款分析结论与原则性建议：\n" + summary})
        if principles:
            session.push({"type": "system", "text": f"✅ 已提炼 {len(principles)} 条建议性原则并落盘，下次所有专家产出前都会过一遍。"})

        # ④ 各专家吸收知识点（原「爆款学习」合并进来）
        session.push({"type": "system", "text": "🧠 进入知识点吸收阶段：各位专家正在对照自身知识档案，提炼并校验值得沉淀的知识点…"})
        _absorb_lessons(session, agents, article, context)
    except Exception as e:  # noqa: BLE001
        print(f"[server] 爆款拆解异常: {e}")
        session.push({"type": "error", "text": f"爆款拆解出错：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "teardown_done"})
        print(f"[server] session {sid} 爆款拆解完成")


# ---------- 终稿评分（各位专家打分） ----------

def _score_prompt(script: str, context: str = "") -> str:
    return (
        "创作背景（本账号的核心信息，评分必须围绕它）：\n"
        f"{(context or '').strip() or '（无特别说明）'}\n\n"
        "【字数红线】整体文案应控制在 600 字以内（含标题），超出需在理由中指出。\n\n"
        "这是用户经过讨论、采纳后最终发布的口播终稿：\n\n"
        "【终稿开始】\n" + script + "\n【终稿结束】\n\n"
        "请给这份终稿打分（满分10分，允许一位小数），并给出详细的评分理由。\n"
        "打分时重点考虑：这篇稿子对目标受众是否成立、是否符合账号主理人的人设与实战定位、"
        "开头 3 秒能否留住人、整体节奏是否适合口播、字数是否在 600 字以内。\n"
        "严格按以下格式输出：\n"
        "【评分】8.5\n"
        "【理由】至少 2-3 句话说明你打这个分的核心理由：哪里做得好，哪里扣分了，为什么。不要只写一句话敷衍。"
    )


def _parse_score(text: str):
    """从专家回复中解析分数（0-10），解析不到返回 None。

    认以下显式格式（按优先级）：
    1. 「【评分】x」 / 「评分：x」
    2. 行首「专家名：x」（模型偶尔会把分数写成「阿沁：6.5」这种前缀形式）
    3. 「数字+分」（如「8.5分」「给 7 分」，限定紧邻「分」字）

    不再从正文里乱抓数字——「我干了8年命理」里的 8 会被误判成 8 分，
    污染评分统计与评分准确性排名。宁缺毋滥，解析不到就按未给分处理。
    """
    m = re.search(r"【评分】\s*([0-9]+(?:\.[0-9]+)?)", text)
    if m:
        v = float(m.group(1))
        return v if 0 <= v <= 10 else None
    m = re.search(r"评分\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)", text)
    if m:
        v = float(m.group(1))
        return v if 0 <= v <= 10 else None
    # 行首「名字：数字」形式（模型格式漂移时会把评分写成「阿沁：6.5」）——
    # 严格限定：行首、1-4 个中文字符、全角/半角冒号、0-10 小数，后随换行/空白/「分」。
    m = re.search(r"(?m)^\s*[\u4e00-\u9fa5]{1,4}\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:分)?\s*(?:\n|$)", text)
    if m:
        v = float(m.group(1))
        return v if 0 <= v <= 10 else None
    # 行首/独立成词的「数字+分」（如「8.5分」「给 7 分」），仍限定紧邻「分」字
    m = re.search(r"(?:^|[^\d.])([0-9]+(?:\.[0-9]+)?)\s*分", text)
    if m:
        v = float(m.group(1))
        return v if 0 <= v <= 10 else None
    return None


def _score_reason(text: str) -> str:
    """抽取一句话理由。"""
    m = re.search(r"【理由】\s*(.+)", text, re.S)
    if m:
        reason = m.group(1).strip()
        reason = re.split(r"\n+", reason)[0].strip()
    else:
        t = re.sub(r"【评分】\s*[0-9.]+", "", text)
        t = re.sub(r"评分\s*[：:]\s*[0-9.]+", "", t)
        t = t.replace("【理由】", "").strip()
        reason = re.split(r"\n+", t)[0].strip()
    return reason[:120]


def _run_score(sid: str, script: str):
    """后台线程：各位专家依次给终稿打分，然后互相吐槽对方打分。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("score"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再评分。"})
        session.push({"type": "score_done"})
        return
    try:
        config = load_config()
        agents = build_agents(config)
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
        results = []
        for a in agents:
            print(f"  [{a.name}] 评分中 ...")
            session.push({"type": "typing", "name": a.name, "title": a.title})
            text = a.say([{"role": "user", "content": _score_prompt(script, context)}])
            score = _parse_score(text)
            reason = _score_reason(text)
            results.append({"name": a.name, "score": score, "reason": reason})
            session.push({"type": "score", "name": a.name, "title": a.title,
                          "score": score, "reason": reason, "text": text})
            print(f"  [{a.name}] 评分 {score} 完成")

        stats_store.update_stats(OUTPUT_DIR, lambda s: stats_store.add_score_record(s, session.ts, script, results))
        # 口播工坊：评分 + 终稿落库到作品
        wid = session.work_id
        if wid and works_store.get(OUTPUT_DIR, wid):
            works_store.add_scores(OUTPUT_DIR, wid, results)
            works_store.set_final(OUTPUT_DIR, wid, script)

        # ===== 互相吐槽环节 =====
        session.push({"type": "system", "text": "🔥 各位专家开始互相吐槽对方打分…"})
        for a in agents:
            others = [r for r in results if r["name"] != a.name]
            others_text = "\n".join(
                [f"  · {r['name']}（{r['score']}分）：{r['reason']}" for r in others]
            )
            my_result = next((r for r in results if r["name"] == a.name), None)
            my_score = my_result["score"] if my_result else "未给分"
            my_reason = my_result["reason"] if my_result else ""
            roast_prompt = (
                f"你是「{a.name}」，{a.title}。你刚给一篇口播终稿打了 {my_score} 分，理由是：{my_reason}\n\n"
                f"以下是其他专家的打分和理由：\n{others_text}\n\n"
                f"现在请你看看其他专家的打分——你觉得谁的分数给高了或低了？谁的理由站不住脚？谁说得有道理？\n"
                f"直接点评 2-3 位其他专家的打分，说明你为什么觉得他/她的分数不合理（或合理）。"
                f"要具体、要犀利，不要客套，不要说「大家都有道理」这种废话。"
                f"每人用一句话说清楚你的观点。"
            )
            session.push({"type": "typing", "name": a.name, "title": a.title})
            roast_text = a.say([{"role": "user", "content": roast_prompt}])
            session.push({"type": "score_roast", "name": a.name, "title": a.title, "text": roast_text})
            print(f"  [{a.name}] 吐槽完成")

        session.push({"type": "system", "text": "✅ 互相吐槽完毕"})
    except Exception as e:  # noqa: BLE001
        print(f"[server] 评分异常: {e}")
        session.push({"type": "error", "text": f"评分过程中发生错误：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "score_done"})
        print(f"[server] session {sid} 评分完成")


# ---------- 爆款文案实战吸收（各位专家各自学习） ----------

def _learn_prompt(agent: Agent, digest_text: str, lessons_text_: str, article: str, context: str = "") -> str:
    return (
        f"你是「{agent.name}」，{agent.title}。以下是你的角色人设：\n{agent.persona}\n\n"
        f"【创作背景（本账号的核心信息，学习爆款时只吸收对本账号、对目标受众真正有用的点）】\n"
        f"{(context or '').strip() or '（无特别说明）'}\n\n"
        "【你的个人知识档案（你长期深度研读形成的知识内化成果，是你当前的知识体系）】\n"
        f"{digest_text[:9000] if digest_text else '（暂无档案）'}\n\n"
        "【你的爆款实战吸收档案（你以往从爆款文案中提炼的知识点，本次请勿重复提炼）】\n"
        f"{lessons_text_[:4000] if lessons_text_ else '（暂无）'}\n\n"
        "【用户提供的爆款文案】\n"
        f"{article[:9000]}\n\n"
        "任务：精读这篇爆款文案，对照你自己的知识档案，找出【你的知识体系中缺失或薄弱、"
        "但这篇爆款文案实际展示了的】知识点，把它们吸收为自己的新知识。\n\n"
        "硬性要求（宁缺毋滥，杜绝编造）：\n"
        "1. 最多提炼 4 条，只提炼你的档案里没有覆盖、真正值得学的点；找不到就输出空数组。\n"
        "2. 每条必须逐字摘录原文中的原句作为证据（quote 不得超过 80 字）。"
        "程序会逐字校验摘录是否真的存在于原文中——编造或改写的摘录会被直接丢弃，宁可少也不要错。\n"
        "3. 只输出一个 JSON 对象，不要任何其他文字或代码块标记：\n"
        '{"items":[{"quote":"原文原句摘录","point":"用你自己的话概括这个知识点",'
        '"apply":"下次写稿时具体怎么用（一句话）"}]}'
    )


def _learn_report(name: str, added: list, rejected: list, path: str) -> str:
    """把某位专家的吸收结果渲染成 Markdown 卡片文本。"""
    lines = [f"🧠 **{name}** 完成研读"]
    if added:
        lines.append(f"本次吸收 **{len(added)}** 条知识点（证据校验通过，已落盘）：")
        for i, it in enumerate(added, 1):
            lines.append(f"\n**{i}. 原文摘录（证据）**\n> {it['quote']}")
            lines.append(f"- 🧠 **知识点**：{it['point']}")
            if it.get("apply"):
                lines.append(f"- ✍️ **怎么用**：{it['apply']}")
    else:
        lines.append("本次没有提炼出可吸收的知识点（可能档案已覆盖，或提炼内容未通过证据校验）。")
    if rejected:
        lines.append(f"\n⚠ 丢弃 **{len(rejected)}** 条：原文中找不到依据的摘录（防 AI 幻觉，宁缺毋滥）")
        for r in rejected[:3]:
            lines.append(f"- {r}")
    if path:
        lines.append(f"\n📁 已写入：`{path}`")
    return "\n".join(lines)


def _absorb_lessons(session, agents, article, context=""):
    """让各位专家各自研读爆款文案，提炼自身不足的知识点，证据校验后落盘。返回 results 列表。
    供「爆款拆解」合并流程与「爆款学习」独立通道共用。"""
    results = []
    for a in agents:
        print(f"  [{a.name}] 研读爆款文案中 ...")
        session.push({"type": "typing", "name": a.name, "title": a.title})
        prompt = _learn_prompt(a, a.knowledge, a.lessons, article, context)
        raw = a.say([{"role": "user", "content": prompt}])
        parsed = _parse_learn_json(raw)
        items = (parsed or {}).get("items") or []
        accepted, rejected = [], []
        if raw.startswith("[") and "调用失败" in raw:
            rejected.append("（模型调用失败，本次未学习）")
        for it in items[:4]:
            quote = (it.get("quote") or "").strip()
            point = (it.get("point") or "").strip()
            apply = (it.get("apply") or "").strip()
            if not quote or not point:
                rejected.append("（缺原文摘录或知识点内容）")
                continue
            if not learn_store.verify_quote(quote, article):
                rejected.append(quote[:36])
                continue
            accepted.append({"quote": quote[:80], "point": point[:150], "apply": apply[:150]})
        digest_dir = DIGEST_DIR
        path, _ = learn_store.append_lessons(digest_dir, a.id, accepted) if accepted else ("", 0)
        results.append({"name": a.name, "added": accepted, "rejected": rejected, "path": path})
        session.push({"type": "learn", "name": a.name, "title": a.title,
                      "text": _learn_report(a.name, accepted, rejected, path),
                      "added": accepted, "rejected_count": len(rejected)})
        print(f"  [{a.name}] 吸收 {len(accepted)} 条 / 丢弃 {len(rejected)} 条")
    # 学习历史入 stats.json（审计留痕，可随时查证）
    def _log(s):
        s.setdefault("learn_history", []).append({
            "session_ts": session.ts,
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "article_head": article[:80],
            "per_expert": {r["name"]: {"added": len(r["added"]), "rejected": len(r["rejected"])}
                           for r in results},
        })

    stats_store.update_stats(OUTPUT_DIR, _log)
    total_added = sum(len(r["added"]) for r in results)
    total_rej = sum(len(r["rejected"]) for r in results)
    session.push({"type": "system",
                  "text": f"📁 吸收完成：{len(agents)} 位专家共吸收 **{total_added}** 条知识点"
                          f"（丢弃 {total_rej} 条无原文依据的摘录）。"
                          "已全部写入 `knowledge_digests/lessons/` 档案文件，下次讨论自动生效。"})
    return results


def _run_learn(sid: str, article: str):
    """后台线程（独立学习通道）：各位专家各自研读爆款文案 → 提炼不足知识点 → 证据校验 → 落盘。
    注：UI 已合并进「爆款拆解」，此通道供单独学习/脚本调用保留。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("learn"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发爆款文案。"})
        session.push({"type": "learn_done"})
        return
    try:
        config = load_config()
        agents = build_agents(config)
        ctx = config.get("context") or {}
        context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()
        session.push({"type": "system",
                      "text": f"🎓 爆款学习开始 · {len(agents)} 位专家正在各自研读这篇爆款文案，对照自身知识档案找差距"})
        _absorb_lessons(session, agents, article, context)
    except Exception as e:  # noqa: BLE001
        print(f"[server] 爆款学习异常: {e}")
        session.push({"type": "error", "text": f"爆款学习过程出错：{e}"})
    finally:
        session.end_phase()
        session.finished = True
        session.push({"type": "learn_done"})
        print(f"[server] session {sid} 爆款学习完成")


# ---------- 讨论 ----------

def _start_discussion(sid: str, script: str):
    """后台线程：跑完整群聊，事件实时入队并推送。"""
    session = SESSIONS.get(sid)
    if not session:
        print(f"[server] 会话 {sid} 不存在（可能已被清理）")
        return
    if not session.try_begin("discussion"):
        session.push({"type": "error", "text": "⚠ 上一项任务还在进行中，请等它完成后再发新文稿。"})
        session.push({"type": "done"})
        return

    try:
        config = load_config()
        # 注入骨架库摘要到创作背景
        try:
            sk_text = skeleton_store.templates_text(OUTPUT_DIR, top_n=5)
            if sk_text:
                ctx = config.setdefault("context", {})
                ctx["skeleton_library"] = "可用骨架模板（讨论时可参考、调用、变奏）：\n" + sk_text
        except Exception as e:  # noqa: BLE001
            print(f"[server] 骨架库注入失败: {e}")
        agents = build_agents(config)
        session.members = [
            {"name": a.name, "title": a.title,
             "color": AVATAR_COLORS.get(a.name, DEFAULT_COLOR)}
            for a in agents
        ]
        rcfg = config.get("recorder") or {}
        session.members.append({
            "name": rcfg.get("name", "记录员"),
            "title": rcfg.get("title", "记录员"),
            "color": rcfg.get("color", "#64748B"),
        })
        session.push({"type": "system", "text": "群聊开始 · 口播文稿专家讨论群"})
        markdown = run_discussion_stream(script, agents, config, on_event=session.push)
        # 存档 Markdown 记录
        try:
            out_path = save_output(markdown, OUTPUT_DIR)
            session.md_path = out_path
            print(f"[server] 讨论记录已保存: {out_path}")
        except Exception as e:  # noqa: BLE001
            print(f"[server] 保存讨论记录失败: {e}")
        # 原则审查：三轮讨论结束后自动对所有终稿做原则审查
        try:
            _run_auto_principle_review(session, script, config)
        except Exception as e:  # noqa: BLE001
            print(f"[server] 原则审查异常: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[server] 讨论异常: {e}")
        session.push({"type": "error", "text": f"讨论过程中发生错误：{e}"})
    finally:
        session.finished = True
        session.end_phase()
        session.push({"type": "done"})
        print(f"[server] session {sid} 结束")


# ---------- 路由 ----------

@app.route("/")
def index():
    resp = send_from_directory(WEB_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"ok": False, "error": "文稿内容不能为空"}), 400
    if len(script) > MAX_SCRIPT_CHARS:
        return jsonify({"ok": False, "error": "文稿过长（超过 10 万字），请精简后发送"}), 400

    sid = uuid.uuid4().hex[:12]
    session = Session()
    session.sid = sid
    session.script = script
    with SESSIONS_LOCK:
        SESSIONS[sid] = session

    # 口播工坊：讨论隶属于一个「作品」——已有作品则绑定，否则自动创建
    wid = (data.get("work_id") or "").strip()
    title = (data.get("title") or "").strip()
    if wid:
        session.work_id = wid
        w = works_store.get(OUTPUT_DIR, wid)
        if w:
            session.work_title = w.get("title", "")
            # 记录最新会话 + 最新初稿，供「继续讨论」恢复现场
            def _bind(x):
                x["session_id"] = sid
                x["draft"] = script
                x["status"] = "discussing"
            works_store.update_work(OUTPUT_DIR, wid, _bind)
    else:
        # 自动取名：文稿前 18 字（去空白），避免一堆「未命名作品」
        auto_title = title or re.sub(r"\s+", "", script)[:18] or "未命名作品"
        w = works_store.create(OUTPUT_DIR, auto_title, script, session_id=sid)
        session.work_id = w["id"]
        session.work_title = w["title"]

    # 先注入群成员信息（从配置读取，不重复构建 agents）
    _ensure_members(session)

    t = threading.Thread(target=_start_discussion, args=(sid, script), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid, "work_id": session.work_id,
                    "work_title": session.work_title, "members": session.members})


_SSE_END_TYPES = ("done", "review_done", "score_done", "learn_done", "comment_done", "retention_done", "debate_done")


@app.route("/api/stream/<sid>")
def api_stream(sid):
    session = SESSIONS.get(sid)
    if not session:
        return Response("data: {\"type\":\"error\",\"text\":\"会话不存在或已过期\"}\n\n",
                        mimetype="text/event-stream")

    def gen():
        # 每个连接维护自己的游标：先重放已有历史（刷新页面不丢消息）
        cursor = len(session.history)
        for item in list(session.history[:cursor]):
            yield _sse(item)
        if getattr(session, "frozen", False):
            return  # 从磁盘重建的只读会话：重放完即断开，不再等待新事件
        if session.finished and session.phase == "idle" and session.history and session.history[-1].get("type") in _SSE_END_TYPES:
            return  # 会话早已结束（无任务在跑），重放完历史即可断开
        while True:
            try:
                session.queue.get(timeout=1.0)   # 只做「有新事件」的唤醒信号，不消费内容
            except queue.Empty:
                pass
            # 事件一律从 history（权威源）读取：多连接各自维护游标，互不干扰
            n = len(session.history)
            if n > cursor:
                for it in session.history[cursor:]:
                    yield _sse(it)
                cursor = n
                if session.finished and session.phase == "idle" and session.history[-1].get("type") in _SSE_END_TYPES:
                    break
            else:
                yield ": keepalive\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/api/adopt", methods=["POST"])
def api_adopt():
    """记录用户采纳：挂到当前作品下，作品不存在则自动创建兜底。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400

    name = (data.get("name") or "").strip()
    round_label = (data.get("round") or "讨论").strip()
    snippet = (data.get("snippet") or "").strip()
    note = (data.get("note") or "").strip()
    if not name or not snippet:
        return jsonify({"ok": False, "error": "缺少采纳信息（专家名/内容）"}), 400

    # 绑定作品：优先取请求指定 → 会话绑定 → 自动创建
    wid = (data.get("work_id") or session.work_id or "").strip()
    if not wid or not works_store.get(OUTPUT_DIR, wid):
        w = works_store.create(OUTPUT_DIR, session.work_title or "未命名作品",
                               session.script, session_id=sid)
        wid = w["id"]
        session.work_id = wid
    # 原则命中统计：采纳的建议若命中原则关键词，则 hits+1（原则库命中率依据）
    try:
        data_insight_store.count_principle_hits(OUTPUT_DIR, snippet)
    except Exception:  # noqa: BLE001
        pass
    adopt_no = works_store.add_adoption(OUTPUT_DIR, wid, {
        "name": name, "round": round_label, "snippet": snippet, "note": note,
    })
    session.adoptions.append({
        "no": adopt_no,
        "name": name,
        "round": round_label,
        "snippet": snippet[:300] + ("…" if len(snippet) > 300 else ""),
        "note": note,
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_adoptions(session)

    parts = [
        f"📋 **采纳记录 #{adopt_no}**\n",
        f"- 来自：**{name}**（{round_label}）",
        f"- 采纳内容：{session.adoptions[-1]['snippet']}",
    ]
    if note:
        parts.append(f"- 你的备注：{note}")
    parts.append("\n已记录在案 ✅ 之后把实际效果数据发我（复盘），我会评估这条改动对不对，并把正/负反馈写进这位专家的档案。\n想让我把终稿发群里打分？直接点「终稿评分」。")
    text = "\n".join(parts)
    notify_store.add(OUTPUT_DIR, "adopt",
                     f"采纳了 {name} 的建议 #{adopt_no}",
                     f"「{snippet[:40]}…」" if len(snippet) > 40 else f"「{snippet}」",
                     {"view": "works", "wid": wid})
    return jsonify({
        "ok": True,
        "adopt_no": adopt_no,
        "work_id": wid,
        "text": text,
        "html": render_md(text),
    })


@app.route("/api/adopt_para", methods=["POST"])
def api_adopt_para():
    """段落级采纳：用户采纳专家对某一段/某一句的改写，可逐步合并成终稿。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400

    original = (data.get("original") or "").strip()
    rewritten = (data.get("rewritten") or "").strip()
    para_idx = data.get("para_idx")
    agent_name = (data.get("agent_name") or "").strip()
    note = (data.get("note") or "").strip()
    if not original or not rewritten:
        return jsonify({"ok": False, "error": "缺少原文或改写文本"}), 400

    session.merges.append({
        "para_idx": para_idx,
        "original": original,
        "rewritten": rewritten,
        "agent_name": agent_name,
        "note": note,
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    # 生成当前合并预览
    preview = _build_merge_preview(session.script, session.merges)
    return jsonify({
        "ok": True,
        "merges": session.merges,
        "preview": preview,
        "html": render_md(f"📋 **已采纳改写**（来自 {agent_name}）\n\n- 原句：{original[:60]}{'…' if len(original)>60 else ''}\n- 改写：{rewritten[:80]}{'…' if len(rewritten)>80 else ''}\n\n已收入合并编辑器，可继续采纳或导出预览。"),
    })


@app.route("/api/merge_preview", methods=["POST"])
def api_merge_preview():
    """返回当前所有段落级采纳应用后的合并预览文本。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    return jsonify({
        "ok": True,
        "merges": session.merges,
        "preview": _build_merge_preview(session.script, session.merges),
    })


def _build_merge_preview(script: str, merges: list) -> str:
    """按 para_idx 优先、original 文本兜底，生成合并后的文本。"""
    if not merges:
        return script
    # 先按 para_idx 排序，无 para_idx 的放最后
    ordered = sorted(merges, key=lambda m: (m.get("para_idx") if m.get("para_idx") is not None else 9999))
    result = script
    for m in ordered:
        original = m.get("original", "")
        rewritten = m.get("rewritten", "")
        if not original or not rewritten:
            continue
        # 用 para_idx 定位时先尝试找原文；找不到则全文替换第一次出现
        idx = result.find(original)
        if idx != -1:
            result = result[:idx] + rewritten + result[idx + len(original):]
    return result


@app.route("/api/merge_remove", methods=["POST"])
def api_merge_remove():
    """移除指定索引的段落级采纳。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    idx = data.get("idx")
    if idx is None or idx < 0 or idx >= len(session.merges):
        return jsonify({"ok": False, "error": "索引无效"}), 400
    session.merges.pop(idx)
    return jsonify({
        "ok": True,
        "merges": session.merges,
        "preview": _build_merge_preview(session.script, session.merges),
    })


@app.route("/api/merge_clear", methods=["POST"])
def api_merge_clear():
    """清空所有段落级采纳。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    session.merges = []
    return jsonify({
        "ok": True,
        "merges": [],
        "preview": session.script,
    })


@app.route("/api/comment", methods=["POST"])
def api_comment():
    """评论迭代：用户对某位专家的建议提出评论/反驳，专家据此重新修改建议。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    agent_name = (data.get("agent_name") or "").strip()
    original_msg = (data.get("original_msg") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not agent_name or not comment:
        return jsonify({"ok": False, "error": "缺少专家名称或评论内容"}), 400
    if not session.try_begin("comment"):
        return jsonify({"ok": False, "error": "另一项任务正在进行中，请稍后再试"}), 409

    # 重开会话以允许 SSE 推送
    session.finished = False

    def _run():
        try:
            config = load_config()
            agent = build_single_agent(config, agent_name)
            if not agent:
                session.push({"type": "error", "text": f"找不到专家「{agent_name}」"})
                return
            # 推送用户评论
            session.push({"type": "system", "text": f"💬 你评论了 {agent_name} 的建议：{comment[:80]}{'…' if len(comment)>80 else ''}"})
            # 推送 typing
            session.push({"type": "typing", "name": agent_name, "title": agent.title})
            # 构造迭代 prompt
            prompt = (
                f"用户对你的建议提出了以下评论/反驳：\n\n---\n{comment}\n---\n\n"
                f"你之前的建议是：\n\n---\n{original_msg[:2000]}\n---\n\n"
                f"请根据用户的反馈，重新修改你的建议。"
                f"如果用户说得对，请承认并给出修改后的建议；如果你认为原来的建议有道理，请进一步解释为什么。"
                f"仍然使用【段落定位】【改写】【理由】的格式，给出 2-4 处修改。"
            )
            script_context = f"\n\n【原稿】\n{session.script}" if session.script else ""
            reply = agent.say([{"role": "user", "content": prompt + script_context}])
            session.push({"type": "message", "name": agent.name, "title": agent.title,
                          "text": f"{agent.name}：{reply}", "round": 4, "is_comment_reply": True})
        except Exception as e:  # noqa: BLE001
            print(f"[server] 评论迭代异常: {e}")
            session.push({"type": "error", "text": f"评论迭代出错：{e}"})
        finally:
            session.end_phase()
            session.finished = True
            session.push({"type": "comment_done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/comment_all", methods=["POST"])
def api_comment_all():
    """句子级评论迭代：用户对某句话所有专家的建议都不满意，
    发评论让所有专家根据评论重新修改该句。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    original_text = (data.get("original_text") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not comment:
        return jsonify({"ok": False, "error": "评论内容不能为空"}), 400
    if not session.try_begin("comment"):
        return jsonify({"ok": False, "error": "另一项任务正在进行中，请稍后再试"}), 409

    session.finished = False

    def _run():
        try:
            config = load_config()
            agents_list = build_agents(config)
            session.push({"type": "system",
                          "text": f"💬 你对这句原文提出了评论：\n「{original_text[:80]}{'…' if len(original_text)>80 else ''}」\n评论：{comment[:100]}{'…' if len(comment)>100 else ''}\n\n所有专家将根据你的评论重新修改。"})
            for a in agents_list:
                session.push({"type": "typing", "name": a.name, "title": a.title})
                prompt = (
                    f"用户对原文中的这句话提出了评论：\n\n"
                    f"【原文】{original_text}\n\n"
                    f"【用户评论】{comment}\n\n"
                    f"请根据用户的评论，重新给出你对这句话的改写建议。"
                    f"仍然使用以下格式：\n"
                    f"【原文】{original_text}\n"
                    f"【改写】你修改后的文本\n"
                    f"【理由】为什么这么改（结合用户评论说明）\n\n"
                    f"如果用户的评论指出了你之前没注意到的问题，请承认并改进。"
                    f"如果你认为用户的方向有问题，也请直接说出来并给出你的替代方案。"
                    f"注意整体文案控制在 600 字以内。"
                )
                script_context = f"\n\n【完整原稿参考】\n{session.script}" if session.script else ""
                reply = a.say([{"role": "user", "content": prompt + script_context}])
                session.push({"type": "message", "name": a.name, "title": a.title,
                              "text": reply, "round": 5, "is_comment_reply": True})
        except Exception as e:  # noqa: BLE001
            print(f"[server] 评论所有专家异常: {e}")
            session.push({"type": "error", "text": f"评论迭代出错：{e}"})
        finally:
            session.end_phase()
            session.finished = True
            session.push({"type": "comment_done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


def _norm_text(text: str) -> str:
    """为匹配归一化：去空白、去所有中英文标点和引号。"""
    if not text:
        return ""
    import unicodedata
    t = text.strip()
    # 去掉常见引号与空白
    t = re.sub(r"[「」『』\"'\"\"'\s]+", "", t)
    # 去掉所有标点符号（unicode 类别 P*）
    t = "".join(ch for ch in t if not unicodedata.category(ch).startswith("P"))
    return t


def _is_meaningful(text: str) -> bool:
    """片段是否包含有效文字（CJK、字母、数字），而非纯标点。"""
    if not text:
        return False
    # 至少含一个 CJK 字符或字母数字
    return bool(re.search(r"[\u4e00-\u9fa5a-zA-Z0-9]", text))


def _text_similarity(a: str, b: str) -> float:
    """两段文字的相似度：优先精确/包含匹配，否则用 difflib 序列相似度。阈值 0.75。"""
    import difflib
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # 互包含且长度差异不大（避免标题包含正文句子被误判）
    if na in nb or nb in na:
        ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        if ratio >= 0.65:
            return max(0.85, ratio)
    # difflib 序列相似度（考虑顺序，避免字符集合重合导致的误判）
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _extract_suggestions(text: str) -> list:
    """从专家消息文本中提取【原文/段落定位】+【改写】+【理由】三元组。"""
    suggestions = []
    for block in re.split(r"(?=【(?:原文|段落定位)】)", text):
        om = re.search(r"【(?:原文|段落定位)】\s*([\s\S]*?)(?=【改写】|【理由】|$)", block)
        rm = re.search(r"【改写】\s*([\s\S]*?)(?=【理由】|$)", block)
        wm = re.search(r"【理由】\s*([\s\S]*?)(?=【(?:原文|段落定位)】|$)", block)
        if om and rm:
            original = om.group(1).strip()
            rewritten = rm.group(1).split("【")[0].strip()
            reason = wm.group(1).strip() if wm else ""
            if original and rewritten and _is_meaningful(original):
                suggestions.append({
                    "original": original,
                    "rewritten": rewritten,
                    "reason": reason,
                })
    return suggestions


def _extract_references(text: str) -> list:
    """从专家消息文本中提取【段落定位】/【原文】引用的原文片段。"""
    return [s["original"] for s in _extract_suggestions(text)]


def _build_heatmap(script: str, history: list) -> dict:
    """分析历史消息，生成争议热力图数据（含每句的改写建议详情）。

    返回:
      {
        "segments": [{
          "text": "...",
          "mentions": 3,
          "agents": ["阿沁","老周"],
          "suggestions": [{"agent":"阿沁","original":"...","rewritten":"...","reason":"...","msg_idx":5}]
        }],
        "hotspots": [...],  # 按 mentions 降序
        "total_refs": 12,
        "total_segments": 20
      }
    """
    # 按换行分段，每段再按中文句号、问号、感叹号分句
    raw_segments = []
    for para in script.split("\n"):
        para = para.strip()
        if not para:
            continue
        sentences = re.split(r"(?<=[。！？\.\!\?])", para)
        for s in sentences:
            s = s.strip()
            if s and _is_meaningful(s):
                raw_segments.append(s)

    if not raw_segments:
        raw_segments = [script.strip()] if script.strip() else []

    # 收集所有建议
    all_suggestions = []  # [{original, rewritten, reason, agent, msg_idx}]
    for idx, item in enumerate(history):
        if item.get("type") not in ("message", "final"):
            continue
        name = item.get("name", "")
        if not name or name == "记录员":
            continue
        text = item.get("text", "")
        for s in _extract_suggestions(text):
            all_suggestions.append({
                "agent": name,
                "original": s["original"],
                "rewritten": s["rewritten"],
                "reason": s["reason"],
                "msg_idx": idx,
            })

    # 为每个原文片段计算被引用次数、引用者、关联建议
    segments = []
    for seg in raw_segments:
        mentions = 0
        agents = set()
        seg_suggestions = []
        for sug in all_suggestions:
            sim = _text_similarity(seg, sug["original"])
            if sim >= 0.72:
                mentions += 1
                agents.add(sug["agent"])
                seg_suggestions.append(sug)
        segments.append({
            "text": seg,
            "mentions": mentions,
            "agents": sorted(agents),
            "suggestions": seg_suggestions,
            "index": len(segments),
        })

    # 热点排序
    hotspots = sorted(
        [s for s in segments if s["mentions"] > 0],
        key=lambda x: x["mentions"],
        reverse=True,
    )

    return {
        "segments": segments,
        "hotspots": hotspots[:10],
        "total_refs": len(all_suggestions),
        "total_segments": len(segments),
    }


@app.route("/api/heatmap", methods=["POST"])
def api_heatmap():
    """争议热力图：分析哪些段落/句子被最多专家提及/争议。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    result = _build_heatmap(session.script, session.history)
    return jsonify({"ok": True, **result})


# ---------- 文案骨架库 ----------

@app.route("/api/skeletons", methods=["GET"])
def api_skeletons_list():
    """列出全部骨架模板。"""
    templates = skeleton_store.list_templates(OUTPUT_DIR)
    return jsonify({"ok": True, "templates": templates})


@app.route("/api/skeletons/match", methods=["POST"])
def api_skeletons_match():
    """根据文稿内容匹配骨架模板。"""
    data = request.get_json(force=True, silent=True) or {}
    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"ok": False, "error": "文稿内容不能为空"}), 400
    matched = skeleton_store.match_templates(OUTPUT_DIR, script)
    return jsonify({"ok": True, "templates": matched})


@app.route("/api/skeletons", methods=["POST"])
def api_skeletons_add():
    """新增骨架模板。"""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "模板名称不能为空"}), 400
    template = {
        "name": name,
        "scene": (data.get("scene") or "").strip(),
        "structure_type": (data.get("structure_type") or "").strip(),
        "core_structure": (data.get("core_structure") or "").strip(),
        "emotion_curve": (data.get("emotion_curve") or "").strip(),
        "hook_types": (data.get("hook_types") or "").strip(),
        "cta_positions": (data.get("cta_positions") or "").strip(),
        "variation_rules": (data.get("variation_rules") or "").strip(),
        "case_example": (data.get("case_example") or "").strip(),
        "compliance_level": (data.get("compliance_level") or "").strip(),
    }
    t = skeleton_store.add_template(OUTPUT_DIR, template)
    return jsonify({"ok": True, "template": t})


@app.route("/api/skeletons/<tid>", methods=["PUT"])
def api_skeletons_update(tid):
    """更新骨架模板。"""
    data = request.get_json(force=True, silent=True) or {}
    updates = {k: v for k, v in data.items() if k != "id"}
    t = skeleton_store.update_template(OUTPUT_DIR, tid, updates)
    if not t:
        return jsonify({"ok": False, "error": "模板不存在"}), 404
    return jsonify({"ok": True, "template": t})


@app.route("/api/skeletons/<tid>", methods=["DELETE"])
def api_skeletons_delete(tid):
    """删除骨架模板。"""
    if skeleton_store.delete_template(OUTPUT_DIR, tid):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "模板不存在"}), 404


@app.route("/api/review", methods=["POST"])
def api_review():
    """记录员复盘：接收实际效果数据，异步分析并 SSE 推送报告；结论落库到作品。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400

    review_data = (data.get("data") or "").strip()
    if not review_data:
        return jsonify({"ok": False, "error": "复盘数据不能为空"}), 400
    if len(review_data) > 5000:
        return jsonify({"ok": False, "error": "复盘数据过长（超过 5000 字）"}), 400

    # 口播工坊：绑定作品 + 效果数据（播放/完播/点赞…），复盘后写入作品库
    wid = (data.get("work_id") or session.work_id or "").strip()
    if wid and works_store.get(OUTPUT_DIR, wid):
        session.work_id = wid
    session.review_metrics = data.get("metrics") or {}

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_review, args=(sid, review_data), daemon=True)
    t.start()
    notify_store.add(OUTPUT_DIR, "review", "复盘分析已启动",
                     "数据专员正在评估实际效果数据，稍后在群里查看报告。",
                     {"view": "chat"})
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/retention", methods=["POST"])
def api_retention():
    """数据专员：接收字幕稿 + 留存数据 + 数据指标，做句子级留存分析并 SSE 推送。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400

    subtitle = (data.get("subtitle") or "").strip()
    retention = (data.get("retention") or "").strip()
    metrics = data.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    if not subtitle:
        return jsonify({"ok": False, "error": "字幕稿不能为空"}), 400
    if len(subtitle) > 20000:
        return jsonify({"ok": False, "error": "字幕稿过长（超过 2 万字）"}), 400
    if len(retention) > 5000:
        return jsonify({"ok": False, "error": "留存数据过长（超过 5000 字）"}), 400

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_retention, args=(sid, subtitle, retention, metrics), daemon=True)
    t.start()
    notify_store.add(OUTPUT_DIR, "retention", "留存分析已启动",
                     "数据专员正在逐句分析留存曲线，稍后可在群里查看句子级归因。",
                     {"view": "chat"})
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/retention_debate", methods=["POST"])
def api_retention_debate():
    """黑榜反馈专家群讨论：专家讨论为什么掉留存 → 投票表决 → 数据专员提炼原则。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_debate, args=(sid,), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/viral_teardown", methods=["POST"])
def api_viral_teardown():
    """爆款拆解：全员分析为什么爆 → 投票 → 提炼原则 → 各专家吸收自身不足知识点（合并原爆款讨论+爆款学习）。
    无 sid 时自动创建一个独立会话承载拆解记录（不绑定作品、不跑讨论），方便用户直接贴爆款文案。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        session = _new_standalone_session()
        sid = session.sid

    article = (data.get("article") or "").strip()
    if not article:
        return jsonify({"ok": False, "error": "爆款文案不能为空"}), 400
    if len(article) > 20000:
        return jsonify({"ok": False, "error": "爆款文案过长（超过 2 万字）"}), 400

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_viral_teardown, args=(sid, article), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/insights", methods=["GET"])
def api_insights():
    """返回数据洞察：黑榜 + 原则性建议 + 播放量归因 + 跟踪记录 + 待处置建议。"""
    data = data_insight_store.load(OUTPUT_DIR)
    return jsonify({
        "ok": True,
        "blacklist": data.get("blacklist", []),
        "principles": data_insight_store.all_principles(OUTPUT_DIR),
        "attributions": data.get("attributions", []),
        "tracks": data.get("tracks", []),
        "pending_actions": data.get("pending_actions", []),
    })


@app.route("/api/insights/principles/<int:index>", methods=["DELETE"])
def api_delete_principle(index):
    """删除一条原则（按索引）。"""
    if data_insight_store.delete_principle(OUTPUT_DIR, index):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "原则不存在"}), 404


@app.route("/api/insights/principles", methods=["POST"])
def api_add_principle():
    """新增一条原则。body: {text, kind}，kind 为 suggest / forbid，默认 suggest。"""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    kind = (data.get("kind") or "suggest").strip()
    if kind not in ("suggest", "forbid"):
        kind = "suggest"
    if not text:
        return jsonify({"ok": False, "error": "原则内容不能为空"}), 400
    if data_insight_store.add_principle(OUTPUT_DIR, text, kind):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "该原则已存在或内容重复"}), 409


@app.route("/api/insights/principles/<int:index>", methods=["PUT"])
def api_update_principle(index):
    """编辑一条原则（按索引）。body: {text, kind}。"""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    kind = (data.get("kind") or "suggest").strip()
    if kind not in ("suggest", "forbid"):
        kind = "suggest"
    if not text:
        return jsonify({"ok": False, "error": "原则内容不能为空"}), 400
    if data_insight_store.update_principle(OUTPUT_DIR, index, text, kind):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "原则不存在"}), 404


@app.route("/api/principles/apply", methods=["POST"])
def api_principles_apply():
    """应用/忽略一条原则审视的待处置建议：修正替换或废除旧原则。"""
    data = request.get_json(force=True, silent=True) or {}
    idx = data.get("index")
    act = (data.get("action") or "apply").strip()  # apply / dismiss
    pending = data_insight_store.get_pending_actions(OUTPUT_DIR)
    if not (isinstance(idx, int) and 0 <= idx < len(pending)):
        return jsonify({"ok": False, "error": "待处置项不存在"}), 404
    item = pending[idx]
    if act == "dismiss":
        data_insight_store.remove_pending_action(OUTPUT_DIR, idx)
        return jsonify({"ok": True, "result": "dismissed"})
    result = data_insight_store.replace_principle(OUTPUT_DIR, item["old_text"], item["new_text"], item["action"])
    data_insight_store.remove_pending_action(OUTPUT_DIR, idx)
    if result == "not_found":
        return jsonify({"ok": False, "error": "未在原则库里匹配到该旧原则，已忽略"}), 404
    return jsonify({"ok": True, "result": result})


@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    """返回每项数据指标的榜单（top1 + 垫底 + 全量）。"""
    leaderboard = _build_leaderboard()
    # 精简返回：去掉 all 全量（太大），保留 top/bottom/count
    slim = {}
    for k, v in leaderboard.items():
        slim[k] = {
            "label": v.get("label", k), "unit": v.get("unit", ""),
            "better": v.get("better", "higher"), "count": v.get("count", 0),
            "top": v.get("top"), "bottom": v.get("bottom"),
        }
    # 已录入指标的作品数（榜单数据是否充足的判断依据，门槛 3）
    works = works_store.list_works(OUTPUT_DIR)
    metrics_works = sum(1 for w in works if w.get("status") != "archived" and (w.get("metrics") or {}))
    return jsonify({"ok": True, "leaderboard": slim, "metrics_works": metrics_works})


@app.route("/api/leaderboard_debate", methods=["POST"])
def api_leaderboard_debate():
    """榜单讨论：top1→建议性原则，bottom→禁止性原则，全员讨论+投票+提炼。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400
    mode = (data.get("mode") or "top").strip()
    if mode not in ("top", "bottom"):
        return jsonify({"ok": False, "error": "mode 必须是 top 或 bottom"}), 400

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_leaderboard_debate, args=(sid, mode), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/principle_review", methods=["POST"])
def api_principle_review():
    """原则审视：某篇文案打破了原则时，全员分析是原则错还是别的原因，产出报告。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400
    note = (data.get("note") or "").strip()

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_principle_review, args=(sid, note), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/score", methods=["POST"])
def api_score():
    """各位专家给用户最终发布的终稿打分（满分10分 + 一句话理由）。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400

    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"ok": False, "error": "终稿内容不能为空"}), 400
    if len(script) > 50000:
        return jsonify({"ok": False, "error": "终稿过长（超过 5 万字）"}), 400

    # 原则命中统计：终稿包含原则关键词则 hits+1
    try:
        data_insight_store.count_principle_hits(OUTPUT_DIR, script)
    except Exception:  # noqa: BLE001
        pass
    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_score, args=(sid, script), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/learn", methods=["POST"])
def api_learn():
    """爆款文案实战吸收：各位专家各自研读，提炼自身不足的知识点，证据校验后落盘。"""
    data = request.get_json(force=True, silent=True) or {}
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期，请先发起讨论"}), 400

    article = (data.get("article") or "").strip()
    if not article:
        return jsonify({"ok": False, "error": "爆款文案不能为空"}), 400
    if len(article) > 50000:
        return jsonify({"ok": False, "error": "爆款文案过长（超过 5 万字），请精简后发送"}), 400

    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断
    t = threading.Thread(target=_run_learn, args=(sid, article), daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/undo-adopt", methods=["POST"])
def api_undo_adopt():
    """撤销一条采纳：软撤销（记录保留可追溯，不再计入统计与专家贡献榜）。"""
    data = request.get_json(force=True, silent=True) or {}
    wid = (data.get("work_id") or "").strip()
    no = data.get("no")
    reason = (data.get("reason") or "").strip()
    if not wid or no is None:
        return jsonify({"ok": False, "error": "缺少作品或采纳编号"}), 400
    try:
        no = int(no)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "采纳编号格式无效"}), 400
    if not works_store.get(OUTPUT_DIR, wid):
        return jsonify({"ok": False, "error": "作品不存在或已删除"}), 404
    if not works_store.revoke_adoption(OUTPUT_DIR, wid, int(no), reason):
        return jsonify({"ok": False, "error": "该采纳记录不存在或已撤销"}), 400
    return jsonify({"ok": True})


@app.route("/api/works", methods=["GET"])
def api_works_list():
    """作品库列表（含统计）。"""
    works = works_store.list_works(OUTPUT_DIR)
    c = works_store.counts(OUTPUT_DIR)
    return jsonify({"ok": True, "works": works, "counts": c})


@app.route("/api/works", methods=["POST"])
def api_works_create():
    """手动创建作品（不进讨论，仅存稿）。"""
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    draft = (data.get("draft") or "").strip()
    if not draft:
        return jsonify({"ok": False, "error": "内容不能为空"}), 400
    w = works_store.create(OUTPUT_DIR, title or "未命名作品", draft)
    return jsonify({"ok": True, "work": w})


@app.route("/api/works/<wid>", methods=["GET"])
def api_work_get(wid):
    w = works_store.get(OUTPUT_DIR, wid)
    if not w:
        return jsonify({"ok": False, "error": "作品不存在"}), 404
    return jsonify({"ok": True, "work": w})


@app.route("/api/works/<wid>", methods=["PATCH"])
def api_work_patch(wid):
    """更新作品：title / status / note / metrics / draft / final（局部更新）。"""
    data = request.get_json(force=True, silent=True) or {}
    w = works_store.get(OUTPUT_DIR, wid)
    if not w:
        return jsonify({"ok": False, "error": "作品不存在"}), 404
    if "title" in data:
        works_store.set_title(OUTPUT_DIR, wid, data["title"])
    if "status" in data and data["status"] in works_store.STATUS_LABELS:
        works_store.set_status(OUTPUT_DIR, wid, data["status"])
    if "note" in data:
        works_store.update_work(OUTPUT_DIR, wid, lambda x: x.update({"note": (data["note"] or "").strip()}))
    if "draft" in data and isinstance(data["draft"], str):
        works_store.update_work(OUTPUT_DIR, wid, lambda x: x.update({"draft": data["draft"]}))
    if "final" in data and isinstance(data["final"], str):
        works_store.update_work(OUTPUT_DIR, wid, lambda x: x.update({"final": data["final"]}))
    if "metrics" in data and isinstance(data["metrics"], dict):
        def _m(x):
            m = x.get("metrics") or {}
            for k, v in data["metrics"].items():
                if isinstance(v, (int, float, str)) and v != "":
                    m[k] = v
            x["metrics"] = m
        works_store.update_work(OUTPUT_DIR, wid, _m)
    return jsonify({"ok": True, "work": works_store.get(OUTPUT_DIR, wid)})


@app.route("/api/import_metrics", methods=["POST"])
def api_import_metrics():
    """录入作品数据：接收文件路径（桌面端原生文件对话框返回），解析并落库。

    files 结构：{traffic: 路径, content: 路径, audience: 路径, subtitle: 路径}
    流量数据/内容吸引力/观众分析为 .xlsx，字幕稿为 .txt。
    """
    data = request.get_json(force=True, silent=True) or {}
    wid = (data.get("wid") or "").strip()
    files = data.get("files") or {}
    w = works_store.get(OUTPUT_DIR, wid)
    if not w:
        return jsonify({"ok": False, "error": "作品不存在"}), 404
    if not isinstance(files, dict) or not files:
        return jsonify({"ok": False, "error": "请先选择要提交的文件"}), 400

    metrics = {}
    subtitle = ""
    retention = ""
    warnings = []
    for slot, path in files.items():
        if not path:
            continue
        path = str(path).strip()
        if not path:
            continue
        if not os.path.isfile(path):
            warnings.append(f"文件不存在：{path}")
            continue
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".txt":
                txt = data_import.read_txt(path)
                if txt:
                    subtitle = txt
                else:
                    warnings.append(f"字幕稿为空：{os.path.basename(path)}")
            elif ext in (".xlsx", ".xlsm"):
                m, wlist = data_import.parse_xlsx(path)
                metrics.update(m)
                warnings.extend(wlist)
                # 留存曲线（每秒留存率 + 同类作品对标）优先从「内容吸引力」xlsx 提取
                rt = data_import.parse_retention(path)
                if rt:
                    retention = rt
            elif ext == ".xls":
                warnings.append(f"暂不支持旧版 .xls，请另存为 .xlsx：{os.path.basename(path)}")
            else:
                warnings.append(f"无法识别的文件类型：{os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"解析失败 {os.path.basename(path)}：{e}")

    # 落库：指标 + 字幕稿 + 留存曲线；状态从「发布前」推进到「已发布」
    if metrics:
        works_store.save_metrics(OUTPUT_DIR, wid, data_import.normalize_metrics(metrics))
    if subtitle:
        def _s(x):
            x["subtitle"] = subtitle
            if retention:
                x["retention"] = retention
        works_store.update_work(OUTPUT_DIR, wid, _s)
    elif retention:
        def _r(x):
            x["retention"] = retention
        works_store.update_work(OUTPUT_DIR, wid, _r)

    def _status(x):
        if x.get("status") in ("draft", "discussing", "to_adopt"):
            x["status"] = "published"
    works_store.update_work(OUTPUT_DIR, wid, _status)

    # 提交完 → 数据专员自动开干（后台线程，避免阻塞请求）
    analysis_started = False
    if subtitle:
        threading.Thread(target=_run_import_analysis, args=(wid,), daemon=True).start()
        analysis_started = True

    return jsonify({
        "ok": True,
        "metrics": metrics,
        "subtitle": subtitle,
        "retention": retention,
        "analysis_started": analysis_started,
        "warnings": warnings,
    })


@app.route("/api/manual_metrics", methods=["POST"])
def api_manual_metrics():
    """手动录入作品数据指标（不选文件，直接在弹窗里填数值）。"""
    data = request.get_json(force=True, silent=True) or {}
    wid = (data.get("wid") or "").strip()
    metrics = data.get("metrics") or {}
    w = works_store.get(OUTPUT_DIR, wid)
    if not w:
        return jsonify({"ok": False, "error": "作品不存在"}), 404
    if not isinstance(metrics, dict) or not metrics:
        return jsonify({"ok": False, "error": "请至少填写一项指标"}), 400
    norm = data_import.normalize_metrics({k: v for k, v in metrics.items() if v not in (None, "")})
    works_store.save_metrics(OUTPUT_DIR, wid, norm)
    works_store.update_work(OUTPUT_DIR, wid, lambda x: (
        x.update({"status": "published"}) if x.get("status") in ("draft", "discussing", "to_adopt") else None))
    return jsonify({"ok": True, "work": works_store.get(OUTPUT_DIR, wid)})


@app.route("/api/works/batch", methods=["POST"])
def api_works_batch():
    """作品库批量操作：{action: 'archive'|'restore'|'delete', ids: [wid...]}。"""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action") or ""
    ids = data.get("ids") or []
    if action not in ("archive", "restore", "delete"):
        return jsonify({"ok": False, "error": "不支持的批量操作"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "请先勾选作品"}), 400
    done, missing = 0, []
    for wid in ids:
        w = works_store.get(OUTPUT_DIR, wid)
        if not w:
            missing.append(wid)
            continue
        if action == "archive":
            works_store.archive(OUTPUT_DIR, wid)
        elif action == "restore":
            works_store.restore(OUTPUT_DIR, wid)
        elif action == "delete":
            works_store.delete(OUTPUT_DIR, wid)
        done += 1
    notify_store.add(OUTPUT_DIR, "system", f"已批量{ {'archive':'归档','restore':'恢复','delete':'删除'}[action] } {done} 个作品",
                     f"操作：{action}，共 {done} 个。", {"view": "works"})
    return jsonify({"ok": True, "done": done, "missing": missing})


@app.route("/api/works/<wid>/archive", methods=["POST"])
def api_work_archive(wid):
    w = works_store.archive(OUTPUT_DIR, wid)
    if not w:
        return jsonify({"ok": False, "error": "作品不存在"}), 404
    return jsonify({"ok": True, "work": w})


@app.route("/api/works/<wid>/restore", methods=["POST"])
def api_work_restore(wid):
    w = works_store.restore(OUTPUT_DIR, wid)
    if not w:
        return jsonify({"ok": False, "error": "作品不存在"}), 404
    return jsonify({"ok": True, "work": w})


@app.route("/api/works/<wid>/delete", methods=["POST"])
def api_work_delete(wid):
    """硬删除作品（真删，不可恢复）。"""
    removed = works_store.delete(OUTPUT_DIR, wid)
    if not removed:
        return jsonify({"ok": False, "error": "作品不存在或已删除"}), 404
    return jsonify({"ok": True})


@app.route("/api/overview")
def api_overview():
    """工作台首页数据：统计卡片 + 最近动态 + 专家贡献榜 + 正确率排名。"""
    ov = works_store.overview(OUTPUT_DIR)
    stats = stats_store.load_stats(OUTPUT_DIR)
    ov["rank_text"] = stats_store.rank_text(stats)
    ov["score_accuracy"] = stats_store.score_accuracy_text(stats)
    return jsonify({"ok": True, **ov})


@app.route("/api/learnings")
def api_learnings():
    """爆款学习档案：按专家分组返回吸收条目（最近的在前）。"""
    config = load_config()
    experts = []
    for acfg in config["agents"]:
        path = learn_store.lessons_path(DIGEST_DIR, acfg["id"])
        items = learn_store._parse_items(path)
        entries = [{"date": it["date"], "no": it["no"], "text": "\n".join(it["lines"])}
                   for it in reversed(items)]
        experts.append({
            "id": acfg["id"], "name": acfg["name"], "title": acfg["title"],
            "count": len(entries), "items": entries,
        })
    return jsonify({"ok": True, "experts": experts})


@app.route("/api/learnings/manual", methods=["POST"])
def api_learnings_manual():
    """手动新增一条学习档案条目（学习档案页「➕ 手动新增」按钮）。"""
    data = request.get_json(force=True, silent=True) or {}
    agent_id = (data.get("agent_id") or "").strip()
    point = (data.get("point") or "").strip()
    apply = (data.get("apply") or "").strip()
    quote = (data.get("quote") or "").strip()
    source = (data.get("source") or "").strip()
    if not agent_id:
        return jsonify({"ok": False, "error": "请选择专家"}), 400
    config = load_config()
    by_id = {a["id"]: a for a in config["agents"]}
    if agent_id not in by_id:
        # 兼容前端直接传专家名
        match = next((a for a in config["agents"] if a["name"] == agent_id), None)
        if not match:
            return jsonify({"ok": False, "error": "专家不存在"}), 400
        agent_id = match["id"]
    path, ok = learn_store.add_manual_lesson(DIGEST_DIR, agent_id, point, apply, quote, source)
    if not ok:
        return jsonify({"ok": False, "error": "知识点不能为空；若提供原文摘录与原文，摘录须能在原文中找到"}), 400
    notify_store.add(OUTPUT_DIR, "system", "学习档案已手动新增",
                     f"专家 {agent_id} 新增吸收条目：{point[:40]}", {"view": "learn"})
    return jsonify({"ok": True, "path": path})


@app.route("/api/wipe", methods=["POST"])
def api_wipe():
    """清空数据（设置页危险操作，需 confirm=DELETE 二次确认）：
    stats=统计与反馈档案 / lessons=爆款学习档案 / works=作品库。"""
    data = request.get_json(force=True, silent=True) or {}
    kind = (data.get("kind") or "").strip()
    if (data.get("confirm") or "").strip() != "DELETE":
        return jsonify({"ok": False, "error": "请输入 DELETE 确认后操作"}), 400
    if kind == "stats":
        p = stats_store.stats_path(OUTPUT_DIR)
        if os.path.exists(p):
            os.remove(p)
    elif kind == "lessons":
        d = learn_store.lessons_dir(DIGEST_DIR)
        for fn in os.listdir(d):
            if fn.endswith("_lessons.md"):
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:  # noqa: BLE001
                    pass
    elif kind == "works":
        works_store.wipe_works(OUTPUT_DIR)
    else:
        return jsonify({"ok": False, "error": "未知清理类型"}), 400
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "running": len(SESSIONS),
        "ts": datetime.datetime.now().isoformat(),
        "data_dir": DATA_DIR,
        "output_dir": OUTPUT_DIR,
    })


@app.route("/api/notifications")
def api_notifications():
    """通知中心：返回最近通知 + 未读数。"""
    return jsonify({"ok": True, **notify_store.list_all(OUTPUT_DIR)})


@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    """标记通知已读：{id} 单个 / 不传 id 全部已读。"""
    data = request.get_json(force=True, silent=True) or {}
    nid = (data.get("id") or "").strip() or None
    cnt = notify_store.mark_read(OUTPUT_DIR, nid)
    return jsonify({"ok": True, "marked": cnt})


@app.route("/api/notifications/clear", methods=["POST"])
def api_notifications_clear():
    """清空全部通知。"""
    cnt = notify_store.clear(OUTPUT_DIR)
    return jsonify({"ok": True, "cleared": cnt})


@app.route("/api/context")
def api_context():
    """返回全局创作背景（账号身份 + 目标受众），前端顶部提示条动态读取。"""
    config = load_config()
    ctx = config.get("context") or {}
    return jsonify({"ok": True, "context": ctx})


@app.route("/api/context", methods=["PUT"])
def api_context_update():
    """在线编辑创作背景（账号定位/目标受众/赛道），即时写入 config.json 并生效。"""
    data = request.get_json(force=True, silent=True) or {}
    path = os.path.join(BASE_DIR, "config.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    ctx = cfg.setdefault("context", {})
    for k in ("user_identity", "target_audience", "track"):
        if k in data:
            ctx[k] = (data.get(k) or "").strip()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return jsonify({"ok": True, "context": ctx})


@app.route("/api/session/<sid>")
def api_session(sid):
    """返回会话当前状态（成员 + 历史事件），供页面刷新后恢复。内存没有时从磁盘重建。"""
    session = SESSIONS.get(sid)
    if not session:
        session = _load_session_from_disk(sid)
        if session:
            with SESSIONS_LOCK:
                SESSIONS.setdefault(sid, session)
        else:
            return jsonify({"ok": False, "error": "会话不存在或已过期"}), 404
    return jsonify({
        "ok": True,
        "sid": sid,
        "members": session.members,
        "history": session.history,
        "finished": session.finished,
        "script": session.script,
        "work_id": session.work_id,
        "ts": session.ts,
        "live": not getattr(session, "frozen", False),
        "interrupted": bool(getattr(session, "interrupted", False)),
    })


# ---------------------------------------------------------------- 洗稿工坊

def _rw_members(session: "Session"):
    """洗稿会话成员 = 全部文案专家 + 阿数 + 阿审（不含记录员参与创作，但保留列表展示）。"""
    session.members = []
    _ensure_members(session)
    cfg = load_config()
    for key in ("data_analyst", "principle_reviewer"):
        rc = cfg.get(key) or {}
        if rc.get("name") and not any(m.get("name") == rc["name"] for m in session.members):
            session.members.append({
                "name": rc["name"], "title": rc.get("title", ""),
                "color": rc.get("color", "#888"),
            })


def _get_rw_session(rid: str):
    """按洗稿 rid 找回承载会话：内存优先；重启后从磁盘重建「可写」会话（用于评论/定稿/评价）。"""
    with SESSIONS_LOCK:
        for s in SESSIONS.values():
            if (s.rw or {}).get("rid") == rid and not getattr(s, "frozen", False):
                return s
    # 磁盘重建：扫描存档找 rw.rid 匹配的（文件名是 <sid>.json，无 s_ 前缀）
    if os.path.isdir(SESSIONS_DIR):
        for fn in os.listdir(SESSIONS_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            if (data.get("rw") or {}).get("rid") == rid:
                s = Session()
                s.sid = data.get("sid", fn[:-5])
                s.members = data.get("members", [])
                s.history = data.get("history", [])
                s.script = data.get("script", "")
                s.work_id = data.get("work_id", "")
                s.work_title = data.get("work_title", "")
                s.ts = data.get("ts", "")
                s.adoptions = data.get("adoptions", [])
                s.merges = data.get("merges", [])
                s.rw = data.get("rw", {})
                s.seq = len(s.history)
                s.finished = True
                s.frozen = False          # 可继续承载评论/定稿事件流
                s.interrupted = False
                s.rw["rid"] = rid
                with SESSIONS_LOCK:
                    SESSIONS[s.sid] = s
                return s
    return None


@app.route("/api/rewrite/start", methods=["POST"])
def api_rewrite_start():
    """启动一篇洗稿：原稿 + 四维数据 +（可选）洗稿要求。"""
    data = request.get_json(force=True, silent=True) or {}
    original = (data.get("original") or "").strip()
    if not original:
        return jsonify({"ok": False, "error": "原稿文案不能为空"}), 400
    if len(original) > 100000:
        return jsonify({"ok": False, "error": "原稿过长（超过 10 万字）"}), 400
    metrics = {
        "likes": data.get("likes", ""),
        "comments": data.get("comments", ""),
        "forwards": data.get("forwards", ""),
        "saves": data.get("saves", ""),
    }
    requirements = (data.get("requirements") or "").strip()

    sid = uuid.uuid4().hex[:12]
    session = Session()
    session.sid = sid
    session.script = original
    session.rw = {
        "rid": "", "original": original, "metrics": metrics,
        "requirements": requirements, "status": "running",
    }
    _rw_members(session)
    with SESSIONS_LOCK:
        SESSIONS[sid] = session

    # 存档条目（rid 先建，供后台线程绑定）
    entry = rewrite_store.create_session(OUTPUT_DIR, original, metrics, requirements)
    session.rw["rid"] = entry["id"]

    def _run():
        try:
            rewrite_flow.start_rewrite_flow(session, load_config(), original, metrics, requirements, OUTPUT_DIR, entry["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[server] 洗稿流程启动异常: {e}")
            session.push({"type": "error", "text": f"洗稿流程出错：{e}"})
            session.finished = True
            session.end_phase()
            session.push({"type": "done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid, "rid": entry["id"], "members": session.members})


@app.route("/api/rewrite/list")
def api_rewrite_list():
    return jsonify({"ok": True, "sessions": rewrite_store.list_sessions(OUTPUT_DIR)})


@app.route("/api/rewrite/meta")
def api_rewrite_meta():
    return jsonify({
        "ok": True,
        "regions": rewrite_store.get_regions(OUTPUT_DIR),
        "assignments": rewrite_store.get_assignments(OUTPUT_DIR),
        "replacement_log": rewrite_store.replacement_log(OUTPUT_DIR),
        "evaluation": rewrite_store.get_evaluation(OUTPUT_DIR),
        "evaluated_count": len(rewrite_store.evaluated_sessions(OUTPUT_DIR)),
    })


def _strip_author_prefix(s: str) -> str:
    """剥掉旧数据里专家回复自带的作者前缀（「XX：」「【整体节奏】」等），返回干净句子。
    若整句是专家的过程性/指令性话语（如「收到，按骨架…」「交给评论区去吵」「我来…」），返回空串丢弃。"""
    t = (s or "").strip()
    # 纯分隔符（---、*** 等）直接丢弃
    if re.fullmatch(r"[—\-*＿=~·\.\s]+", t):
        return ""
    # 剥掉整段开头的「【某某】」独占行前缀
    t = re.sub(r"^【[^】]+】\s*", "", t).strip()
    # 剥掉句首「作者名：」前缀（中文冒号或英文冒号，后接正文）
    t = re.sub(r"^[^\s，。！？！；]{1,6}[:：]\s*", "", t).strip()
    if not t:
        return ""
    # 过滤明显的过程性/指令性句子（旧数据的专家自言自语）
    _PROC = (
        "收到", "我来", "我负责", "我这边", "已收到", "好的", "好嘞", "嗯，", "明白",
        "按骨架", "按第", "这一part", "这一部分", "这部分", "交给评论区", "我只负责",
        "先给你", "我来写", "我先把", "我的思路", "这里我", "这part",
    )
    head = t[:6]
    if any(k in head for k in _PROC):
        return ""
    return t


def _backfill_final_from_parts(output_dir, entry: dict) -> dict:
    """旧数据兼容：若洗稿存档没有 final 分区（老流程跑的），则按 regions 顺序把各分区
    内容现场拼成一个 final 分区（剥作者前缀、逐句切分、记录每句作者），供前端以「连续文章」渲染。
    因旧流程 8 个分区常各自完整成稿、内容互相重复，拼接时做跨分区归一化去重，避免成品变成堆砌。
    返回（可能是新 dict 的）entry。"""
    parts = entry.get("parts") or {}
    if not parts:
        return entry
    if parts.get("final") and parts["final"].get("sentences"):
        return entry  # 已有 final，无需兜底
    try:
        regions = rewrite_store.get_regions(output_dir)
    except Exception:  # noqa: BLE001
        regions = []
    if not regions:
        return entry
    assignments = {}
    try:
        assignments = rewrite_store.get_assignments(output_dir) or {}
    except Exception:  # noqa: BLE001
        assignments = {}
    sents, agents = [], []
    seen = set()  # 归一化后句子，用于跨分区去重
    for r in regions:
        rid = r["id"]
        if rid == "final":
            continue
        part = parts.get(rid) or {}
        s_list = part.get("sentences")
        if not s_list:
            s_list = rewrite_store.split_sentences(part.get("text", ""))
        agent = part.get("agent") or assignments.get(rid) or r.get("default") or ""
        for s in s_list:
            if not s or not str(s).strip():
                continue
            clean = _strip_author_prefix(str(s))
            if not clean:
                continue
            norm = re.sub(r"[\s\u3000]+", "", clean)
            if not norm or norm in seen:
                continue  # 跳过与前面分区重复的句子
            seen.add(norm)
            sents.append(clean)
            agents.append(agent)
    # 只要拼出了内容，就补一个 final
    if sents:
        parts["final"] = {
            "agent": "",
            "text": "\n\n".join(sents),
            "sentences": sents,
            "agents": agents,
            "comments": [],
            "backfilled": True,   # 标记为后端兼容生成的成品
        }
    return entry


@app.route("/api/rewrite/<rid>")
def api_rewrite_get(rid):
    entry = rewrite_store.get_session(OUTPUT_DIR, rid)
    if not entry:
        return jsonify({"ok": False, "error": "洗稿记录不存在"}), 404
    # 旧数据兜底：无 final 时现场拼一个连续文章成品，保证旧会话也能以连续文章展示
    _backfill_final_from_parts(OUTPUT_DIR, entry)
    sess = _get_rw_session(rid)
    return jsonify({"ok": True, "session": entry, "sid": sess.sid if sess else ""})


@app.route("/api/rewrite/<rid>/redo", methods=["POST"])
def api_rewrite_redo(rid):
    """「重新洗」：保留原稿与四维数据，换要求一键重跑（生成新 rid）。"""
    data = request.get_json(force=True, silent=True) or {}
    requirements = (data.get("requirements") or "").strip()
    old = rewrite_store.get_session(OUTPUT_DIR, rid)
    if not old:
        return jsonify({"ok": False, "error": "找不到该洗稿记录"}), 404
    entry = rewrite_store.redo_session(OUTPUT_DIR, rid, requirements)
    if not entry:
        return jsonify({"ok": False, "error": "重新洗失败"}), 500
    # 启动新会话（复用 start 的流程）
    session = Session()
    session.sid = uuid.uuid4().hex[:12]
    session.script = entry["original"]
    session.rw = {
        "rid": entry["id"], "original": entry["original"],
        "metrics": entry.get("metrics") or {},
        "requirements": entry.get("requirements") or "", "status": "running",
    }
    _rw_members(session)
    with SESSIONS_LOCK:
        SESSIONS[session.sid] = session

    def _run():
        try:
            rewrite_flow.start_rewrite_flow(
                session, load_config(), entry["original"],
                entry.get("metrics") or {}, entry.get("requirements") or "",
                OUTPUT_DIR, entry["id"],
            )
        except Exception as e:  # noqa: BLE001
            print(f"[server] 重新洗流程异常: {e}")
            session.push({"type": "error", "text": f"洗稿流程出错：{e}"})
            session.finished = True
            session.end_phase()
            session.push({"type": "done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    notify_store.add(OUTPUT_DIR, "rewrite", f"已重新洗稿：{entry.get('title') or entry['id']}",
                     "保留原稿与数据，新要求已生效。", {"view": "rewrite", "rid": entry["id"]})
    return jsonify({"ok": True, "sid": session.sid, "rid": entry["id"], "members": session.members})


@app.route("/api/rewrite/batch", methods=["POST"])
def api_rewrite_batch():
    """批量删除洗稿记录。"""
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids") or []
    action = data.get("action") or "delete"
    if action != "delete" or not ids:
        return jsonify({"ok": False, "error": "参数错误"}), 400
    n = rewrite_store.delete_sessions(OUTPUT_DIR, ids)
    return jsonify({"ok": True, "done": n})


@app.route("/api/rewrite/winrate")
def api_rewrite_winrate():
    """负责人胜率曲线数据（按区域）。"""
    return jsonify({"ok": True, **rewrite_store.winrate(OUTPUT_DIR)})


@app.route("/api/rewrite/<rid>/comment", methods=["POST"])
def api_rewrite_comment(rid):
    """用户对某区域评论 → 负责该区域的专家重写。"""
    data = request.get_json(force=True, silent=True) or {}
    session = _get_rw_session(rid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    region_id = (data.get("region") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not region_id or not comment:
        return jsonify({"ok": False, "error": "缺少区域或评论内容"}), 400
    if not session.try_begin("rw_comment"):
        return jsonify({"ok": False, "error": "另一项任务正在进行中，请稍后再试"}), 409
    session.finished = False

    def _run():
        try:
            rewrite_flow.run_part_comment(session, load_config(), rid, region_id, comment, OUTPUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[server] 洗稿评论迭代异常: {e}")
            session.push({"type": "error", "text": f"评论迭代出错：{e}"})
        finally:
            session.end_phase()
            session.finished = True
            session.push({"type": "done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/rewrite/<rid>/sentence_comment", methods=["POST"])
def api_rewrite_sentence_comment(rid):
    """用户对成品【某一句话】评论 → 负责该分区的专家只重写这一句。"""
    data = request.get_json(force=True, silent=True) or {}
    session = _get_rw_session(rid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    region_id = (data.get("region") or "").strip()
    sentence = (data.get("sentence") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not region_id or not sentence or not comment:
        return jsonify({"ok": False, "error": "缺少区域、句子或评论内容"}), 400
    if not session.try_begin("rw_comment"):
        return jsonify({"ok": False, "error": "另一项任务正在进行中，请稍后再试"}), 409
    session.finished = False

    def _run():
        try:
            rewrite_flow.run_sentence_comment(session, load_config(), rid, region_id, sentence, comment, OUTPUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[server] 洗稿句级评论迭代异常: {e}")
            session.push({"type": "error", "text": f"句级评论迭代出错：{e}"})
        finally:
            session.end_phase()
            session.finished = True
            session.push({"type": "done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/rewrite/<rid>/finalize", methods=["POST"])
def api_rewrite_finalize(rid):
    """满意后：最终阿审审查 + 阿数记录分工。"""
    session = _get_rw_session(rid)
    if not session:
        return jsonify({"ok": False, "error": "会话不存在或已过期"}), 400
    if not session.try_begin("rw_finalize"):
        return jsonify({"ok": False, "error": "另一项任务正在进行中，请稍后再试"}), 409
    session.finished = False

    def _run():
        try:
            rewrite_flow.run_final_review(session, load_config(), rid, OUTPUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[server] 洗稿定稿异常: {e}")
            session.push({"type": "error", "text": f"定稿流程出错：{e}"})
        finally:
            session.end_phase()
            session.finished = True
            session.push({"type": "done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/rewrite/<rid>/result", methods=["POST"])
def api_rewrite_result(rid):
    """用户回填成品数据（点赞/评论/转发/收藏）。"""
    data = request.get_json(force=True, silent=True) or {}
    if not rewrite_store.get_session(OUTPUT_DIR, rid):
        return jsonify({"ok": False, "error": "找不到该洗稿记录，无法回填数据"}), 404
    saved = rewrite_store.with_result_metrics(OUTPUT_DIR, rid, {
        "likes": data.get("likes", ""),
        "comments": data.get("comments", ""),
        "forwards": data.get("forwards", ""),
        "saves": data.get("saves", ""),
    })
    if not saved:
        return jsonify({"ok": False, "error": "洗稿记录已变化，数据未保存，请刷新后重试"}), 409
    return jsonify({"ok": True, "evaluated_count": len(rewrite_store.evaluated_sessions(OUTPUT_DIR))})


@app.route("/api/rewrite/<rid>/to_work", methods=["POST"])
def api_rewrite_to_work(rid):
    """把洗稿成品一键保存为作品库作品，四维数据写入 metrics，打通数据闭环。"""
    entry = rewrite_store.get_session(OUTPUT_DIR, rid)
    if not entry:
        return jsonify({"ok": False, "error": "找不到该洗稿记录"})
    parts = entry.get("parts") or {}
    regions = rewrite_store.REGIONS
    lines = []
    for r in regions:
        p = parts.get(r["id"]) or {}
        if p.get("text"):
            lines.append("【" + r["label"] + "】\n" + p["text"])
    final_text = "\n\n".join(lines).strip()
    if not final_text:
        return jsonify({"ok": False, "error": "该洗稿还没有成品内容"})
    title = (entry.get("title") or "").strip() or ("✂️ 洗稿成品 " + (entry.get("created_at") or "")[:10])
    w = works_store.create(OUTPUT_DIR, title, entry.get("original") or "", "", note="✂️ 来自洗稿工坊")
    if w:
        works_store.set_final(OUTPUT_DIR, w["id"], final_text)
        rm = entry.get("result_metrics") or {}
        metrics = {}
        if rm.get("likes") not in (None, ""):
            metrics["点赞量"] = rm["likes"]
        if rm.get("comments") not in (None, ""):
            metrics["评论量"] = rm["comments"]
        if rm.get("forwards") not in (None, ""):
            metrics["分享量"] = rm["forwards"]
        if rm.get("saves") not in (None, ""):
            metrics["收藏量"] = rm["saves"]
        if metrics:
            works_store.save_metrics(OUTPUT_DIR, w["id"], metrics)
    try:
        data_insight_store.count_principle_hits(OUTPUT_DIR, final_text)
    except Exception:  # noqa: BLE001
        pass
    notify_store.add(OUTPUT_DIR, "rewrite", f"洗稿成品已保存为作品：{title}",
                     "已并入作品库，可在作品库中继续讨论/复盘。",
                     {"view": "works", "wid": w["id"] if w else None})
    return jsonify({"ok": True, "wid": w["id"] if w else None, "title": title})


@app.route("/api/rewrite/evaluate", methods=["POST"])
def api_rewrite_evaluate():
    """满 3 篇：阿数建立评价标准 + 判断负责人。"""
    data = request.get_json(force=True, silent=True) or {}
    # 前置校验：不足 3 篇成品数据直接拦截（不进后台线程，前端能第一时间拿到 error）
    _evaluated = rewrite_store.evaluated_sessions(OUTPUT_DIR)
    if len(_evaluated) < 3:
        return jsonify({"ok": False, "error": f"已有成品数据回填的洗稿 {len(_evaluated)} 篇，满 3 篇才能建立评价标准。"})
    sid = (data.get("sid") or "").strip()
    session = SESSIONS.get(sid)
    if not session:
        # 无承载会话时自动创建一个临时会话（仅用于推送评价事件流）
        sid = "rwe_" + uuid.uuid4().hex[:10]
        session = Session()
        session.sid = sid
        session.script = ""
        session.rw = {"rid": "", "status": "running", "kind": "evaluate"}
        with SESSIONS_LOCK:
            SESSIONS[sid] = session
    if not session.try_begin("rw_evaluate"):
        return jsonify({"ok": False, "error": "另一项任务正在进行中，请稍后再试"}), 409
    session.finished = False

    def _run():
        try:
            rewrite_flow.run_evaluate(session, load_config(), OUTPUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[server] 洗稿评价异常: {e}")
            session.push({"type": "error", "text": f"评价流程出错：{e}"})
        finally:
            session.end_phase()
            session.finished = True
            session.push({"type": "done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/rewrite/apply", methods=["POST"])
def api_rewrite_apply():
    """应用负责人替换（评价结果确认后）。"""
    data = request.get_json(force=True, silent=True) or {}
    replacements = data.get("replacements") or []
    if not replacements:
        return jsonify({"ok": False, "error": "没有可应用的替换"}), 400
    rewrite_store.apply_replacements(OUTPUT_DIR, replacements)
    return jsonify({"ok": True, "assignments": rewrite_store.get_assignments(OUTPUT_DIR)})


# ---------- 会话生命周期清理 ----------

# ---------------------------------------------------------------- 对标监控

@app.route("/api/monitor/accounts", methods=["GET"])
def api_monitor_accounts_list():
    return jsonify(monitor_server.get_accounts(DATA_DIR, OUTPUT_DIR))


@app.route("/api/monitor/accounts", methods=["POST"])
def api_monitor_accounts_add():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(
        monitor_server.add_account_with_fetch(
            DATA_DIR,
            data.get("home_url", ""),
            data.get("note", ""),
        )
    )


@app.route("/api/monitor/resolve", methods=["POST"])
def api_monitor_resolve():
    """粘贴主页链接后预览资料（昵称/粉丝数/作品数），供前端确认后再添加。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(monitor_server.resolve_account(data.get("home_url", "")))


@app.route("/api/monitor/accounts/<aid>", methods=["PUT"])
def api_monitor_accounts_update(aid):
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(monitor_server.update_account(DATA_DIR, aid, data.get("note", "")))


@app.route("/api/monitor/accounts/<aid>", methods=["DELETE"])
def api_monitor_accounts_delete(aid):
    return jsonify(monitor_server.remove_account(DATA_DIR, aid))


@app.route("/api/monitor/fetch", methods=["POST"])
def api_monitor_fetch():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(monitor_server.start_fetch(DATA_DIR, OUTPUT_DIR, force=bool(data.get("force"))))


@app.route("/api/monitor/status", methods=["GET"])
def api_monitor_status():
    return jsonify(monitor_server.get_status())


@app.route("/api/monitor/report", methods=["GET"])
def api_monitor_report():
    return jsonify(monitor_server.get_report(OUTPUT_DIR))


@app.route("/api/monitor/transcript", methods=["POST"])
def api_monitor_transcript():
    """按 aweme_id 获取视频完整口播文案（逐字稿），供查看/编辑/洗稿。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(
        monitor_server.get_video_transcript(
            OUTPUT_DIR,
            data.get("aweme_id", ""),
            _api_config(),
        )
    )


# ---------------------------------------------------------------- 自动轮询（对标监控·持续盯号）

@app.route("/api/monitor/poll/start", methods=["POST"])
def api_monitor_poll_start():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(
        monitor_server.start_poll(
            DATA_DIR,
            OUTPUT_DIR,
            interval=data.get("interval"),
            count=data.get("count", 10),
            force=bool(data.get("force")),
        )
    )


@app.route("/api/monitor/poll/stop", methods=["POST"])
def api_monitor_poll_stop():
    return jsonify(monitor_server.stop_poll())


@app.route("/api/monitor/poll/status", methods=["GET"])
def api_monitor_poll_status():
    return jsonify(monitor_server.get_poll_status())


@app.route("/api/monitor/poll/report", methods=["GET"])
def api_monitor_poll_report():
    return jsonify(monitor_server.get_poll_report(OUTPUT_DIR))


@app.route("/api/monitor/alerts", methods=["GET"])
def api_monitor_alerts():
    return jsonify(monitor_server.get_alerts(OUTPUT_DIR))


@app.route("/api/monitor/alerts/read", methods=["POST"])
def api_monitor_alerts_read():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(monitor_server.mark_alerts_read(OUTPUT_DIR, data.get("id")))


# ---------------------------------------------------------------- 选题雷达（内置晨报）

@app.route("/api/radar/generate", methods=["POST"])
def api_radar_generate():
    return jsonify(radar_server.generate_radar(BASE_DIR, OUTPUT_DIR))


@app.route("/api/radar/latest", methods=["GET"])
def api_radar_latest():
    return jsonify(radar_server.latest_radar(OUTPUT_DIR))


# ---------------------------------------------------------------- 配音工坊（IndexTTS-2.5）


@app.route("/api/tts/settings", methods=["GET"])
def api_tts_settings():
    return jsonify(tts_server.get_settings(OUTPUT_DIR))


@app.route("/api/tts/settings", methods=["POST"])
def api_tts_settings_save():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tts_server.save_settings(OUTPUT_DIR, data.get("token", ""),
                                            backend=data.get("backend", "")))


@app.route("/api/system/gpu", methods=["GET"])
def api_system_gpu():
    """本机 GPU 信息（供设置页展示，判断是否可跑本地 IndexTTS）。"""
    return jsonify({"ok": True, "gpu": tts_server._gpu_info()})


@app.route("/api/tts/test", methods=["POST"])
def api_tts_test():
    return jsonify(tts_server.test_connection(OUTPUT_DIR))


@app.route("/api/tts/presets", methods=["GET"])
def api_tts_presets():
    return jsonify(tts_server.list_presets(OUTPUT_DIR))


@app.route("/api/tts/presets", methods=["POST"])
def api_tts_presets_add():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tts_server.add_preset(OUTPUT_DIR, data.get("name", ""),
                                         data.get("ref_path", ""), data.get("note", "")))


@app.route("/api/tts/presets/<pid>", methods=["DELETE"])
def api_tts_presets_delete(pid):
    return jsonify(tts_server.remove_preset(OUTPUT_DIR, pid))


@app.route("/api/tts/presets/<pid>", methods=["PATCH"])
def api_tts_presets_update(pid):
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tts_server.update_preset(OUTPUT_DIR, pid, data.get("name"), data.get("note")))


@app.route("/api/tts/presets/<pid>/cleanup-duplicates", methods=["POST"])
def api_tts_presets_cleanup_duplicates(pid):
    return jsonify(tts_server.cleanup_duplicate_presets(OUTPUT_DIR, pid))


@app.route("/api/tts/ref_speed", methods=["POST"])
def api_tts_ref_speed():
    """测量参考音频语速，返回建议的 duration_factor（让合成语速对齐发言人原声）。"""
    data = request.get_json(force=True, silent=True) or {}
    ref_path = data.get("ref_path", "")
    sample_text = data.get("text", "")
    # 若传 preset_id，则从预设里取参考音频路径
    if not ref_path and data.get("preset_id"):
        try:
            base = tts_server._dir(OUTPUT_DIR)
            presets = tts_server._read_json(os.path.join(base, "presets.json"), [])
            preset = next((p for p in presets if p.get("id") == data["preset_id"]), None)
            if preset:
                ref_path = os.path.join(base, preset.get("ref_audio", ""))
        except Exception:
            ref_path = ""
    return jsonify(tts_server.measure_ref_speed(ref_path, sample_text))


@app.route("/api/tts/upload", methods=["POST"])
def api_tts_upload():
    f = request.files.get("file")
    if f is None:
        return jsonify({"ok": False, "error": "未收到音频文件"}), 400
    return jsonify(tts_server.save_upload(OUTPUT_DIR, f))


@app.route("/api/tts/generate", methods=["POST"])
def api_tts_generate():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tts_server.start_generate(OUTPUT_DIR, data))


@app.route("/api/tts/status", methods=["GET"])
def api_tts_status():
    return jsonify(tts_server.get_status())


@app.route("/api/tts/task/<task_id>", methods=["GET"])
def api_tts_task(task_id):
    return jsonify(tts_server.get_task_status(task_id))


@app.route("/api/tts/cancel", methods=["POST"])
def api_tts_cancel():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tts_server.cancel_generate(data.get("task_id")))


@app.route("/api/tts/history", methods=["GET"])
def api_tts_history():
    return jsonify(tts_server.get_history(OUTPUT_DIR))


@app.route("/api/tts/history/<hid>", methods=["DELETE"])
def api_tts_history_delete(hid):
    return jsonify(tts_server.remove_history(OUTPUT_DIR, hid))


@app.route("/api/tts/audio/<path:fname>", methods=["GET"])
def api_tts_audio(fname):
    return send_from_directory(os.path.join(OUTPUT_DIR, "tts", "audio"), fname)


# ---------------------------------------------------------------- 批量 TTS（对话逐条合成）

@app.route("/api/tts/batch", methods=["POST"])
def api_tts_batch():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tts_server.start_batch(OUTPUT_DIR, data))


@app.route("/api/tts/batch/status", methods=["GET"])
def api_tts_batch_status():
    return jsonify(tts_server.get_batch_status())


# ---------------------------------------------------------------- 抖音链接提取 + 发言人区分

def _api_config() -> dict:
    cfg = load_config()
    return cfg.get("api") or {}


@app.route("/api/extract/link", methods=["POST"])
def api_extract_link():
    data = request.get_json(force=True, silent=True) or {}
    raw_url = (data.get("url") or "").strip()
    if not raw_url:
        return jsonify({"ok": False, "error": "请输入抖音分享链接"}), 400
    # 抖音转发内容通常不是裸 URL，而是「口令文字 + [短链接] + 复制此链接」整段文本。
    # 先复用提取模块从整段文本中抽出真正的 URL，再做域名校验；否则 urlparse 会把整段
    # 文本当成 path，导致标准 v.douyin.com 短链被错误拒绝。
    url = extract_server._extract_share_url(raw_url)
    if len(raw_url) > 20_000 or not _valid_extract_url(url):
        return jsonify({"ok": False, "error": "仅支持抖音或小红书单条分享链接"}), 400
    return jsonify(extract_server.extract_from_link(OUTPUT_DIR, url, _api_config()))


@app.route("/api/extract/text", methods=["POST"])
def api_extract_text():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "文本内容为空"}), 400
    if len(text) > MAX_EXTRACT_TEXT_CHARS:
        return jsonify({"ok": False, "error": "文本超过 20 万字，请拆分后再提取"}), 400
    return jsonify(extract_server.extract_from_text(OUTPUT_DIR, text, _api_config()))


@app.route("/api/extract/latest", methods=["GET"])
def api_extract_latest():
    return jsonify(extract_server.get_latest(OUTPUT_DIR))


@app.route("/api/extract/segment", methods=["POST"])
def api_extract_segment_update():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.update_segment(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("seg_idx", -1), data.get("speaker", "A")))


@app.route("/api/extract/segment/text", methods=["POST"])
def api_extract_segment_text():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.edit_segment_text(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("seg_idx", -1), data.get("text", "")))


@app.route("/api/extract/segment/add", methods=["POST"])
def api_extract_segment_add():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.add_segment(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("after_idx", -1),
        data.get("speaker", "A"), data.get("text", "")))


@app.route("/api/extract/segment/delete", methods=["POST"])
def api_extract_segment_delete():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.delete_segment(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("seg_idx", -1)))


@app.route("/api/extract/segment/merge", methods=["POST"])
def api_extract_segment_merge():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.merge_segments(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("seg_idx", -1)))


@app.route("/api/extract/segment/split", methods=["POST"])
def api_extract_segment_split():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.split_segment(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("seg_idx", -1), data.get("split_pos", 0)))


@app.route("/api/extract/experience", methods=["POST"])
def api_extract_experience_reextract():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.reextract_visitor_profile(
        OUTPUT_DIR, data.get("extract_id", ""), _api_config()))


@app.route("/api/extract/resegment", methods=["POST"])
def api_extract_resegment():
    """不重新下载素材，按最新规则修复已有记录的说话人和分段。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.resegment_record(
        OUTPUT_DIR, data.get("extract_id", ""), _api_config()))


@app.route("/api/extract/experience/update", methods=["POST"])
def api_extract_experience_update():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.update_visitor_profile(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("profile") or {}))


# ---------------------------------------------------------------- 语速测量（逐句实测）

@app.route("/api/extract/speed", methods=["POST"])
def api_extract_speed():
    """按目标发言人实际说的那句话在音轨里的真实时长，测量其逐句语速并固化。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.measure_speaker_speed(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("speaker", "A")))


@app.route("/api/extract/style", methods=["POST"])
def api_extract_style():
    """目标发言人「口语变化档案」：实测语速随内容变化、情绪起伏、重音停顿习惯。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.measure_speaker_style(
        OUTPUT_DIR, data.get("extract_id", ""), data.get("speaker", "A"), _api_config()))


@app.route("/api/extract/cleanup_audio", methods=["POST"])
def api_extract_cleanup_audio():
    """语速已固化后，删除音轨/视频临时文件（释放磁盘）。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(extract_server.cleanup_audio(
        OUTPUT_DIR, data.get("extract_id", "")))


# ---------------------------------------------------------------- 作品库 / 模拟对话库

@app.route("/api/workslib/accounts", methods=["GET"])
def api_workslib_accounts():
    return jsonify(works_library_server.list_accounts(OUTPUT_DIR))


@app.route("/api/workslib/accounts/add", methods=["POST"])
def api_workslib_add_account():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(works_library_server.add_account(
        OUTPUT_DIR, data.get("platform", ""), data.get("home_url", "")))


@app.route("/api/workslib/accounts/delete", methods=["POST"])
def api_workslib_delete_account():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(works_library_server.remove_account(
        OUTPUT_DIR, data.get("account_id", "")))


@app.route("/api/workslib/crawl", methods=["POST"])
def api_workslib_crawl():
    """批量抓取：遍历所有账号，抓视频列表 + 扒文案，后台执行。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(works_library_server.start_crawl(
        OUTPUT_DIR, count=int(data.get("count", 50)), api_config=_api_config()))


@app.route("/api/workslib/status", methods=["GET"])
def api_workslib_status():
    return jsonify(works_library_server.get_status())


@app.route("/api/workslib/videos", methods=["GET"])
def api_workslib_videos():
    account_id = request.args.get("account_id", "")
    return jsonify(works_library_server.get_videos(OUTPUT_DIR, account_id))


@app.route("/api/workslib/video", methods=["GET"])
def api_workslib_video():
    account_id = request.args.get("account_id", "")
    aweme_id = request.args.get("aweme_id", "")
    return jsonify(works_library_server.get_video_detail(OUTPUT_DIR, account_id, aweme_id))


@app.route("/api/workslib/video/delete", methods=["POST"])
def api_workslib_delete_video():
    data = request.get_json(force=True, silent=True) or {}
    # exclude=true 时记入排除清单，下次「抓取全部账号」不再抓回这条
    return jsonify(works_library_server.delete_video(
        OUTPUT_DIR, data.get("account_id", ""), data.get("aweme_id", ""),
        exclude=bool(data.get("exclude", False))))


@app.route("/api/workslib/video/reextract", methods=["POST"])
def api_workslib_reextract_video():
    """强制重新扒某条视频的文案（用户觉得扒得不对时）。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(works_library_server.reextract_video(
        OUTPUT_DIR, data.get("account_id", ""), data.get("aweme_id", ""),
        api_config=_api_config()))


@app.route("/api/workslib/reextract_all", methods=["POST"])
def api_workslib_reextract_all():
    """批量重扒某个账号下「段数过少或失败」的视频。

    默认条件：segments<3 段 或 error 非空。可通过 max_segments/min_seg_count 控制。
    返回 {"ok": True, "total": N, "re_extracted": N, "skipped": N, "failed": N,
          "details": [{"aweme_id", "before", "after", "ok"}, ...]}
    """
    data = request.get_json(force=True, silent=True) or {}
    account_id = data.get("account_id", "")
    max_segments = int(data.get("max_segments", 3))  # segments < 此值视为过低
    only_errors = bool(data.get("only_errors", False))  # 只重扒有 error 的
    if not account_id:
        return jsonify({"ok": False, "error": "缺少 account_id"})
    return jsonify(works_library_server.reextract_stale_videos(
        OUTPUT_DIR, account_id, api_config=_api_config(),
        max_segments=max_segments, only_errors=only_errors))


@app.route("/api/workslib/delete_short", methods=["POST"])
def api_workslib_delete_short():
    """批量删除时长不足 60 秒的视频（同时记入排除清单，下次抓取不再抓回）。"""
    data = request.get_json(force=True, silent=True) or {}
    account_id = data.get("account_id", "")  # 为空时扫描所有账号
    min_duration_ms = int(data.get("min_duration_ms", 60000))
    return jsonify(works_library_server.delete_short_videos(
        OUTPUT_DIR, account_id, min_duration_ms))


@app.route("/api/workslib/import", methods=["POST"])
def api_workslib_import():
    """把作品库某视频的对话导入配音工坊（落成 extract 记录，复用建角色流程）。"""
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(works_library_server.import_to_extract(
        OUTPUT_DIR, data.get("account_id", ""), data.get("aweme_id", "")))


@app.route("/api/settings/weibo_cookie", methods=["GET", "POST"])
def api_settings_weibo_cookie():
    """微博主页抓取登录态 cookie 管理：GET 查状态，POST 保存/清除。"""
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        return jsonify(works_library_server.save_weibo_cookie(
            OUTPUT_DIR, data.get("cookie", "")))
    return jsonify(works_library_server.get_weibo_cookie_config(OUTPUT_DIR))


# ---------------------------------------------------------------- 语音转文字（ASR）

@app.route("/api/extract/asr/settings", methods=["GET", "POST"])
def api_asr_settings():
    import asr_server
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        return jsonify(asr_server.save_asr_settings(OUTPUT_DIR, data.get("api_key", "")))
    return jsonify(asr_server.get_asr_settings(OUTPUT_DIR))


@app.route("/api/extract/asr/transcribe", methods=["POST"])
def api_asr_transcribe():
    import asr_server
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请输入抖音分享链接"})
    # 启动后台转录任务，立即返回；前端轮询 /api/extract/asr/status 拿进度
    return jsonify(asr_server.start_transcribe(OUTPUT_DIR, url, _api_config()))


@app.route("/api/extract/asr/status", methods=["GET"])
def api_asr_status():
    import asr_server
    return jsonify(asr_server.get_status())


# ---------------------------------------------------------------- API 设置中心

@app.route("/api/settings", methods=["GET"])
def api_settings_all():
    return jsonify(api_settings_server.get_all_settings(OUTPUT_DIR))


@app.route("/api/settings/data_dir", methods=["GET"])
def api_settings_data_dir_get():
    """返回当前数据目录信息（路径 + 是否自定义 + 占用）。"""
    import shutil as _shutil
    try:
        size_bytes = 0
        for root, _dirs, files in os.walk(DATA_DIR):
            for fn in files:
                try:
                    size_bytes += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        size_bytes = 0
    return jsonify({
        "ok": True,
        "data_dir": DATA_DIR,
        "output_dir": OUTPUT_DIR,
        "size_mb": round(size_bytes / 1048576, 1),
    })


@app.route("/api/settings/data_dir", methods=["POST"])
def api_settings_data_dir_set():
    """把数据目录改到指定位置（如 E 盘），并把当前数据迁移过去。
    返回后需重启软件生效（下次启动会读取持久化配置）。"""
    import shutil as _shutil
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get("data_dir") or "").strip()
    if not target:
        return jsonify({"ok": False, "error": "请填写数据目录路径"})
    target = os.path.abspath(target)
    # 安全校验：目标不能是 C 盘根/当前数据目录本身
    if os.path.normcase(target).lower() == os.path.normcase(DATA_DIR).lower():
        return jsonify({"ok": False, "error": "目标目录与当前数据目录相同，无需迁移"})
    target_drive = os.path.splitdrive(target)[0].lower()
    if not target_drive:
        return jsonify({"ok": False, "error": "请填写完整路径，如 E:\\靓仔文案工作台数据"})
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return jsonify({"ok": False, "error": f"无法创建目标目录：{e}"})
    # 迁移：把当前 output 等内容复制到目标目录（若目标已有同内容则跳过）
    try:
        src = OUTPUT_DIR
        dst = os.path.join(target, "output")
        if os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            migrated = 0
            for name in os.listdir(src):
                s = os.path.join(src, name)
                d = os.path.join(dst, name)
                if not os.path.exists(d):
                    if os.path.isdir(s):
                        _shutil.copytree(s, d)
                    else:
                        _shutil.copy2(s, d)
                    migrated += 1
        # 持久化配置（desktop_app 下次启动读取）
        if not _write_custom_data_dir(target):
            return jsonify({"ok": False, "error": "迁移数据完成，但写入持久化配置失败"})
        return jsonify({
            "ok": True,
            "data_dir": target,
            "migrated_items": migrated,
            "message": f"数据已迁移到 {target}，请重启软件生效。重启后原 %APPDATA% 数据可手动删除。",
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"迁移失败：{e}"})


def _write_custom_data_dir(path: str) -> bool:
    """把自定义数据目录写入固定位置的持久化配置（%LOCALAPPDATA%\\靓仔文案工作台\\data_dir.txt）。"""
    try:
        cfg_dir = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "靓仔文案工作台",
        )
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "data_dir.txt"), "w", encoding="utf-8") as f:
            f.write(path)
        return True
    except Exception:  # noqa: BLE001
        return False


@app.route("/api/settings/llm", methods=["POST"])
def api_settings_llm_save():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(api_settings_server.save_llm_settings(
        data.get("base_url"), data.get("api_key"), data.get("model")))


@app.route("/api/settings/asr", methods=["POST"])
def api_settings_asr_save():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(api_settings_server.save_asr_settings(OUTPUT_DIR, data.get("api_key", "")))


@app.route("/api/settings/tts", methods=["POST"])
def api_settings_tts_save():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(api_settings_server.save_tts_settings(
        OUTPUT_DIR, data.get("token", ""), backend=data.get("backend", "")))


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    data = request.get_json(force=True, silent=True) or {}
    kind = (data.get("kind") or "").strip()
    if kind == "llm":
        return jsonify(api_settings_server.test_llm(load_config()))
    if kind == "asr":
        return jsonify(api_settings_server.test_asr(OUTPUT_DIR))
    if kind == "tts":
        return jsonify(api_settings_server.test_tts(OUTPUT_DIR))
    return jsonify({"ok": False, "error": "未知测试类型：" + kind})


@app.route("/api/settings/fetch_modelscope_token", methods=["POST"])
def api_settings_fetch_ms_token():
    return jsonify(api_settings_server.fetch_modelscope_token_from_browser())


# ---------------------------------------------------------------- Agent 模拟对话

@app.route("/api/agent/persona", methods=["POST"])
def api_agent_persona():
    data = request.get_json(force=True, silent=True) or {}
    segments = data.get("segments") or []
    speaker = data.get("speaker", "A")
    scene = data.get("scene", "")
    extra_style = data.get("extra_style", "")
    visitor_profile = data.get("visitor_profile") or None
    return jsonify(agent_chat_server.build_persona(
        OUTPUT_DIR, segments, speaker, scene, _api_config(), extra_style, visitor_profile))


@app.route("/api/agent/send", methods=["POST"])
def api_agent_send():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg = data.get("message", "")
    return jsonify(agent_chat_server.send_message(OUTPUT_DIR, sid, msg, _api_config()))


@app.route("/api/agent/update_profile", methods=["POST"])
def api_agent_update_profile():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    profile = data.get("visitor_profile") or {}
    return jsonify(agent_chat_server.update_session_profile(OUTPUT_DIR, sid, profile))


@app.route("/api/agent/insert", methods=["POST"])
def api_agent_insert():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    role = data.get("role", "user")
    content = data.get("content", "")
    after_id = data.get("after_id", "")
    return jsonify(agent_chat_server.insert_message(OUTPUT_DIR, sid, role, content, after_id))


@app.route("/api/agent/regenerate", methods=["POST"])
def api_agent_regenerate():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg_id = data.get("msg_id", "")
    comment = data.get("comment", "")
    return jsonify(agent_chat_server.regenerate(OUTPUT_DIR, sid, msg_id, comment, _api_config()))


@app.route("/api/agent/update", methods=["POST"])
def api_agent_update():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg_id = data.get("msg_id", "")
    content = data.get("content", "")
    return jsonify(agent_chat_server.update_message(OUTPUT_DIR, sid, msg_id, content))


@app.route("/api/agent/delete", methods=["POST"])
def api_agent_delete():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg_id = data.get("msg_id", "")
    return jsonify(agent_chat_server.delete_message(OUTPUT_DIR, sid, msg_id))


@app.route("/api/agent/msg_audio", methods=["POST"])
def api_agent_msg_audio():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg_id = data.get("msg_id", "")
    audio = data.get("audio", "")
    preset = data.get("preset", "")
    return jsonify(agent_chat_server.set_message_audio(OUTPUT_DIR, sid, msg_id, audio, preset))


@app.route("/api/agent/reset", methods=["POST"])
def api_agent_reset():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    return jsonify(agent_chat_server.reset_session(OUTPUT_DIR, sid))


@app.route("/api/agent/end", methods=["POST"])
def api_agent_end():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    return jsonify(agent_chat_server.end_session(OUTPUT_DIR, sid))


@app.route("/api/agent/session/<sid>", methods=["GET"])
def api_agent_session(sid):
    return jsonify(agent_chat_server.get_session(OUTPUT_DIR, sid))


@app.route("/api/agent/sessions", methods=["GET"])
def api_agent_sessions():
    return jsonify(agent_chat_server.list_sessions(OUTPUT_DIR))


@app.route("/api/diagnostics/llm", methods=["GET"])
def api_llm_diagnostics():
    """只返回模型连接元数据，供定位网络/配置问题，不暴露密钥和正文。"""
    return jsonify(agent_chat_server.get_llm_diagnostics(OUTPUT_DIR))


@app.route("/api/agent/tts_messages/<sid>", methods=["GET"])
def api_agent_tts_messages(sid):
    return jsonify(agent_chat_server.get_agent_messages_for_tts(OUTPUT_DIR, sid))


@app.route("/api/agent/line_emotion", methods=["POST"])
def api_agent_line_emotion():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg_id = data.get("msg_id", "")
    text = data.get("text", "")
    return jsonify(agent_chat_server.analyze_line_emotion(OUTPUT_DIR, sid, msg_id, text, _api_config()))


@app.route("/api/agent/audio_comment", methods=["POST"])
def api_agent_audio_comment():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    msg_id = data.get("msg_id", "")
    text = data.get("text", "")
    comment = data.get("comment", "")
    return jsonify(agent_chat_server.apply_audio_comment(OUTPUT_DIR, sid, msg_id, text, comment, _api_config()))


# ---------------------------------------------------------------- 会话清理


_SESSION_TTL_FINISHED = 6 * 3600    # 已结束的会话保留 6 小时后清理
_SESSION_TTL_RUNNING = 3 * 3600     # 异常卡死的会话（未结束）保留 3 小时后清理


def _sweep_sessions():
    """后台守护线程：定期清理过期会话，防止 SESSIONS 无限膨胀（内存泄漏）。"""
    while True:
        time.sleep(600)  # 每 10 分钟扫一次
        try:
            now = time.time()
            with SESSIONS_LOCK:
                for sid in list(SESSIONS):
                    s = SESSIONS[sid]
                    age = now - s.created_ts
                    if (s.finished and age > _SESSION_TTL_FINISHED) or (not s.finished and age > _SESSION_TTL_RUNNING):
                        SESSIONS.pop(sid, None)
                        print(f"[server] 清理过期会话 {sid}（存活 {age / 3600:.1f}h）")
        except Exception as e:  # noqa: BLE001
            print(f"[server] 会话清理异常: {e}")


# ---------- 导出 ----------
@app.route("/api/export/works", methods=["POST"])
def api_export_works():
    data = request.get_json(force=True, silent=True) or {}
    fmt = data.get("format") or "md"
    if fmt not in ("md", "json"):
        return jsonify({"ok": False, "error": "format 必须是 md 或 json"}), 400
    try:
        text = export_utils.export_works(OUTPUT_DIR, fmt)
        filename = f"works_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
        return jsonify({"ok": True, "format": fmt, "filename": filename, "content": text})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export/rewrite/<rid>", methods=["GET"])
def api_export_rewrite(rid):
    try:
        text = export_utils.export_rewrite(OUTPUT_DIR, rid)
        filename = f"rewrite_{rid}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        return jsonify({"ok": True, "filename": filename, "content": text})
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export/insights", methods=["GET"])
def api_export_insights():
    try:
        text = export_utils.export_insights(OUTPUT_DIR)
        filename = f"insights_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        return jsonify({"ok": True, "filename": filename, "content": text})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export/lessons", methods=["GET"])
def api_export_lessons():
    try:
        text = export_utils.export_lessons(OUTPUT_DIR)
        filename = f"lessons_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        return jsonify({"ok": True, "filename": filename, "content": text})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/weekly_report", methods=["GET"])
def api_weekly_report():
    try:
        text = weekly_report.generate(OUTPUT_DIR)
        filename = f"weekly_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        return jsonify({"ok": True, "filename": filename, "content": text})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


# 启动时确保骨架库包含最新默认模板（作为模块被桌面端导入时也会执行）
try:
    skeleton_store.ensure_defaults(OUTPUT_DIR)
except Exception:  # noqa: BLE001
    pass


if __name__ == "__main__":
    threading.Thread(target=_sweep_sessions, daemon=True).start()
    # 启动时自动续跑：上次「扒文案」任务因关软件/进程被杀而中断时，自动接着跑（增量跳过已完成）
    try:
        resumed = works_library_server.maybe_auto_resume_crawl(OUTPUT_DIR, _api_config())
        if resumed:
            print("  [workslib] 检测到上次扒文案任务中断，已在后台自动续跑（已完成视频自动跳过）")
    except Exception as e:  # noqa: BLE001
        print(f"  [workslib] 自动续跑检测跳过：{e}")
    port = int(os.environ.get("PORT", 8765))
    print("=" * 56)
    print("  口播文稿 · 专家讨论群 实时服务已启动")
    print(f"  打开浏览器访问:  http://127.0.0.1:{port}")
    print("  把口播文稿粘贴进页面输入框，点发送即可")
    print("=" * 56)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
