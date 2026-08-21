# -*- coding: utf-8 -*-
"""
知识内化模块（Knowledge Distillation）
=====================================
核心思想：专家不是「每次讨论临时翻原始知识库」，而是先做一次性的「深度研读」，
把原始知识库（txt/md/pdf/docx/图片OCR）消化提炼成一份结构化的《个人知识档案》，
之后每次讨论都直接使用这份内化后的档案。

流程（对每个专家）：
  1. 读取原始知识库全文（分块，不做长度截断）
  2. 每一块交给 DeepSeek 深度研读 -> 生成该块的浓缩笔记（吸收）
  3. 所有块的笔记合并，再交给 DeepSeek 做一次整合提炼 -> 《知识档案》（沉淀）
  4. 保存到 knowledge_digests/<agent_id>.md

用法:
  python knowledge_distill.py            # 蒸馏 config.json 里全部专家
  python knowledge_distill.py --agent agent_emotional   # 只蒸馏指定专家
  python knowledge_distill.py --force    # 强制重新蒸馏（覆盖已有档案）
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openai import OpenAI  # noqa: E402
from knowledge_loader import load_knowledge_dir  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STUDY_PROMPT = """你正在执行一项「深度研读」任务：把一段你专属知识库的原始资料，消化吸收成结构化笔记。
这段资料是{name}（{title}）的知识库内容，未来你会依靠这些笔记形成自己的专业能力。

请对以下原始资料做深度研读，提炼出结构化笔记，注意：
1. 抓取【核心理念】——这套内容反复强调的核心信条、底层逻辑
2. 抓取【方法论框架】——步骤化、可执行的方法（保留原文中的公式名、步骤数、关键数字）
3. 抓取【可复用素材】——具体的话术模板、标题/开头公式、金句、案例要点（尽量保留原文原句，这是你最宝贵的弹药库）
4. 抓取【禁忌/反例】——内容中明确警告不要做的事
5. 输出为 Markdown，用小标题分节，条目化，信息密度要高，不要客套话，不要复述原文段落，只留「将来能直接用」的干货

【原始资料开始】
{chunk}
【原始资料结束】

请输出你的研读笔记："""

SYNTHESIS_PROMPT = """你是一个知识整理专家。下面是你对同一份知识库分块研读后得到的所有笔记。
请把这些零散笔记**整合提炼成一份完整的《个人知识档案》**，这份档案将成为{name}（{title}）长期使用的「内化知识库」。

要求：
1. 结构清晰，使用以下固定骨架（可补充小节）：
   # {name}（{title}）· 个人知识档案
   ## 一、核心理念（3-5条信条，用一句话表达）
   ## 二、方法论框架（编号列出，步骤化、可执行，保留关键数字/公式名）
   ## 三、话术与模板弹药库（最重要的部分：保留可套用的原文原句、标题公式、开头钩子、金句、案例要点）
   ## 四、禁忌与反例（明确警告不能做的事）
   ## 五、实战应用指引（拿到一篇口播稿后，我应该依次用哪些知识点去分析/改写）
2. 跨块去重：不同笔记中重复的内容合并为一条
3. 语言风格：像一个资深文案专家在给自己写「武功秘籍」，直接、务实、可操作
4. 总长度控制在 2500-4500 字，信息密度优先，宁可保留具体句子也不要抽象概括

【所有分块笔记开始】
{notes}
【所有分块笔记结束】

请输出整合后的《个人知识档案》："""


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(raw: str) -> str:
    return raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw)


def chunk_text(text: str, size: int) -> list:
    """按字符数切块，尽量在段落边界切断。"""
    chunks, start = [], 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # 向后找最近的换行，避免把一句话劈开
            nl = text.find("\n", end)
            if 0 < nl - end < size // 2:
                end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def call_llm(client, model, prompt: str, temperature: float = 0.4) -> str:
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


def distill_agent(client, model, temperature, acfg: dict, cfg: dict, force: bool) -> bool:
    name, title = acfg["name"], acfg["title"]
    raw_path = acfg.get("knowledge_path") or ""
    if not raw_path:
        print(f"  ✗ {name} 未配置 knowledge_path，跳过")
        return False
    kb_path = resolve_path(raw_path)
    digest_rel = acfg.get("digest_path") or f"knowledge_digests/{acfg['id']}.md"
    digest_path = os.path.join(BASE_DIR, digest_rel)

    if os.path.exists(digest_path) and not force:
        print(f"  ○ {name}（{title}）：知识档案已存在（{digest_rel}），跳过（--force 可重跑）")
        return True

    # 1. 读取原始知识库（超长库按蒸馏输入上限截断，保证研读质量与耗时可控）
    distill_max = cfg["distill"].get("max_input_chars", 60000)
    print(f"  ▶ {name}（{title}）：读取原始知识库 {kb_path} ...")
    raw = load_knowledge_dir(kb_path, max_chars=distill_max)
    if not raw.strip():
        print(f"  ✗ {name}：知识库为空，跳过")
        return False
    truncated = len(raw) >= distill_max
    print(f"    原始知识库 {len(raw)} 字符{'（超出研读上限，已取前 %d 字符）' % distill_max if truncated else ''}")

    # 2. 分块深度研读
    chunk_size = cfg["distill"].get("chunk_size", 10000)
    chunks = chunk_text(raw, chunk_size)
    notes = []
    for i, chunk in enumerate(chunks, 1):
        print(f"    [{i}/{len(chunks)}] 深度研读中 ...", end="", flush=True)
        prompt = STUDY_PROMPT.format(name=name, title=title, chunk=chunk)
        note = call_llm(client, model, prompt, temperature)
        notes.append(note)
        print(f" 完成（{len(note)} 字）")
        time.sleep(0.5)

    # 3. 整合提炼
    print("    ▸ 整合提炼最终知识档案 ...", end="", flush=True)
    combined = "\n\n---分块分隔---\n\n".join(notes)
    final = call_llm(client, model, SYNTHESIS_PROMPT.format(name=name, title=title, notes=combined), temperature)

    # 4. 保存
    os.makedirs(os.path.dirname(digest_path), exist_ok=True)
    header = (
        f"> 本档案由「知识内化」自动生成：基于 {kb_path} 深度研读沉淀。\n"
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M')} · 源材料 {len(raw)} 字符 · {len(chunks)} 个研读块\n\n"
    )
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(header + final)
    print(f" 完成（{len(final)} 字）")
    print(f"  ✓ 知识档案已保存: {digest_rel}")
    return True


def main():
    parser = argparse.ArgumentParser(description="知识内化：深度研读知识库，生成各专家个人知识档案")
    parser.add_argument("--agent", default=None, help="只蒸馏指定 agent id，默认全部")
    parser.add_argument("--force", action="store_true", help="强制重新蒸馏（覆盖已有档案）")
    args = parser.parse_args()

    config = load_config()
    api = config["api"]
    client = OpenAI(base_url=api["base_url"], api_key=api["api_key"])
    agents = config["agents"]

    targets = [a for a in agents if not args.agent or a["id"] == args.agent]
    print(f"开始知识内化，共 {len(targets)} 个专家：{'、'.join(a['name'] for a in targets)}\n")

    ok = 0
    for acfg in targets:
        if distill_agent(client, api["model"], api["temperature"], acfg, config, args.force):
            ok += 1
        print()

    print(f"完成：{ok}/{len(targets)} 个专家的知识档案已就绪")


if __name__ == "__main__":
    main()
