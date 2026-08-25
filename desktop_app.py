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

# 单实例互斥锁（命名 mutex）：防止桌面同时打开多个 exe 实例。
# 若已存在实例 → 尝试激活旧窗口后退出；不占用端口、不启服务。
_SINGLE_INSTANCE_MUTEX = "Local\\靓仔文案工作台_SingleInstance"


def _acquire_single_instance() -> bool:
    """尝试获取单实例互斥锁。返回 True 表示抢到了（可继续启动），False 表示已有实例运行中。"""
    try:
        import ctypes
        from ctypes import wintypes
        # use_last_error=True 才能让 CreateMutexW 的 ERROR_ALREADY_EXISTS 被 get_last_error 捕获
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # CreateMutexW 返回 NULL 表示失败；ERROR_ALREADY_EXISTS(183) 表示已存在
        CreateMutexW = kernel32.CreateMutexW
        CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        CreateMutexW.restype = wintypes.HANDLE
        h = CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
        if not h:
            return True  # 获取失败时不影响启动（避免 lock 异常阻塞老板）
        ERROR_ALREADY_EXISTS = 183
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # 已有实例在跑 → 尝试激活旧窗口（找到主窗口并置前）
            try:
                FindWindowW = user32.FindWindowW
                FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
                FindWindowW.restype = wintypes.HWND
                SetForegroundWindow = user32.SetForegroundWindow
                SetForegroundWindow.argtypes = [wintypes.HWND]
                SetForegroundWindow.restype = wintypes.BOOL
                ShowWindow = user32.ShowWindow
                ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                ShowWindow.restype = wintypes.BOOL
                hwnd = FindWindowW(None, "靓仔文案工作台")
                if hwnd:
                    ShowWindow(hwnd, 9)   # SW_RESTORE
                    SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            return False
        return True
    except Exception:  # noqa: BLE001
        # 非 Windows 或 ctypes 不可用 → 不阻塞启动（开发模式可能跑在非 Windows）
        return True


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")


def _pick_data_dir() -> str:
    """决定可写数据目录（output 统计 / lessons 学习档案 / f2 日志落在这里）。

    打包后不再写到 exe 所在目录（避免污染桌面），统一放到
    %APPDATA%\\靓仔文案工作台\\，桌面只保留单个 exe。

    优先级：
      1) 环境变量 WB_DATA_DIR 强制指定（测试 / 一次性覆盖）
      2) 持久化配置（data_dir.txt）——老板可把数据目录改到 E 盘等非 C 盘位置，
         避免占用系统盘 / 桌面；配置文件放在固定的 %LOCALAPPDATA%\\靓仔文案工作台\\，
         不随数据目录迁移，避免循环引用。
      3) 默认：打包 -> %APPDATA%，开发 -> %LOCALAPPDATA%
    """
    env = os.environ.get("WB_DATA_DIR")
    if env:
        return env
    # 持久化配置：老板在「设置」里改过数据目录后，这里读到并生效
    try:
        local_cfg = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "靓仔文案工作台",
            "data_dir.txt",
        )
        if os.path.isfile(local_cfg):
            with open(local_cfg, "r", encoding="utf-8") as f:
                custom = f.read().strip()
            if custom and os.path.isdir(custom):
                return custom
    except Exception:  # noqa: BLE001
        pass
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


def _write_custom_data_dir(path: str) -> bool:
    """把自定义数据目录写入持久化配置（供「设置」界面调用）。"""
    try:
        local_cfg = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "靓仔文案工作台",
        )
        os.makedirs(local_cfg, exist_ok=True)
        with open(os.path.join(local_cfg, "data_dir.txt"), "w", encoding="utf-8") as f:
            f.write(path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _data_dir_config_path() -> str:
    """持久化配置文件的固定路径（不随数据目录迁移）。"""
    return os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "靓仔文案工作台",
        "data_dir.txt",
    )


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


def _notify_user(title: str, msg: str) -> None:
    """窗口版 exe 下 print 没人看得见——启动失败/异常时弹 Windows 消息框给用户明确反馈。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10 | 0x0)  # MB_ICONERROR
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="靓仔文案工作台 桌面版")
    ap.add_argument("--probe", action="store_true", help="自检模式：只验证服务可用")
    ap.add_argument("--port", type=int, default=8765, help="首选端口（默认 8765）")
    ap.add_argument("--autoclose", type=int, default=0,
                    help="N 秒后自动关闭窗口（自动化测试用）")
    args = ap.parse_args()

    # 启动诊断日志：进程死了也要有迹可查（老板反馈"服务无响应"反复出现，必须看到底死在哪）
    def _diag_log(msg):
        try:
            out_dir = os.environ.get("WB_DATA_DIR") or os.path.join(os.environ.get("APPDATA", ""), "靓仔文案工作台")
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "desktop.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " [pid=" + str(os.getpid()) + "] " + str(msg) + "\n")
        except Exception:  # noqa: BLE001
            pass
    _diag_log("[boot] 启动 pid=" + str(os.getpid()) + " cwd=" + os.getcwd())
    # 把进程意外崩溃时也尽量留下 traceback
    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback as _tb
        _diag_log("[crash] 未捕获异常:\n" + "".join(_tb.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    # 单实例守护：自检/测试模式允许并发；正常运行时若已有 exe 在运行，
    # 探活旧实例后端——活着就退出；死了（僵死进程占着 mutex）就杀掉接管。
    # 这是「服务无响应」反复出现的根因之一：旧实例僵死 → 新实例双击没反应/连死进程。
    if not args.probe and not args.autoclose and not _acquire_single_instance():
        _diag_log("[boot] 已有实例在运行，探活旧后端")
        alive = False
        try:
            import urllib.request as _ur
            _ur.urlopen("http://127.0.0.1:8765/api/status", timeout=2)
            alive = True
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            _diag_log("[boot] 旧实例存活，激活旧窗口并退出")
            print("[desktop] 已有一个实例在运行，激活旧窗口并退出")
            sys.exit(0)
        # 旧实例僵死（后端无响应）→ 杀掉旧进程，接管单实例锁，正常启动
        _diag_log("[boot] 旧实例僵死（后端无响应），终止旧进程并接管")
        print("[desktop] 旧实例已僵死，终止旧进程并接管")
        import subprocess as _sp
        _sp.run(["taskkill", "/F", "/IM", "靓仔文案工作台.exe", "/T"],
                capture_output=True, timeout=15)
        time.sleep(1.5)

    port = _find_free_port(args.port)
    threading.Thread(target=_start_server, args=(port,), daemon=True).start()

    if not _wait_ready(port):
        msg = f"服务启动失败（端口 {port}）。请重启电脑或检查杀毒软件是否拦截。"
        print(f"[desktop] {msg}")
        _diag_log("[boot] " + msg)
        if not args.probe:
            _notify_user("靓仔文案工作台 启动失败", msg)
        sys.exit(1)
    print(f"[desktop] 服务已就绪: http://127.0.0.1:{port}")
    _diag_log("[boot] 服务就绪 http://127.0.0.1:" + str(port))
    print(f"[desktop] 数据目录: {os.environ['WB_DATA_DIR']}")

    # 桌面模式下也要启动会话清理线程（server.py __main__ 分支不会执行）
    try:
        _sweep = getattr(server_mod, "_sweep_sessions", None)
        if _sweep:
            threading.Thread(target=_sweep, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass

    # 启动时自动续跑 + 24h 周期检查：先让 webview 窗口完全渲染（延迟 3s），
    # 再在后台线程触发，避免初始化阶段 heavy 抓取把界面卡成白屏。
    try:
        _api_cfg = getattr(server_mod, "_api_config", None)
        _api = _api_cfg() if _api_cfg else {}
        _out = os.path.join(os.environ["WB_DATA_DIR"], "output")

        def _auto_start_after_delay():
            time.sleep(3)
            result = works_library_server.auto_start_jobs(_out, _api, crawl_count=200)
            actions = result.get("actions", []) if isinstance(result, dict) else []
            if actions:
                print(f"[desktop] 自动启动触发: {actions}")
            elif result.get("need_crawl"):
                print(f"[desktop] 已记录 24h 触发时间，下次启动按周期判断")
            # 对标监控：24h 周期自动抓每个账号 top10 高赞对比 + 预加载文案
            try:
                mon = getattr(server_mod, "monitor_server", None)
                if mon:
                    mres = mon.auto_start_fetch(os.environ["WB_DATA_DIR"], _out, _api)
                    if mres and mres.get("ok"):
                        print("[desktop] 对标监控 24h 自动抓取已启动")
                    elif mres and mres.get("skipped"):
                        print(f"[desktop] 对标监控自动抓取跳过: {mres.get('skipped')}")
                    # 24h 自动预加载文案（字幕/ASR），与抓取联动
                    try:
                        pres = mon.auto_preload_pending(_out, _api)
                        if pres and pres.get("loaded"):
                            print(f"[desktop] 对标监控自动预加载文案 {pres['loaded']} 条")
                    except Exception as e:
                        print(f"[desktop] 对标监控自动预加载异常: {e}")
            except Exception as e:
                print(f"[desktop] 对标监控自动抓取异常: {e}")
        threading.Thread(target=_auto_start_after_delay, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] 自动启动检测跳过：{e}")

    if args.probe:
        print("[desktop] probe OK")
        return

    try:
        import webview
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] webview 不可用: {e}")
        sys.exit(1)
    # onefile 打包下，WebView2 若用默认临时目录可能初始化失败导致白屏。
    # 把 WebView2 用户数据目录固定到应用数据目录（持久化、可写、不随 exe 解压变化）。
    # 注意：pywebview 的 storage_path 是 webview.start(storage_path=...) 的参数，
    # 不是 webview.storage_path 属性（那个赋值不生效）。
    _webview_data_dir = os.path.join(os.environ["WB_DATA_DIR"], "webview_data")
    try:
        os.makedirs(_webview_data_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

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

    # 窗口关闭钩子：老板点 X 关闭窗口后，立即强制退出整个进程，
    # 防止 Flask 线程/WebView 残留成僵死进程——僵死进程会持有单实例锁和端口，
    # 导致下次双击打不开、或连上死进程出现「服务无响应」（历史反复 bug 根因之一）。
    def _on_window_closed():
        _diag_log("[shutdown] 窗口已关闭，os._exit(0)")
        print("[desktop] 窗口已关闭，立即退出进程（防止僵死占用）")
        try:
            os._exit(0)
        except Exception:  # noqa: BLE001
            pass
    try:
        window.events.closed += _on_window_closed
    except Exception:  # noqa: BLE001
        pass

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

    webview.start(storage_path=_webview_data_dir)
    print("[desktop] 窗口已关闭，服务随进程退出")


if __name__ == "__main__":
    main()
