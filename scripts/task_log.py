# -*- coding: utf-8 -*-
"""
任务日志对账（tasks.jsonl）
===========================
后端每个长任务（洗稿 start/redo/resume/finalize/句级评论、识别 extract、
抓取 monitor fetch、TTS 生成）在开始/成功/异常时写一行文件日志，
供「服务无响应」遮罩出现时对账真实错误——前端遮罩只是表象，真实原因在这里。

每行一条 JSON（JSONL），字段：
  ts      发生时间（ISO8601 秒级）
  task    任务名：rewrite / extract / monitor_fetch / tts
  op      操作名：start / redo / resume / finalize / comment / sentence_comment / evaluate
  status  begin / success / error
  error   异常信息（status=error 时）
  extra   附加信息（rid / task_id / 长度等，值截断到 200 字符）

写入方式：进程内锁 + append + flush + fsync，多线程并发下每行仍完整不串行。
"""
import datetime
import json
import os
import threading

_LOCK = threading.Lock()

FILENAME = "tasks.jsonl"


def log_path(output_dir: str) -> str:
    d = os.path.join(output_dir, "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, FILENAME)


def write(output_dir: str, task: str, op: str, status: str,
          error: str = "", **extra) -> None:
    """原子追加一行任务日志。task/op/status 见模块说明。"""
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "op": op,
        "status": status,
        "error": (str(error)[:500]) if error else "",
    }
    if extra:
        rec["extra"] = {k: (str(v)[:200] if v is not None else "") for k, v in extra.items()}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with _LOCK:
            with open(log_path(output_dir), "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
    except OSError as e:
        # 日志写不进也不该拖垮主任务：退到 stderr（exe 无控制台时仍进 stdout 重定向）
        print(f"[tasklog] 写任务日志失败: {e}")


def error_text(exc: BaseException) -> str:
    """统一把异常转成可对账文本（类型 + 摘要）。"""
    return f"{type(exc).__name__}: {str(exc)[:400]}"
