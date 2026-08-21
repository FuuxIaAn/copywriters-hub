# -*- coding: utf-8 -*-
"""
技能方法论档案加载器
=============================================
把「已安装技能」中可沉淀的方法论（去AI味/爆款标题/合规红线等）提炼成
markdown 文件放在 knowledge/skills_methods/ 下，本模块负责加载合并，
注入到专家群聊 / 洗稿的 prompt 里，让 AI 改写时自动遵守。

配置（config.json）：
  "skill_methods": {
    "enabled": true,
    "max_chars": 2600,        # 合并后的总字符上限（防止挤占知识上下文）
    "files": []               # 空=加载目录下所有 .md；可指定子集文件名
  }
"""
import os
import re

_METHODS_DIR_REL = os.path.join("knowledge", "skills_methods")


def _methods_dir(base_dir: str) -> str:
    return os.path.join(base_dir, _METHODS_DIR_REL)


def load_methods_text(base_dir: str, config: dict | None = None) -> str:
    """加载并合并技能方法论文档，返回单个文本块；未启用或目录为空返回空串。"""
    cfg = (config or {}).get("skill_methods") or {}
    if not cfg.get("enabled", True):
        return ""
    max_chars = int(cfg.get("max_chars", 2600) or 2600)
    mdir = _methods_dir(base_dir)
    if not os.path.isdir(mdir):
        return ""

    only = cfg.get("files") or []
    names = []
    if only:
        for fn in only:
            fp = os.path.join(mdir, fn)
            if os.path.isfile(fp):
                names.append(fp)
    else:
        for fn in sorted(os.listdir(mdir)):
            if fn.endswith(".md"):
                names.append(os.path.join(mdir, fn))

    blocks = []
    total = 0
    for fp in names:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            continue
        if not text:
            continue
        title = os.path.splitext(os.path.basename(fp))[0]
        block = f"### {title}\n{text}".strip()
        if total + len(block) > max_chars:
            # 超过上限则截断保留
            remain = max_chars - total
            if remain > 120:
                blocks.append(block[:remain] + "\n…")
            break
        blocks.append(block)
        total += len(block)

    if not blocks:
        return ""
    return "\n\n".join(blocks)
