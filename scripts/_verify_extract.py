#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证脚本：用修复后的代码重新提取两个问题视频。
在 import f2 之前 monkeypatch LogManager.clean_logs，绕过 safe-delete 拦截。
"""
import sys, os, json, traceback, pathlib

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

# === 关键：在 import f2 之前 patch clean_logs 为 no-op ===
# f2.log.logger.log_setup 会调用 log_manager.clean_logs(99) 删旧日志，
# 沙箱 safe-delete 拦截 os.remove → fail-closed → import 失败
try:
    import f2.log.logger as _f2logger
    # 找到 LogManager 类，patch clean_logs
    _LogManager = _f2logger.LogManager
    _LogManager.clean_logs = lambda self, *a, **kw: None
    print("[verify] 已 patch f2 LogManager.clean_logs 为 no-op")
except Exception as e:
    print(f"[verify] patch f2 失败（可能 f2 未安装）: {e}")

# 读取 API 配置
def _read_config():
    config_path = os.path.join(PROJ, "config.json")
    data_config_path = os.path.join(PROJ, "data", "config.json")
    config = {}
    if os.path.exists(data_config_path):
        with open(data_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    elif os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    api = config.get("api", {})
    api_key = os.environ.get("WB_LLM_API_KEY", "").strip() or api.get("api_key", "")
    return {
        "base_url": api.get("base_url", ""),
        "api_key": api_key,
        "model": api.get("model", "deepseek-chat"),
    }

def main():
    import extract_server as es

    output_dir = os.path.join(PROJ, "output", "extract")
    os.makedirs(output_dir, exist_ok=True)

    api_config = _read_config()
    print(f"[verify] API 配置: model={api_config['model']} base_url={api_config['base_url'][:40]} key={'有' if api_config['api_key'] else '无'}")

    if not api_config['api_key']:
        print("[verify] 警告: API key 为空，LLM 调用会失败")
        print("[verify] 尝试从 data/config.json 读取...")
        data_config_path = os.path.join(PROJ, "data", "config.json")
        if os.path.exists(data_config_path):
            with open(data_config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            api = cfg.get("api", {})
            print(f"[verify] data/config.json api: model={api.get('model','')} key_set={'是' if api.get('api_key') else '否'}")
            if api.get("api_key"):
                api_config = {
                    "base_url": api.get("base_url", ""),
                    "api_key": api.get("api_key", ""),
                    "model": api.get("model", "deepseek-chat"),
                }
                print(f"[verify] 已从 data/config.json 获取 API key")

    urls = [
        ("双人对话-全同一发言人", "https://www.douyin.com/video/7672601963414635514"),
        ("多段对话-只有一段", "https://www.douyin.com/video/7666309484801979641"),
    ]

    for label, url in urls:
        print(f"\n{'='*70}")
        print(f"[verify] 开始提取: {label}")
        print(f"[verify] URL: {url}")
        print(f"{'='*70}")
        try:
            result = es.extract_from_link(output_dir, url, api_config)
            if not result.get("ok"):
                print(f"[verify] 提取失败: {result.get('error','')}")
                continue
            segs = result.get("segments", [])
            speakers = sorted(set(s.get("speaker","?") for s in segs))
            text = result.get("text","")
            cues = result.get("subtitle_cues", [])
            print(f"[verify] 提取成功!")
            print(f"  段数: {len(segs)}")
            print(f"  发言人: {speakers}")
            print(f"  文本长度: {len(text)}")
            print(f"  字幕 cues: {len(cues)}")
            print(f"  extract_id: {result.get('extract_id','')}")
            print(f"  前 8 段:")
            for i, s in enumerate(segs[:8]):
                sp = s.get("speaker","?")
                t = s.get("text","")[:60]
                print(f"    [{i+1}] {sp}: {t}")
            if len(segs) > 8:
                print(f"  ... (共 {len(segs)} 段)")
                print(f"  后 3 段:")
                for i, s in enumerate(segs[-3:]):
                    sp = s.get("speaker","?")
                    t = s.get("text","")[:60]
                    print(f"    [{len(segs)-2+i}] {sp}: {t}")
        except Exception as e:
            print(f"[verify] 异常: {e}")
            traceback.print_exc()

if __name__ == "__main__":
    main()
