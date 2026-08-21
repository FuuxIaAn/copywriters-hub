# -*- coding: utf-8 -*-
"""自动生成周度数据报告。"""
import datetime

import works_store
import data_insight_store


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        try:
            return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return None


def _week_bounds(dt):
    """返回 dt 所在自然周的周一 00:00 与下周一 00:00。"""
    start = dt - datetime.timedelta(days=dt.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=7)
    return start, end


def generate(output_dir: str, anchor: datetime.datetime = None) -> str:
    """生成本周报告 Markdown。"""
    now = anchor or datetime.datetime.now()
    week_start, week_end = _week_bounds(now)
    ws_label = week_start.strftime("%m.%d")
    we_label = (week_end - datetime.timedelta(days=1)).strftime("%m.%d")

    works = works_store.list_works(output_dir)
    total = len(works)

    def in_week(w):
        d = _parse_dt(w.get("created_at")) or _parse_dt(w.get("updated_at"))
        return d and week_start <= d < week_end

    week_works = [w for w in works if in_week(w)]
    new_count = len(week_works)
    published = sum(1 for w in week_works if w.get("status") == "published")
    archived = sum(1 for w in week_works if w.get("status") == "archived")
    reviewed = sum(1 for w in week_works if w.get("status") == "reviewed")

    # 采纳：本周更新且 status 已采纳相关
    adopted = sum(1 for w in week_works if w.get("status") in ("to_adopt", "published", "reviewed"))

    # 指标聚合
    metric_keys = set()
    for w in works:
        metric_keys.update((w.get("metrics") or {}).keys())
    metric_summary = {}
    for k in metric_keys:
        vals = []
        for w in works:
            v = (w.get("metrics") or {}).get(k)
            try:
                if v != "" and v is not None:
                    vals.append(float(str(v).replace(",", "")))
            except Exception:  # noqa: BLE001
                continue
        if vals:
            metric_summary[k] = {"sum": round(sum(vals), 2), "avg": round(sum(vals) / len(vals), 2), "count": len(vals)}

    # 原则 / 黑榜 / 跟踪
    insights = data_insight_store.load(output_dir)
    principles = insights.get("principles") or []
    blacklist = insights.get("blacklist") or []
    tracks = insights.get("tracks") or []

    lines = [
        f"# 靓仔文案工作台 · 周报（{ws_label} - {we_label}）",
        "",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')} ｜ 数据截止到本周日",
        "",
        "## 本周核心数据",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 作品库总数 | {total} |",
        f"| 本周新增作品 | {new_count} |",
        f"| 本周发布 | {published} |",
        f"| 本周归档 | {archived} |",
        f"| 本周复盘 | {reviewed} |",
        f"| 本周采纳 | {adopted} |",
        "",
    ]

    if metric_summary:
        lines.append("## 效果数据聚合")
        lines.append("")
        lines.append("| 指标 | 累计 | 均值 | 有数据作品数 |")
        lines.append("|------|------|------|-------------|")
        for k, v in metric_summary.items():
            lines.append(f"| {k} | {v['sum']} | {v['avg']} | {v['count']} |")
        lines.append("")

    lines.append("## 本周新增作品")
    lines.append("")
    if week_works:
        for w in week_works:
            lines.append(f"- **{w.get('title') or '未命名'}**（{w.get('status_label', w.get('status', ''))}）")
            if w.get("note"):
                lines.append(f"  - 备注：{w['note']}")
    else:
        lines.append("本周还没有新增作品，去「新建文稿」开启下一轮专家讨论吧。")
    lines.append("")

    lines.append("## 原则库与黑榜")
    lines.append("")
    lines.append(f"- 累计原则：{len(principles)} 条（建议 {sum(1 for p in principles if p.get('kind')!='forbid')} / 禁止 {sum(1 for p in principles if p.get('kind')=='forbid')}）")
    lines.append(f"- 黑榜句子：{len(blacklist)} 条")
    lines.append(f"- 跟踪记录：{len(tracks)} 条")
    lines.append("")

    if blacklist:
        lines.append("### 本周黑榜 TOP3")
        for b in blacklist[:3]:
            lines.append(f"- {b.get('sentence', '')}（流失 {b.get('drop', '')}）")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 数据来自作品库、复盘档案与学习档案。每周一自动生成。")
    return "\n".join(lines)
