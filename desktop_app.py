# -*- coding: utf-8 -*-
"""
口播文稿专家群聊 · 靓仔文案工作台（桌面窗口版）
==========================
双击运行 -> 直接弹出仿微信独立窗口（内置服务，无需浏览器）。

用法:
  python desktop_app.py            正常启动（弹窗）
  python desktop_app.py --probe    自检模式：启动服务验证后退出，不弹窗
  python desktop_app.py --port N   指定首选端口（默认 8765，被占用自动顺延）

数据目录规则:
  开发模式（python 直接跑）  -> %LOCALAPPDATA%\\靓仔文案工作台\\
  打包模式（exe）            -> %APPDATA%\\靓仔文案工作台\\（不污染桌面）
  也可用环境变量 WB_DATA_DIR 强制指定
"""
import os
import socket
import sys
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")


def _pick_data_dir() -> str:
    """决定可写数据目录（output 统计 / lessons 学习档案 / f2 日志落在这里）。

    打包后不再写到 exe 所在目录（避免污染桌面），统一放到
    %APPDATA%\\靓仔文案工作台\\，桌面只保留单个 exe。
    """
    env = os.environ.get("WB_DATA_DIR")
    if env:
        return env
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "靓仔文案工作台")
    # 开发模式也不再把用户密钥、作品和测试数据写回源码目录，避免下次
    # PyInstaller 打包时把本机配置一起塞进 exe。需要隔离数据时可设置
    # WB_DATA_DIR（测试已通过该变量使用临时目录）。
    local_appdata = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or os.path.expanduser("~")
    )
    return os.path.join(local_appdata, "靓仔文案工作台")


# 必须在 import server 之前设置，server 模块顶层读取
os.environ["WB_DATA_DIR"] = _pick_data_dir()

# 打包后把进程工作目录也切到数据目录，让 f2 等第三方库的
# 相对路径日志（如 logs/）一并落到 AppData，而不是桌面。
if getattr(sys, "frozen", False):
    _dd = os.environ["WB_DATA_DIR"]
    os.makedirs(_dd, exist_ok=True)
    os.chdir(_dd)

sys.path.insert(0, SCRIPTS_DIR)
import server as server_mod  # noqa: E402

# 显式引入 server 依赖，确保 PyInstaller 打包时不会遗漏
import flask  # noqa: F401
import openai  # noqa: F401
import openpyxl  # noqa: F401
import agents  # noqa: E402,F401
import discussion  # noqa: E402,F401
import knowledge_loader  # noqa: E402,F401
import learn_store  # noqa: E402,F401
import render_chat  # noqa: E402,F401
import stats_store  # noqa: E402,F401
import data_import  # noqa: E402,F401
import works_library_server  # noqa: E402,F401


def _find_free_port(preferred: int = 8765) -> int:
    """从首选端口起找第一个空闲端口（8765 被占用则顺延 8766...）。"""
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("无可用端口（8765~8784 均被占用）")


def _wait_ready(port: int, tries: int = 80) -> bool:
    url = f"http://127.0.0.1:{port}/api/status"
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _start_server(port: int) -> None:
    server_mod.app.run(
        host="127.0.0.1", port=port, threaded=True,
        debug=False, use_reloader=False,
    )


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="靓仔文案工作台 桌面版")
    ap.add_argument("--probe", action="store_true", help="自检模式：只验证服务可用")
    ap.add_argument("--port", type=int, default=8765, help="首选端口（默认 8765）")
    ap.add_argument("--autoclose", type=int, default=0,
                    help="N 秒后自动关闭窗口（自动化测试用）")
    args = ap.parse_args()

    port = _find_free_port(args.port)
    threading.Thread(target=_start_server, args=(port,), daemon=True).start()

    if not _wait_ready(port):
        print(f"[desktop] 服务启动失败（端口 {port}）")
        sys.exit(1)
    print(f"[desktop] 服务已就绪: http://127.0.0.1:{port}")
    print(f"[desktop] 数据目录: {os.environ['WB_DATA_DIR']}")

    # 启动时自动续跑「扒文案」任务：上次因关软件/进程被杀而中断时自动接着跑（增量跳过已完成视频）
    try:
        _api_cfg = getattr(server_mod, "_api_config", None)
        _api = _api_cfg() if _api_cfg else {}
        _out = os.path.join(os.environ["WB_DATA_DIR"], "output")
        if works_library_server.maybe_auto_resume_crawl(_out, _api):
            print("[desktop] 检测到上次扒文案任务中断，已在后台自动续跑（已完成视频自动跳过）")
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] 自动续跑检测跳过：{e}")

    if args.probe:
        print("[desktop] probe OK")
        return

    try:
        import webview
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] webview 不可用: {e}")
        sys.exit(1)

    class Api:
        """暴露给前端 JS 的原生能力（window.pywebview.api）。"""

        def pick_file(self, filter_kind="xlsx", label="选择文件"):
            """打开原生文件选择对话框，返回所选文件的绝对路径（取消返回 None）。"""
            try:
                if filter_kind == "txt":
                    file_types = ("文本文件 (*.txt)",)
                else:
                    file_types = ("Excel 文件 (*.xlsx;*.xls)",)
                result = webview.windows[0].create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False, file_types=file_types)
                if not result:
                    return None
                if isinstance(result, (list, tuple)):
                    return result[0] if result else None
                return str(result)
            except Exception as e:  # noqa: BLE001
                print(f"[desktop] 文件选择失败: {e}")
                return None

    launch_ts = int(time.time())
    window = webview.create_window(
        "靓仔文案工作台",
        f"http://127.0.0.1:{port}/?v={launch_ts}",
        width=1080,
        height=800,
        min_size=(920, 640),
        background_color="#EDEDED",
        js_api=Api(),
    )

    if args.autoclose > 0:
        def _closer() -> None:
            time.sleep(args.autoclose)
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
            # 兜底：某些打包环境下 destroy() 不会结束消息循环，
            # 等待 1 秒后强制退出整个进程（仅测试模式使用）。
            time.sleep(1)
            os._exit(0)
        threading.Thread(target=_closer, daemon=True).start()

    webview.start()
    print("[desktop] 窗口已关闭，服务随进程退出")


if __name__ == "__main__":
    main()
