# -*- coding: utf-8 -*-
"""
洗稿工坊业务流程
=============================================
一篇洗稿的完整生命周期（后台线程 + SSE 事件流）：

1. 阿骨拆解骨架（并保存到档案）
2. 全员分析（8 位文案专家 + 数据专员阿数）：
   爆点/炸点/争议点/共鸣点/情绪点 + 不可动句子 + 爆款原因 + 原则性建议
3. 分区补写：每个区域由当前「负责人」专家补写文案
4. 阿审审查（第一遍）
5. 用户逐区评论迭代 → 负责专家重写（循环直到满意）
6. 满意后最终阿审 + 阿数记录分工
7. 用户回填成品数据（满 3 篇）→ 阿数建立评价标准 → 负责人替换

事件类型（均通过 session.push 推送）：
  system  + kind=phase        阶段提示
  message + kind=analysis     全员分析发言
  message + kind=skeleton     阿骨骨架
  message + kind=part         分区成品（附 region 字段）
  message + kind=review       阿审审查报告
  message + kind=record       阿数分工记录
  message + kind=result       数据回填确认
  message + kind=evaluate     评价标准与负责人判断
  message + kind=replace      替换结果
  done                        流程结束
"""
import json
import re

import rewrite_store

# 文案专家（参与分析与补写；补写时每人负责一个区域）
COPYWRITER_NAMES = ["阿沁", "老周", "阿爆", "小黄", "爆哥", "阿证", "阿骨", "阿导"]


# ---------- 文本工具（与 server 保持一致，避免循环依赖） ----------

def _norm_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[。；;、，,.\s\u3000\"'“”‘’!！?？:：()（）\-—]+", "", str(text))


def _text_similarity(a: str, b: str) -> float:
    """粗略相似度：短文本互包含判定。"""
    if not a or not b:
        return 0.0
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    common = len(set(na[:8]) & set(nb[:8]))
    return common / max(len(set(na[:8])), 1)


def _extract_section(text: str, marker: str) -> str:
    """按【标记】截取文本中的一节。marker 形如 '不可动句子'，匹配【不可动句子】。"""
    if not text:
        return ""
    pat = re.compile(r"【\s*" + re.escape(marker) + r"\s*】")
    m = pat.search(text)
    if not m:
        return ""
    start = m.end()
    # 截到下一个【xxx】或结尾
    nxt = re.search(r"【[^】]{2,12}】", text[start:])
    seg = text[start:start + (nxt.start() if nxt else len(text[start:]))]
    return seg.strip()


def _extract_sentences(seg: str) -> list:
    """把一段文本切成句子（按换行/句号等）。"""
    if not seg:
        return []
    parts = re.split(r"[\n。！？!?]+", seg)
    return [p.strip() for p in parts if len(p.strip()) >= 4]


def _consensus_untouchable(analyses: list, threshold_ratio: float = 0.5) -> list:
    """从全员分析中聚合「不可动句子」共识。
    analyses: [{"agent": name, "text": 分析全文}]
    返回: [{sentence, agents: [..], reason}]，被 >= 一半专家标记的句子为共识。
    """
    candidates = []  # [{sentence, agents, reasons}]
    for a in analyses:
        seg = _extract_section(a["text"], "不可动句子")
        for sent in _extract_sentences(seg):
            hit = None
            for c in candidates:
                if _text_similarity(c["sentence"], sent) >= 0.7:
                    hit = c
                    break
            if hit:
                if a["agent"] not in hit["agents"]:
                    hit["agents"].append(a["agent"])
                hit["reasons"].append(sent)
            else:
                candidates.append({"sentence": sent, "agents": [a["agent"]], "reasons": [sent]})
    n = max(len(analyses), 1)
    cons = [c for c in candidates if len(c["agents"]) >= max(1, int(n * threshold_ratio))]
    cons.sort(key=lambda c: len(c["agents"]), reverse=True)
    return [{"sentence": c["sentence"][:200], "agents": c["agents"], "reason": (c["reasons"][0] or "")[:150]} for c in cons]


# ---------- 各阶段 Prompt ----------

def _skeleton_prompt(original: str, requirements: str) -> str:
    parts = [
        "你是一位口播文稿骨架师。请拆解下面这篇原稿，严格按以下标记输出（每个标记单独成段）：",
        "",
        "【唯一核心主张】一句话（可核查的断言）",
        "【段落结构】段序/段功能/篇幅百分比/观众心理状态（逐段列出）",
        "【黄金3秒钩子位】钩子类型 + 要达到的心理目标",
        "【不可动句子】原稿中逻辑/情绪/信息上不能动的句子，逐句列出并各附一句理由",
        "【爆款要素位置】爆点/炸点/争议点/共鸣点/情绪点，分别落在原稿哪一句或哪一段",
        "【本稿禁忌】3 条（这类稿子绝对不能犯的错）",
        "【洗稿要点】根据洗稿要求给出的改写方向（若用户没有要求则写：无特殊要求）",
        "",
    ]
    if requirements:
        parts.append(f"【用户洗稿要求】{requirements}")
        parts.append("")
    parts.append("【原稿】")
    parts.append(original)
    return "\n".join(parts)


def _analysis_prompt(original: str, metrics: dict, requirements: str, extra: str = "") -> str:
    m = metrics or {}
    parts = [
        "你正在参与一篇口播文稿的洗稿分析。下面给你原稿和它的数据表现，请以你的专业视角逐项分析：",
        "",
        f"【数据】点赞 {m.get('likes', '?')} / 评论 {m.get('comments', '?')} / 转发 {m.get('forwards', '?')} / 收藏 {m.get('saves', '?')}",
    ]
    if requirements:
        parts.append(f"【用户洗稿要求】{requirements}")
    parts += [
        "",
        "严格按以下标记输出（每个标记单独成段，宁精勿长）：",
        "【爆点】这篇稿子最容易爆/最高能的地方（原句引用+一句话分析）",
        "【炸点】最有冲击力的地方（原句引用+一句话分析）",
        "【争议点】最容易引发讨论的地方，或可以设计争议的位置",
        "【共鸣点】最容易引发目标人群共鸣的地方",
        "【情绪点】情绪曲线在哪里起伏、怎么起伏",
        "【不可动句子】原稿中绝对不能动的句子，逐句列出（每句一行），各附极短理由",
        "【爆款原因】结合四维数据判断这篇稿子为什么能跑（或哪里差）",
        "【原则性建议】提炼 1-2 条可复用、可沉淀的原则",
        "",
    ]
    if extra:
        parts.append(f"【补充信息】{extra}")
        parts.append("")
    parts.append("【原稿】")
    parts.append(original)
    return "\n".join(parts)


def _part_prompt(agent, region_label: str, region_desc: str, original: str, skeleton: str,
                 untouchable: list, analyses_text: str, requirements: str, char_budget: int = 180) -> str:
    parts = [
        f"你是「{agent.name}」（{agent.title}）。在洗稿流程中，你负责补写【{region_label}】（{region_desc}）。",
        "",
        f"请直接输出你负责的这一部分的文案（可用 1-3 个自然段，控制在 {char_budget} 字以内，直接可用的成品，不要解释过程）。",
        "",
        "【硬性要求】",
        "1. 必须保留全部「不可动句子」（原句照抄，一字不改）；",
        "2. 风格与信息密度贴近原稿，但可以升级表达；",
        "3. 运用你自己的专业手法（钩子/论证/共鸣/核查/节奏等）；",
        "4. 只写你负责的区域，不要越界写其他部分；",
    ]
    if requirements:
        parts.append(f"5. 满足用户洗稿要求：{requirements}")
    parts += [
        "",
        "【原稿】",
        original[:4000],
        "",
        "【骨架】",
        (skeleton or "")[:3000],
        "",
        "【不可动句子（必须原样保留）】",
        "\n".join(f"- {u['sentence']}" for u in untouchable) or "（无共识不可动句）",
        "",
        "【其他专家的分析参考】",
        (analyses_text or "")[:2500],
        "",
        f"现在输出【{region_label}】的文案：",
    ]
    return "\n".join(parts)


def _review_prompt(parts_text: str, principles: str, original: str) -> str:
    return (
        "你正在审查一篇洗稿后的口播文稿成品。请逐条对照全部原则（建议性+禁止性）审查：\n\n"
        f"【原则清单】\n{principles or '（暂无原则）'}\n\n"
        f"【洗稿成品全文】\n{parts_text}\n\n"
        f"【原稿参考】\n{original[:2000]}\n\n"
        "【审查要求】\n"
        "1. 🚫 禁止性原则：检查是否有违反，指出具体句子、违反哪条、怎么改；\n"
        "2. ✅ 建议性原则：检查是否忽略，指出忽略哪条、可能的影响；\n"
        "3. 完全合规则标注「✅ 全部原则审查通过」；\n"
        "4. 只审查、给修改方向，不重写整篇。\n"
        "输出：逐条对应 + 末尾一句总体结论（共发现 X 处违规、Y 处忽略）。"
    )


def _record_prompt(parts: dict, regions: list) -> str:
    lines = ["请记录本次洗稿的分工明细，按以下标记输出：", "", "【分工表】"]
    for r in regions:
        p = parts.get(r["id"]) or {}
        if p.get("agent"):
            lines.append(f"- {r['label']} → {p['agent']}（一句点评：这部分他写得怎么样）")
    lines += [
        "",
        "【整体评价】一句话总结这次分区补写的配合情况。",
    ]
    return "\n".join(lines)


def _evaluate_prompt(sessions: list, assignments: dict, regions: list) -> str:
    lines = [
        "你是数据专员阿数。以下是多篇洗稿成品的分工与真实数据表现，请建立「分区负责人评价标准」并判断每个区域当前负责人是否胜任。",
        "",
        "【四维数据的区域含义参考】",
        "- 点赞量 → 爆点/共鸣点/情绪点（让人认同）",
        "- 评论量 → 争议点（让人想说话）",
        "- 转发量 → 共鸣点/爆点（让人想分享）",
        "- 收藏量 → 中间段落/结尾（干货价值）",
        "- 开头与节奏：影响完播/跳出（数据中无完播时按点赞+收藏推断吸引力）",
        "",
        "【当前负责人映射】",
    ]
    for r in regions:
        lines.append(f"- {r['label']}（{r['id']}）→ {assignments.get(r['id'], '?')}")
    lines += ["", "【各篇数据（含分工）】"]
    for s in sessions[:8]:
        m = s.get("result_metrics") or {}
        parts = s.get("parts") or {}
        lines.append(
            f"- 「{s.get('title','')[:20]}」数据：点赞{m.get('likes','?')} 评论{m.get('comments','?')} "
            f"转发{m.get('forwards','?')} 收藏{m.get('saves','?')}"
        )
        for r in regions:
            p = parts.get(r["id"]) or {}
            if p.get("agent"):
                lines.append(f"    {r['label']}（{r['id']}）→ {p['agent']}")
    lines += [
        "",
        "【输出要求】只输出一个 JSON 对象，不要任何多余文字：",
        '{',
        '  "standards": "评价标准说明（每个区域用什么数据指标衡量，怎么算好）",',
        '  "verdicts": [',
        '    {"region": "opening", "agent": "小黄", "verdict": "keep|replace", "reason": "依据数据的一句话理由", "suggested": "建议替换为谁（仅 verdict=replace 时）"}',
        "  ]",
        "}",
        "verdicts 必须覆盖全部区域，逐一判断当前负责人行不行。",
    ]
    return "\n".join(lines)


def _parse_evaluate_json(text: str) -> dict:
    """解析阿数输出的 JSON（容错：截取 { } 区间）。"""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}


def _iter_comment_prompt(agent, region_label: str, current_text: str, comment: str, original: str,
                         untouchable: list = None) -> str:
    unt = untouchable or []
    parts = [
        f"用户对你负责的【{region_label}】提出了评论，请你根据评论重写这一部分。\n\n",
        f"【你当前的部分】\n{current_text}\n\n",
        f"【用户评论】\n{comment}\n\n",
        f"【原稿参考】\n{original[:2000]}\n\n",
        "【硬性要求】\n"
        "1. 以下「不可动句子」是全员共识，必须原样保留（一字不改），你的重写只能围绕它们展开：\n"
        + ("\n".join(f"   - {u['sentence']}" for u in unt) if unt else "   （无共识不可动句）") + "\n\n"
        "2. 直接输出修改后的该部分文案（成品，不要解释）；"
        "如果用户的评论成立，按评论方向修改；如果你认为评论方向有问题，先一句说明再给出你认为更好的版本。\n"
        "3. 注意与其他部分的衔接保持一致。",
    ]
    return "\n".join(parts)


def _iter_sentence_prompt(agent, region_label: str, sentence: str, comment: str, original: str,
                          untouchable: list = None) -> str:
    """用户对成品里【某一句话】评论 → 让负责该句的专家只重写这一句。
    强调：只输出这一句，不要输出整段。"""
    unt = untouchable or []
    unt_hit = [u["sentence"] for u in unt
               if u.get("sentence") and (u["sentence"] in sentence or sentence in u["sentence"])]
    parts = [
        f"你负责的【{region_label}】里有一句话，用户提出了评论，请你【只重写这一句话】。\n\n",
        f"【这句话】\n{sentence}\n\n",
        f"【用户评论】\n{comment}\n\n",
        f"【原稿参考】\n{original[:1500]}\n\n",
        "【硬性要求】\n"
        "1. 只输出重写后的这一句话本身（成品，不要解释、不要加前后缀、不要写『好的』『以下是』之类）；\n"
        "2. 保持这句话在原文中的语气与信息密度，不要引入新的段落；\n"
        "3. 如果这句话属于「不可动句子」共识，则不能改动其表达核心：\n"
        + ("\n".join(f"   - {u}" for u in unt_hit) if unt_hit else "   （这句不在不可动共识内，可自由改写）") + "\n"
        "4. 如果用户的评论不适用于这一句，仍尽量按评论意图微调；若实在无法改，就原样复述这一句。",
    ]
    return "\n".join(parts)


# ---------- 主流程 ----------

def start_rewrite_flow(session, config, original: str, metrics: dict, requirements: str, output_dir: str, rid: str):
    """后台线程：跑完整洗稿流程（骨架→分析→补写→审查）。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass

    entry = rewrite_store.get_session(output_dir, rid)
    if entry is None:
        entry = rewrite_store.create_session(output_dir, original, metrics, requirements)

    try:
        from server import build_agents, _build_data_analyst
        from data_insight_store import add_principles, principles_text

        # 参与分析的全员 = 8 位文案专家 + 数据专员
        agents = build_agents(config)
        da = _build_data_analyst(config)

        # ---------- 阶段 1：阿骨拆解骨架 ----------
        push(type="system", text="🧬 洗稿流程启动 · 阶段1：阿骨拆解骨架", kind="phase")
        push(type="typing", name="阿骨", title="文案骨架派")
        skeleton_agent = next((a for a in agents if a.name == "阿骨"), None)
        skeleton_text = ""
        if skeleton_agent:
            skeleton_text = skeleton_agent.say([{"role": "user", "content": _skeleton_prompt(original, requirements)}])
        else:
            skeleton_text = "（未找到阿骨，跳过骨架拆解）"
        push(type="message", name="阿骨", title="文案骨架派", text=skeleton_text, kind="skeleton")
        # 存档骨架
        rewrite_store.update_session(output_dir, rid, lambda s: s.update({"skeleton": skeleton_text}))

        # ---------- 阶段 2：全员分析 ----------
        push(type="system", text="🔍 阶段2：全员分析原稿（爆点/炸点/争议点/共鸣点/情绪点/不可动句子）", kind="phase")
        analyses = []
        for a in agents + [da]:
            push(type="typing", name=a.name, title=a.title)
            extra = "你是数据专员：请额外给出【数据判级】（点赞/评论/转发/收藏 相对强弱，说明这篇稿子强在哪项、弱在哪项）。" if a.name == "阿数" else ""
            reply = a.say([{"role": "user", "content": _analysis_prompt(original, metrics, requirements, extra)}])
            push(type="message", name=a.name, title=a.title, text=reply, kind="analysis")
            analyses.append({"agent": a.name, "text": reply})
        # 不可动句子共识
        untouchable = _consensus_untouchable(analyses)
        rewrite_store.update_session(output_dir, rid, lambda s: s.update({
            "analysis": {x["agent"]: x["text"] for x in analyses},
            "untouchable": untouchable,
        }))
        if untouchable:
            push(type="system",
                 text="🤝 共识不可动句子（≥半数专家标记）：\n" + "\n".join(f"- 「{u['sentence']}」（{len(u['agents'])}人）" for u in untouchable),
                 kind="phase")
        # 原则性建议落库（阿数的）
        da_analysis = next((x["text"] for x in analyses if x["agent"] == "阿数"), "")
        seg = _extract_section(da_analysis, "原则性建议")
        for line in _extract_sentences(seg):
            if "原则" in line or "必须" in line or "建议" in line or "禁止" in line or "不要" in line:
                add_principles(output_dir, [line], kind="suggest")

        # ---------- 阶段 3：分区补写 ----------
        push(type="system", text="✍️ 阶段3：专家分区补写（每位专家负责自己的区域）", kind="phase")
        regions = rewrite_store.get_regions(output_dir)
        assignments = rewrite_store.get_assignments(output_dir)
        analyses_text = "\n\n".join(f"### {x['agent']}\n{x['text'][:800]}" for x in analyses)
        parts = {}
        for r in regions:
            owner = assignments.get(r["id"], r["default"])
            a = next((x for x in agents if x.name == owner), None)
            if not a:
                continue
            push(type="typing", name=a.name, title=a.title)
            reply = a.say([{"role": "user",
                            "content": _part_prompt(a, r["label"], r["desc"], original, skeleton_text,
                                                    untouchable, analyses_text, requirements)}])
            parts[r["id"]] = {"agent": owner, "text": reply, "comments": [],
                              "sentences": rewrite_store.split_sentences(reply)}
            push(type="message", name=a.name, title=a.title, text=reply, kind="part", region=r["id"], region_label=r["label"])
        rewrite_store.update_session(output_dir, rid, lambda s: s.update({"parts": parts}))

        # ---------- 阶段 4：阿审审查（第一遍） ----------
        push(type="system", text="⚖️ 阶段4：阿审原则审查", kind="phase")
        parts_text = _parts_to_text(parts, regions)
        principles = principles_text(output_dir, max_chars=3000)
        push(type="typing", name="阿审", title="原则审查员")
        reviewer = None
        try:
            from server import _build_principle_reviewer
            reviewer = _build_principle_reviewer(config)
        except Exception:  # noqa: BLE001
            pass
        if reviewer:
            report = reviewer.say([{"role": "user", "content": _review_prompt(parts_text, principles, original)}])
        else:
            report = "（未找到阿审，跳过原则审查）"
        push(type="message", name="阿审", title="原则审查员", text=report, kind="review")
        rewrite_store.update_session(output_dir, rid, lambda s: s.update({"principle_review": report}))

        # 存档状态
        rewrite_store.set_session_status(output_dir, rid, "review")
        session.rw["status"] = "review"
        push(type="system", text="✅ 洗稿初稿完成！你可以逐区查看、评论修改；满意后点击「最终审查 + 记录分工」。", kind="phase")
    except Exception as e:  # noqa: BLE001
        print(f"[rewrite] 洗稿流程异常: {e}")
        import traceback
        traceback.print_exc()
        push(type="error", text=f"洗稿流程出错：{e}")
    finally:
        session.finished = True
        session.end_phase()
        push(type="done")


def _parts_to_text(parts: dict, regions: list) -> str:
    """按区域顺序拼装成品全文（供审查/记录/拼接用）。"""
    lines = []
    for r in regions:
        p = parts.get(r["id"]) or {}
        if p.get("text"):
            lines.append(f"【{r['label']}】\n{p['text']}")
    return "\n\n".join(lines)


def run_part_comment(session, config, rid: str, region_id: str, comment: str, output_dir: str):
    """用户对某区域评论 → 负责该区域的专家重写。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    from server import build_single_agent
    regions = rewrite_store.get_regions(output_dir)
    region = next((r for r in regions if r["id"] == region_id), None)
    if not region:
        push(type="error", text=f"找不到区域 {region_id}")
        return
    entry = rewrite_store.get_session(output_dir, rid)
    parts = (entry or {}).get("parts") or {}
    part = parts.get(region_id) or {}
    owner = part.get("agent") or rewrite_store.get_agent_for_region(output_dir, region_id)
    agent = build_single_agent(config, owner)
    untouchable = (entry or {}).get("untouchable") or []
    push(type="system", text=f"💬 你评论了【{region['label']}】（负责人 {owner}）：{comment[:120]}{'…' if len(comment)>120 else ''}", kind="phase")
    push(type="typing", name=owner, title=agent.title if agent else "")
    if not agent:
        push(type="error", text=f"找不到专家 {owner}")
        return
    # 标记「迭代中」，让列表/详情状态可见
    rewrite_store.set_session_status(output_dir, rid, "iterating")
    current = part.get("text", "")
    try:
        reply = agent.say([{"role": "user", "content": _iter_comment_prompt(agent, region["label"], current, comment, (entry or {}).get("original", ""), untouchable)}])
    except Exception:  # noqa: BLE001
        rewrite_store.set_session_status(output_dir, rid, "review")
        raise
    comments = list(part.get("comments") or [])
    comments.append({"comment": comment, "reply": reply, "time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")})
    rewrite_store.update_session(output_dir, rid, lambda s: s["parts"].setdefault(region_id, {}).update({"text": reply, "comments": comments}))
    # 迭代完成恢复「待定稿」
    rewrite_store.set_session_status(output_dir, rid, "review")
    push(type="message", name=owner, title=agent.title, text=reply, kind="part", region=region_id, region_label=region["label"], is_iteration=True)


def run_sentence_comment(session, config, rid: str, region_id: str, sentence: str, comment: str, output_dir: str):
    """用户对成品里【某一句话】评论 → 负责该分区的专家只重写这一句。
    回写 parts[region_id].sentences 中的对应句，并通过 SSE 推送 sentence_update 事件就地更新。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    from server import build_single_agent
    regions = rewrite_store.get_regions(output_dir)
    region = next((r for r in regions if r["id"] == region_id), None)
    if not region:
        push(type="error", text=f"找不到区域 {region_id}")
        return
    entry = rewrite_store.get_session(output_dir, rid)
    parts = (entry or {}).get("parts") or {}
    part = parts.get(region_id) or {}
    owner = part.get("agent") or rewrite_store.get_agent_for_region(output_dir, region_id)
    agent = build_single_agent(config, owner)
    if not agent:
        push(type="error", text=f"找不到专家 {owner}")
        return
    untouchable = (entry or {}).get("untouchable") or []
    original = (entry or {}).get("original") or ""

    push(type="system",
         text=f"💬 你评论了【{region['label']}】的一句话（{owner}）：{comment[:100]}{'…' if len(comment)>100 else ''}",
         kind="phase")
    push(type="typing", name=owner, title=agent.title)
    rewrite_store.set_session_status(output_dir, rid, "iterating")
    try:
        reply = agent.say([{"role": "user",
                            "content": _iter_sentence_prompt(agent, region["label"], sentence, comment, original, untouchable)}])
    except Exception:  # noqa: BLE001
        rewrite_store.set_session_status(output_dir, rid, "review")
        raise
    new_text = (reply or "").strip()
    # 去掉专家可能加的多余引号/标点
    new_text = new_text.strip("\"'“”‘’「」『』")
    if not new_text:
        new_text = sentence
    # 一次原子写入：替换单句 + 重组 text + 追加评论记录
    ret = rewrite_store.update_sentence(
        output_dir, rid, region_id, sentence, new_text,
        comment=comment,
        reply_time=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    rewrite_store.set_session_status(output_dir, rid, "review")
    push(type="message", name=owner, title=agent.title, text=new_text, kind="sentence_update",
         region=region_id, region_label=region["label"], sentence=sentence, is_iteration=True)


def run_final_review(session, config, rid: str, output_dir: str):
    """满意后：最终阿审审查 + 阿数记录分工。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    from server import _build_principle_reviewer, _build_data_analyst
    from data_insight_store import principles_text
    entry = rewrite_store.get_session(output_dir, rid)
    regions = rewrite_store.get_regions(output_dir)
    parts = (entry or {}).get("parts") or {}
    parts_text = _parts_to_text(parts, regions)
    principles = principles_text(output_dir, max_chars=3000)

    push(type="system", text="⚖️ 最终原则审查（针对你满意后的版本）", kind="phase")
    push(type="typing", name="阿审", title="原则审查员")
    reviewer = _build_principle_reviewer(config)
    report = reviewer.say([{"role": "user", "content": _review_prompt(parts_text, principles, (entry or {}).get("original", ""))}])
    push(type="message", name="阿审", title="原则审查员", text=report, kind="review", is_final=True)
    rewrite_store.update_session(output_dir, rid, lambda s: s.update({"principle_review": report}))

    push(type="system", text="📋 阿数记录本次分工", kind="phase")
    push(type="typing", name="阿数", title="数据专员")
    da = _build_data_analyst(config)
    record = da.say([{"role": "user", "content": _record_prompt(parts, regions)}])
    push(type="message", name="阿数", title="数据专员", text=record, kind="record")
    rewrite_store.update_session(output_dir, rid, lambda s: s.update({"owner_record": record, "status": "done"}))
    session.rw["status"] = "done"
    push(type="system", text="🎉 洗稿完成！分工已记录。发布后拿到数据，回填「成品数据」；累计满 3 篇即可让阿数建立评价标准、优化分区负责人。", kind="phase")


def run_evaluate(session, config, output_dir: str):
    """满 3 篇：阿数建立评价标准 + 逐区判断负责人。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    from server import _build_data_analyst
    sessions = rewrite_store.evaluated_sessions(output_dir)
    if len(sessions) < 3:
        push(type="error", text=f"已有成品数据回填的洗稿 {len(sessions)} 篇，满 3 篇才能建立评价标准。")
        return
    assignments = rewrite_store.get_assignments(output_dir)
    regions = rewrite_store.get_regions(output_dir)
    push(type="system", text=f"📊 阿数建立评价标准（已收集 {len(sessions)} 篇成品数据）", kind="phase")
    push(type="typing", name="阿数", title="数据专员")
    da = _build_data_analyst(config)
    reply = da.say([{"role": "user", "content": _evaluate_prompt(sessions, assignments, regions)}])
    parsed = _parse_evaluate_json(reply)
    verdicts = parsed.get("verdicts") or []
    standards = parsed.get("standards") or reply[:800]
    push(type="message", name="阿数", title="数据专员", text=reply, kind="evaluate")
    rewrite_store.set_evaluation(output_dir, standards, verdicts)
    # 统计 replace 数量
    n_replace = sum(1 for v in verdicts if v.get("verdict") == "replace")
    push(type="system", text=f"评价完成：共 {len(verdicts)} 个区域，建议替换 {n_replace} 个负责人。你可以在页面上查看并一键应用替换。", kind="phase")
