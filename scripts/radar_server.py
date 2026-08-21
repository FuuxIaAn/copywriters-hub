# -*- coding: utf-8 -*-
"""
选题雷达 · 内置晨报服务端
=============================================
把对标监控已抓取的高赞榜/账号爆款，加上选题撞车检测，再用 LLM 分析成一份
「选题雷达日报」：值得跟的选题 Top、撞车风险、命中爆款基因、口播切入角度。

纯内建：零新爬虫（复用 monitor 已抓数据）、零 UI 结构颠覆（只在 monitor 视图
加一个入口）。完全不碰配音工坊 / 洗稿工坊链路。

数据落盘：OUTPUT_DIR/monitor/radar/radar_YYYYMMDD_HHMM.md
"""
import datetime
import json
import os
import re
import sys
import time

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import monitor.store as mstore  # noqa: E402
import monitor.topics as mtopics  # noqa: E402

RADAR_DIR_REL = os.path.join("output", "monitor", "radar")


# ---------------------------------------------------------------- 工具

def _radar_dir(output_dir: str) -> str:
    d = os.path.join(output_dir, "monitor", "radar")
    os.makedirs(d, exist_ok=True)
    return d


def _load_base_config(base_dir: str) -> dict:
    with open(os.path.join(base_dir, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _collect_report(output_dir: str) -> dict | None:
    """从对标监控最新报告聚合「选题雷达」输入数据。"""
    rep = mstore.load_latest_report(output_dir)
    if not rep:
        return None
    # 高赞榜（近 90 天）
    top = rep.get("top_videos", []) or []
    # 账号爆款（各账号点赞≥1万）
    acc_tops = []
    for b in rep.get("account_top", []) or []:
        for v in b.get("top", []) or []:
            v = dict(v)
            v["author_nickname"] = b.get("nickname", "")
            acc_tops.append(v)
    # 合并去重
    by_id = {}
    for v in list(top) + list(acc_tops):
        vid = str(v.get("aweme_id", ""))
        if not vid:
            continue
        if vid not in by_id or v.get("digg_count", 0) > by_id[vid].get("digg_count", 0):
            by_id[vid] = v
    videos = list(by_id.values())
    # 撞车检测（跨账号同题）
    collisions = mtopics.detect_collisions(videos, threshold=0.35)
    return {
        "fetched_at": rep.get("fetched_at", ""),
        "account_count": rep.get("account_count", 0),
        "top_videos": videos[:15],
        "collisions": collisions[:8],
    }


# ---------------------------------------------------------------- LLM 分析

_SYSTEM = (
    "你是「靓仔文案工作台」内置的选题雷达分析师，服务一位深耕 8 年的玄学命理师"
    "（面向 20-30 岁年轻女性口播短视频）。你的任务：根据对标账号的真实数据，"
    "输出一份可落地的「选题雷达日报」。"
    "必须遵守：①不编造数据，只用给定视频；②给出可直接口播的选题切入角度；"
    "③用爆款四基因（情绪钩子/信息差/身份标签/行动触发）判断；"
    "④撞车选题明确标红提示，避免跟风；⑤玄学垂类合规，不做医疗/绝对化承诺。"
)


def _build_user_prompt(data: dict) -> str:
    lines = [f"抓取时间：{data['fetched_at']}", f"监控账号数：{data['account_count']}"]
    lines.append("\n## 高赞/爆款视频清单")
    for i, v in enumerate(data["top_videos"], 1):
        lines.append(
            f"{i}. [{v.get('digg_count', 0)}赞/{v.get('comment_count', 0)}评] "
            f"{v.get('author_nickname', '')}：{v.get('desc', '')[:60]}"
        )
    lines.append("\n## 撞车选题（多账号同题，谨慎跟风）")
    if data["collisions"]:
        for c in data["collisions"]:
            lines.append(f"- {c.get('topic', '')}（{c.get('count', 0)}个账号：{', '.join(c.get('accounts', [])[:4])}）")
    else:
        lines.append("- 暂无明显的跨账号同题撞车")
    lines.append(
        "\n请输出 Markdown 报告，结构：\n"
        "## 今日值得跟的选题 Top 5\n每条含：选题一句话、命中基因(标注)、"
        "为什么值得做(1句)、口播切入角度(1句，可直接照着录)。\n"
        "## 撞车警示\n列出需谨慎/绕开的撞车选题及理由。\n"
        "## 一句话总结\n"
    )
    return "\n".join(lines)


def _call_llm(base_dir: str, data: dict) -> str:
    try:
        from openai import OpenAI
    except Exception:
        return "⚠ 未安装 openai 依赖，暂无法生成选题分析。可用下方原始清单自行判断。"

    config = _load_base_config(base_dir)
    api = config.get("api") or {}
    base_url = api.get("base_url")
    api_key = api.get("api_key")
    model = api.get("model", "deepseek-chat")
    if not base_url or not api_key:
        return "⚠ 未配置 LLM API（请在设置里填写），暂无法生成选题分析。可用下方原始清单自行判断。"
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=60.0)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _build_user_prompt(data)},
            ],
            temperature=0.4,
            max_tokens=1600,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"⚠ LLM 分析失败：{e}\n\n可先用下方原始数据人工判断。"


# ---------------------------------------------------------------- 对外 API

def generate_radar(base_dir: str, output_dir: str) -> dict:
    """生成选题雷达日报。返回 {ok, path, report, fallback}。"""
    data = _collect_report(output_dir)
    if not data or not data["top_videos"]:
        return {"ok": False, "error": "对标监控暂无数据，请先在对标监控里「立即抓取」或添加账号"}
    md = _call_llm(base_dir, data)
    now = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(_radar_dir(output_dir), f"radar_{now}.md")
    # fallback = LLM 没成功产出结构化报告（无 API / 失败 / 未装依赖）
    fallback = not (md.startswith("## 今日值得跟") or "今日值得跟的选题" in md)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 🎯 选题雷达日报\n\n- 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(md)
        if fallback:
            f.write("\n\n---\n## 原始数据（LLM 未覆盖时的兜底）\n")
            for v in data["top_videos"]:
                f.write(f"- [{v.get('digg_count', 0)}赞] {v.get('desc', '')[:50]}\n")
    return {"ok": True, "path": path, "report": md, "fallback": fallback}


def latest_radar(output_dir: str) -> dict:
    """读取最近一份选题雷达日报。"""
    d = os.path.join(output_dir, "monitor", "radar")
    if not os.path.isdir(d):
        return {"ok": True, "has": False}
    files = sorted(f for f in os.listdir(d) if f.startswith("radar_") and f.endswith(".md"))
    if not files:
        return {"ok": True, "has": False}
    p = os.path.join(d, files[-1])
    with open(p, "r", encoding="utf-8") as f:
        return {"ok": True, "has": True, "path": p, "filename": files[-1], "content": f.read()}
