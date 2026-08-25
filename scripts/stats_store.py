# -*- coding: utf-8 -*-
"""
统计与反馈档案管理
====================
负责 output/stats.json 的读写与计算：

  - 每位专家的采纳评估历史（conclusion: 有效/部分有效/无效）
  - 正确率计算与排名（有效=1.0，部分有效=0.5，无效=0.0）
  - 每位专家的正/负反馈档案（由复盘时提炼，下次讨论注入 system prompt）
  - 终稿评分记录 + 评分准确性（对比阿记判定的实际分，偏差越小越准）
"""
import datetime
import json
import os
import threading

try:
    from _safe_io import atomic_write_json, safe_load_json
except ImportError:
    atomic_write_json = safe_load_json = None

STATS_FILENAME = "stats.json"
VERDICT_WEIGHT = {"有效": 1.0, "部分有效": 0.5, "无效": 0.0}
MAX_FEEDBACK_PER_EXPERT = 12      # 每位专家最多保留的正/负反馈条数
MAX_FEEDBACK_CHARS = 60           # 单条反馈在注入 prompt 时最多展示的字符数

# 进程内读写锁：评分/复盘/学习三个后台线程可能并发改 stats.json，
# 没有锁的话「读-改-写」会互相覆盖丢更新。
_LOCK = threading.Lock()


def stats_path(output_dir: str) -> str:
    return os.path.join(output_dir, STATS_FILENAME)


def load_stats(output_dir: str) -> dict:
    path = stats_path(output_dir)
    if safe_load_json is not None:
        return safe_load_json(path, {"experts": {}, "scores": [], "score_accuracy": {}, "updated_at": ""})
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[stats] 读取统计失败，已重置: {e}")
    return {"experts": {}, "scores": [], "score_accuracy": {}, "updated_at": ""}


def save_stats(output_dir: str, stats: dict):
    stats["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = stats_path(output_dir)
    if atomic_write_json is not None:
        if not atomic_write_json(path, stats):
            print(f"[stats] 保存统计失败: {path}")
            return ""
        return path
    try:
        # 先写临时文件再原子替换：进程崩溃/断电不会留下写一半的损坏文件
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception as e:  # noqa: BLE001
        print(f"[stats] 保存统计失败: {e}")
        return ""


def update_stats(output_dir: str, fn):
    """原子「读-改-写」stats.json：fn(stats) 内完成修改，锁内串行化并落盘。
    返回 (修改后的 stats, fn 的返回值)。"""
    with _LOCK:
        stats = load_stats(output_dir)
        ret = fn(stats)
        save_stats(output_dir, stats)
        return stats, ret


def _expert_entry(stats: dict, name: str) -> dict:
    entry = stats["experts"].setdefault(name, {
        "suggestions": [], "positive_feedback": [], "negative_feedback": [],
    })
    entry.setdefault("suggestions", [])
    entry.setdefault("positive_feedback", [])
    entry.setdefault("negative_feedback", [])
    return entry


def _add_feedback(entry: dict, key: str, text: str):
    if not text or not text.strip():
        return
    text = text.strip().rstrip("。；;")
    if not text:
        return
    items = entry[key]
    # 去重（含子串近似去重）
    for old in items:
        if old == text or old[:20] == text[:20]:
            return
    items.append(text + "。")
    # 控制数量
    if len(items) > MAX_FEEDBACK_PER_EXPERT:
        del items[: len(items) - MAX_FEEDBACK_PER_EXPERT]


def apply_verdicts(stats: dict, verdicts: list, session_ts: str = "") -> dict:
    """把一次复盘的结构化评估结果写入统计，返回汇总信息。

    verdict: {"name","round","snippet","conclusion","reason","next",
              "feedback_positive","feedback_negative"}
    返回 {"per_expert": {name: {"evaluated","effective","correct_rate"}},
          "ranked": [(name, rate, evaluated), ...]}
    """
    per_expert = {}
    for v in verdicts or []:
        name = (v.get("name") or "").strip()
        conclusion = (v.get("conclusion") or "").strip()
        if not name or conclusion not in VERDICT_WEIGHT:
            continue
        entry = _expert_entry(stats, name)
        entry["suggestions"].append({
            "session_ts": session_ts,
            "round": v.get("round", ""),
            "snippet": (v.get("snippet") or "")[:120],
            "conclusion": conclusion,
            "reason": (v.get("reason") or "")[:300],
            "next": v.get("next", ""),
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 提炼正/负反馈（去重后入库）
        _add_feedback(entry, "negative_feedback", v.get("feedback_negative"))
        _add_feedback(entry, "positive_feedback", v.get("feedback_positive"))

    # 汇总每个专家正确率
    ranked = []
    for name, entry in stats["experts"].items():
        evals = entry["suggestions"]
        if not evals:
            continue
        score = sum(VERDICT_WEIGHT.get(s["conclusion"], 0.0) for s in evals)
        rate = score / len(evals)
        per_expert[name] = {
            "evaluated": len(evals),
            "effective": round(score, 2),
            "correct_rate": round(rate * 100, 1),
        }
        ranked.append((name, rate, len(evals)))
    ranked.sort(key=lambda x: (-x[1], -x[2]))
    return {"per_expert": per_expert, "ranked": ranked}


def feedback_archive_text(stats: dict, name: str) -> str:
    """生成该专家要注入 system prompt 的「历史反馈档案」文本。"""
    entry = stats["experts"].get(name)
    if not entry:
        return ""
    neg = entry.get("negative_feedback", [])
    pos = entry.get("positive_feedback", [])
    if not neg and not pos:
        return ""
    lines = []
    if neg:
        lines.append("【负面清单 · 你的这些改动已被用户的实际数据验证为无效，今后严禁再犯】：")
        for i, t in enumerate(neg, 1):
            t = t if len(t) <= MAX_FEEDBACK_CHARS else t[:MAX_FEEDBACK_CHARS] + "…"
            lines.append(f"{i}. 禁止：{t}")
    if pos:
        lines.append("【正面清单 · 你的这些改动已被验证有效，继续保持/发扬】：")
        for i, t in enumerate(pos, 1):
            t = t if len(t) <= MAX_FEEDBACK_CHARS else t[:MAX_FEEDBACK_CHARS] + "…"
            lines.append(f"{i}. 保持：{t}")
    return "\n".join(lines)


def rank_text(stats: dict) -> str:
    """按历史正确率排名的展示文本（正确率从高到低）。"""
    rows = []
    for name, entry in stats["experts"].items():
        evals = entry.get("suggestions", [])
        if not evals:
            continue
        score = sum(VERDICT_WEIGHT.get(s.get("conclusion", ""), 0.0) for s in evals)
        rate = score / len(evals) * 100
        rows.append((name, rate, len(evals), score))
    if not rows:
        return "（暂无历史评估数据）"
    rows.sort(key=lambda x: -x[1])
    lines = []
    for i, (name, rate, n, score) in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {name}：正确率 **{rate:.1f}%**（被评估 {n} 条，得分 {score:.1f}/{n}）")
    return "\n".join(lines)


def add_score_record(stats: dict, session_ts: str, script: str, scores: list):
    """记录一次终稿评分（scores: [{name, score, reason}]）。"""
    stats.setdefault("scores", [])
    stats["scores"].append({
        "session_ts": session_ts,
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": (script or "")[:200],
        "scores": scores,
    })
    # 控制只保留最近 50 次
    if len(stats["scores"]) > 50:
        del stats["scores"][:-50]


def update_score_accuracy(stats: dict, actual_score: float, session_ts: str = ""):
    """复盘后：根据阿记判定的实际分，更新每位专家的评分偏差（越小越准）。

    按 session_ts 匹配评分记录——复盘数据对应的应该是「同一会话里打的那次分」，
    而不是全局最后一条（否则在 A 会话评分、在 B 会话复盘会错配）。
    """
    if actual_score is None:
        return
    scores = stats.get("scores") or []
    if not scores:
        return
    if session_ts:
        matches = [s for s in scores if s.get("session_ts") == session_ts]
        last = matches[-1] if matches else None
    else:
        last = scores[-1]
    if last is None:
        return
    acc = stats.setdefault("score_accuracy", {})
    for s in last.get("scores", []):
        name = s.get("name", "")
        score = s.get("score")
        if not name or score is None:
            continue
        dev = abs(float(score) - float(actual_score))
        item = acc.setdefault(name, {"times": 0, "total_dev": 0.0})
        item["times"] += 1
        item["total_dev"] = round(item["total_dev"] + dev, 2)
        item["avg_dev"] = round(item["total_dev"] / item["times"], 2)


def score_accuracy_text(stats: dict) -> str:
    """评分准确性排名文本（平均偏差从小到大）。"""
    acc = stats.get("score_accuracy", {})
    rows = [(name, item["avg_dev"], item["times"])
            for name, item in acc.items() if item.get("times")]
    if not rows:
        return "（暂无评分准确性数据，发一次「评分：终稿」后复盘即可生成）"
    rows.sort(key=lambda x: x[1])
    lines = ["评分准确性（平均偏差越小越准）："]
    for i, (name, dev, times) in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {name}：平均偏差 **{dev:.1f} 分**（参与 {times} 次）")
    return "\n".join(lines)
