# -*- coding: utf-8 -*-
"""
入口: 口播文稿专家群聊

用法:
  python main.py "口播文稿内容"
  python main.py --file 文稿.txt
  python main.py            # 交互模式, 粘贴文稿后 Ctrl+D(Ctrl+Z) 结束输入
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openai import OpenAI  # noqa: E402
from knowledge_loader import load_knowledge_dir  # noqa: E402
from agents import Agent  # noqa: E402
from discussion import run_discussion, save_output  # noqa: E402
from render_chat import render_chat_html  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config() -> dict:
    cfg_path = os.path.join(BASE_DIR, "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _member_model(api: dict, member_cfg: dict) -> str:
    return (member_cfg or {}).get("model") or api.get("model") or "deepseek-chat"


def _make_client(provider_cfg: dict) -> "OpenAI":
    return OpenAI(
        base_url=provider_cfg["base_url"],
        api_key=provider_cfg["api_key"],
        timeout=180.0,
    )


def _resolve_member_provider(config: dict, model: str) -> dict:
    if model.startswith("deepseek-"):
        ds = config.get("deepseek") or {}
        if ds.get("base_url") and ds.get("api_key"):
            return ds
    return config["api"]


def build_agents(config: dict) -> list:
    api = config["api"]
    max_chars = config["discussion"].get("max_context_chars", 20000)
    agents = []
    for acfg in config["agents"]:
        digest_rel = acfg.get("digest_path") or ""
        digest_path = os.path.join(BASE_DIR, digest_rel) if digest_rel else ""
        if digest_path and os.path.exists(digest_path):
            with open(digest_path, "r", encoding="utf-8") as f:
                knowledge = f.read()
            knowledge_source = "深度研读内化的个人知识档案"
        else:
            raw_path = acfg.get("knowledge_path") or ""
            kb_path = raw_path if os.path.isabs(raw_path) else os.path.join(BASE_DIR, raw_path)
            knowledge = load_knowledge_dir(kb_path, max_chars)
            knowledge_source = "原始知识库（尚未做知识内化，建议先运行 knowledge_distill.py）"
        model = _member_model(api, acfg)
        client = _make_client(_resolve_member_provider(config, model))
        agents.append(
            Agent(
                cfg=acfg,
                knowledge=knowledge,
                client=client,
                model=model,
                temperature=api["temperature"],
                knowledge_source=knowledge_source,
            )
        )
    return agents


def read_script(args) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read().strip()
    if args.script:
        return args.script.strip()
    # 交互模式
    print("请粘贴口播文稿，粘贴完毕后按 Ctrl+D（Windows: Ctrl+Z 回车）结束：")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="口播文稿专家群聊系统")
    parser.add_argument("script", nargs="?", default=None, help="直接传入口播文稿内容")
    parser.add_argument("--file", default=None, help="从文件读取口播文稿")
    args = parser.parse_args()

    script = read_script(args)
    if not script:
        print("错误：没有输入文稿内容。")
        parser.print_help()
        sys.exit(1)

    config = load_config()
    agents = build_agents(config)

    print(f"\n参与讨论：{'、'.join(f'{a.name}({a.title})' for a in agents)}")
    for a in agents:
        kb_info = "已加载知识库" if a.knowledge else "（暂无知识库，仅凭专业人设）"
        print(f"  - {a.name}（{a.title}）: {kb_info} · {a.knowledge_source}")

    markdown = run_discussion(script, agents, config)
    out_path = save_output(markdown, os.path.join(BASE_DIR, "output"))
    print(f"\n讨论完成！记录已保存到: {out_path}")
    stem = os.path.splitext(os.path.basename(out_path))[0]
    try:
        chat_path = render_chat_html(markdown, os.path.join(BASE_DIR, "output"), stem)
        print(f"微信群聊界面已生成: {chat_path}")
        print(f"用浏览器打开即可看到 {len(agents)} 位专家实时聊天的效果")
    except Exception as e:  # noqa: BLE001
        print(f"（微信群聊界面生成失败，已跳过: {e}）")


if __name__ == "__main__":
    main()
