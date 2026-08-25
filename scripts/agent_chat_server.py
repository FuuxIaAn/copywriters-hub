# -*- coding: utf-8 -*-
"""
Agent 模拟对话服务
==================
职责：
  1. 根据提取的发言人台词，用 LLM 分析其说话风格（情绪/口头禅/口语化）
  2. 构建 agent persona，与用户进行模拟对话（如算命场景）
  3. 支持用户评论 -> agent 重新生成回复
  4. 对话完毕后，agent 的回复逐条送 TTS 生成音频

会话状态存内存 + 落盘 output/agent_chat/
"""
import hashlib
import json
import os
import queue
import sys
import threading
import time

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _dir(output_dir: str, *parts: str) -> str:
    p = os.path.join(output_dir, "agent_chat", *parts)
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


def _is_network_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(key in text for key in (
        "_ssl.c", "ssl", "handshake", "timed out", "timeout",
        "connection", "reset", "eof", "network is unreachable",
    ))


def _record_llm_diagnostic(output_dir: str, operation: str, api_config: dict,
                           attempts: int, error: Exception) -> None:
    """落盘最小化诊断信息，定位连接问题时不记录密钥或用户文本。"""
    try:
        log_dir = _dir(output_dir, "diagnostics")
        entry = {
            "time": _now(),
            "operation": operation,
            "base_url": str(api_config.get("base_url") or ""),
            "model": str(api_config.get("model") or ""),
            "attempts": attempts,
            "error_type": type(error).__name__,
            "network_error": _is_network_error(error),
            "error": str(error)[:300],
        }
        with open(os.path.join(log_dir, "llm_calls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_llm_diagnostics(output_dir: str, limit: int = 30) -> dict:
    """返回最近模型连接诊断（仅元数据，不含密钥、提示词或用户正文）。"""
    path = os.path.join(_dir(output_dir, "diagnostics"), "llm_calls.jsonl")
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f.readlines()[-max(1, min(int(limit), 100)):]:
                try:
                    row = json.loads(line)
                    row.pop("error", None)
                    rows.append(row)
                except json.JSONDecodeError:
                    continue
    except (OSError, ValueError):
        pass
    return {"ok": True, "items": rows}


def _chat_with_retry(api_config: dict, messages: list, temperature: float,
                     max_tokens: int = 500, output_dir: str = "", operation: str = "chat") -> str:
    """模型调用统一使用较长超时和退避重试，屏蔽短暂 TLS 握手故障。"""
    from openai import OpenAI

    if not str(api_config.get("base_url") or "").strip():
        raise RuntimeError("未配置 AI 服务地址，请在 API 设置中填写 Base URL")
    if not str(api_config.get("api_key") or "").strip():
        raise RuntimeError("未配置 AI 服务密钥，请在 API 设置中填写 API Key")
    if not str(api_config.get("model") or "").strip():
        raise RuntimeError("未配置 AI 模型名，请在 API 设置中填写模型名")

    client = OpenAI(
        base_url=api_config["base_url"],
        api_key=api_config["api_key"],
        timeout=120.0,
        max_retries=2,
    )
    last_error = None
    for attempt in range(4):
        try:
            response = client.chat.completions.create(
                model=api_config.get("model", "deepseek-chat"),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as error:  # noqa: BLE001
            last_error = error
            if not _is_network_error(error) or attempt == 3:
                break
            time.sleep(2.0 * (attempt + 1))
    if _is_network_error(last_error or Exception("")):
        if output_dir:
            _record_llm_diagnostic(output_dir, operation, api_config, 4, last_error)
        raise RuntimeError("网络连接模型服务超时，请检查网络/代理后点击重试") from last_error
    if output_dir and last_error:
        _record_llm_diagnostic(output_dir, operation, api_config, attempt + 1, last_error)
    raise RuntimeError(str(last_error or "模型服务无响应")) from last_error


# ---------------------------------------------------------------- 会话

class ChatSession:
    def __init__(self, sid: str):
        self.sid = sid
        self.persona = ""          # agent 人设 prompt
        self.persona_name = ""     # 发言人标记 (A/B/C)
        self.scene = ""            # 场景描述
        self.speaker_samples = []  # 原始台词样本 [{speaker, text}]
        self.visitor_profile = {}  # 求测者经历画像
        self.messages = []         # 对话记录 [{role: "user"/"assistant", content, time, id}]
        self.queue = queue.Queue()
        self.finished = False
        self.lock = threading.Lock()
        self.created_at = time.time()

    def push(self, event: dict):
        self.messages.append(event)
        self.queue.put(event)
        self._persist()

    def _persist(self):
        pass  # 在外部用 _save_session 实现


CHAT_SESSIONS = {}


# ---------------------------------------------------------------- Persona 构建

def _format_visitor_profile(profile: dict) -> str:
    """把求测者经历画像格式化为 persona 用的文本段"""
    if not profile or not isinstance(profile, dict):
        return ""
    parts = []
    if profile.get("summary"):
        parts.append(f"- 一句话画像：{profile['summary']}")
    if profile.get("basics"):
        parts.append(f"- 基本情况：{profile['basics']}")
    exps = profile.get("experiences") or []
    if exps:
        parts.append("- 经历时间线：")
        parts.extend(f"  {i+1}. {e}" for i, e in enumerate(exps))
    probs = profile.get("problems") or []
    if probs:
        parts.append("- 当前困扰：" + "；".join(probs))
    dems = profile.get("demands") or []
    if dems:
        parts.append("- 来找师傅的诉求：" + "；".join(dems))
    if profile.get("emotion"):
        parts.append(f"- 情绪状态：{profile['emotion']}")
    return "\n".join(parts)


def _analyze_speech_style(speaker: str, samples_text: str, profile_text: str, api_config: dict) -> dict:
    """结构化提取目标发言人的口语指纹（LLM，输出 JSON）。

    重中之重是口语化用语/口语化习惯：口头禅逐字摘录、语气词、开口方式、
    称呼、句式、情绪化表达、方言特征，以及结合其经历的"分话题反应模式"。
    """
    profile_hint = f"\n此人的真实经历（供推断他关心什么、对什么话题敏感）：\n{profile_text}\n" if profile_text else ""
    prompt = (
        "你是顶级的人物语言模仿分析师。下面是短视频中一位发言人的全部台词，"
        "你的任务是把此人的【口语化指纹】逐字提炼出来——模仿一个人，最核心的是模仿他怎么说话，"
        "而不是说话内容。\n\n"
        f"该发言人（{speaker}）的台词：\n{samples_text}\n"
        f"{profile_hint}\n"
        "请严格输出以下 JSON（不要输出任何其他文字、不要 markdown 代码块）：\n"
        "{\n"
        '  "catchphrases": ["口头禅/高频用语，逐字摘录原文短语，至少5条，如：\u201c我跟你说啊\u201d、\u201c真的假的\u201d"],\n'
        '  "filler_words": "语气词习惯（嗯/啊/呢/吧/哈/唉…各出现在句首还是句尾，频率如何，举原文例子）",\n'
        '  "opening_habit": "开口习惯：他起话题/第一句话通常怎么说（直接抛问题？先客套？先讲背景？举原文例子）",\n'
        '  "address_term": "称呼方式：怎么称呼对方（师傅/大师/老师/哎…），怎么自称（我/本人/俺…）",\n'
        '  "sentence_style": "句式特征：短句连击还是长句？爱反问吗？爱重复强调吗？爱打断式追问吗？平均一句话多长？",\n'
        '  "emotional_expression": "情绪化表达：着急/焦虑/惊喜/委屈时分别说什么原话，语气怎么变",\n'
        '  "dialect_traits": "方言或地域口音痕迹（用词、语法、称呼上的特征，没有就写\u201c无\u201d）",\n'
        '  "thinking_style": "思维方式：他关心什么（钱？时间点？确定性？细节确认？），怎么组织一段话",\n'
        '  "topic_reactions": [\n'
        '    {"topic": "感情/婚姻", "reaction": "结合他的经历，别人提起这个话题时他会是什么反应、会说什么类型的话（用他的口吻描述）"},\n'
        '    {"topic": "事业/工作", "reaction": "…"},\n'
        '    {"topic": "钱财/运势", "reaction": "…"},\n'
        '    {"topic": "家庭/亲人", "reaction": "…"},\n'
        '    {"topic": "健康", "reaction": "…"},\n'
        '    {"topic": "没经历过的新话题", "reaction": "他没经历过的领域，他会怎么反应（好奇？不信？岔开？）"}\n'
        "  ]\n"
        "}\n"
        "要求：所有例子必须逐字摘自原文，禁止编造他没说过的用语。"
    )
    try:
        from openai import OpenAI
        client = OpenAI(base_url=api_config["base_url"], api_key=api_config["api_key"],
                        timeout=60, max_retries=1)
        resp = client.chat.completions.create(
            model=api_config.get("model", "deepseek-chat"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1500,
            timeout=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # 剥掉可能的 ```json 围栏
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(raw[start:end + 1])
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def build_persona(output_dir: str, segments: list, speaker: str, scene: str,
                  api_config: dict, extra_style: str = "", visitor_profile: dict = None) -> dict:
    """分析指定发言人的台词风格，构建 agent persona。
    segments: [{speaker, text}] — 提取的对话分段
    speaker: "A" 或 "B" — 要模拟的发言人
    scene: 用户描述的场景（如"你来找我算命"）
    extra_style: 用户额外指定的风格要求
    visitor_profile: 求测者经历画像（从视频中提取，可手动编辑后传入）
    """
    speaker = (speaker or "A").strip()[:1].upper()
    # 收集该发言人的所有台词
    samples = [s["text"] for s in segments if s.get("speaker", "A").upper() == speaker]
    if not samples:
        return {"ok": False, "error": f"没有找到发言人 {speaker} 的台词"}

    all_samples = "\n".join(f"- {t}" for t in samples[:20])
    profile_text = _format_visitor_profile(visitor_profile)
    style = _analyze_speech_style(speaker, all_samples, profile_text, api_config)

    # ------- 第一节：口语化模仿（重中之重，置于 persona 最前） -------
    catch = [c for c in (style.get("catchphrases") or []) if c][:10]
    catch_lines = ("\n".join(f"    - 「{c}」" for c in catch)
                   if catch else "    -（未提取到明显口头禅，参照下方参考台词模仿其用词）")
    topic_reactions = [t for t in (style.get("topic_reactions") or [])
                       if isinstance(t, dict) and t.get("topic") and t.get("reaction")]
    topic_lines = "\n".join(f"    - 师傅提到【{t['topic']}】→ {t['reaction']}" for t in topic_reactions)
    if not topic_lines:
        topic_lines = ("    -（无提取结果时：根据下方「你的经历」判断——经历过的话题就自然接话，"
                       "没经历过的按你的身份/年龄正常反应，不要编造与经历时间线矛盾的事）")

    persona = (
        f"## ⭐ 最高优先级：口语化模仿（这是重中之重）\n"
        f"你模仿的核心不是「说什么内容」，而是「这个人怎么说话」。每发一条消息前自检：\n"
        f"这句话像不像他本人说的？口吻不像 = 不合格，宁可少说也要像。\n\n"
        f"【口头禅/高频用语】（逐字照用，每 2-3 条消息里至少出现一次）：\n{catch_lines}\n"
        f"【语气词习惯】{style.get('filler_words') or '参照参考台词，多用语气词让句子口语化'}\n"
        f"【开口习惯】{style.get('opening_habit') or '参照参考台词'}\n"
        f"【称呼方式】{style.get('address_term') or '称呼对方为「师傅」，自称「我」'}\n"
        f"【句式特征】{style.get('sentence_style') or '短句为主，像发微信'}\n"
        f"【情绪化表达】{style.get('emotional_expression') or '着急时语气变急、追问变密'}\n"
        f"【方言/口音痕迹】{style.get('dialect_traits') or '无'}\n\n"
        f"## 他的思维方式与反应模式\n"
        f"【思维方式】{style.get('thinking_style') or '关心确定性，爱问什么时候、怎么办'}\n"
        f"别人（师傅）说不同的事，你的反应必须按下表来，而且要用上面他的口语习惯说出来：\n"
        f"{topic_lines}\n\n"
        f"## 场景设定\n{scene or '自由对话'}\n\n"
        f"## 沟通方式\n"
        f"你们是通过微信文字聊天沟通，不是面对面。\n"
        f"不要出现面对面才有的动作描述（如请坐、看手相、看面相等）。\n"
        f"说话要像微信聊天：简短、直接、口语化，可以发短句连击。\n\n"
        f"## 角色关系\n"
        f"你（AI）是被模拟的发言人 {speaker}，你是来访者/求助者。\n"
        f"与你对话的人（用户）是师傅/服务方。\n"
        f"你通过微信主动找用户，向用户寻求帮助或解答。\n\n"
        f"## 行为规则\n"
        f"1. 【最高优先级】完全模仿他的口语习惯：口头禅、语气词、句式、称呼逐字复用。先想「他会怎么说这句话」，再发消息；内容对但口吻不像 = 不合格\n"
        f"2. 别人（师傅）说不同的事时，按「思维方式与反应模式」里的分话题反应来回应，反应要和你的经历对得上\n"
        f"2.1 【紧扣话题 · 最高优先级】先接住师傅这句话再往下说：师傅问什么就答什么，师傅聊什么就顺着聊什么。\n"
        f"    绝不允许答非所问、突然把话题岔到一件跟师傅这句话无关的事上（比如师傅说的是A，你却突然讲起自己练瑜伽、开店、买房这类毫不相干的经历）。\n"
        f"    每一句回复都要能直接回应当前这一条师傅说的话；如果一时不知道回什么，就针对师傅的话追问细节、确认他的意思，而不是自己另起一个话题。\n"
        f"2.5 【克制条款】经历里的往事（尤其那些高冲突的事）不要反复主动提起：只有当师傅明确问起、或当前话题直接相关时，才用一两句自然带过；已经提过的事后续不再翻旧账，更不要每句话都往那件事上靠\n"
        f"2.6 【严禁编造】人物背景里没写的具体身份/职业/地点/事件，一律不许脑补添加；宁可含糊带过或说不清楚，也不能凭空编造\n"
        f"3. 你是来访者，要主动提问、表达困惑、倾诉烦恼，不要替师傅做判断或给建议\n"
        f"4. 每次回复不超过 150 字（口语化，像真人发微信）\n"
        f"5. 不要使用书面语、不要分点列举、不要用 markdown 格式\n"
        f"6. 保持人物一致性，不要跳出角色，不要承认自己是 AI\n"
        f"7. 可以使用语气词（嗯、啊、呢、吧、哈）让对话更自然\n"
        f"8. 根据师傅（用户）的回应自然推进对话，该追问就追问，该焦虑就焦虑\n"
        f"9. 这是微信聊天，可以用口语化短句，不要出现请坐、看手相等面对面行为\n"
    )
    if extra_style:
        persona += f"\n## 额外风格要求\n{extra_style}\n"

    if profile_text:
        persona += (
            f"\n## 你的经历（人物背景）\n"
            f"以下是从原视频对话中提取的你本人的真实经历，对话时必须保持一致：\n"
            f"{profile_text}\n\n"
            f"使用规则：\n"
            f"- 师傅问起相关话题时，如实讲述这些经历，细节不要改动；"
            f"但不要一次性全部倒出来，随对话自然展开\n"
            f"- 对不同话题的反应要和经历挂钩：经历过的事说得出细节和情绪，"
            f"没经历过的事就像普通人一样反应（好奇/疑惑/岔开），绝不能编造与时间线矛盾的新经历\n"
            f"- 【严禁编造细节】上面「你的经历」里没写到的具体身份/职业/地点/事件（比如谁做什么工作、开什么店、"
            f"住哪、有什么爱好），一律不许自己脑补添加；师傅问起时若经历里没写，就含糊带过或说不清楚，"
            f"宁可少说也不能凭空编一个出来\n"
            f"- 讲经历时也要用他的口语习惯说（口头禅+语气词+句式），不要变成背简历\n"
            f"- 【克制条款】同一件往事（尤其高冲突的事）不要反复主动翻旧账："
            f"只有师傅明确问起、或当前话题直接相关时才用一两句带过；已经提过的就不再重复提，"
            f"不相关的话题就正常反应，别老往那件事上硬扯\n"
        )

    persona += f"\n## 参考台词（模仿其风格）\n{all_samples}"

    sid = _uid(speaker + scene)
    session = ChatSession(sid)
    session.persona = persona
    session.persona_name = speaker
    session.scene = scene or "自由对话"
    session.speaker_samples = [s for s in segments if s.get("speaker", "A").upper() == speaker]
    session.visitor_profile = visitor_profile if isinstance(visitor_profile, dict) else {}

    CHAT_SESSIONS[sid] = session
    _save_session(output_dir, session)
    return {"ok": True, "sid": sid, "persona": persona, "speaker": speaker,
            "style_analysis": style, "style_desc": "\n".join(catch)}


# ---------------------------------------------------------------- 对话

def send_message(output_dir: str, sid: str, user_message: str, api_config: dict) -> dict:
    """用户发消息 -> agent 回复（非流式，返回完整回复）"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        return {"ok": False, "error": "会话不存在或已过期"}

    user_message = (user_message or "").strip()
    if not user_message:
        return {"ok": False, "error": "请输入消息内容"}

    msg_id = _uid("msg")
    user_msg = {"role": "user", "content": user_message, "time": _now(), "id": msg_id}
    session.messages.append(user_msg)

    # 构建 LLM 上下文
    history = []
    history.append({"role": "system", "content": session.persona})
    for m in session.messages[-20:]:  # 最近 20 条
        history.append({"role": m["role"], "content": m["content"]})
    # 发消息前自检提醒：强化口语习惯模仿（persona 最高优先级的执行兜底）
    history.append({"role": "system", "content": (
        "【发消息前自检】①先紧扣话题：你这条回复是不是直接接住师傅刚才那句话？"
        "绝不能答非所问、突然岔到跟师傅这句话无关的事上（比如师傅说的是A，你却突然讲自己练瑜伽、开店、买房）。"
        "②这句话像不像他本人说的（口头禅/语气词/句式用上了吗）？"
        "③对这个话题的反应和他的经历对得上吗？不像就重写再发。"
        "④别老翻旧账：只有当前话题相关、或师傅明确问起时才提往事，提过的就不再重复，"
        "不相关就正常反应。"
    )})

    try:
        reply = _chat_with_retry(api_config, history, temperature=0.8,
                                 output_dir=output_dir, operation="agent_send")
    except Exception as e:
        return {"ok": False, "error": f"AI 回复失败：{e}"}

    agent_msg = {"role": "assistant", "content": reply, "time": _now(), "id": _uid("reply")}
    session.messages.append(agent_msg)
    _save_session(output_dir, session)

    return {"ok": True, "user_msg": user_msg, "agent_msg": agent_msg}


def insert_message(output_dir: str, sid: str, role: str, content: str, after_id: str = "") -> dict:
    """手动插入一条消息到对话中（补录 A 或 B 说的话，不触发 AI 生成）。

    role: "user"（用户/师傅说的）或 "assistant"（被模拟的求测者说的）
    after_id: 若指定，则插到该消息之后；否则追加到末尾。
    """
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        CHAT_SESSIONS[sid] = session

    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "请输入消息内容"}

    if role not in ("user", "assistant"):
        role = "user"

    msg = {"role": role, "content": content, "time": _now(), "id": _uid("insert"), "inserted": True}

    # 定位插入位置：找到 after_id 对应消息的下标，插到它后面
    after_id = (after_id or "").strip()
    insert_at = len(session.messages)
    if after_id:
        for i, m in enumerate(session.messages):
            if m.get("id") == after_id:
                insert_at = i + 1
                break

    session.messages.insert(insert_at, msg)
    _save_session(output_dir, session)
    return {"ok": True, "msg": msg, "index": insert_at}


def set_message_audio(output_dir: str, sid: str, msg_id: str, audio: str, preset: str = "") -> dict:
    """把某条消息已生成的音频结果绑定到该消息（持久化，供重开软件后恢复语音条）。

    前端每次 TTS 合成成功后调用，把 {audio: "xxx.wav", preset: 音色名} 写进该 message，
    下次 get_session / list_sessions 返回时前端即可据此直接渲染语音条，无需重新合成。
    """
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        CHAT_SESSIONS[sid] = session

    audio = (audio or "").strip()
    for m in session.messages:
        if m.get("id") == msg_id:
            if audio:
                m["audio"] = audio
                m["audio_preset"] = (preset or "").strip() or m.get("audio_preset", "")
            else:
                # 传空表示清除音频绑定（例如重写内容后旧音频作废）
                m.pop("audio", None)
                m.pop("audio_preset", None)
            _save_session(output_dir, session)
            return {"ok": True, "msg": m}

    return {"ok": False, "error": "未找到该消息"}


def update_message(output_dir: str, sid: str, msg_id: str, content: str) -> dict:
    """手动编辑一条消息的文字（不改角色，不触发 AI）。"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        CHAT_SESSIONS[sid] = session

    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "内容不能为空"}

    for m in session.messages:
        if m.get("id") == msg_id:
            m["content"] = content
            m["time"] = _now()
            m["edited"] = True
            _save_session(output_dir, session)
            return {"ok": True, "msg": m}

    return {"ok": False, "error": "未找到该消息"}


def delete_message(output_dir: str, sid: str, msg_id: str) -> dict:
    """删除一条消息。"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        CHAT_SESSIONS[sid] = session

    before = len(session.messages)
    session.messages = [m for m in session.messages if m.get("id") != msg_id]
    if len(session.messages) == before:
        return {"ok": False, "error": "未找到该消息"}
    _save_session(output_dir, session)
    return {"ok": True}


def reset_session(output_dir: str, sid: str) -> dict:
    """清空当前会话的所有消息（保留角色 persona / 场景 / 经历画像），回到刚创建角色的空聊天状态。"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        CHAT_SESSIONS[sid] = session

    # 清空消息与队列
    session.messages = []
    try:
        while not session.queue.empty():
            session.queue.get_nowait()
    except Exception:
        pass
    session.finished = False
    _save_session(output_dir, session)
    return {"ok": True, "sid": sid, "scene": session.scene, "persona_name": session.persona_name}


def end_session(output_dir: str, sid: str) -> dict:
    """结束模拟：彻底删除该会话及其产出的音频文件（不可恢复）。

    与 reset_session（只清空消息、保留角色）不同，本函数是「结束 + 清理」：
      1. 收集会话里所有消息绑定过的音频文件名（message["audio"]）
      2. 删除这些 wav 文件（output/tts/audio/<fname>）
      3. 删除会话落盘文件（output/agent_chat/<sid>.json）
      4. 从内存 CHAT_SESSIONS 移除
    返回删除统计。
    """
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)

    # 收集该会话绑定过的音频文件名
    audio_names = set()
    if session:
        for m in session.messages or []:
            a = (m.get("audio") or "").strip()
            if a:
                audio_names.add(os.path.basename(a))

    # 删除音频文件（output/tts/audio/<fname>）
    audio_dir = os.path.join(output_dir, "tts", "audio")
    removed_audio = 0
    for fname in audio_names:
        # 安全：只允许纯文件名，禁止路径穿越
        if os.path.basename(fname) != fname or "/" in fname or "\\" in fname:
            continue
        fp = os.path.join(audio_dir, fname)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                removed_audio += 1
        except Exception:
            pass

    # 删除会话落盘文件
    removed_session = 0
    try:
        sp = os.path.join(_dir(output_dir), f"{sid}.json")
        if os.path.isfile(sp):
            os.remove(sp)
            removed_session = 1
    except Exception:
        pass

    # 从内存移除
    if sid in CHAT_SESSIONS:
        del CHAT_SESSIONS[sid]

    return {"ok": True, "sid": sid,
            "removed_audio": removed_audio, "removed_session": removed_session}


def regenerate(output_dir: str, sid: str, msg_id: str, comment: str, api_config: dict) -> dict:
    """根据用户评论，让 agent 重新生成指定回复"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        return {"ok": False, "error": "会话不存在或已过期"}

    # 找到要重新生成的消息位置
    idx = -1
    for i, m in enumerate(session.messages):
        if m.get("id") == msg_id and m.get("role") == "assistant":
            idx = i
            break
    if idx < 0:
        return {"ok": False, "error": "未找到要重新生成的回复"}

    # 删除该消息及其后的所有消息（重新生成会改变后续上下文）
    # 实际上只替换该条，保留后续用户消息
    old_content = session.messages[idx]["content"]

    # 构建上下文：到 idx 之前的所有消息 + 评论指令
    history = [{"role": "system", "content": session.persona}]
    for m in session.messages[:idx]:
        history.append({"role": m["role"], "content": m["content"]})

    # 加入评论作为修正指令
    comment = (comment or "").strip()
    if comment:
        history.append({
            "role": "user",
            "content": f"【系统指令】这是你上一条回复，用户觉得不满意：\n「{old_content}」\n"
                       f"用户的评论/要求：{comment}\n"
                       f"请根据用户的评论重新回复上一条消息对应的内容。保持角色不变。"
        })
        # 重新加上 idx-1 的用户消息作为上下文
        if idx > 0 and session.messages[idx - 1].get("role") == "user":
            history.append({"role": "user", "content": session.messages[idx - 1]["content"]})
    else:
        # 无评论，直接重新生成
        if idx > 0 and session.messages[idx - 1].get("role") == "user":
            history.append({"role": "user", "content": session.messages[idx - 1]["content"]})
    history.append({"role": "system", "content": (
        "【发消息前自检】①先紧扣话题：你这条回复是不是直接接住师傅刚才那句话？"
        "绝不能答非所问、突然岔到跟师傅这句话无关的事上。"
        "②这句话像不像他本人说的（口头禅/语气词/句式用上了吗）？"
        "③对这个话题的反应和他的经历对得上吗？不像就重写再发。"
        "④别老翻旧账：只有当前话题相关、或师傅明确问起时才提往事，提过的就不再重复，"
        "不相关就正常反应。"
    )})

    try:
        new_reply = _chat_with_retry(api_config, history, temperature=0.9,
                                     output_dir=output_dir, operation="agent_regenerate")
    except Exception as e:
        return {"ok": False, "error": f"重新生成失败：{e}"}

    # 替换消息
    session.messages[idx]["content"] = new_reply
    session.messages[idx]["time"] = _now()
    session.messages[idx]["regenerated"] = True
    _save_session(output_dir, session)

    return {"ok": True, "agent_msg": session.messages[idx]}


def get_session(output_dir: str, sid: str) -> dict:
    """获取会话详情"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        # 尝试从磁盘加载
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在"}
        CHAT_SESSIONS[sid] = session
    return {
        "ok": True,
        "sid": sid,
        "persona": session.persona,
        "persona_name": session.persona_name,
        "scene": session.scene,
        "visitor_profile": session.visitor_profile,
        "messages": session.messages,
    }


def update_session_profile(output_dir: str, sid: str, visitor_profile: dict) -> dict:
    """修改求测者经历后，同步更新已创建会话的 persona（无需重建角色）。

    只替换 persona 中的「你的经历（人物背景）」段，不重跑风格分析，
    让已经开聊的 agent 立刻带着最新经历继续对话。
    """
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在"}
        CHAT_SESSIONS[sid] = session

    profile = visitor_profile if isinstance(visitor_profile, dict) else {}
    profile_text = _format_visitor_profile(profile)

    # 重建「你的经历」段（与 build_persona 里的结构保持一致）
    new_block = ""
    if profile_text:
        new_block = (
            f"## 你的经历（人物背景）\n"
            f"以下是从原视频对话中提取的你本人的真实经历，对话时必须保持一致：\n"
            f"{profile_text}\n\n"
            f"使用规则：\n"
            f"- 师傅问起相关话题时，如实讲述这些经历，细节不要改动；"
            f"但不要一次性全部倒出来，随对话自然展开\n"
            f"- 对不同话题的反应要和经历挂钩：经历过的事说得出细节和情绪，"
            f"没经历过的事就像普通人一样反应（好奇/疑惑/岔开），绝不能编造与时间线矛盾的新经历\n"
            f"- 【严禁编造细节】上面「你的经历」里没写到的具体身份/职业/地点/事件（比如谁做什么工作、开什么店、"
            f"住哪、有什么爱好），一律不许自己脑补添加；师傅问起时若经历里没写，就含糊带过或说不清楚，"
            f"宁可少说也不能凭空编一个出来\n"
            f"- 讲经历时也要用他的口语习惯说（口头禅+语气词+句式），不要变成背简历\n"
            f"- 【克制条款】同一件往事（尤其高冲突的事）不要反复主动翻旧账："
            f"只有师傅明确问起、或当前话题直接相关时才用一两句带过；已经提过的就不再重复提，"
            f"不相关的话题就正常反应，别老往那件事上硬扯\n"
        )

    persona = session.persona or ""
    # 替换旧的经历段（从「## 你的经历」到「## 参考台词」之前）
    import re
    pattern = re.compile(r"\n## 你的经历（人物背景）.*?(?=\n## 参考台词)", re.DOTALL)
    if pattern.search(persona):
        persona = pattern.sub(("\n" + new_block) if new_block else "", persona, count=1)
    elif new_block:
        # 原本没有经历段（旧数据/无画像），插到「## 参考台词」之前
        marker = "\n## 参考台词"
        if marker in persona:
            persona = persona.replace(marker, "\n" + new_block + marker, 1)
        else:
            persona = persona + "\n" + new_block

    session.persona = persona
    session.visitor_profile = profile
    _save_session(output_dir, session)

    return {"ok": True, "sid": sid,
            "visitor_profile": profile,
            "profile_injected": bool(profile_text)}


def list_sessions(output_dir: str) -> dict:
    """列出所有会话"""
    chat_dir = _dir(output_dir)
    sessions = []
    try:
        for fname in os.listdir(chat_dir):
            if fname.endswith(".json") and fname != "latest.json":
                data = _read_json(os.path.join(chat_dir, fname), {})
                if data.get("sid"):
                    sessions.append({
                        "sid": data["sid"],
                        "persona_name": data.get("persona_name", ""),
                        "scene": data.get("scene", ""),
                        "msg_count": len(data.get("messages") or []),
                        "created_at": data.get("created_at", 0),
                    })
    except Exception:
        pass
    sessions.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"ok": True, "sessions": sessions}


# ---------------------------------------------------------------- 持久化

def _save_session(output_dir: str, session: ChatSession) -> None:
    data = {
        "sid": session.sid,
        "persona": session.persona,
        "persona_name": session.persona_name,
        "scene": session.scene,
        "speaker_samples": session.speaker_samples,
        "visitor_profile": session.visitor_profile,
        "messages": session.messages,
        "created_at": session.created_at,
    }
    _write_json(os.path.join(_dir(output_dir), f"{session.sid}.json"), data)


def _load_session(output_dir: str, sid: str) -> ChatSession | None:
    data = _read_json(os.path.join(_dir(output_dir), f"{sid}.json"), None)
    if not data:
        return None
    session = ChatSession(sid)
    session.persona = data.get("persona", "")
    session.persona_name = data.get("persona_name", "")
    session.scene = data.get("scene", "")
    session.speaker_samples = data.get("speaker_samples", [])
    session.visitor_profile = data.get("visitor_profile", {})
    session.messages = data.get("messages", [])
    session.created_at = data.get("created_at", time.time())
    return session


# ---------------------------------------------------------------- 台词情绪智能分析

# IndexTTS-2.5 的 8 维情感向量顺序（固定，勿改）
EMO_VEC_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


def _load_session_anywhere(output_dir: str, sid: str):
    """从内存或磁盘加载会话对象（供情绪分析等内部函数复用）。"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if session:
            CHAT_SESSIONS[sid] = session
    return session


def analyze_line_emotion(output_dir: str, sid: str, msg_id: str, text: str,
                         api_config: dict) -> dict:
    """根据目标发言人的性格、对事情的反应、情绪变动，智能分析一句台词的语气情绪。

    输入：
      sid: 会话 ID；msg_id: 要分析的消息 ID（用于定位对话上下文）；text: 台词原文
    输出（供前端转 TTS 参数）：
      {
        "ok": True,
        "emotion_label": "焦虑/惊喜/委屈/平静/…",   # 人类可读标签
        "emo_text": "用急切又带着点委屈的语气说，语速稍快",   # 自然语言情感描述（IndexTTS 官方推荐）
        "emo_vec": [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm],  # 8 维，0~1.2，和 ≤1.5
        "emo_weight": 0.6,          # 情感向量/文本权重，0~1
        "duration_factor": 1.0,     # 语速因子，<1 更快，>1 更慢
        "reason": "判断依据（简短）"
      }
    """
    session = _load_session_anywhere(output_dir, sid)
    if not session:
        return {"ok": False, "error": "会话不存在或已过期"}

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "台词不能为空"}

    # 1) 定位目标消息的上下文（前 1 条 + 后 1 条，最多往前看 6 条）
    idx = -1
    for i, m in enumerate(session.messages):
        if m.get("id") == msg_id:
            idx = i
            break
    if idx < 0:
        idx = len(session.messages) - 1  # 找不到就当作最后一条
    ctx_start = max(0, idx - 6)
    context_lines = []
    for m in session.messages[ctx_start:idx]:
        role = "师傅" if m.get("role") == "user" else session.persona_name
        context_lines.append(f"{role}：{m.get('content', '')}")
    if idx >= 0 and idx < len(session.messages):
        m = session.messages[idx]
        role = "师傅" if m.get("role") == "user" else session.persona_name
        context_lines.append(f"{role}（本句）：{m.get('content', '')}")
    context_text = "\n".join(context_lines) if context_lines else f"{session.persona_name}：{text}"

    # 2) 从 persona 中提炼性格/情绪/反应关键信息（persona 已含口语指纹，直接用原文给 LLM）
    #    persona 很长，截取最相关的「情绪化表达」「反应模式」「思维方式」「经历」几段
    persona = session.persona or ""
    persona_hint = persona[:3000]  # 截断防止超长

    prompt = (
        "你是配音导演，精通 IndexTTS-2.5 的情感合成。现在要给一句话配音，"
        "你要根据说话人的性格、他此刻对事情的反应、情绪的变化，判断这句台词该用什么语气情绪说出来。\n\n"
        f"【说话人是谁】{session.persona_name}（被模拟的来访者/求助者，你通过微信跟师傅聊天）\n"
        f"【说话人性格与口语指纹摘要】\n{persona_hint}\n\n"
        f"【对话上下文】\n{context_text}\n\n"
        f"【要配音的这句台词】\n「{text}」\n\n"
        "请严格只输出一个 JSON 对象（不要 markdown、不要任何解释文字）：\n"
        "{\n"
        '  "emotion_label": "一个最贴切的中文情绪词（如：平静/焦虑/惊喜/委屈/着急/疑惑/高兴/沮丧/担忧/释然/犹豫…）",\n'
        '  "emo_text": "一句自然语言描述这句台词该怎么念，必须包含语气+情绪+语速+停顿+重音，30字以内（如：急切又带点委屈地说，语速稍快，中间顿一下，「真的」两个字加重）",\n'
        '  "emo_vec": [高兴, 生气, 悲伤, 害怕, 厌恶, 忧郁, 惊讶, 平静]，8 个 0~1.2 的数，总和不超过 1.5，主情绪给高、其余给低，\n'
        '  "emo_weight": 0.6 附近（0~1，情感权重，越大越夸张，口语对话用 0.5~0.7）,\n'
        '  "duration_factor": 语速因子（精确给值：0.7~0.85 很快、0.85~0.95 偏快、0.95~1.05 正常、1.05~1.2 偏慢、1.2~1.5 很慢）,\n'
        '  "annotated_text": "带拼音声调标注的台词原文：只给这句台词里【易读错的多音字】和【需要读轻声的字】加标注（格式：字后紧跟[拼音+声调数字]，如 重[zhòng]、了[le]、裳[shang]。声调数字：1阴平2阳平3上声4去声5轻声），其余字原样保留、不要加任何多余空格；若没有需要标注的字，就直接原样返回台词原文",\n'
        '  "stress_word": "这句台词最该重读/强调的那个词或字（1~4个字，如：真的/必须/我），若没有特别强调点就写空字符串",\n'
        '  "reason": "一句话说明判断依据（结合性格/上下文/情绪变化/为什么这样定语速和重音）"\n'
        "}\n"
        "规则：\n"
        "1. 主情绪必须来自「这句台词 + 上下文 + 他的性格」三者结合，不能只看台词字面；\n"
        "2. 情绪要有变化：如果他刚被师傅一句话戳中（上下文里有转折），主情绪要跟上下文呼应，不要每句都平静；\n"
        "3. 语速要跟情绪和性格配套，参考下表（务必精确给 duration_factor，别总用 1.0）：\n"
        "   - 着急/激动/生气/兴奋/抢话 → 0.8~0.9（偏快）\n"
        "   - 委屈/难过/沮丧/疲惫/敷衍 → 0.95~1.1（略慢，仅比正常慢一点点）\n"
        "   - 犹豫/支支吾吾/思考/不好意思 → 1.0~1.15（稍慢，带停顿，但不要拖成慢放）\n"
        "   - 平静/正常陈述/轻松闲聊 → 0.95~1.05（正常）\n"
        "   - 性格本身爱啰嗦/慢吞吞的人，整体语速再慢 0.03~0.06；急性子/嘴快的人再快 0.03~0.06；\n"
        "   - 重要：duration_factor 只是「情绪微调」，基准语速由发言人真实语速决定。\n"
        "     绝大多数句子都该落在 0.9~1.1 之间，除非是明显的情绪爆发，否则不要给到 1.2 以上，\n"
        "     否则整段会变成慢放，不像正常人说话；\n"
        "4. 停顿要写进 emo_text：犹豫、转折、被问住、叹气、欲言又止时，明确写「中间顿一下/前面停一下/说到XX慢下来」；\n"
        "5. 重音/声调：\n"
        "   - 想强调、反驳、下结论、表决心时，把那个关键词写进 stress_word，并在 emo_text 里写「XX两个字加重/重读」；\n"
        "   - 台词里出现多音字（如 重/行/长/还/乐/了/着/得/的/都 等）或该读轻声的字（的/了/着/子/头 等虚词），在 annotated_text 里用「字[拼音声调]」标注正确读音，防止读错；\n"
        "   - 方言口语/口语弱化音不必标注，保持自然；\n"
        "6. 数值要合理：主情绪 0.5~0.9，次要情绪 0.1~0.3，8 维总和 ≤ 1.5。"
    )
    from openai import OpenAI
    # 显式设置超时与重试：避免网络抖动/代理断连时挂死或抛 SSL EOF
    client = OpenAI(
        base_url=api_config["base_url"],
        api_key=api_config["api_key"],
        timeout=20.0,          # 连接+读超时 20s（情绪分析是锦上添花，别长时间挂着）
        max_retries=1,         # 失败最多重试 1 次
    )
    last_err = None
    raw = None
    for attempt in range(2):  # 总共尝试 2 次（1 次 + 1 次重试），应对瞬时 SSL EOF
        try:
            resp = client.chat.completions.create(
                model=api_config.get("model", "deepseek-chat"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,   # 情绪判断要稳定，不要随机
                max_tokens=400,
            )
            raw = (resp.choices[0].message.content or "").strip()
            break
        except Exception as e:
            last_err = e
            # 网络类瞬时错误（SSL EOF / 连接重置 / 超时）才重试，其余直接放弃
            err_s = str(e).lower()
            if any(k in err_s for k in ("eof", "ssl", "reset", "timeout", "timed out", "connection")):
                time.sleep(1.0 * (attempt + 1))
                continue
            break
    if raw is None:
        msg = str(last_err) if last_err else "未知错误"
        # 网络中断等瞬时问题，回退平静语气，不阻断合成
        return _default_emotion(text, reason=f"情绪分析网络中断，回退平静语气（{msg[:80]}）")

    # 3) 解析 LLM 输出，容错
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
        except Exception:
            data = None

    if not isinstance(data, dict):
        # LLM 输出失败，回退为默认平静情绪
        return _default_emotion(text)

    # 4) 归一化并校验 emo_vec
    vec = data.get("emo_vec")
    if not isinstance(vec, list) or len(vec) != 8:
        vec = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8]  # 默认平静
    vec = [max(0.0, min(1.2, float(v))) for v in vec]
    total = sum(vec)
    if total > 1.5:  # 超上限按比例压缩
        scale = 1.5 / total
        vec = [round(v * scale, 3) for v in vec]

    def _clamp(v, lo, hi, default):
        try:
            return max(lo, min(hi, float(v)))
        except Exception:
            return default

    # 5) 提取重音与拼音标注
    annotated_text = str(data.get("annotated_text") or "").strip()
    stress_word = str(data.get("stress_word") or "").strip()
    # 若 LLM 没给标注文本，回退到原台词（不破坏合成）
    if not annotated_text:
        annotated_text = text
    # 若 stress_word 没给，但 emo_text 里没提重音，则不额外处理

    result = {
        "ok": True,
        "emotion_label": str(data.get("emotion_label") or "平静").strip() or "平静",
        "emo_text": str(data.get("emo_text") or "").strip() or "用自然平静的语气说",
        "emo_vec": [round(v, 3) for v in vec],
        "emo_weight": _clamp(data.get("emo_weight"), 0.0, 1.0, 0.6),
        "duration_factor": _clamp(data.get("duration_factor"), 0.7, 1.3, 1.0),
        "annotated_text": annotated_text,
        "stress_word": stress_word,
        "reason": str(data.get("reason") or "").strip(),
    }
    return result


def _default_emotion(text: str, reason: str = "情绪分析失败，使用默认平静情绪") -> dict:
    """情绪分析失败时的兜底：根据简单启发式给个平静情绪。"""
    return {
        "ok": True,
        "emotion_label": "平静",
        "emo_text": "用自然平静的语气说",
        "emo_vec": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8],
        "emo_weight": 0.6,
        "duration_factor": 1.0,
        "reason": reason,
    }


def apply_audio_comment(output_dir: str, sid: str, msg_id: str, text: str,
                        comment: str, api_config: dict) -> dict:
    """把用户对一段音频的评论，翻译成 IndexTTS 的一整套音频参数。

    与 analyze_line_emotion 不同：这里不是从零判断台词语气，而是理解用户对
    「上一版音频」提出的调整意见（语速/情绪强度/语气/重音/停顿等），据此生成
    调整后的完整参数集（emo_text + emo_vec + emo_weight + duration_factor）。

    输入：
      sid: 会话 ID；msg_id: 要调整的消息 ID；text: 台词原文；comment: 用户评论
    输出：同 analyze_line_emotion 的结构（emo_text/emo_vec/emo_weight/duration_factor/...）
    """
    session = _load_session_anywhere(output_dir, sid)
    if not session:
        return {"ok": False, "error": "会话不存在或已过期"}

    text = (text or "").strip()
    comment = (comment or "").strip()
    if not text:
        return {"ok": False, "error": "台词不能为空"}
    if not comment:
        return {"ok": False, "error": "评论不能为空"}

    # 音频评论是同一条台词的连续调音指令：保留之前的有效约束，
    # 并明确告诉模型“本次评论优先解决冲突”，避免第二次调速把第一次情绪要求抹掉。
    target_msg = next((m for m in session.messages if m.get("id") == msg_id), None)
    previous_adjustments = (target_msg or {}).get("audio_adjustments") or []
    previous_text = ""
    if previous_adjustments:
        compact = []
        for item in previous_adjustments[-6:]:
            compact.append(
                f"第{item.get('index', len(compact)+1)}次意见：{item.get('comment', '')[:120]}；"
                f"已采用参数：语速因子{item.get('duration_factor', 1.0)}，"
                f"情绪权重{item.get('emo_weight', 0.6)}，语气{item.get('emo_text', '')[:80]}"
            )
        previous_text = "\n".join(compact)

    # 定位上下文（前 6 条），帮助理解这句台词在什么情境下说
    idx = -1
    for i, m in enumerate(session.messages):
        if m.get("id") == msg_id:
            idx = i
            break
    if idx < 0:
        idx = len(session.messages) - 1
    ctx_start = max(0, idx - 6)
    context_lines = []
    for m in session.messages[ctx_start:idx]:
        role = "师傅" if m.get("role") == "user" else session.persona_name
        context_lines.append(f"{role}：{m.get('content', '')}")
    if idx >= 0 and idx < len(session.messages):
        m = session.messages[idx]
        role = "师傅" if m.get("role") == "user" else session.persona_name
        context_lines.append(f"{role}（本句）：{m.get('content', '')}")
    context_text = "\n".join(context_lines) if context_lines else f"{session.persona_name}：{text}"

    persona = session.persona or ""
    persona_hint = persona[:3000]

    prompt = (
        "你是配音导演，精通 IndexTTS-2.5 的情感合成。已经给一句话配了一版音，"
        "现在师傅（用户）对上一版音频提了调整意见，你要把他的意见翻译成 IndexTTS "
        "的一整套具体音频参数，让下一版更符合他的要求。\n\n"
        f"【说话人是谁】{session.persona_name}（被模拟的来访者/求助者，通过微信跟师傅聊天）\n"
        f"【说话人性格与口语指纹摘要】\n{persona_hint}\n\n"
        f"【对话上下文】\n{context_text}\n\n"
        f"【要配音的这句台词】\n「{text}」\n\n"
        f"【师傅对上一版音频的评论/意见】\n「{comment}」\n\n"
        f"【此前已采用的音频调整（如与本次意见冲突，以本次意见为准；不冲突的继续保留）】\n"
        f"{previous_text or '无'}\n\n"
        "请严格只输出一个 JSON 对象（不要 markdown、不要任何解释文字）：\n"
        "{\n"
        '  "emotion_label": "调整后最贴切的中文情绪词（如：平静/焦虑/惊喜/委屈/着急/温柔/激动/低落…）",\n'
        '  "emo_text": "一句自然语言描述这句台词调整后该怎么念，必须体现师傅评论里的要求，包含语气+情绪+语速+停顿+重音，30字以内",\n'
        '  "emo_vec": [高兴, 生气, 悲伤, 害怕, 厌恶, 忧郁, 惊讶, 平静]，8 个 0~1.2 的数，总和不超过 1.5，主情绪给高、其余给低，\n'
        '  "emo_weight": 0~1 的情感权重（越大越夸张）,\n'
        '  "duration_factor": 语速因子（精确给值：0.7~0.85 很快、0.85~0.95 偏快、0.95~1.05 正常、1.05~1.2 偏慢、1.2~1.5 很慢）,\n'
        '  "annotated_text": "带拼音声调标注的台词原文：只给【易读错的多音字】和【需读轻声的字】加标注（格式：字后紧跟[拼音+声调数字]，如 重[zhòng]、了[le]。声调数字：1阴平2阳平3上声4去声5轻声），其余字原样保留；若无标注需求就直接原样返回台词原文",\n'
        '  "stress_word": "这句台词最该重读/强调的那个词或字（1~4个字），若没有特别强调点就写空字符串",\n'
        '  "reason": "一句话说明：你是如何把师傅的评论落到这些具体参数上的"\n'
        "}\n"
        "规则（务必理解师傅评论的「方向」再给参数）：\n"
        "0. 这是连续调音，不是每次从零开始：先继承此前不冲突的调整；若本次明确相反（如‘别慢了/快一点’、‘情绪收一点/更饱满’），只在冲突点以本次为最终值。\n"
        "0.1 语速和情绪要联动：最终语速确定后，重新校准情绪表达的力度、停顿、重音和情感权重；慢速不能仍按平淡读法，快速也不能堆叠过度拖沓的情绪。\n"
        "1. 先判断评论的类型，再对症调整：\n"
        "   - 语速类（太快/太慢/赶/拖）：只改 duration_factor，情绪基本不动；\n"
        "   - 情绪强度类（太激动/太平淡/再饱满/收一点/温柔点）：改 emo_vec 的主情绪强弱 + emo_weight；\n"
        "   - 语气/态度类（温柔/严厉/俏皮/委屈/诚恳/敷衍）：改 emotion_label + emo_text + emo_vec；\n"
        "   - 重音/停顿类（XX 要重读/这里停一下/一口气）：写进 emo_text 和 stress_word；\n"
        "2. 参数要和「这句台词 + 上下文 + 他的性格」保持一致，不要因为师傅一句评论就完全背离台词本意；\n"
        "3. duration_factor 精确给值，别总用 1.0：\n"
        "   - 着急/激动/生气/兴奋/抢话 → 0.8~0.9；委屈/难过/疲惫/敷衍 → 1.05~1.2；犹豫/思考/不好意思 → 1.1~1.3；平静/正常 → 0.95~1.05；\n"
        "4. 停顿写进 emo_text（「中间顿一下/前面停一下/说到XX慢下来」）；重音写进 stress_word 并在 emo_text 里写「XX加重/重读」；\n"
        "5. 多音字/轻声字在 annotated_text 里用「字[拼音声调]」标注正确读音；\n"
        "6. 数值要合理：主情绪 0.5~0.9，次要 0.1~0.3，8 维总和 ≤ 1.5；emo_weight 口语对话 0.5~0.7，要更夸张才给 0.7~0.9。"
    )

    from openai import OpenAI
    client = OpenAI(
        base_url=api_config["base_url"],
        api_key=api_config["api_key"],
        timeout=180.0,
        max_retries=3,
    )
    last_err = None
    raw = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=api_config.get("model", "deepseek-chat"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=400,
            )
            raw = (resp.choices[0].message.content or "").strip()
            break
        except Exception as e:
            last_err = e
            err_s = str(e).lower()
            if any(k in err_s for k in ("eof", "ssl", "reset", "timeout", "timed out", "connection")):
                time.sleep(1.0 * (attempt + 1))
                continue
            break
    if raw is None:
        msg = str(last_err) if last_err else "未知错误"
        err_s = msg.lower()
        if any(k in err_s for k in ("ssl", "handshake", "timed out", "timeout", "connection", "reset", "eof")):
            return {"ok": False, "error": "评论理解暂时无法连接模型服务：网络握手超时，请稍后重试"}
        return {"ok": False, "error": f"评论理解失败：{msg[:120]}"}

    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
        except Exception:
            data = None
    if not isinstance(data, dict):
        return {"ok": False, "error": "评论理解失败，请换个说法再试"}

    vec = data.get("emo_vec")
    if not isinstance(vec, list) or len(vec) != 8:
        vec = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8]
    vec = [max(0.0, min(1.2, float(v))) for v in vec]
    total = sum(vec)
    if total > 1.5:
        scale = 1.5 / total
        vec = [round(v * scale, 3) for v in vec]

    def _clamp(v, lo, hi, default):
        try:
            return max(lo, min(hi, float(v)))
        except Exception:
            return default

    annotated_text = str(data.get("annotated_text") or "").strip() or text

    result = {
        "ok": True,
        "emotion_label": str(data.get("emotion_label") or "平静").strip() or "平静",
        "emo_text": str(data.get("emo_text") or "").strip() or "用自然平静的语气说",
        "emo_vec": [round(v, 3) for v in vec],
        "emo_weight": _clamp(data.get("emo_weight"), 0.0, 1.0, 0.6),
        "duration_factor": _clamp(data.get("duration_factor"), 0.7, 1.3, 1.0),
        "annotated_text": annotated_text,
        "stress_word": str(data.get("stress_word") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
    }
    if target_msg is not None:
        history = list(previous_adjustments)
        history.append({
            "index": len(history) + 1,
            "comment": comment[:300],
            "duration_factor": result["duration_factor"],
            "emo_weight": result["emo_weight"],
            "emo_text": result["emo_text"][:120],
            "emotion_label": result["emotion_label"],
            "at": _now(),
        })
        target_msg["audio_adjustments"] = history[-12:]
        _save_session(output_dir, session)
        result["adjustment_index"] = len(history)
        result["adjustment_count"] = len(history)
    return result


# ---------------------------------------------------------------- 批量 TTS

def get_agent_messages_for_tts(output_dir: str, sid: str) -> dict:
    """获取会话中所有 agent 的回复，供 TTS 批量生成"""
    session = CHAT_SESSIONS.get(sid)
    if not session:
        session = _load_session(output_dir, sid)
        if not session:
            return {"ok": False, "error": "会话不存在"}
        CHAT_SESSIONS[sid] = session
    agent_msgs = [m for m in session.messages if m.get("role") == "assistant"]
    return {"ok": True, "messages": agent_msgs}
