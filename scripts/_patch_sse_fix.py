# -*- coding: utf-8 -*-
"""SSE 任务接力修复补丁（一次性脚本，跑完即删）。

根因：讨论结束时 _start_discussion 的 finally 置 finished=True 并 push(done)，
而 /api/score、/api/review 等「任务接力」handler 起线程前没有同步重置 finished=False，
前端 POST 成功后立即 openStream() 重连，命中 api_stream 的
「finished=True 且 history[-1] 是 done 类端事件 → 立即断开」判断，评分/复盘/留存/
黑榜/拆解/学习等后续任务的事件永远推不出来（8 人时代才暴露的产品级 bug）。

修复：
1. Session.try_begin 成功时重置 finished=False（任务活跃 ⟹ 未结束）
2. 8 个任务接力 handler 在起线程前同步置 finished=False（关闭重连竞态窗口）
3. 8 个 _run_* 任务的 finally 补置 finished=True（结束 ⟹ 恢复已结束状态，
   修复会话清理 TTL 与页面刷新后 SSE 空连挂 keepalive 的泄漏）
4. api_stream 断开判断加 session.phase == "idle" 双保险
"""
import io
import sys

PATH = "scripts/server.py"

REPLACEMENTS = [
    # 1. try_begin 重置 finished
    (
        "            self.phase = phase\n"
        "            return True",
        "            self.phase = phase\n"
        "            self.finished = False   # 新任务开始即重开会话：SSE 保持接收，任务可接力（评分/复盘/留存…）\n"
        "            return True",
    ),
    # 2. handler 同步重置 finished（起线程前），共 8 处
    (
        "    t = threading.Thread(target=_run_review, args=(sid, review_data), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_review, args=(sid, review_data), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_retention, args=(sid, subtitle, retention, metrics), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_retention, args=(sid, subtitle, retention, metrics), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_debate, args=(sid,), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_debate, args=(sid,), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_viral_teardown, args=(sid, article), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_viral_teardown, args=(sid, article), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_leaderboard_debate, args=(sid, mode), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_leaderboard_debate, args=(sid, mode), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_principle_review, args=(sid, note), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_principle_review, args=(sid, note), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_score, args=(sid, script), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_score, args=(sid, script), daemon=True)",
    ),
    (
        "    t = threading.Thread(target=_run_learn, args=(sid, article), daemon=True)",
        "    session.finished = False  # 任务接力：重开会话，避免 SSE 重连被「done」掐断\n"
        "    t = threading.Thread(target=_run_learn, args=(sid, article), daemon=True)",
    ),
    # 3. 各 _run_* finally 补置 finished=True（8 处）
    (
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"review_done\"})",
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"review_done\"})",
    ),
    (
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"retention_done\"})",
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"retention_done\"})",
    ),
    (
        "        session.push({\"type\": \"error\", \"text\": f\"黑榜讨论出错：{e}\"})\n"
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"debate_done\"})",
        "        session.push({\"type\": \"error\", \"text\": f\"黑榜讨论出错：{e}\"})\n"
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"debate_done\"})",
    ),
    (
        "        session.push({\"type\": \"error\", \"text\": f\"榜单讨论出错：{e}\"})\n"
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"debate_done\"})",
        "        session.push({\"type\": \"error\", \"text\": f\"榜单讨论出错：{e}\"})\n"
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"debate_done\"})",
    ),
    (
        "        session.push({\"type\": \"error\", \"text\": f\"原则审视出错：{e}\"})\n"
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"debate_done\"})",
        "        session.push({\"type\": \"error\", \"text\": f\"原则审视出错：{e}\"})\n"
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"debate_done\"})",
    ),
    (
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"teardown_done\"})",
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"teardown_done\"})",
    ),
    (
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"learn_done\"})",
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"learn_done\"})",
    ),
    (
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.push({\"type\": \"score_done\"})",
        "    finally:\n"
        "        session.end_phase()\n"
        "        session.finished = True\n"
        "        session.push({\"type\": \"score_done\"})",
    ),
    # 4. api_stream 断开判断双保险（phase == idle 才断）
    (
        "        if session.finished and session.history and session.history[-1].get(\"type\") in _SSE_END_TYPES:\n"
        "            return  # 会话早已结束，重放完历史即可断开",
        "        if session.finished and session.phase == \"idle\" and session.history and session.history[-1].get(\"type\") in _SSE_END_TYPES:\n"
        "            return  # 会话早已结束（无任务在跑），重放完历史即可断开",
    ),
    (
        "                if session.finished and session.history[-1].get(\"type\") in _SSE_END_TYPES:\n"
        "                    break",
        "                if session.finished and session.phase == \"idle\" and session.history[-1].get(\"type\") in _SSE_END_TYPES:\n"
        "                    break",
    ),
]


def main():
    with io.open(PATH, "r", encoding="utf-8") as f:
        content = f.read()
    for i, (old, new) in enumerate(REPLACEMENTS, 1):
        cnt = content.count(old)
        if cnt != 1:
            print(f"[FAIL] 第 {i} 处替换锚点不唯一/不存在：count={cnt}\n---\n{old[:120]}...\n")
            sys.exit(1)
        content = content.replace(old, new)
        print(f"[OK] 第 {i} 处替换完成")
    with io.open(PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print(f"\n全部 {len(REPLACEMENTS)} 处替换成功，已写回 {PATH}")


if __name__ == "__main__":
    main()
