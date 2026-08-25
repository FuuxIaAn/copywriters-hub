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
import time

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


# 四个「点」的嵌入定位：从各点负责人输出的【嵌入位置】里识别目标段落
_POINT_BODY_ALIASES = {
    "开头": "opening", "开头段落": "opening", "开头段": "opening",
    "中间": "middle", "中间段落": "middle", "中间段": "middle",
    "结尾": "ending", "结尾段落": "ending", "结尾段": "ending",
}


def _parse_point(text: str) -> dict:
    """解析单个「点」负责人（爆点/争议/共鸣/情绪）的输出。
    期望格式：含【嵌入位置】和【优化句】两个标记。
    返回 {target_body: 'opening'|'middle'|'ending', anchor: 定位关键词, opt: 优化句文本}。
    解析不到时 opt 为空（表示该点本次不生效）。"""
    import re as _re
    res = {"target_body": "", "anchor": "", "opt": ""}
    t = (text or "").strip()
    if not t:
        return res

    def _between(marker: str, next_marker: str) -> str:
        """提取【marker】到【next_marker】（或文本末尾）之间的内容，不把内层【…】当边界。"""
        pm = _re.search(_re.escape(f"【{marker}】"), t)
        if not pm:
            return ""
        start = pm.end()
        if next_marker:
            nm = _re.search(_re.escape(f"【{next_marker}】"), t[start:])
            end = start + nm.start() if nm else len(t)
        else:
            end = len(t)
        return t[start:end].strip()

    pos = _between("嵌入位置", "优化句")
    opt = _between("优化句", None)
    res["opt"] = (opt or "").strip()
    # 从嵌入位置里识别目标段落
    if pos:
        low = pos
        for alias, rid in _POINT_BODY_ALIASES.items():
            if alias in low:
                res["target_body"] = rid
                break
        # 提取锚点：去掉段落名/定位外壳后，剩下定位描述（例如「第3句之后」「“越老越吃香”之后」）
        anchor = pos
        for alias in _POINT_BODY_ALIASES:
            anchor = _re.sub(alias, "", anchor)
        # 去掉「在…的」这类外壳与残留的【段落】字样
        anchor = _re.sub(r"^在?\s*", "", anchor)
        anchor = _re.sub(r"【段落】|段落", "", anchor)
        anchor = _re.sub(r"的$", "", anchor)
        res["anchor"] = anchor.strip("，。 、：:【】（）()“”\"'")
    return res


def _locate_anchor(sentences: list, anchor: str) -> int:
    """在段落句子列表中定位要替换/优化的那句的索引。
    支持两种锚点：
    - 「第N句」→ 按 1 基索引定位（越界返回 -1）；
    - 「…「某短语」…」/ “某短语” → 找到包含该短语的那句。
    返回句子下标；找不到返回 -1。"""
    import re as _re
    if not sentences or not anchor:
        return -1
    # 第N句
    m = _re.search(r"第\s*(\d+)\s*句", anchor)
    if m:
        idx = int(m.group(1)) - 1
        return idx if 0 <= idx < len(sentences) else -1
    # 引号短语：取「…」或 “…” 或 “…” 内内容，做包含匹配
    q = _re.search(r"[「“『]([^」”』]+)[」”』]", anchor)
    if q:
        phrase = _normalize(q.group(1))
        if phrase:
            for i, s in enumerate(sentences):
                if phrase and phrase in _normalize(s):
                    return i
    # 兜底：anchor 整体作为短语包含匹配
    whole = _normalize(anchor)
    if whole:
        for i, s in enumerate(sentences):
            if whole and whole in _normalize(s):
                return i
    return -1


def _apply_point_edits(body_parts: dict, point_edits: list, untouchable: list):
    """程序化地把四个「点」的优化句就地替换进对应段落，替代原先「整体节奏重新拼装整篇」。

    body_parts: {"opening": {"sentences": [...], "agent": "小黄"}, ...}（仅三段的正文）
    point_edits: [{target_body, anchor, opt, point_label}]
    untouchable: 不可动句子（任何情况下都不得被替换）

    返回 (final_sentences, final_agents)：
    final_sentences 按 开头→中间→结尾 顺序拼好；每个点的【优化句】替换掉目标句；
    每句带该段的负责人作者标记。
    """
    import re as _re
    unt_norm = {_normalize(u.get("sentence", "")) for u in untouchable if u.get("sentence")}
    order = ["opening", "middle", "ending"]
    # 每个段落：句子列表 + 每句作者
    segs = {}
    for rid in order:
        p = body_parts.get(rid) or {}
        sents = list(p.get("sentences") or [])
        ag = p.get("agent") or ""
        segs[rid] = {"sents": sents, "agents": [ag] * len(sents)}

    applied = []
    for pt in point_edits:
        rid = pt.get("target_body") or ""
        opt = (pt.get("opt") or "").strip()
        if not rid or rid not in segs or not opt:
            continue  # 定位或优化句缺失，本点不生效
        seg = segs[rid]
        idx = _locate_anchor(seg["sents"], pt.get("anchor") or "")
        if idx < 0:
            continue  # 定位不到目标句，跳过（宁可不动也不误替换）
        target = seg["sents"][idx]
        # 不可动句保护：目标句是不可动句则绝不替换
        if _normalize(target) in unt_norm:
            continue
        seg["sents"][idx] = opt  # 就地替换（优化句保留该段负责人标记）
        applied.append(f"{pt.get('point_label')}→{rid}[{idx}]")

    # 拼装 final：开头→中间→结尾
    final_sentences = []
    final_agents = []
    for rid in order:
        seg = segs[rid]
        for s, a in zip(seg["sents"], seg["agents"]):
            final_sentences.append(s)
            final_agents.append(a)
    return final_sentences, final_agents, applied


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
    parts.append(original[:4000])
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
    parts.append(original[:4000])
    return "\n".join(parts)


def _part_prompt(agent, region_label: str, region_desc: str, original: str, skeleton: str,
                 untouchable: list, analyses_text: str, requirements: str, char_budget: int = 80) -> str:
    parts = [
        f"你是「{agent.name}」（{agent.title}）。在洗稿流程中，你负责补写【{region_label}】（{region_desc}）。",
        "",
        f"请直接输出你负责的这一部分的文案。全文总字数控制在 550-700 字，你的这一部分严格按节奏把控人分配的 {char_budget} 字写（硬性要求第 6 条：差距不超过 ±10%）。可直接用的成品，不要解释过程。",
        "",
        "【硬性要求】",
        "1. 必须保留全部「不可动句子」（原句照抄，一字不改）；",
        "2. 风格与信息密度贴近原稿，但可以升级表达；",
        "3. 运用你自己的专业手法（钩子/论证/共鸣/核查/节奏等）；",
        "4. 只写你负责的区域，不要越界写其他部分；",
        "5. 必须严格按照【骨架】里对你这部分的规划来写——段功能、篇幅比例、观众心理状态都要符合骨架师的拆解，不要自己另起结构；",
        f"6. 必须严格按节奏把控人分配的 {char_budget} 字写，差距不超过 ±10%（即 {int(char_budget*0.9)}-{int(char_budget*1.1)} 字）；",
        "7. 🚫 严禁在正文里出现任何过程性/分析性/标注性词语——包括但不限于「行动指令」「炸点所在」「爆点」「争议点」「共鸣点」「情绪点」「记忆点」「钩子」「口诀收尾」「身份+反差钩子」「反差钩子」「身份钩子」「金句收束」「字数收束」「锚点」「情绪锚点」「叙事钩子」「价值锚点」「引导钩子」「开场钩子」「结尾钩子」「信息钩子」「全文共鸣」「共情点」「代入感」「身份感」「叙事张力」「节奏铺垫」「情绪势能」「情绪浓度」「反差点」「话术结构」「金句结构」「结构化表达」等。这些是骨架师和分析师用的内部术语，绝对不能出现在给读者看的口播正文里；",
        "8. 只输出可直接朗读发布的口播正文，不要输出「【某某】」类标记、不要解释你怎么写的、不要点评自己。",
    ]
    if requirements:
        parts.append(f"7. 满足用户洗稿要求：{requirements}")
    # 结尾段落：明确禁止强引导互动话术（评论区留言/收藏/点赞/转发求关注等）
    if region_label and "结尾" in region_label:
        parts.append(
            "8. 🚫 严禁任何「强引导互动」话术——不许写「评论区告诉我」「留言区聊聊」「点赞关注」「收藏转发」"
            "「下期想看什么评论」等这类催互动/催关注的句子。结尾要自然收束、留给观众思考空间，而不是喊话让观众去评论。"
        )
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


def _point_prompt(agent, region_label: str, region_desc: str, original: str, skeleton: str,
                  untouchable: list, analyses_text: str, requirements: str,
                  body_texts: dict = None, char_budget: int = 60) -> str:
    """针对「爆点/争议点/共鸣点/情绪点」这类「点」的专家：
    必须从**已经写好的开头/中间/结尾正文里**挑一句，用你的手法把它优化成你负责的这个点。
    只给定位+优化句，不给理由。body_texts 是已写好的三段正文，供本点专家从中选句。"""
    parts = [
        f"你是「{agent.name}」（{agent.title}）。在洗稿流程中，你负责【{region_label}】（{region_desc}）。",
        "",
        "你的职责是：**从下面已写好的【开头/中间/结尾正文】里，挑出一句最合适的话，用你的专业手法把它优化成你负责的这个点。**",
        "你不是单独写一段，也不是在正文末尾追加一句——而是把正文里已有的那一句，改写/强化成你的点。",
        "",
        f"输出约 {char_budget} 字。必须给出你改的是哪一句、以及优化后的句子，格式如下：",
        "",
        "【嵌入位置】在【开头段落 / 中间段落 / 结尾段落】里，你优化的是哪一句（可用「第几句」或直接引用那句原文的片段）",
        "【优化句】你优化后的句子（只写句子本身，不带作者前缀）",
        "",
        "【硬性要求】",
        "1. 必须保留全部「不可动句子」（原句照抄，一字不改），你优化的那句绝不能是不可动句子；",
        "2. 你的点要服务于全篇节奏，落在开头/中间/结尾最合适的位置；",
        "3. 参照【骨架】里「爆款要素位置」的规划，把对应的爆点/争议点/共鸣点/情绪点放到骨架师指定的段落；",
        "4. 只输出【嵌入位置】和【优化句】两个标记，不要解释理由、不要写分析过程；",
        "5. 🚫 【优化句】里严禁出现任何过程性/分析性/标注性词语——如「行动指令」「炸点所在」「爆点」「争议点」「共鸣点」「情绪点」「记忆点」「钩子」「口诀收尾」「身份+反差钩子」「反差钩子」「身份钩子」「金句收束」「字数收束」「锚点」「情绪锚点」「叙事钩子」「价值锚点」「引导钩子」「开场钩子」「结尾钩子」「信息钩子」「全文共鸣」「共情点」「代入感」「身份感」「叙事张力」「节奏铺垫」「情绪势能」「情绪浓度」「反差点」「话术结构」「金句结构」「结构化表达」等。这些是内部术语，不能出现在给读者看的正文里；",
        "6. 优化句必须是可直接朗读的口播正文，不要带「【某某】」标记、不要解释你怎么改的。",
    ]
    if requirements:
        parts.append(f"7. 满足用户洗稿要求：{requirements}")
    parts += [
        "",
        "【原稿】",
        original[:4000],
        "",
        "【骨架】",
        (skeleton or "")[:3000],
        "",
        "【不可动句子（必须原样保留，你绝不能优化它们）】",
        "\n".join(f"- {u['sentence']}" for u in untouchable) or "（无共识不可动句）",
        "",
        "【其他专家的分析参考】",
        (analyses_text or "")[:2500],
    ]
    if body_texts:
        body = "\n\n".join(
            f"【{label}】\n{body_texts.get(rid) or '（未提供）'}"
            for rid, label in (("opening", "开头段落"), ("middle", "中间段落"), ("ending", "结尾段落"))
        )
        parts += ["", "【已写好的正文（请从这里选一句来优化）】", body]
    parts += ["", f"现在输出【{region_label}】的嵌入位置与优化句："]
    return "\n".join(parts)


def _title_prompt(agent, final_text: str, original: str) -> str:
    """给洗稿成品起一个抖音短视频标题。只输出标题本身，不解释。"""
    return (
        f"你是「{agent.name}」（{agent.title}）。请为下面这篇已经写好的口播稿，起一个**抖音短视频标题**。\n\n"
        "要求：\n"
        "1. 有吸引力、有钩子，能让人想点开看，但不要标题党、不要夸大失真；\n"
        "2. 紧扣正文核心，不剧透全部，留点悬念；\n"
        "3. 长度控制在 10-20 字，符合短视频标题习惯；\n"
        "4. 只输出标题本身一行，不要加引号、不要解释。\n\n"
        f"【原稿参考】\n{original[:1000]}\n\n"
        f"【洗稿成品全文】\n{final_text[:1500]}\n\n"
        "标题："
    )


def _pace_prompt(agent, regions, untouchable, original, skeleton_text):
    """整体节奏负责人（当前担任「整体节奏」分区的专家，动态，不写死）只规划开头/中间/结尾三段的篇幅配比。
    只返回分配结果（JSON），不给理由/过程。四个「点」是就地优化三段里的句子，不占独立篇幅。
    总字数 550-700。"""
    # 只让节奏负责人给正文三段分配字数；四个点是「就地优化某句」，无需独立篇幅
    body_regions = [r for r in regions if r["id"] in ("opening", "middle", "ending")]
    region_lines = "\n".join(
        f"- {r['id']}：{r['label']}（{r['desc']}）" for r in body_regions
    )
    return (
        f"你是「{agent.name}」（{agent.title}），负责把控这篇口播全文的整体节奏与篇幅配比。\n\n"
        "你的职责：**根据骨架师拆出来的【骨架】里各段的【篇幅百分比】，为开头/中间/结尾三大块分配具体字数**。\n"
        "  - 骨架里的篇幅百分比是节奏把控人的核心依据（骨架师已经定好开头/中间/结尾各占多少比例）；\n"
        "  - 你的分配要与骨架的篇幅百分比保持一致，差距不要超过 ±10%；\n"
        "  - 例如：骨架说开头占 25%、中间 50%、结尾 25%，全文 600 字 → 你分配 opening=150, middle=300, ending=150。\n\n"
        "注意：你只做字数规划，不写正文；爆点/争议/共鸣/情绪四个点是各点负责人在写好的三句话里就地优化，不占独立篇幅。\n"
        f"硬性要求：三个部分的字数加起来必须在 550-700 字之间，且符合骨架的篇幅百分比。\n\n"
        f"【分区清单（仅正文三块）】\n{region_lines}\n\n"
        f"【不可动句子】\n" + ("\n".join(f"- {u['sentence']}" for u in untouchable) or "（无）") + "\n\n"
        f"【骨架（含各段篇幅百分比）】\n{(skeleton_text or '')[:1200]}\n\n"
        f"【原稿】\n{original[:2000]}\n\n"
        "只输出一个 JSON 对象，格式：{\"opening\": 字数, \"middle\": 字数, \"ending\": 字数}，"
        "键用分区的 id，值用纯数字，三个值加起来在 550-700 之间。不要输出任何其他文字、不要给理由。"
    )


def _parse_pace(reply: str, regions, fallback: int = 80) -> dict:
    """解析整体节奏负责人返回的三段字数分配 JSON；解析失败时回退为平均分配。"""
    text = (reply or "").strip()
    # 提取第一个 {...} 块
    import re as _re
    m = _re.search(r"\{.*\}", text, _re.S)
    budget = {}
    if m:
        try:
            import json as _json
            raw = _json.loads(m.group(0))
            if isinstance(raw, dict):
                for r in regions:
                    if r["id"] in ("opening", "middle", "ending"):
                        v = raw.get(r["id"])
                        if isinstance(v, (int, float)) and v > 0:
                            budget[r["id"]] = int(v)
        except Exception:
            budget = {}
    # 四个点不需要独立篇幅（就地优化），整体节奏也不占篇幅
    if not budget:
        budget = {"opening": 200, "middle": 350, "ending": 100}
    return budget


def _normalize(t: str) -> str:
    """去掉空白/引号/句末标点/行首项目符号，用于不可动句比对 / 全句去重。

    把常见拼装脏数据都归到同一句：
    - 「\"癸水…」=「癸水…」（句首引号）
    - 「…适应力强。」=「…适应力强」（句末标点）
    - 「- 癸水人，我认为是最聪明的」=「癸水人，我认为是最聪明的」（行首项目符号）
    这样不可动句的比对/去重才不会因为前缀、引号、句号差异而漏判、导致重复刷屏。"""
    import re as _re
    s = t or ""
    # 去掉行首项目符号（- / * / • / 编号圆点）与紧随的空格
    s = _re.sub(r"^\s*(?:[-*•·]|\d+[.、])\s*", "", s)
    s = _re.sub(r"[\s\u3000\"'“”‘’「」『』《》]+", "", s)
    # 句末标点也归一到空：X。 / X！ / X… 与 X 视为同句
    s = _re.sub(r"[。！？!?…]+$", "", s)
    return s


_QUOTE_CHARS = "\"'“”‘’「」『』《》"


def _strip_dangling_quotes(s: str) -> str:
    """去掉句子首尾残留的「孤立引号 / 项目符号」字符（整体节奏拼装时的常见脏数据）。

    例如句首多出一个 `"`（如 `"癸水见了丙火…`），或句尾挂着没闭合的引号，
    或行首是 `- ` 项目符号（如 `- 癸水人，我认为是最聪明的`），
    会让我复制的成品出现残缺的 `"…` / `- …` 行。这里只剥掉首尾脏字符，不影响正文。"""
    import re as _re
    s = (s or "").strip()
    if not s:
        return s
    # 去掉行首项目符号（- / * / • / 编号圆点）与紧随的空格
    s = _re.sub(r"^\s*(?:[-*•·]|\d+[.、])\s*", "", s)
    # 句首剥掉一个引号类字符
    if s and s[0] in _QUOTE_CHARS:
        s = s[1:].lstrip()
    # 句尾剥掉一个引号类字符（注意别误删中文句号/感叹号）
    if s and s[-1] in _QUOTE_CHARS and s[-1] not in "。！？!?…":
        s = s[:-1].rstrip()
    return s


def _enforce_untouchable(sentences: list, agents: list, untouchable: list) -> tuple:
    """硬校验 + 全句去重，保证成品干净、不重复刷屏。

    1. **全句去重**：任何归一化后完全相同的句子只保留第一次出现，
       剔除后续重复（成品里同一句反复出现是当前反馈的主要痛点）。
    2. **不可动句必须保留**：不可动句若缺失则原样补回；已出现则不再重复补。
    返回 (sentences, agents)。"""
    if not sentences and not untouchable:
        return sentences, agents
    out = []
    out_agents = []
    seen = set()            # 已出现过的句子（归一化）→ 全句去重
    placed_unt = set()      # 已落地的不可动句（归一化）→ 不可动句去重
    for s, ag in zip(sentences, agents):
        norm = _normalize(s)
        if not norm:
            continue
        if norm in seen:
            continue  # 完全重复句 → 只保留第一次
        # 去残留的「句首引号 / 句尾悬挂引号」（整体节奏拼装时常见：把某句用引号又带了一次）
        cleaned = _strip_dangling_quotes(s)
        cnorm = _normalize(cleaned)
        if cnorm in seen:
            continue  # 去掉引号后仍是重复 → 也剔除
        # 判断该句是否是某个不可动句（归一化完全一致）
        is_unt = False
        for u in untouchable:
            t = u.get("sentence", "")
            if t and _normalize(t) and _normalize(t) == cnorm:
                is_unt = True
                break
        if is_unt and cnorm in placed_unt:
            continue  # 该不可动句已落地一次，剔除重复
        if is_unt:
            placed_unt.add(cnorm)
        out.append(cleaned)
        out_agents.append(ag)
        seen.add(cnorm)
    # 缺失的不可动句补回（原样保留，且不去重冲突）；补回时用清理版，避免带出「- / 引号」脏前缀
    present = set(seen)
    for u in untouchable:
        target = u.get("sentence", "")
        if not target:
            continue
        norm_t = _normalize(target)
        if norm_t and norm_t not in present and norm_t not in placed_unt:
            out.append(_strip_dangling_quotes(target))
            out_agents.append("")
            present.add(norm_t)
    return out, out_agents


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
    # 占住 phase：横幅正确显示「运行中任务 1」，同时阻止 finalize/comment 并发撞车
    try:
        session.try_begin("rewrite")
    except Exception:  # noqa: BLE001
        pass
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
        # 跟踪阶段和心跳：让列表卡片 stage_text 实时跟着详情走
        # （历史 bug：列表一直显示「阿骨正在拆骨架」但详情已经到阶段 3）
        try:
            session.rw["last_event_ts"] = time.time()
            if kw.get("type") == "system" and kw.get("kind") == "phase":
                session.rw["last_phase"] = kw.get("text", "")[:120]
                session.rw["last_phase_at"] = time.time()
            # 把 last_phase/last_event_ts 也落盘一份，避免重启软件后列表丢阶段
            try:
                rewrite_store.update_session(output_dir, rid, lambda s: s.update({
                    "last_event_ts": session.rw.get("last_event_ts"),
                    "last_phase": session.rw.get("last_phase"),
                    "last_phase_at": session.rw.get("last_phase_at"),
                }))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    entry = rewrite_store.get_session(output_dir, rid)
    if entry is None:
        entry = rewrite_store.create_session(output_dir, original, metrics, requirements)

    try:
        from server import build_agents, _build_data_analyst
        from data_insight_store import add_principles, principles_text

        # 参与分析的全员 = 8 位文案专家 + 数据专员
        agents = build_agents(config) or []
        da = _build_data_analyst(config)

        # ---------- 阶段 1：阿骨拆解骨架 ----------
        push(type="system", text="🧬 洗稿流程启动 · 阶段1：阿骨拆解骨架", kind="phase", expert="阿骨")
        push(type="typing", name="阿骨", title="文案骨架派")
        skeleton_agent = next((a for a in agents if a.name == "阿骨"), None)
        skeleton_text = ""
        if skeleton_agent:
            skeleton_text = _raise_if_agent_error(
                skeleton_agent.say([{"role": "user", "content": _skeleton_prompt(original, requirements)}]),
                "阿骨")
        else:
            skeleton_text = "（未找到阿骨，跳过骨架拆解）"
        push(type="message", name="阿骨", title="文案骨架派", text=skeleton_text, kind="skeleton")
        # 存档骨架
        rewrite_store.update_session(output_dir, rid, lambda s: s.update({"skeleton": skeleton_text}))

        # ---------- 阶段 2：全员分析（并行，根治串行太慢） ----------
        push(type="system", text="🔍 阶段2：全员分析原稿（爆点/炸点/争议点/共鸣点/情绪点/不可动句子）", kind="phase")
        # da 为 None（config 缺数据专员配置）时过滤，避免 a.name 抛 AttributeError 整篇失败
        participants = [a for a in agents + [da] if a is not None]
        analyses_results = [None] * len(participants)

        def _analyze_one(idx, agent):
            try:
                extra = "你是数据专员：请额外给出【数据判级】（点赞/评论/转发/收藏 相对强弱，说明这篇稿子强在哪项、弱在哪项）。" if agent.name == "阿数" else ""
                reply = _raise_if_agent_error(
                    agent.say([{"role": "user", "content": _analysis_prompt(original, metrics, requirements, extra)}]),
                    agent.name)
                analyses_results[idx] = {"agent": agent.name, "text": reply}
            except Exception as e:  # noqa: BLE001
                analyses_results[idx] = {"agent": agent.name, "text": f"[{agent.name} 分析失败：{str(e)[:80]}]"}

        # 并发上限 3：既保留并行提速，又避免 8 路同时打第三方中转站触发限流/429。
        _sem = threading.BoundedSemaphore(3)

        def _run(i, agent):
            with _sem:
                _analyze_one(i, agent)

        threads = []
        for i, a in enumerate(participants):
            t = threading.Thread(target=_run, args=(i, a), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        analyses = [r for r in analyses_results if r]
        for r in analyses:
            agent_name = r["agent"]
            agent_title = next((a.title for a in participants if a.name == agent_name), agent_name)
            push(type="message", name=agent_name, title=agent_title, text=r["text"], kind="analysis")
        # 全员分析全部失败 → 明确报错（保留真实原因，别归一成笼统的"模型服务连接失败"）
        ok_analyses = [r for r in analyses if "分析失败" not in (r.get("text") or "")]
        if not ok_analyses:
            raise RuntimeError(
                f"阶段2 全员分析失败：{len(participants)} 位专家都无法连接模型服务"
                f"（{participants[0].name if participants else '专家'}：{(analyses[0].get('text') or '')[:60]}）。"
                "请检查 API 设置（Base URL/Key/模型）与网络，或确认第三方服务配额未用尽。"
            )
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

        # ---------- 阶段 3：分区补写 + 整体节奏拼装 ----------
        push(type="system", text="✍️ 阶段3：专家写稿 + 整体节奏拼装", kind="phase")
        regions = rewrite_store.get_regions(output_dir)
        assignments = rewrite_store.get_assignments(output_dir)
        analyses_text = "\n\n".join(f"### {x['agent']}\n{x['text'][:800]}" for x in analyses)

        # 阶段 2.5：整体节奏负责人（动态取「整体节奏」分区当前负责人，不写死）分配各分区字数
        budget = {}
        pace_owner = ""
        pace_agent = None
        pace_region = next((r for r in regions if r["id"] == "rhythm"), None)
        if pace_region:
            pace_owner = assignments.get("rhythm") or pace_region.get("default") or ""
            pace_agent = next((x for x in agents if x.name == pace_owner), None)
            if pace_agent:
                # 补发一条带 expert 的 phase：前端流水线卡片据此高亮节奏把控人
                push(type="system", text=f"🎚️ 节奏把控 {pace_owner} 开始分配开头/中间/结尾字数", kind="phase", expert=pace_owner)
                push(type="typing", name=pace_agent.name, title=pace_agent.title)
                pace_reply = pace_agent.say([{"role": "user",
                    "content": _pace_prompt(pace_agent, regions, untouchable, original, skeleton_text)}])
                budget = _parse_pace(pace_reply, regions)
        # 兜底：若未拿到分配（无节奏负责人或解析失败），各分区按默认 80 字
        if not budget:
            budget = {r["id"]: 80 for r in regions}

        # 3A+3B：主体三段用 _part_prompt，四个「点」用 _point_prompt（要求明确嵌入定位）
        _POINT_IDS = ("bang", "controversy", "resonance", "emotion")
        raw_parts = {}
        content_regions = [r for r in regions if r["id"] != "rhythm"]
        # ---- 3A：先写正文三段（开头/中间/结尾） ----
        body_ids = ("opening", "middle", "ending")
        for r in content_regions:
            if r["id"] not in body_ids:
                continue
            owner = assignments.get(r["id"], r["default"])
            a = next((x for x in agents if x.name == owner), None)
            if not a:
                continue
            reply = _raise_if_agent_error(a.say([{"role": "user",
                            "content": _part_prompt(a, r["label"], r["desc"], original, skeleton_text,
                                                    untouchable, analyses_text, requirements,
                                                    char_budget=budget.get(r["id"], 80))}]), owner)
            raw_parts[r["id"]] = {"agent": owner, "text": reply, "comments": [],
                                  "sentences": rewrite_store.split_sentences(reply)}
        # ---- 3B：再让四个「点」负责人，对着已写好的三段正文，就地优化其中一句 ----
        body_texts = {rid: raw_parts.get(rid, {}).get("text", "") for rid in body_ids}
        for r in content_regions:
            if r["id"] not in _POINT_IDS:
                continue
            owner = assignments.get(r["id"], r["default"])
            a = next((x for x in agents if x.name == owner), None)
            if not a:
                continue
            reply = _raise_if_agent_error(a.say([{"role": "user",
                            "content": _point_prompt(a, r["label"], r["desc"], original, skeleton_text,
                                                     untouchable, analyses_text, requirements,
                                                     body_texts=body_texts,
                                                     char_budget=budget.get(r["id"], 60))}]), owner)
            raw_parts[r["id"]] = {"agent": owner, "text": reply, "comments": [],
                                  "sentences": rewrite_store.split_sentences(reply)}

        # 3C：程序化「就地优化」——四个点的负责人各自产出【嵌入位置】+【优化句】，
        #     由代码把优化句替换进开头/中间/结尾里对应的那一句，而不是让整体节奏重新拼装整篇。
        #     （原先 _assemble_prompt 让 LLM 二次生成整篇，正是末尾重复/残缺的根源）
        body_parts = {}
        for rid in ("opening", "middle", "ending"):
            rp = raw_parts.get(rid)
            if rp:
                body_parts[rid] = {"sentences": rp.get("sentences") or [], "agent": rp.get("agent") or ""}
        final_sentences = []
        final_agents = []
        final_text = ""
        applied_points = []
        if body_parts:
            point_edits = []
            for pid in _POINT_IDS:
                rp = raw_parts.get(pid)
                if not rp:
                    continue
                parsed = _parse_point(rp.get("text") or "")
                parsed["point_label"] = pid
                point_edits.append(parsed)
            final_sentences, final_agents, applied_points = _apply_point_edits(body_parts, point_edits, untouchable)
            # 硬校验：不可动句子必须原样保留，缺失则补回
            final_sentences, final_agents = _enforce_untouchable(final_sentences, final_agents, untouchable)
            final_text = "\n\n".join(final_sentences)
            if applied_points:
                push(type="system", text="🔧 四个点已就地优化：\n" + "\n".join(f"- {pid}：替换到 {rid} 第 {idx+1} 句" for pid, rid, idx in
                     ((a.split("→")[0], a.split("→")[1].split("[")[0], int(a.split("[")[1].split("]")[0])) for a in applied_points)),
                     kind="phase")

        # 最终成品存为 final 分区；原始分区保留供后续迭代
        parts = dict(raw_parts)
        parts["final"] = {
            "agent": pace_owner if pace_agent else "",
            "text": final_text,
            "sentences": final_sentences,
            "agents": final_agents,
            "comments": [],
        }
        rewrite_store.update_session(output_dir, rid, lambda s: s.update({"parts": parts}))
        # 只把最终成品推送给用户（不展示中间过程）
        if final_text:
            push(type="message", name="成品全文", title="洗稿成品",
                 text=final_text, kind="part", region="final", region_label="成品全文",
                 agents=final_agents, is_assemble=True)

        # 给成品起一个抖音标题（优先整体节奏负责人，兜底阿骨）
        title = ""
        title_agent = pace_agent or next((x for x in agents if x.name == "阿骨"), None)
        if title_agent:
            try:
                push(type="typing", name=title_agent.name, title=title_agent.title)
                title = (_raise_if_agent_error(title_agent.say([{"role": "user",
                          "content": _title_prompt(title_agent, final_text, original)}]), title_agent.name) or "").strip()
                title = title.strip("\"'“”‘’「」『』《》【】")
            except Exception:  # noqa: BLE001
                title = ""
        if title:
            parts["final"]["title"] = title
            rewrite_store.update_session(output_dir, rid, lambda s: s.update({"title": title}))
            push(type="system", text=f"🎬 成品标题：{title}", kind="phase")

        # ---------- 阶段 4：阿审审查（第一遍） ----------
        push(type="system", text="⚖️ 阶段4：阿审原则审查", kind="phase", expert="阿审")
        parts_text = final_text if final_text else _parts_to_text(parts, regions)
        principles = principles_text(output_dir, max_chars=3000)
        push(type="typing", name="阿审", title="原则审查员")
        reviewer = None
        try:
            from server import _build_principle_reviewer
            reviewer = _build_principle_reviewer(config)
        except Exception:  # noqa: BLE001
            pass
        if reviewer:
            try:
                report = _raise_if_agent_error(
                    reviewer.say([{"role": "user", "content": _review_prompt(parts_text, principles, original)}]),
                    "阿审")
            except RuntimeError:
                raise  # 审查失败也中止，不能把错误串当审查结论存盘
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
        # 失败必须落盘 failed 状态 + 失败原因，否则记录永久卡在"进行中"，且用户看不到真实原因
        err_text = _friendly_flow_error(e)
        try:
            rewrite_store.set_session_status(output_dir, rid, "failed")
            rewrite_store.update_session(output_dir, rid, lambda s: s.update({"last_error": err_text}))
        except Exception:  # noqa: BLE001
            pass
        session.rw["status"] = "failed"
        push(type="error", text=f"洗稿流程出错：{err_text}")
    finally:
        session.finished = True
        session.end_phase()
        push(type="done")


def _friendly_flow_error(e: Exception) -> str:
    """把底层异常转成用户能看懂的话，不泄露堆栈。"""
    s = str(e or "")
    if "模型服务" in s or "调用失败" in s or "无法连接" in s:
        return s
    return "模型服务连接失败，请检查网络/API Key 后重试"


def _is_agent_error_reply(text: str) -> bool:
    """Agent.say 失败时返回「[xxx 调用失败：…]」这类错误串而非真实内容。
    洗稿流程必须识别并中止，绝不能把错误文本当成品存盘（历史 bug：假成品）。"""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("[") and ("调用失败" in t or "无法连接" in t or "暂时无法" in t):
        return True
    return False


def _raise_if_agent_error(reply: str, name: str) -> str:
    if _is_agent_error_reply(reply):
        raise RuntimeError(f"[{name}] {reply.strip('[]')}")
    return reply


def _parts_to_text(parts: dict, regions: list) -> str:
    """按区域顺序拼装成品全文（供审查/记录/拼接用）。
    若存在 final（整体节奏拼装的成品），直接返回 final 全文。"""
    if parts and parts.get("final") and parts["final"].get("text"):
        return parts["final"]["text"]
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
        reply = _raise_if_agent_error(agent.say([{"role": "user", "content": _iter_comment_prompt(agent, region["label"], current, comment, (entry or {}).get("original", ""), untouchable)}]), owner)
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
    回写 parts[region_id].sentences 中的对应句，并通过 SSE 推送 sentence_update 事件就地更新。
    若 region_id == 'final'，从 parts.final.agents 找到该句作者，让该作者重写该句。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    from server import build_single_agent
    regions = rewrite_store.get_regions(output_dir)
    entry = rewrite_store.get_session(output_dir, rid)
    parts = (entry or {}).get("parts") or {}
    untouchable = (entry or {}).get("untouchable") or []
    original = (entry or {}).get("original") or ""

    # ---- final 分区：定位该句作者，让对应专家只重写这一句，再更新 final ----
    if region_id == 'final':
        final = parts.get('final') or {}
        agents = final.get('agents') or []
        sents = final.get('sentences') or []
        idx = _index_sentence(sents, sentence)
        if idx < 0:
            push(type="error", text="找不到对应句子")
            return
        owner = agents[idx] or final.get('agent') or ""
        # 若作者为空，回退到整体节奏负责人
        agent = build_single_agent(config, owner) if owner else None
        if not agent:
            push(type="error", text=f"找不到专家 {owner}")
            return
        push(type="system",
             text=f"💬 你评论了成品中的一句话（{owner or '整体节奏'}）：{comment[:100]}{'…' if len(comment)>100 else ''}",
             kind="phase")
        push(type="typing", name=owner or '整体节奏', title=agent.title)
        rewrite_store.set_session_status(output_dir, rid, "iterating")
        try:
            reply = _raise_if_agent_error(agent.say([{"role": "user",
                                "content": _iter_sentence_prompt(agent, "成品", sentence, comment, original, untouchable)}]), owner or '整体节奏')
        except Exception:  # noqa: BLE001
            rewrite_store.set_session_status(output_dir, rid, "review")
            raise
        new_text = (reply or "").strip().strip("\"'“”‘’「」『』")
        if not new_text:
            new_text = sentence
        # 逐句采纳：先不写回 archive，只推送 pending 事件（原句保留）。
        # 用户点「✅ 采用」才真正写回（run_sentence_accept），点「❌ 保留原句」丢弃。
        rewrite_store.set_session_status(output_dir, rid, "review")
        push(type="message", name=owner or '整体节奏', title=agent.title, text=new_text,
             kind="sentence_update", region="final", region_label="成品全文",
             sentence=sentence, is_iteration=True, pending=True)
        return

    region = next((r for r in regions if r["id"] == region_id), None)
    if not region:
        push(type="error", text=f"找不到区域 {region_id}")
        return
    part = parts.get(region_id) or {}
    owner = part.get("agent") or rewrite_store.get_agent_for_region(output_dir, region_id)
    agent = build_single_agent(config, owner)
    if not agent:
        push(type="error", text=f"找不到专家 {owner}")
        return

    push(type="system",
         text=f"💬 你评论了【{region['label']}】的一句话（{owner}）：{comment[:100]}{'…' if len(comment)>100 else ''}",
         kind="phase")
    push(type="typing", name=owner, title=agent.title)
    rewrite_store.set_session_status(output_dir, rid, "iterating")
    try:
        reply = _raise_if_agent_error(agent.say([{"role": "user",
                            "content": _iter_sentence_prompt(agent, region["label"], sentence, comment, original, untouchable)}]), owner)
    except Exception:  # noqa: BLE001
        rewrite_store.set_session_status(output_dir, rid, "review")
        raise
    new_text = (reply or "").strip()
    # 去掉专家可能加的多余引号/标点
    new_text = new_text.strip("\"'“”‘’「」『』")
    if not new_text:
        new_text = sentence
    # 逐句采纳：先不写回 archive，只推送 pending 事件（原句保留），
    # 用户点「✅ 采用」才写回（run_sentence_accept），点「❌ 保留原句」丢弃。
    rewrite_store.set_session_status(output_dir, rid, "review")
    push(type="message", name=owner, title=agent.title, text=new_text, kind="sentence_update",
         region=region_id, region_label=region["label"], sentence=sentence, is_iteration=True, pending=True)


def _index_sentence(sentences, target) -> int:
    """在句子列表中定位 target（容错：先精确、再前12字前缀、再包含）。"""
    if not sentences or not target:
        return -1
    t = str(target)
    tn = _normalize(t)
    for i, s in enumerate(sentences):
        sn = _normalize(s)
        if sn == tn:
            return i
    for i, s in enumerate(sentences):
        sn = _normalize(s)
        if tn and sn[:12] == tn[:12]:
            return i
    for i, s in enumerate(sentences):
        sn = _normalize(s)
        if tn and (tn in sn or sn in tn):
            return i
    return -1


def _emit_resume_history(session, old, final):
    """resume 已有成品的会话时，把 entry 的现有产物只推 RwFullText 可见的事件
    —— 不重跑 AI，也不推聊天区的分析/骨架消息（老板反馈那些阿骨/小黄/金句乱七八糟
    的不该显示）。前端 RwFullText 立即显示成品 → 不再假卡死。"""
    import re as _re
    # 一条 phase 提示：让聊天区显示"已恢复"，不静默
    session.push({
        "type": "system",
        "text": "▶️ 继续上次洗稿 · 成品已恢复（不重跑）",
        "kind": "phase",
    })
    final_sents = []
    final_agents = []
    if isinstance(final, dict):
        final_sents = final.get("sentences") or []
        final_agents = final.get("agents") or []
        if not final_sents and final.get("text"):
            final_sents = [s.strip() for s in _re.split(r"[\n。！？!?]+", final["text"]) if s.strip()]
    for i, sent in enumerate(final_sents):
        if sent is None or sent == "":
            continue
        agent = final_agents[i] if i < len(final_agents) else (final.get("agent") or "")
        session.push({
            "type": "message",
            "name": agent or "整体节奏",
            "title": _agent_title(agent or "整体节奏"),
            "text": str(sent),
            "kind": "sentence_update",
            "region": "final",
            "region_label": "成品全文",
            "sentence": "",
            "is_iteration": False,
            "pending": False,
        })

def run_sentence_accept(session, rid: str, region_id: str, sentence: str, new_text: str, output_dir: str):
    """逐句采纳：用户点「✅ 采用」→ 把原句替换为新句写回 archive，推送 sentence_accepted。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    # 写回：替换单句（复用 update_sentence；final 分区特殊处理）
    if region_id == "final":
        def _fn(data):
            for e in data.get("sessions", []):
                if e.get("id") != rid:
                    continue
                p = e.setdefault("parts", {}).setdefault("final", {})
                ss = list(p.get("sentences") or [])
                idx = _index_sentence(ss, sentence)
                if 0 <= idx < len(ss):
                    ss[idx] = new_text
                p["sentences"] = ss
                p["text"] = "\n\n".join(str(x) for x in ss)
                return
        rewrite_store._update(output_dir, _fn)
    else:
        rewrite_store.update_sentence(output_dir, rid, region_id, sentence, new_text)
    push(type="message", name="采纳", title="逐句采纳", text=new_text,
         kind="sentence_accepted", region=region_id, sentence=sentence)


def run_sentence_reject(session, rid: str, region_id: str, sentence: str, new_text: str, output_dir: str):
    """逐句采纳：用户点「❌ 保留原句」→ 丢弃候选句（archive 未动，无需写回），推送 sentence_rejected。"""
    def push(**kw):
        try:
            session.push(kw)
        except Exception:  # noqa: BLE001
            pass
    push(type="message", name="保留", title="逐句采纳", text=sentence,
         kind="sentence_rejected", region=region_id, sentence=sentence)


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
    push(type="system", text="⏱ 预计 1-3 分钟（阿审审查 + 阿数记录分工）", kind="phase")
    push(type="typing", name="阿审", title="原则审查员")
    reviewer = _build_principle_reviewer(config)
    if reviewer is None:
        report = "（未配置原则审查员，跳过本次审查）"
    else:
        try:
            report = _raise_if_agent_error(
                reviewer.say([{"role": "user", "content": _review_prompt(parts_text, principles, (entry or {}).get("original", ""))}]),
                "阿审")
        except RuntimeError as e:
            report = str(e)
    push(type="message", name="阿审", title="原则审查员", text=report, kind="review", is_final=True)
    rewrite_store.update_session(output_dir, rid, lambda s: s.update({"principle_review": report}))

    push(type="system", text="📋 阿数记录本次分工（约 1-2 分钟）", kind="phase")
    push(type="typing", name="阿数", title="数据专员")
    da = _build_data_analyst(config)
    if da is None:
        record = "（未配置数据专员，分工记录跳过）"
    else:
        try:
            record = _raise_if_agent_error(
                da.say([{"role": "user", "content": _record_prompt(parts, regions)}]), "阿数")
        except RuntimeError as e:
            record = str(e)
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
    if da is None:
        push(type="error", text="未配置数据专员，无法建立评价标准。")
        return
    reply = _raise_if_agent_error(
        da.say([{"role": "user", "content": _evaluate_prompt(sessions, assignments, regions)}]), "阿数")
    parsed = _parse_evaluate_json(reply)
    verdicts = parsed.get("verdicts") or []
    standards = parsed.get("standards") or reply[:800]
    push(type="message", name="阿数", title="数据专员", text=reply, kind="evaluate")
    rewrite_store.set_evaluation(output_dir, standards, verdicts)
    # 统计 replace 数量
    n_replace = sum(1 for v in verdicts if v.get("verdict") == "replace")
    push(type="system", text=f"评价完成：共 {len(verdicts)} 个区域，建议替换 {n_replace} 个负责人。你可以在页面上查看并一键应用替换。", kind="phase")
