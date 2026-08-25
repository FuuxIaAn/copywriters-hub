# -*- coding: utf-8 -*-
"""
数据洞察存储：黑榜 + 原则性建议 + 播放量归因 + 跟踪闭环
====================================================
数据专员（阿数）分析真实口播数据后，把结论落盘到这里：
  - blacklist：句子级黑榜（哪句留存掉最厉害、是不是某专家建议导致的）
  - principles：原则性建议（提炼后，每个专家产出建议前必须过一遍）
  - attributions：播放量归因记录（这条视频为什么高/低，哪个数据导致的）
  - track：跟踪闭环（某类句子按新方法改后，下次数据有没有提升）

存储文件：<data_dir>/output/data_insights.json
原子读写 + 进程内锁，防并发覆盖。
"""
import datetime
import json
import os
import re
import threading

try:
    from _safe_io import atomic_write_json, safe_load_json
except ImportError:
    atomic_write_json = safe_load_json = None

INSIGHT_FILENAME = "data_insights.json"
MAX_BLACKLIST = 100          # 黑榜最多保留条数
MAX_PRINCIPLES = 60          # 原则建议最多保留条数
MAX_ATTRIBUTIONS = 50        # 播放量归因最多保留条数

_LOCK = threading.Lock()


def insight_path(output_dir: str) -> str:
    return os.path.join(output_dir, INSIGHT_FILENAME)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load(output_dir: str) -> dict:
    path = insight_path(output_dir)
    if safe_load_json is not None:
        return safe_load_json(path, {"blacklist": [], "principles": [], "attributions": [], "tracks": [], "updated_at": ""})
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[insight] 读取数据洞察失败，已重置: {e}")
    return {"blacklist": [], "principles": [], "attributions": [], "tracks": [], "updated_at": ""}


def save(output_dir: str, data: dict):
    data["updated_at"] = _now()
    path = insight_path(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    if atomic_write_json is not None:
        if not atomic_write_json(path, data):
            print(f"[insight] 保存数据洞察失败: {path}")
            return ""
        return path
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception as e:  # noqa: BLE001
        print(f"[insight] 保存数据洞察失败: {e}")
        return ""


def update(output_dir: str, fn):
    """原子读-改-写，返回 (修改后的 data, fn 返回值)。"""
    with _LOCK:
        data = load(output_dir)
        ret = fn(data)
        save(output_dir, data)
        return data, ret


# ---------- 黑榜 ----------

def add_blacklist(output_dir: str, items: list):
    """批量写入句子级黑榜。items: [{sentence, agent, reason, rewrite, retention_drop, remedy_text, verdict}]
    verdict: first / pending / effective / ineffective（首次 vs 与历史手段留存对比）"""
    def _fn(data):
        for it in items or []:
            data.setdefault("blacklist", []).append({
                "sentence": (it.get("sentence") or "")[:200],
                "agent": (it.get("agent") or "").strip(),
                "reason": (it.get("reason") or "")[:300],
                "rewrite": (it.get("rewrite") or "")[:300],
                "retention_drop": it.get("retention_drop", ""),
                "remedy_text": (it.get("remedy_text") or "")[:300],
                "history": it.get("history") or [],
                "verdict": it.get("verdict") or "first",
                "date": _now(),
            })
        if len(data["blacklist"]) > MAX_BLACKLIST:
            del data["blacklist"][:-MAX_BLACKLIST]
    update(output_dir, _fn)


def find_focus_sentence(output_dir: str) -> dict | None:
    """找历史黑榜里最新一条，作为复盘开场要讨论的那句。
    复盘开场自动报「本次最低留存句子：XXX（留存率X%）」。"""
    data = load(output_dir)
    items = data.get("blacklist", [])
    if not items:
        return None
    return items[-1]


def update_blacklist_verdict(output_dir: str, sentence: str, retention_after,
                              verdict: str, remedy_text: str = "") -> None:
    """下次复盘对比留存后调用：把上一轮 verdict 写入 history 圆环。"""
    def _fn(data):
        for it in data.get("blacklist", []):
            if it.get("sentence") == sentence:
                history = list(it.get("history") or [])
                history.append({
                    "round": len(history) + 1,
                    "remedy": remedy_text,
                    "retention_after": retention_after,
                    "verdict": verdict,
                    "date": _now(),
                })
                it["history"] = history
                it["verdict"] = verdict
                it["remedy_text"] = remedy_text
                return
        data.setdefault("blacklist", []).append({
            "sentence": sentence[:200],
            "history": [{
                "round": 1, "remedy": remedy_text,
                "retention_after": retention_after, "verdict": verdict,
                "date": _now(),
            }],
            "verdict": verdict,
            "date": _now(),
        })
    update(output_dir, _fn)

def blacklist_text(output_dir: str, max_chars: int = 4000) -> str:
    """黑榜文本（注入专家讨论 / 同类句子记忆用）。"""
    data = load(output_dir)
    items = data.get("blacklist", [])
    if not items:
        return ""
    lines = ["【句子级黑榜 · 以下句子被数据专员判定为留存流失最严重，按流失程度排序】"]
    for i, it in enumerate(items[-20:], 1):
        agent = f"（源自 {it['agent']} 的建议）" if it.get("agent") else ""
        lines.append(f"{i}. 「{it['sentence']}」{agent}\n   流失原因：{it.get('reason','')}")
        if it.get("rewrite"):
            lines.append(f"   上次改写方向：{it.get('rewrite','')}")
    text = "\n".join(lines)
    return text[:max_chars]


# ---------- 原则性建议 ----------

def add_principles(output_dir: str, principles: list, kind: str = "suggest"):
    """写入原则性建议。kind: suggest=建议性原则, forbid=禁止性原则。
    principles: ["原则1", "原则2", ...]"""
    def _fn(data):
        for p in principles or []:
            p = (p or "").strip().rstrip("。；;")
            if not p:
                continue
            p = p + "。"
            # 去重（同类型内）
            existing = data.setdefault("principles", [])
            if any((p[:20] == old["text"][:20] and old.get("kind") == kind) for old in existing):
                continue
            existing.append({"text": p, "kind": kind, "date": _now(),
                             "version": 1, "history": [], "hits": 0})
        if len(data["principles"]) > MAX_PRINCIPLES:
            del data["principles"][:-MAX_PRINCIPLES]
    update(output_dir, _fn)


def _principle_text(p: dict) -> str:
    """单条原则的文本。"""
    return p if isinstance(p, str) else p.get("text", "")


def principles_text(output_dir: str, max_chars: int = 3000, kind: str = None) -> str:
    """原则性建议文本（每个专家产出建议前必须过一遍）。kind 为 None 返回全部，否则只返回该类型。"""
    data = load(output_dir)
    items = data.get("principles", [])
    if kind:
        items = [p for p in items if (p if isinstance(p, str) else p.get("kind")) == kind]
    if not items:
        return ""
    lines = ["【原则性建议 · 数据专员基于真实留存数据提炼，你在给出任何建议前必须逐条过一遍并遵守】"]
    # 禁止性原则优先放前面（踩中必炸，最重要），建议性原则随后；不再只取最后 15 条，改由 max_chars 控制总量
    forbid_items = [p for p in items if (p if isinstance(p, str) else p.get("kind")) == "forbid"]
    suggest_items = [p for p in items if (p if isinstance(p, str) else p.get("kind")) != "forbid"]
    ordered = forbid_items + suggest_items
    for i, p in enumerate(ordered, 1):
        if isinstance(p, str):
            lines.append(f"{i}. {p}")
        else:
            tag = "🚫 禁止" if p.get("kind") == "forbid" else "✅ 建议"
            lines.append(f"{i}. [{tag}] {p.get('text','')}")
    text = "\n".join(lines)
    return text[:max_chars]


def all_principles(output_dir: str) -> list:
    """返回所有原则（结构化，供前端查看）。"""
    data = load(output_dir)
    items = data.get("principles", [])
    out = []
    for p in items:
        if isinstance(p, str):
            out.append({"text": p, "kind": "suggest", "date": "", "version": 1, "history": [], "hits": 0})
        else:
            out.append({
                "text": p.get("text", ""), "kind": p.get("kind", "suggest"),
                "date": p.get("date", ""), "version": p.get("version", 1),
                "history": p.get("history", []), "hits": p.get("hits", 0),
            })
    return out


def delete_principle(output_dir: str, index: int) -> bool:
    """按索引删除一条原则。"""
    def _fn(data):
        items = data.get("principles", [])
        if 0 <= index < len(items):
            items.pop(index)
            return True
        return False
    _, ok = update(output_dir, _fn)
    return ok


def add_principle(output_dir: str, text: str, kind: str = "suggest") -> bool:
    """单条新增一条原则（去重 + 补句号 + 上限裁剪）。返回 True 表示新增成功，False 表示已存在或内容为空。"""
    def _fn(data):
        p = (text or "").strip().rstrip("。；;")
        if not p:
            return False
        p = p + "。"
        existing = data.setdefault("principles", [])
        # 同类型前 20 字去重（与批量 add_principles 一致）
        if any((p[:20] == old["text"][:20] and old.get("kind") == kind) for old in existing):
            return False
        existing.append({"text": p, "kind": kind, "date": _now(),
                         "version": 1, "history": [], "hits": 0})
        if len(data["principles"]) > MAX_PRINCIPLES:
            del data["principles"][:-MAX_PRINCIPLES]
        return True
    _, ok = update(output_dir, _fn)
    return ok


def count_principle_hits(output_dir: str, text: str) -> dict:
    """
    原则命中统计：产出（采纳终稿 / 洗稿成品 / 评分终稿）后调用。
    如果内容包含某原则的关键片段（去标点前 12 字），则该原则 hits + 1。
    返回 {checked: 参与统计的原则数, hit: 命中的原则数}。
    用于原则库「命中率」与无效原则自动淘汰依据。
    """
    body = _norm_text(text)
    if not body:
        return {"checked": 0, "hit": 0}

    def _fn(data):
        items = data.get("principles", [])
        hit = 0
        for p in items:
            key = _norm_text(p.get("text", ""))[:12]
            if key and key in body:
                p["hits"] = p.get("hits", 0) + 1
                hit += 1
        return {"checked": len(items), "hit": hit}
    _, ret = update(output_dir, _fn)
    return ret


def update_principle(output_dir: str, index: int, text: str, kind: str) -> bool:
    """按索引编辑一条原则（更新 text / kind / date，旧版本进 history 供追溯）。"""
    def _fn(data):
        items = data.get("principles", [])
        if not (0 <= index < len(items)):
            return False
        p = (text or "").strip().rstrip("。；;")
        if not p:
            return False
        p = p + "。"
        old = items[index]
        items[index] = {
            "text": p, "kind": kind, "date": _now(),
            "version": old.get("version", 1) + 1,
            "history": list(old.get("history", [])) + [{"text": old["text"], "kind": old.get("kind"), "date": old.get("date")}],
            "hits": old.get("hits", 0),
        }
        return True
    _, ok = update(output_dir, _fn)
    return ok


def _norm_text(t) -> str:
    """去掉标点/空白，用于模糊匹配旧原则文本。"""
    return re.sub(r"[。；;、，,.\s\u3000]+", "", str(t or ""))


def replace_principle(output_dir: str, old_text: str, new_text: str, action: str = "fix"):
    """按文本模糊匹配替换/废除一条原则。返回 'fixed' / 'removed' / 'not_found'。"""
    def _fn(data):
        items = data.get("principles", [])
        old_norm = _norm_text(old_text)
        if not old_norm:
            return "not_found"
        best = None
        for i, p in enumerate(items):
            p_norm = _norm_text(p if isinstance(p, str) else p.get("text", ""))
            if not p_norm:
                continue
            if old_norm in p_norm or p_norm in old_norm:
                best = i
                break
        if best is None:
            return "not_found"
        if action == "remove":
            items.pop(best)
            return "removed"
        kind = items[best].get("kind", "suggest") if isinstance(items[best], dict) else "suggest"
        items[best] = {"text": (new_text or "").strip().rstrip("。；;") + "。", "kind": kind, "date": _now()}
        return "fixed"
    _, ret = update(output_dir, _fn)
    return ret


# ---------- 原则审视 · 待处置清单 ----------

def add_pending_actions(output_dir: str, actions: list):
    """写入原则审视产生的待处置建议。actions: [{action: fix|remove, old_text, new_text}]"""
    def _fn(data):
        for a in actions or []:
            data.setdefault("pending_actions", []).append({
                "action": a.get("action", "fix"),
                "old_text": (a.get("old_text") or "").strip(),
                "new_text": (a.get("new_text") or "").strip(),
                "date": _now(),
            })
        if len(data["pending_actions"]) > 30:
            del data["pending_actions"][:-30]
    update(output_dir, _fn)


def get_pending_actions(output_dir: str) -> list:
    return load(output_dir).get("pending_actions", [])


def remove_pending_action(output_dir: str, index: int) -> bool:
    def _fn(data):
        items = data.get("pending_actions", [])
        if 0 <= index < len(items):
            items.pop(index)
            return True
        return False
    _, ok = update(output_dir, _fn)
    return ok


# ---------- 播放量归因 ----------

def add_attribution(output_dir: str, attribution: dict):
    """写入一条播放量归因。attribution: {video_title, plays, key_metric, analysis}"""
    def _fn(data):
        data.setdefault("attributions", []).append({
            "video_title": (attribution.get("video_title") or "")[:100],
            "plays": attribution.get("plays", ""),
            "key_metric": (attribution.get("key_metric") or "")[:200],
            "analysis": (attribution.get("analysis") or "")[:500],
            "date": _now(),
        })
        if len(data["attributions"]) > MAX_ATTRIBUTIONS:
            del data["attributions"][:-MAX_ATTRIBUTIONS]
    update(output_dir, _fn)
