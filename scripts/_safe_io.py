# -*- coding: utf-8 -*-
"""统一安全写入工具。

背景：WorkBuddy 沙箱 shim 会拦截 os.remove/os.rename（把文件移到回收站，回收站不可用时
抛 OSError），且 Windows 杀毒/同步软件可能瞬时占用文件。历史 bug 模式：各 store 裸用
``tmp + os.replace`` 原子写，shim 环境下写入失败/卡死，或损坏文件被静默重置覆盖。

本模块提供：
- atomic_write_json(path, data)：原子写（tmp + os.replace），带重试与降级；
  若 replace 被拦（PermissionError/OSError），回退「直接原地写」保证数据不丢。
- safe_load_json(path, default)：读 JSON；文件损坏时先备份为 *.corrupt-<ts> 再返回默认，
  绝不静默重置覆盖原文件（保留现场供排查）。

打包注意：文件名不以 test_/e2e_ 开头，会被 copywriters_chat.spec 自动收集。
"""
import json
import os
import time

_RETRIES = 5
_RETRY_SLEEP = 0.3


def atomic_write_json(path: str, data, indent: int = 2) -> bool:
    """原子写 JSON。返回是否成功。失败不抛异常（调用方可自行降级提示）。"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    tmp = path + ".tmp"
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    # ① 先写临时文件（写失败可重试，不破坏原文件）
    for _ in range(_RETRIES):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            break
        except OSError:
            time.sleep(_RETRY_SLEEP)
    else:
        return False
    # ② replace 覆盖（shim 可能拦 rename）
    for _ in range(_RETRIES):
        try:
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(_RETRY_SLEEP)
    # ③ 降级：直接原地写（会短暂破坏原子性，但数据不丢；shim 拦 rename 时的兜底）
    # 先备份旧文件，避免 open(w) 先截断后写失败导致双重损坏
    try:
        if os.path.exists(path):
            os.replace(path, f"{path}.bak")
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError:
        pass
    return False


def safe_load_json(path: str, default):
    """读 JSON。损坏时把现场备份成 *.corrupt-<ts> 后返回 default（不覆盖原文件）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        # 保留损坏现场，便于排查；绝不在损坏时直接回写空数据覆盖
        try:
            backup = f"{path}.corrupt-{int(time.time())}"
            os.replace(path, backup)
        except OSError:
            pass
        return default
