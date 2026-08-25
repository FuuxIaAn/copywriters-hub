# -*- coding: utf-8 -*-
"""
Agent 定义与 DeepSeek 客户端
"""
import time

from openai import OpenAI

_SAY_TIMEOUT = 180        # 单次调用最长等待（秒）
_MAX_RETRIES = 3          # 失败重试次数（网络抖动/SSL 握手自愈）

# 单次请求所有消息拼接后的字符安全上限。超过即触发模型输入超长 400。
# 兜底防御：任何模型输入（system + user）超过该长度时，优先压缩 system prompt 里
# 最占地方、对任务最不关键的 knowledge 档案，而不是让请求直接打爆上下文。
_MAX_MSG_CHARS = 30000


class Agent:
    def __init__(self, cfg: dict, knowledge: str, client: OpenAI, model: str, temperature: float,
                 knowledge_source: str = "原始知识库", name_prefix: bool = True,
                 feedback: str = "", lessons: str = "", context: str = "", methods: str = ""):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.title = cfg["title"]
        self.persona = cfg["persona"]
        self.criteria = cfg.get("criteria", "")
        self.knowledge = knowledge
        self.knowledge_source = knowledge_source
        self.feedback = feedback
        self.lessons = lessons
        self.context = context
        self.methods = methods
        self.client = client
        self.model = model
        self.temperature = temperature
        self.name_prefix = name_prefix

    def _system_prompt(self, knowledge_cap: int | None = None) -> str:
        knowledge = self.knowledge
        if knowledge_cap is not None and knowledge and len(knowledge) > knowledge_cap:
            knowledge = knowledge[:knowledge_cap] + (
                f"\n\n[知识档案过长，已截断至 {knowledge_cap} 字符，其余部分已省略。]"
            )
        parts = [self.persona]
        if self.criteria:
            parts.append(f"\n【评审标准】\n{self.criteria}")
        if self.context:
            parts.append(
                "\n【创作背景（本账号的核心信息，你的所有建议都必须围绕它，不能跑偏）】\n" + self.context
            )
        if self.methods:
            parts.append(
                "\n【技能方法论档案（融合了多套实战改写/标题/合规方法论，改写和给建议时务必主动运用）】\n"
                + self.methods
            )
        if knowledge:
            parts.append(
                f"\n【你的个人知识档案（{self.knowledge_source}，是你长期深度研读形成的知识内化成果，"
                f"讨论时务必主动运用其中理念、公式与话术，不要当作临时资料）】\n{self.knowledge}"
            )
        if self.feedback:
            parts.append(
                "\n【历史反馈档案（由记录员根据你历次被采纳改动的实际效果数据提炼，"
                "负面清单务必严格回避，正面清单要主动延续）】\n" + self.feedback
            )
        if self.lessons:
            parts.append(
                "\n【爆款实战吸收档案（你从用户提供的历篇爆款文案中提炼的实战知识点，"
                "每条都附原文摘录证据并经程序校验；讨论时主动运用这些已被验证的手法）】\n" + self.lessons
            )
        work_parts = []
        if self.name_prefix:
            work_parts.append("1. 发言请以「角色名：」开头，观点要具体、可执行。")
        else:
            work_parts.append("1. 观点要具体、可执行。")
        work_parts.append("2. 发言一律用「改写+理由」短格式：指出有问题的段落时，直接用【改写】把改好的文本写出来给用户看，再用一句话【理由】说明为什么这么改。宁可精炼，不要长篇大论。")
        work_parts.append("3. 讨论环节要针对其他专家的观点做出回应：认同、质疑或补充，不要各说各话；回应也要短。")
        parts.append("\n\n【工作方式】\n" + "\n".join(work_parts))
        return "\n".join(parts)

    def say(self, messages: list) -> str:
        """调用 DeepSeek，返回发言文本。带硬超时与自动重试（网络抖动自愈）。
        关键：用线程级 join(timeout) 强制最外层硬超时——OpenAI SDK 的 timeout 参数在
        TCP 半开/TLS 握手挂起等场景下不生效，请求会永久挂起导致 `say()` 永不返回、
        洗稿流程卡死。这里保证 `say()` 一定在限定时间内返回，流程才能推进并落盘失败。"""
        import threading
        result = {"ok": False, "text": "", "err": None}

        def _work():
            system = {"role": "system", "content": self._system_prompt()}
            # 兜底防线 1：总消息超长时优先压缩 system 里的 knowledge（最占地方、对任务最不关键）。
            if self.knowledge:
                total = len(system["content"]) + sum(len(m.get("content") or "") for m in messages)
                if total > _MAX_MSG_CHARS:
                    excess = total - _MAX_MSG_CHARS
                    cap = max(len(self.knowledge) - excess - 500, 1500)
                    system = {"role": "system", "content": self._system_prompt(knowledge_cap=cap)}
            # 兜底防线 2：压到最小 knowledge 后若仍超限（极端超大原文/上下文），对 user 消息
            # 按长度比例右截断，保证发给模型的请求一定 ≤ 安全上限，彻底杜绝输入超长 400。
            _total2 = len(system["content"]) + sum(len(m.get("content") or "") for m in messages)
            if _total2 > _MAX_MSG_CHARS:
                _over = _total2 - _MAX_MSG_CHARS
                # 各 user 消息按占比分摊需截断的字符，保留头部（指令/原文都在前面，截尾部细节影响最小）
                _alloc = [len(m.get("content") or "") for m in messages]
                _alloc_sum = sum(_alloc) or 1
                messages = [
                    dict(m, content=(m.get("content") or "")[: max(int(c - _over * c / _alloc_sum), 300)])
                    for m, c in zip(messages, _alloc)
                ]
            last_err = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    resp = self.client.chat.completions.create(
                        model=self.model,
                        temperature=self.temperature,
                        messages=[system] + messages,
                        timeout=_SAY_TIMEOUT,
                    )
                    content = resp.choices[0].message.content
                    result["ok"] = True
                    result["text"] = (content or "").strip()
                    return
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < _MAX_RETRIES:
                        # TLS 握手超时、连接重置等错误通常是瞬时网络问题；
                        # 逐步退避，给代理/运营商连接池时间恢复。
                        err_s = str(e).lower()
                        delay = 2.0 * (attempt + 1)
                        if any(k in err_s for k in (
                            "ssl", "handshake", "timed out", "timeout",
                            "connection", "reset", "eof", "temporarily",
                        )):
                            delay = 3.0 * (attempt + 1)
                        time.sleep(delay)
            result["err"] = last_err

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        # 硬超时：总时长 = 单次超时 × (重试次数+1) + 缓冲，超时即强制返回错误
        t.join(timeout=_SAY_TIMEOUT * (_MAX_RETRIES + 1) + 5)
        if t.is_alive():
            result["err"] = Exception("模型调用超时挂起（超过限定时间无响应），已强制中断，请稍后重试")
        if result["ok"]:
            return result["text"]
        err = result["err"]
        err_s = str(err or "").lower()
        if any(k in err_s for k in ("ssl", "handshake", "timed out", "timeout", "connection", "reset", "eof")):
            return f"[{self.name} 暂时无法连接模型服务：网络握手超时。请稍后点击重试，或检查网络/代理设置。]"
        return f"[{self.name} 调用失败：{str(err or '未知错误')[:160]}]"
