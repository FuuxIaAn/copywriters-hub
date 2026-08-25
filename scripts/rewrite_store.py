# -*- coding: utf-8 -*-
"""
洗稿工坊存储模块
=============================================
「洗稿板块」的持久化档案：

- regions：分区定义（开头/中间/结尾/爆点/争议点/共鸣点/情绪点/节奏）
- assignments：当前分区负责人（region_id -> 专家名，可被评价机制替换）
- replacement_log：负责人替换历史
- sessions：每篇洗稿的完整存档（原稿/四维数据/洗稿要求/骨架/分析/分区成品/审查/分工记录/成品数据）
- evaluations：满 3 篇数据后，数据专员建立的评价标准与负责人判断

存储文件：<data_dir>/output/rewrites.json
原子读写 + 进程内锁，防并发覆盖。
"""
import datetime
import json
import os
import re
import threading
import uuid

try:
    from _safe_io import atomic_write_json, safe_load_json
except ImportError:
    atomic_write_json = safe_load_json = None

REWRITES_FILENAME = "rewrites.json"

# 分区定义（初始负责人 = 默认最优匹配，评价机制可替换）
REGIONS = [
    {"id": "opening",     "label": "开头段落", "desc": "3 秒钩子 / 注意入口",                 "default": "小黄"},
    {"id": "middle",      "label": "中间段落", "desc": "信息主体 / 论证支撑",                 "default": "老周"},
    {"id": "ending",      "label": "结尾段落", "desc": "促动收尾 / 行动号召",                 "default": "阿骨"},
    {"id": "bang",        "label": "爆点/炸点", "desc": "高能记忆点 / 反转金句",               "default": "阿爆"},
    {"id": "controversy", "label": "争议点",   "desc": "可讨论的争议设计（合规前提下）",       "default": "阿证"},
    {"id": "resonance",   "label": "共鸣点",   "desc": "让目标人群共情的句子",                 "default": "阿沁"},
    {"id": "emotion",     "label": "情绪点",   "desc": "情绪曲线起伏 / 代入感设计",            "default": "阿导"},
    {"id": "rhythm",      "label": "整体节奏", "desc": "节奏卡点 / 篇幅配比 / 衔接把控",       "default": "爆哥"},
]

_LOCK = threading.Lock()


# ---------- 句子级工具（洗稿成品逐句展示/评论用） ----------

_SENT_SPLIT = re.compile(r"([^。！？!?…\n]+[。！？!?…]?)")

def split_sentences(text: str) -> list:
    """把一段成品文案切成句子列表（保留标点，去掉纯空白段）。
    用于前端逐句标注作者 + 逐句评论。"""
    if not text:
        return []
    out = []
    for raw in _SENT_SPLIT.findall(str(text)):
        s = raw.strip()
        if len(s) >= 2:
            out.append(s)
    return out


def rebuild_text(sentences: list) -> str:
    """把句子列表重新拼回一段文本（句间空一行，保持口播分段可读）。"""
    if not sentences:
        return ""
    return "\n\n".join(str(s).strip() for s in sentences if str(s).strip())


def sentence_index(sentences: list, target: str) -> int:
    """在句子列表中找到与 target 匹配的句子的下标（容错：先精确、再按前 12 字前缀、再按包含）。"""
    if not sentences or not target:
        return -1
    t = re.sub(r"[\s\u3000]+", "", str(target))
    if not t:
        return -1
    norm = [re.sub(r"[\s\u3000]+", "", str(s)) for s in sentences]
    # 1) 精确
    for i, s in enumerate(norm):
        if s == t:
            return i
    # 2) 前 12 字前缀（目标被句子以相同开头覆盖）
    head = t[:12]
    if head:
        for i, s in enumerate(norm):
            if s[:len(head)] == head:
                return i
    # 3) 包含（目标较长时，句子被包含在目标里，或目标被包含在句子里）
    for i, s in enumerate(norm):
        if t in s or s in t:
            return i
    return -1


def rewrites_path(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, REWRITES_FILENAME)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load(output_dir: str) -> dict:
    path = rewrites_path(output_dir)
    if safe_load_json is not None:
        data = safe_load_json(path, None)
        if data is not None:
            return data
    elif os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[rewrite] 读取洗稿档案失败，已重置: {e}")
    data = {
        "version": 1,
        "regions": list(REGIONS),
        "assignments": {r["id"]: r["default"] for r in REGIONS},
        "replacement_log": [],
        "sessions": [],
        "evaluations": {"status": "pending", "standards": "", "verdicts": [], "applied_at": ""},
        "updated_at": _now(),
    }
    _save(output_dir, data)
    return data


def _save(output_dir: str, data: dict):
    data["updated_at"] = _now()
    path = rewrites_path(output_dir)
    if atomic_write_json is not None:
        if not atomic_write_json(path, data):
            print(f"[rewrite] 保存洗稿档案失败: {path}")
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print(f"[rewrite] 保存洗稿档案失败: {e}")


def _update(output_dir: str, fn):
    """原子读-改-写，返回 (data, fn 返回值)。"""
    with _LOCK:
        data = _load(output_dir)
        ret = fn(data)
        _save(output_dir, data)
        return data, ret


# ---------- 分区与负责人 ----------

def get_regions(output_dir: str) -> list:
    return _load(output_dir).get("regions", [])


def get_assignments(output_dir: str) -> dict:
    return _load(output_dir).get("assignments", {})


def get_agent_for_region(output_dir: str, region_id: str) -> str:
    return get_assignments(output_dir).get(region_id, "")


def apply_replacements(output_dir: str, replacements: list, reason: str = ""):
    """应用负责人替换。replacements: [{region, from, to, reason}]"""
    def _fn(data):
        applied = set()
        valid_agents = {m.get("name") for m in data.get("members", []) if isinstance(m, dict)}
        # 配置文件没有 members 时使用工作台内置专家名单，防止任意字符串污染负责人配置。
        if not valid_agents:
            valid_agents = {"阿沁", "老周", "阿爆", "小黄", "爆哥", "阿证", "阿骨", "阿导", "阿记", "阿数", "阿审"}
        for r in replacements or []:
            region = r.get("region", "")
            old = data["assignments"].get(region)
            new = r.get("to") or ""
            if region not in data.get("assignments", {}) or not new or new == old or new not in valid_agents:
                continue
            data["assignments"][region] = new
            data.setdefault("replacement_log", []).append({
                "region": region,
                "region_label": next((x.get("label", "") for x in REGIONS if x["id"] == region), region),
                "from": old or "",
                "to": new,
                "reason": (r.get("reason") or reason or "")[:300],
                "date": _now(),
            })
            applied.add(region)
        data["evaluations"]["applied_at"] = _now()
        # 给已应用的 verdict 打标记，前端据此隐藏「应用」按钮
        for v in data.get("evaluations", {}).get("verdicts", []):
            if v.get("region") in applied:
                v["applied"] = True
    _update(output_dir, _fn)


def replacement_log(output_dir: str) -> list:
    return _load(output_dir).get("replacement_log", [])


# ---------- 洗稿会话存档 ----------

def create_session(output_dir: str, original: str, metrics: dict, requirements: str = "", title: str = "",
                   source_url: str = "", source_platform: str = "", source_video_id: str = "") -> dict:
    """新建一篇洗稿的存档骨架，返回存档条目。source_* 记录素材来源（从监控选材带入），
    供保存为作品时把「打开视频」链接一并带过去。"""
    rid = "rw_" + uuid.uuid4().hex[:10]
    entry = {
        "id": rid,
        "title": (title or "").strip() or (original or "")[:18].replace("\n", " ") or "未命名洗稿",
        "original": original,
        "metrics": metrics or {},
        "requirements": (requirements or "").strip(),
        "source_url": source_url or "",
        "source_platform": source_platform or "",
        "source_video_id": source_video_id or "",
        "status": "running",          # running / review / iterating / done
        "skeleton": None,             # 阿骨拆解的骨架
        "analysis": {},               # region_id -> {agent, text}（全员分析）
        "untouchable": [],            # 共识「不可动句子」
        "parts": {},                  # region_id -> {agent, text, comments:[]}
        "principle_review": "",       # 阿审审查报告
        "owner_record": "",           # 阿数分工记录
        "result_metrics": {},         # 用户回填的成品数据
        "created_at": _now(),
        "updated_at": _now(),
    }
    def _fn(data):
        data.setdefault("sessions", []).append(entry)
    _update(output_dir, _fn)
    return entry


def get_session(output_dir: str, rid: str) -> dict | None:
    for s in _load(output_dir).get("sessions", []):
        if s["id"] == rid:
            return s
    return None


def delete_sessions(output_dir: str, rids: list) -> int:
    """批量删除洗稿记录（含重新洗产生的旧稿）。返回删除数量。"""
    def _fn(data):
        keep = [s for s in data.get("sessions", []) if s.get("id") not in (rids or [])]
        n = len(data.get("sessions", [])) - len(keep)
        data["sessions"] = keep
        return n
    _, n = _update(output_dir, _fn)
    return n


def redo_session(output_dir: str, rid: str, requirements: str = "", title: str = "") -> dict | None:
    """「重新洗」：保留原稿与四维数据，用新要求重开一篇（生成新 rid，旧稿保留可对比）。"""
    old = get_session(output_dir, rid)
    if not old:
        return None
    entry = create_session(
        output_dir,
        old.get("original") or "",
        old.get("metrics") or {},
        requirements or old.get("requirements") or "",
        (title or "").strip() or ("🔄 " + (old.get("title") or "洗稿")),
    )
    entry["redo_of"] = rid
    def _fn(data):
        for s in data.get("sessions", []):
            if s["id"] == entry["id"]:
                s["redo_of"] = rid
    _update(output_dir, _fn)
    return entry


def _stage_text(entry: dict) -> str:
    """根据 entry 实际状态推算「正在做什么」的文案（避免列表只显示干巴巴的"进行中"）。
    阶段依据：
    1. 优先用最近一次 phase 事件文案（last_phase，与详情里的事件流实时一致），
       避免「列表说『阿骨拆骨架』但详情已经到阶段 3」的历史 bug；
    2. 若超过 5 分钟没活动（last_event_ts），标"⚠️ 可能卡住"提示用户重试；
    3. 兜底按 skeleton/analysis/parts 字段推断。"""
    import time as _t
    status = entry.get("status") or "running"
    parts = entry.get("parts") or {}
    if status == "done":
        return "🎉 已完成"
    if status == "failed":
        return "❌ 洗稿失败，可点「🔄 重新洗」重试"
    if status == "iterating":
        return "🔄 按你的评论重写中"
    if status == "review":
        return "✅ 初稿完成，等你逐句点评"
    # running：先看 last_phase（实时）+ stalled 判断
    last_phase = entry.get("last_phase")
    last_ts = entry.get("last_event_ts")
    if last_phase:
        if last_ts and (_t.time() - float(last_ts)) > 900:  # 15 分钟没活动 = 可能卡住（洗稿全流程通常 10-15 分钟，5 分钟太敏感）
            return f"⚠️ 可能卡住：{last_phase}"
        return last_phase
    # 兜底按字段推断（兼容老数据没有 last_phase）
    if not entry.get("skeleton"):
        return "🧬 阿骨正在拆骨架"
    if not entry.get("analysis"):
        return "🔍 全员分析原稿（爆点/不可动句）"
    if not parts:
        return "✍️ 专家分区写稿中"
    if not parts.get("final") or not parts["final"].get("sentences"):
        return "🧩 整体节奏拼装中"
    if not entry.get("principle_review"):
        return "⚖️ 阿审原则审查中"
    return "✍️ 微调中"


def list_sessions(output_dir: str) -> list:
    sess = _load(output_dir).get("sessions", [])
    sess = sorted(sess, key=lambda s: s.get("created_at", ""), reverse=True)
    # 给每条加上 stage_text，供前端列表 badge 显示真实阶段而非干巴巴的状态名
    for s in sess:
        s["stage_text"] = _stage_text(s)
    return sess


def update_session(output_dir: str, rid: str, fn) -> dict | None:
    """针对单篇洗稿做原子修改。fn(entry) 内修改，自动刷新 updated_at。"""
    def _fn(data):
        for s in data.get("sessions", []):
            if s["id"] == rid:
                fn(s)
                s["updated_at"] = _now()
                return s
        return None
    _, entry = _update(output_dir, _fn)
    return entry


def set_session_status(output_dir: str, rid: str, status: str):
    update_session(output_dir, rid, lambda s: s.update({"status": status}))


def with_result_metrics(output_dir: str, rid: str, metrics: dict) -> bool:
    """用户回填成品数据（用于满 3 篇评价）。"""
    return bool(update_session(output_dir, rid, lambda s: s.update({"result_metrics": metrics or {}})))


def update_sentence(output_dir: str, rid: str, region_id: str, old_sentence: str, new_sentence: str,
                    comment: str = None, reply_time: str = None):
    """句级评论重写后，把某分区里的指定一句替换成新句，重组该区 text，并追加一条评论记录。
    comment/reply_time 提供时自动写入 part.comments（一次原子写入，避免并发覆盖）。
    返回更新后的 (region_text, sentences)。"""
    def _fn(data):
        # 遍历 sessions 找到本篇洗稿
        for entry in data.get("sessions", []):
            if entry.get("id") != rid:
                continue
            parts = entry.setdefault("parts", {})
            part = parts.setdefault(region_id, {})
            sentences = part.get("sentences")
            if sentences is None:
                # 旧数据无句子拆分：从 text 现场切分
                sentences = split_sentences(part.get("text", ""))
            idx = sentence_index(sentences, old_sentence)
            if idx < 0:
                # 找不到精确句：追加到末尾（专家重写的兜底）
                idx = len(sentences)
            new_s = (new_sentence or "").strip()
            if not new_s:
                return None
            if idx < len(sentences):
                sentences[idx] = new_s
            else:
                sentences.append(new_s)
            part["sentences"] = sentences
            part["text"] = rebuild_text(sentences)
            if comment is not None:
                comments = list(part.get("comments") or [])
                comments.append({
                    "comment": comment,
                    "reply": new_s,
                    "time": reply_time or _now(),
                    "kind": "sentence",
                })
                part["comments"] = comments
            entry["updated_at"] = _now()
            return (part["text"], list(sentences))
        return None
    _, ret = _update(output_dir, _fn)
    return ret


# ---------- 评价标准（满 3 篇） ----------

def set_evaluation(output_dir: str, standards: str, verdicts: list):
    """保存数据专员建立的评价标准与负责人判断（历史轮次追加到 evaluation_history 供胜率曲线）。"""
    now = _now()
    def _fn(data):
        hist = data.setdefault("evaluation_history", [])
        hist.append({
            "date": now,
            "standards": (standards or "")[:500],
            "verdicts": verdicts or [],
        })
        # 只保留最近 12 轮，避免无限膨胀
        data["evaluation_history"] = hist[-12:]
        data["evaluations"] = {
            "status": "evaluated",
            "standards": standards or "",
            "verdicts": verdicts or [],
            "applied_at": data.get("evaluations", {}).get("applied_at", ""),
        }
    _update(output_dir, _fn)


def get_evaluation(output_dir: str) -> dict:
    return _load(output_dir).get("evaluations", {})


def winrate(output_dir: str) -> dict:
    """
    负责人胜率曲线数据（按区域）。
    - 每个区域从初始负责人出发，结合 replacement_log 重建负责人更替时间线；
    - evaluation_history 每轮 verdict 判定当时负责人的 keep/replace；
    - 累计胜率 = 该负责人被 keep 的次数 / 被评价次数；
    - 输出：{regions: [{id, label, points: [{date, agent, win, total, rate}] }], events: [...]}
    """
    data = _load(output_dir)
    regions = data.get("regions", [])
    assignments = data.get("assignments", {})
    repl_log = data.get("replacement_log", [])
    hist = data.get("evaluation_history", [])
    out_regions = []
    events = []
    for r in regions:
        rid = r["id"]
        label = r["label"]
        # 重建负责人时间线：初始 default 或当前 assignments（若从未替换，以 default 为准）
        timeline = []  # [{date, agent}]
        cur = r.get("default") or assignments.get(rid, "")
        # 按时间顺序应用替换
        repls = sorted([x for x in repl_log if x.get("region") == rid], key=lambda x: x.get("date", ""))
        if not repls:
            timeline = [{"date": "", "agent": cur}]
        else:
            first_date = repls[0]["date"]
            if repls[0].get("from"):
                timeline.append({"date": first_date, "agent": repls[0]["from"]})
            for x in repls:
                timeline.append({"date": x.get("date", ""), "agent": x.get("to", "")})
        # 遍历每轮评价 verdict，归属到当时负责人
        points = []      # 按轮次累计
        cum = {}         # agent -> {keep, total}
        for round_i, ev in enumerate(hist):
            v = next((x for x in ev.get("verdicts", []) if x.get("region") == rid), None)
            if not v:
                continue
            agent = (v.get("agent") or "").strip() or _agent_at(timeline, ev.get("date", ""), cur)
            keep = v.get("verdict") == "keep"
            st = cum.setdefault(agent, {"keep": 0, "total": 0})
            st["total"] += 1
            if keep:
                st["keep"] += 1
            win_total = sum(a["total"] for a in cum.values())
            win_keep = sum(a["keep"] for a in cum.values())
            points.append({
                "date": ev.get("date", ""),
                "agent": agent,
                "win": st["keep"],
                "total": st["total"],
                "rate": round(st["keep"] / st["total"], 2) if st["total"] else 0,
                "overall_rate": round(win_keep / win_total, 2) if win_total else 0,
            })
            events.append({
                "region": rid, "region_label": label, "date": ev.get("date", ""),
                "agent": agent, "verdict": v.get("verdict", ""), "reason": (v.get("reason") or "")[:120],
            })
        out_regions.append({
            "id": rid, "label": label,
            "current": assignments.get(rid) or cur,
            "points": points,
        })
    return {"regions": out_regions, "events": events, "evaluated_count": len(hist)}


def _agent_at(timeline: list, date: str, fallback: str) -> str:
    """找到 date 时刻该区域负责人（timeline 已按时间排序）。"""
    if not date:
        return timeline[-1]["agent"] if timeline else fallback
    cur = fallback
    for t in timeline:
        if t.get("date") and t["date"] <= date:
            cur = t["agent"]
    return cur


def evaluated_sessions(output_dir: str) -> list:
    """返回已有成品数据回填的洗稿（满 3 篇才能建评价标准）。"""
    return [s for s in _load(output_dir).get("sessions", []) if s.get("result_metrics")]
