# -*- coding: utf-8 -*-
"""
爆款文案实战吸收档案管理
========================
用户不定期提供「现成爆款文案」，每位专家对照自己的知识档案，
提炼自身缺失的知识点，**落盘**到 knowledge_digests/lessons/<agent_id>_lessons.md，
下次讨论/评分/学习时自动注入该专家的 system prompt。

核心反幻觉机制（杜绝 AI 幻觉的关键）：
  - 每条知识点必须携带【原文摘录】(quote)，程序用 verify_quote 逐字校验
    摘录是否真的存在于用户提供的原文中：编造或改写的摘录直接丢弃并计数。
  - 宁缺毋滥：单次学习每人最多吸收 4 条，过短的摘录无法验证 → 直接拒绝。
"""
import datetime
import os
import re
import threading

# 模块级锁：「爆款拆解」与「爆款学习」两个后台线程可能并发写同一专家 lessons 文件，
# 读-解析-追加交错会导致条目丢失、编号重复。
_LOCK = threading.Lock()

LESSONS_DIRNAME = "lessons"
MAX_LESSONS = 30          # 每位专家最多保留的吸收条目数（超出丢弃最旧的）
MAX_QUOTE_CHARS = 80      # 摘录最长字符数
MAX_POINT_CHARS = 150     # 知识点/应用方法最长字符数


def lessons_dir(digest_dir: str) -> str:
    d = os.path.join(digest_dir, LESSONS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def lessons_path(digest_dir: str, agent_id: str) -> str:
    return os.path.join(lessons_dir(digest_dir), f"{agent_id}_lessons.md")


def _norm(s: str) -> str:
    """归一化：去掉所有空白（含全角空格）与中英文标点（含中文直角/弯引号），用于证据比对。"""
    return re.sub(r"[\s ，。、！？；：“”‘’「」\"''（）《》…—·,.!?;:'\"()-]+", "", s)


def verify_quote(quote: str, source: str, min_hit_ratio: float = 0.7, win: int = 12) -> bool:
    """证据校验：摘录是否真的出自原文。

    - 归一化后完整包含 → 通过
    - 否则按 12 字符窗口滑动，命中比例 >= 0.7 才通过（容忍细微标点差异）
    - 摘录过短(<8字)或原文过短(<20字) → 拒绝（无法验证，宁缺毋滥）
    """
    q = _norm(quote or "")
    s = _norm(source or "")
    if len(q) < 8 or len(s) < 20:
        return False
    if q in s:
        return True
    hits, total = 0, 0
    i = 0
    while i + win <= len(q):
        total += 1
        if q[i:i + win] in s:
            hits += 1
        i += win
    if i < len(q):
        total += 1
        if q[i:] in s:
            hits += 1
    return total > 0 and hits / total >= min_hit_ratio


def _parse_items(path: str) -> list:
    """解析档案文件，返回条目字典列表（用于编号/裁剪）。"""
    items = []
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    cur = None
    for ln in lines:
        m = re.match(r"^## \[(.*?)\] 吸收 #(\d+)$", ln.strip())
        if m:
            if cur:
                items.append(cur)
            cur = {"date": m.group(1), "no": int(m.group(2)), "lines": [ln]}
        elif cur is not None:
            cur["lines"].append(ln)
    if cur:
        items.append(cur)
    return items


def _head(agent_id: str) -> str:
    return (
        f"# 爆款实战吸收档案 · {agent_id}\n"
        "> 本档案收录该专家从用户提供的【现成爆款文案】中提炼的、自身知识档案缺失的知识点。\n"
        "> 每条都带【原文摘录】证据，且经程序逐字校验（编造/改写的摘录已被丢弃）。\n\n"
    )


def append_lessons(digest_dir: str, agent_id: str, items: list) -> tuple:
    """把一批吸收条目追加写入档案文件；超出 MAX_LESSONS 时丢弃最旧的。
    返回 (文件路径, 实际写入条数)。"""
    path = lessons_path(digest_dir, agent_id)
    if not items:
        return path, 0
    with _LOCK:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_head(agent_id))
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = _parse_items(path)
        start_no = (existing[-1]["no"] + 1) if existing else 1
        with open(path, "a", encoding="utf-8") as f:
            for i, it in enumerate(items):
                f.write(f"\n## [{stamp}] 吸收 #{start_no + i}\n")
                f.write(f"- 📌 原文摘录：「{it['quote'][:MAX_QUOTE_CHARS]}」\n")
                f.write(f"- 🧠 吸收知识点：{it['point'][:MAX_POINT_CHARS]}\n")
                if it.get("apply"):
                    f.write(f"- ✍️ 应用方法：{it['apply'][:MAX_POINT_CHARS]}\n")
        # 裁剪：超过 MAX_LESSONS 丢弃最旧条目
        after = _parse_items(path)
        if len(after) > MAX_LESSONS:
            keep = after[-MAX_LESSONS:]
            with open(path, "w", encoding="utf-8") as f:
                f.write(_head(agent_id))
                for it in keep:
                    f.write("\n".join(it["lines"]) + "\n")
    return path, len(items)


def add_manual_lesson(digest_dir: str, agent_id: str, point: str, apply: str = "", quote: str = "", source: str = "") -> tuple:
    """手动新增一条吸收条目（用户在学习档案页直接录入）。

    - 提供 quote 且提供 source 时走 verify_quote 校验（与自动吸收一致，防幻觉）；
    - 未提供 quote 时作为「经验补充」直接写入（quote 标记为手工补充）。
    返回 (文件路径, 是否写入)。
    """
    point = (point or "").strip()
    if not point:
        return lessons_path(digest_dir, agent_id), False
    quote = (quote or "").strip()
    source = (source or "").strip()
    if quote and source and not verify_quote(quote, source):
        return lessons_path(digest_dir, agent_id), False
    with _LOCK:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        path = lessons_path(digest_dir, agent_id)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(_head(agent_id))
        existing = _parse_items(path)
        start_no = (existing[-1]["no"] + 1) if existing else 1
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] 吸收 #{start_no}（用户手动补充）\n")
            if quote:
                f.write(f"- 📌 原文摘录：「{quote[:MAX_QUOTE_CHARS]}」\n")
            else:
                f.write("- 📌 原文摘录：无（用户手工补充经验）\n")
            f.write(f"- 🧠 吸收知识点：{point[:MAX_POINT_CHARS]}\n")
            if apply:
                f.write(f"- ✍️ 应用方法：{apply[:MAX_POINT_CHARS]}\n")
        after = _parse_items(path)
        if len(after) > MAX_LESSONS:
            keep = after[-MAX_LESSONS:]
            with open(path, "w", encoding="utf-8") as f:
                f.write(_head(agent_id))
                for it in keep:
                    f.write("\n".join(it["lines"]) + "\n")
    return path, True


def lessons_text(digest_dir: str, agent_id: str, max_chars: int = 6000) -> str:
    """读取档案文件文本（注入 system prompt 用）。超长时保留头部注释 + 最近的条目。"""
    path = lessons_path(digest_dir, agent_id)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    if len(text) <= max_chars:
        return text
    idx = text.find("## [")
    if idx <= 0:
        return text[:max_chars]
    head = text[:idx]
    tail = text[idx:]
    blocks = [b for b in re.split(r"(?=^## \[)", tail, flags=re.M) if b.strip()]
    out = head
    for b in reversed(blocks):          # 最近的条目优先保留
        b = b.rstrip("\n")
        if len(out) + len(b) + 1 <= max_chars:
            out = b + "\n" + out
        else:
            break
    return out.strip()
