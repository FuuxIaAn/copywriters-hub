# -*- coding: utf-8 -*-
"""
群聊讨论编排器

流程:
  Round 1  独立分析:  每个专家各自研读口播文稿 + 自己的知识库, 给出专业分析
  Round 2  互相讨论:  每个专家看到其他专家的观点, 回应/质疑/补充
  Round 3  给出终稿:  每个专家基于完整讨论, 给出最终修改版口播文稿
输出: Markdown 讨论记录 -> output/
"""
import datetime
import os

def _ctx_text(context: str = "") -> str:
    """归一化 context 参数：None/空 → 占位说明。"""
    return (context or "").strip() or "（无特别说明）"


def _script_header(script: str, context: str = "") -> str:
    """构建带创作背景和原稿的公共头部（字符串拼接，避免 .format() 注入）。"""
    return (
        "创作背景（本账号的核心信息，你的一切分析与改写都必须围绕它，不能跑偏）：\n"
        + _ctx_text(context) + "\n\n"
        "【字数红线】整体文案必须控制在 600 字以内（含标题），除非用户另行特殊要求。"
        "你的所有改写建议都应以此为前提——如果原稿超了，你要帮着砍；如果改写后更长了，你要控制住。\n\n"
        "这是一篇待打磨的口播文稿：\n\n【口播文稿开始】\n" + script + "\n【口播文稿结束】\n\n"
    )


def _round1_prompt(script: str, context: str = "") -> str:
    return (
        _script_header(script, context)
        + "请你以专业身份，独立分析并给出修改建议。\n"
        "要求：一律用「改写+理由」短格式，直接给出改写结果，不要长篇大论：\n"
        "1. 挑出问题最严重的 1-3 处，每处按以下格式输出：\n"
        "   【原文】该段原文（截取关键句即可）\n"
        "   【改写】改好的文本（直接改出来给我看，必须是可直接替换的完整段落）\n"
        "   【理由】一句话说明为什么这么改\n"
        "2. 最后用一句话总评收尾。"
    )


def _round2_prompt(script: str, others_replies: list, context: str = "") -> str:
    others_text = "\n\n".join(others_replies)
    return (
        _script_header(script, context)
        + "以下是其他专家对本稿的第一轮分析：\n\n"
        + others_text
        + "\n\n现在进入群聊讨论环节。请针对其他专家的观点明确回应：哪些你认同、哪些你有不同意见。\n"
        "要求：同样用「改写+理由」短格式——你的回应要落到具体段落上，直接给出你坚持或修正后的【改写】文本和一句话【理由】；"
        "讨论要针锋相对、具体，不要客套，不要长篇大论。"
    )


def _round3_prompt(script: str, full_log: str, context: str = "") -> str:
    return (
        _script_header(script, context)
        + "以下是刚才完整的群聊讨论记录：\n\n"
        + full_log
        + "\n\n现在请你作为专家，基于以上讨论，给出**最终段落级改写建议**。"
        "要求：不要输出整篇成稿全文，而是针对原稿中值得改的**具体段落/关键句子**，逐条给出可直接替换的改写建议。"
        "每条按以下格式输出：\n"
        "【段落定位】用原文中的一句话或关键词定位你要改的位置\n"
        "【改写】改好的文本（可直接替换该段落/句子）\n"
        "【理由】一句话说明为什么这么改\n\n"
        "只输出最有必要的 3-5 处改写，不要面面俱到；不标新立异，只在讨论基础上给出你最坚持的改动。"
        "开头标注【段落级改写建议】，末尾可给一句 50 字以内的整体结论。"
    )


def run_discussion(script: str, agents: list, config: dict) -> str:
    return run_discussion_stream(script, agents, config)


def run_discussion_stream(script: str, agents: list, config: dict, on_event=None) -> str:
    """流式版群聊编排：每轮每位专家发言后通过 on_event(dict) 回调推送事件。

    事件类型:
      system  -> {"type":"system","text":...}
      typing  -> {"type":"typing","name":...,"title":...}
      message -> {"type":"message","name":...,"title":...,"text":...}
      final   -> {"type":"final","name":...,"title":...,"text":...}
      done    -> {"type":"done"}
    """
    def emit(**kw):
        if on_event:
            try:
                on_event(kw)
            except Exception:  # noqa: BLE001
                pass

    ctx = (config or {}).get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()

    round1 = {}   # agent_id -> 发言
    round2 = {}   # agent_id -> 发言
    finals = {}   # agent_id -> 终稿

    print("===== Round 1: 各自独立分析 =====")
    emit(type="system", text="Round 1 · 各位专家独立研读文稿")
    for a in agents:
        print(f"  [{a.name}] 分析中 ...")
        emit(type="typing", name=a.name, title=a.title)
        round1[a.id] = a.say([{"role": "user", "content": _round1_prompt(script, context)}])
        emit(type="message", name=a.name, title=a.title, text=round1[a.id])
        print(f"  [{a.name}] 完成")

    print("===== Round 2: 群聊互评 =====")
    emit(type="system", text="Round 2 · 群聊互评交锋")
    for a in agents:
        others = [round1[o.id] for o in agents if o.id != a.id]
        print(f"  [{a.name}] 回应中 ...")
        emit(type="typing", name=a.name, title=a.title)
        round2[a.id] = a.say([{"role": "user", "content": _round2_prompt(script, others, context)}])
        emit(type="message", name=a.name, title=a.title, text=round2[a.id])
        print(f"  [{a.name}] 完成")

    print("===== Round 3: 给出终稿 =====")
    emit(type="system", text="Round 3 · 各位专家给出终稿")
    full_log = "\n\n".join(
        [f"### {a.name}（{a.title}）第一轮分析\n{round1[a.id]}\n\n### {a.name}（{a.title}）讨论回应\n{round2[a.id]}"
         for a in agents]
    )
    for a in agents:
        print(f"  [{a.name}] 撰写终稿 ...")
        emit(type="typing", name=a.name, title=a.title)
        finals[a.id] = a.say([{"role": "user", "content": _round3_prompt(script, full_log, context)}])
        emit(type="final", name=a.name, title=a.title, text=finals[a.id])
        print(f"  [{a.name}] 完成")

    return _render_markdown(script, agents, round1, round2, finals)


def _render_markdown(script, agents, round1, round2, finals) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 口播文稿 · 专家群聊讨论记录",
        "",
        f"> 生成时间：{now}",
        f"> 参与专家：{'、'.join(f'{a.name}（{a.title}）' for a in agents)}",
        "",
        "## 原稿",
        "",
        script,
        "",
    ]
    for a in agents:
        lines += [
            f"## {a.name}（{a.title}）· 第一轮独立分析",
            "",
            round1[a.id],
            "",
            f"## {a.name}（{a.title}）· 讨论回应",
            "",
            round2[a.id],
            "",
        ]
    lines += ["---", "", "## 各专家最终修改稿", ""]
    for a in agents:
        lines += [
            f"### {a.name}（{a.title}）终稿",
            "",
            finals[a.id],
            "",
        ]
    return "\n".join(lines)


def save_output(markdown: str, output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"discussion_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    return path
