# -*- coding: utf-8 -*-
"""开发/打包前自检：不调用外部 API，也不会输出任何密钥。"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_MODULES = {
    "flask": "flask",
    "openai": "openai",
    "requests": "requests",
    "httpx": "httpx",
    "webview": "pywebview",
    "gradio_client": "gradio_client",
    "openpyxl": "openpyxl",
}


def main() -> int:
    print(f"项目目录: {ROOT}")
    print(f"Python: {sys.executable} ({sys.version.split()[0]})")
    missing = [package for module, package in REQUIRED_MODULES.items()
               if importlib.util.find_spec(module) is None]
    if missing:
        print("缺少依赖: " + ", ".join(missing))
        print("修复命令: python -m pip install -r requirements.txt")
    else:
        print("依赖检查: 通过")

    config_path = ROOT / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        bundled_key = (config.get("api") or {}).get("api_key") or ""
        print("发布配置密钥: " + ("⚠️ 非空，打包前必须清空" if bundled_key else "通过（未内置）"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"配置检查失败: {exc}")
        return 1

    print("ffmpeg: " + (shutil.which("ffmpeg") or "未找到（视频/音频处理功能会受限）"))
    print("数据目录: " + (os.environ.get("WB_DATA_DIR") or "%LOCALAPPDATA%\\靓仔文案工作台"))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
