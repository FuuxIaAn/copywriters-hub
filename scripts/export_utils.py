# -*- coding: utf-8 -*-
"""通用导出工具：作品库 / 洗稿 / 复盘 / 学习档案。"""
import datetime
import json
import os

import works_store
import rewrite_store
import data_insight_store


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def export_works(output_dir: str, fmt: str = "md") -> str:
    """导出全部作品库数据。fmt=json|md。"""
    works = works_store.list_works(output_dir)
    if fmt == "json":
        return json.dumps({"exported_at": _now(), "count": len(works), "works": works}, ensure_ascii=False, indent=2)
    # Markdown
    lines = ["# 作品库导出", "", f"导出时间：{_now()} ｜ 作品数：{len(works)}", ""]
    for i, w in enumerate(works, 1):
        lines.append(f"## {i}. {w.get('title') or '未命名作品'}")
        lines.append(f"- 状态：{w.get('status_label', w.get('status', ''))}")
        lines.append(f"- 创建：{w.get('created_at', '')}")
        lines.append(f"- 更新：{w.get('updated_at', '')}")
        if w.get("note"):
            lines.append(f"- 备注：{w.get('note')}")
        metrics = w.get("metrics") or {}
        if metrics:
            lines.append("- 数据指标：")
            for k, v in metrics.items():
                lines.append(f"  - {k}：{v}")
        lines.append("")
        if w.get("draft"):
            lines.append("### 初稿")
            lines.append(w["draft"])
            lines.append("")
        if w.get("final"):
            lines.append("### 终稿")
            lines.append(w["final"])
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def export_rewrite(output_dir: str, rid: str) -> str:
    """导出单篇洗稿记录为 Markdown。"""
    entry = rewrite_store.get_session(output_dir, rid)
    if not entry:
        raise FileNotFoundError("洗稿记录不存在")
    regions = rewrite_store.get_regions(output_dir)
    parts = entry.get("parts") or {}
    lines = [f"# 洗稿成品导出：{entry.get('title') or rid}", "", f"- 原稿长度：{len(entry.get('original') or '')} 字", f"- 创建时间：{entry.get('created_at', '')}", f"- 状态：{entry.get('status', '')}", ""]
    metrics = entry.get("metrics") or {}
    if metrics:
        lines.append("## 对标四维数据")
        for k, v in metrics.items():
            lines.append(f"- {k}：{v}")
        lines.append("")
    lines.append("## 原稿")
    lines.append(entry.get("original") or "（空）")
    lines.append("")
    lines.append("## 分区成品")
    for r in regions:
        pid = r["id"]
        p = parts.get(pid) or {}
        lines.append(f"### {r['label']}（负责人：{p.get('agent') or '未分配'}）")
        lines.append(p.get("text") or "（未生成）")
        lines.append("")
    review = entry.get("principle_review") or ""
    if review:
        lines.append("## 阿审最终审查")
        lines.append(review)
        lines.append("")
    record = entry.get("owner_record") or ""
    if record:
        lines.append("## 阿数分工记录")
        lines.append(record)
        lines.append("")
    return "\n".join(lines)


def export_insights(output_dir: str) -> str:
    """导出数据复盘（原则、黑榜、归因、跟踪）。"""
    data = data_insight_store.load(output_dir)
    lines = ["# 数据复盘导出", "", f"导出时间：{_now()}", ""]
    principles = data.get("principles") or []
    lines.append(f"## 原则体系（共 {len(principles)} 条）")
    for p in principles:
        kind = p.get("kind", "suggest")
        tag = "🚫 禁止" if kind == "forbid" else "✅ 建议"
        lines.append(f"### {tag} {p.get('text', '')}")
        lines.append(f"- 生效时间：{p.get('date', '')}")
        lines.append(f"- 版本：{p.get('version', 1)}")
        lines.append(f"- 命中次数：{p.get('hits', 0)}")
        lines.append("")
    blacklist = data.get("blacklist") or []
    lines.append(f"## 黑榜句子（共 {len(blacklist)} 条）")
    for b in blacklist:
        lines.append(f"- 句子：{b.get('sentence', '')}")
        lines.append(f"  - 流失幅度：{b.get('drop', '')}")
        lines.append(f"  - 关联作品：{b.get('work_title', '')}")
        lines.append(f"  - 时间：{b.get('date', '')}")
        lines.append("")
    attributions = data.get("attributions") or []
    lines.append(f"## 播放量归因（共 {len(attributions)} 条）")
    for a in attributions:
        lines.append(f"- 作品：{a.get('work_title', '')}")
        lines.append(f"  - 诊断：{a.get('diagnosis', '')}")
        lines.append(f"  - 时间：{a.get('date', '')}")
        lines.append("")
    tracks = data.get("tracks") or []
    lines.append(f"## 跟踪记录（共 {len(tracks)} 条）")
    for t in tracks:
        lines.append(f"- {t}")
    return "\n".join(lines)


def export_lessons(output_dir: str) -> str:
    """导出学习档案（全部专家 lessons）。"""
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge_digests", "lessons")
    files = []
    if os.path.isdir(base):
        files = sorted([f for f in os.listdir(base) if f.endswith(".md")])
    lines = ["# 爆款学习档案导出", "", f"导出时间：{_now()} ｜ 专家数：{len(files)}", ""]
    for fn in files:
        path = os.path.join(base, fn)
        name = fn.replace("_lessons.md", "").replace("agent_", "")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        lines.append(f"## {name}")
        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
