# -*- coding: utf-8 -*-
"""
角色定义会：让现有专家团讨论一个新角色的职责/任务/边界/输出格式。

用法:
  python scripts/role_design.py --role 文案骨架师
  python scripts/role_design.py --role 文案骨架师 --desc 负责口播文稿的内容骨架搭建（主线/段落编排/节奏分配）

流程:
  Round 1  独立定义:  每位专家从自己专业视角给新角色下定义
  Round 2  互评交锋:  看到其他人定义后回应/质疑/补充
  Round 3  角色卡:    每位专家整合讨论，输出完整的《角色卡》
输出: output/role_design_<ts>.md（可再 render_chat 生成 HTML）
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402  (复用 load_config / build_agents)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ctx_text(context: str = "") -> str:
    return (context or "").strip() or "（无特别说明）"


def _round1_prompt(role: str, desc: str, context: str) -> str:
    return (
        f"创作背景（本账号的核心信息，你的所有建议都必须围绕它，不能跑偏）：\n{_ctx_text(context)}\n\n"
        f"【背景】我们这群口播文案专家准备新增一位「{role}」。"
        + (f"初步设想：{desc}。" if desc else "")
        + "在正式创建他之前，邀请你这位资深专家参与角色定义会，从你的专业视角给出对这个新角色的定义建议。\n"
        "要求：直接、具体、可执行，控制在 400 字以内，分点回答以下 4 个问题：\n"
        "1.【核心职责】你认为他最该承担的 3-5 项核心职责是什么？\n"
        "2.【任务清单】他拿到一篇口播文稿后，应该按什么步骤干活（给 4-6 步流程）？\n"
        "3.【职责边界】他和你（以及和其他专家）的边界在哪？特别是：你负责什么、他负责什么，怎么分工不打架、不重复？\n"
        "4.【输出格式】他的产出应该长什么样（骨架图/清单/标注稿？请描述你理想中的输出模板）。"
    )


def _round2_prompt(role: str, desc: str, context: str, others: list) -> str:
    others_text = "\n\n".join(others)
    return (
        f"创作背景（本账号的核心信息）：\n{_ctx_text(context)}\n\n"
        f"【背景】我们正在定义新角色「{role}」"
        + (f"（初步设想：{desc}）。" if desc else "。")
        + "以下是其他专家对这个新角色的定义建议：\n\n"
        + others_text
        + "\n\n现在进入讨论环节。请针对其他专家的定义明确回应：哪些你认同、哪些你有不同意见、哪里需要补充或修正。"
        "要求：具体、直接、针锋相对，400 字以内。"
    )


def _round3_prompt(role: str, desc: str, context: str, full_log: str) -> str:
    return (
        f"创作背景（本账号的核心信息）：\n{_ctx_text(context)}\n\n"
        f"【背景】我们正在定义新角色「{role}」"
        + (f"（初步设想：{desc}）。" if desc else "。")
        + "以下是刚才角色定义会的完整讨论记录：\n\n"
        + full_log
        + f"\n\n请你基于全部讨论，整合出一份你自己认为最完善的《{role}角色卡》，按以下结构输出（600 字以内）：\n"
        "【角色定位】一句话定位\n"
        "【核心职责】3-5 条\n"
        "【工作流程】4-6 步\n"
        "【职责边界】与各位专家的分工边界\n"
        "【输出模板】描述理想输出格式\n"
        "【考核要点】什么样的产出算好的产出"
    )


def run(role: str, desc: str = "") -> str:
    config = main.load_config()
    agents = main.build_agents(config)
    ctx = (config or {}).get("context") or {}
    context = "\n".join([f"- {v}" for v in ctx.values() if v]).strip()

    print(f"参与角色定义会：{'、'.join(f'{a.name}({a.title})' for a in agents)}")
    print(f"议题：新增「{role}」" + (f"（{desc}）" if desc else ""))

    lines = [f"# 角色定义会：新增「{role}」", ""]
    if desc:
        lines += [f"> 初步设想：{desc}", ""]
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # Round 1
    lines += [f"## Round 1 · 各位专家独立定义「{role}」", ""]
    print("===== Round 1: 独立定义 =====")
    round1 = {}
    for a in agents:
        print(f"  [{a.name}] 定义中 ...")
        r = a.say([{"role": "user", "content": _round1_prompt(role, desc, context)}])
        round1[a.id] = r
        lines += [f"### {a.name}（{a.title}）", r, ""]
        print(f"  [{a.name}] 完成")

    # Round 2
    lines += ["## Round 2 · 互评交锋", ""]
    print("===== Round 2: 互评 =====")
    round2 = {}
    for a in agents:
        others = [round1[o.id] for o in agents if o.id != a.id]
        print(f"  [{a.name}] 回应中 ...")
        r = a.say([{"role": "user", "content": _round2_prompt(role, desc, context, others)}])
        round2[a.id] = r
        lines += [f"### {a.name}（{a.title}）", r, ""]
        print(f"  [{a.name}] 完成")

    # Round 3
    lines += [f"## Round 3 · 各位专家给出《{role}角色卡》", ""]
    print("===== Round 3: 角色卡 =====")
    full_log = "\n\n".join(
        [f"### {a.name}（{a.title}）定义\n{round1[a.id]}\n\n### {a.name}（{a.title}）回应\n{round2[a.id]}"
         for a in agents]
    )
    finals = {}
    for a in agents:
        print(f"  [{a.name}] 撰写角色卡 ...")
        r = a.say([{"role": "user", "content": _round3_prompt(role, desc, context, full_log)}])
        finals[a.id] = r
        lines += [f"### {a.name}（{a.title}）角色卡", r, ""]
        print(f"  [{a.name}] 完成")

    markdown = "\n".join(lines)
    out_dir = os.path.join(BASE_DIR, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"role_design_{ts}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"\n角色定义会完成！记录已保存: {out_path}")

    # 顺便渲染微信群聊 HTML（复用 render_chat，头衔规则不匹配会走兜底，不影响阅读）
    try:
        from render_chat import render_chat_html
        stem = os.path.splitext(os.path.basename(out_path))[0]
        chat_path = render_chat_html(markdown, out_dir, stem)
        print(f"微信群聊界面: {chat_path}")
    except Exception as e:  # noqa: BLE001
        print(f"（HTML 渲染跳过: {e}）")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="角色定义会")
    parser.add_argument("--role", default="文案骨架师", help="新角色名称")
    parser.add_argument("--desc", default="", help="初步设想描述")
    args = parser.parse_args()
    run(args.role, args.desc)
