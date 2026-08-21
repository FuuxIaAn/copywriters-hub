# -*- coding: utf-8 -*-
"""e2e：求测者经历提取 → 编辑 → 注入 persona → 对话（mock 抓取 + 真实 LLM）"""
import json
import sys
import urllib.request

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else 8899}"
PASSED = []


def call(path, payload=None, method=None):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    if method:
        req.get_method = lambda: method
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    PASSED.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name, detail if not cond else "")


# 1. 提取（mock）
d = call("/api/extract/link", {"url": "https://v.douyin.com/mock/"})
check("1 提取成功", d.get("ok"))
eid = d.get("extract_id", "")
segs = d.get("segments") or []

# 2. 求测者经历画像已生成
vp = d.get("visitor_profile") or {}
check("2 经历画像.summary 非空", vp.get("summary"), str(vp)[:200])
check("3 经历画像.experiences 非空", vp.get("experiences"))
check("3b 经历画像含 visitor_speaker(A/B)", vp.get("visitor_speaker") in ("A", "B"), str(vp.get("visitor_speaker")))
print("   summary:", vp.get("summary", "")[:80])

# 2b. 发言人只允许 A/B，传 C 会被归一
check("3c 所有 speaker 只有 A/B", all(s.get("speaker") in ("A", "B") for s in segs),
      str([s.get("speaker") for s in segs]))
d_seg = call("/api/extract/segment", {"extract_id": eid, "seg_idx": 0, "speaker": "C"})
check("3d 传 C 被归一为 A", d_seg.get("ok") and d_seg["segments"][0]["speaker"] == "A",
      str(d_seg.get("segments", [{}])[0].get("speaker")))

# 4. latest 含画像
d2 = call("/api/extract/latest")
check("4 latest 含 visitor_profile", (d2.get("visitor_profile") or {}).get("summary"))

# 5. 手动编辑保存
edited = {
    "summary": "测试编辑：一位32岁男性问事业",
    "basics": "男，32岁",
    "visitor_speaker": "B",
    "experiences": ["事业不顺两年", "师傅说三十岁后转运"],
    "problems": ["事业停滞"],
    "demands": ["问转机"],
    "emotion": "焦虑",
}
d3 = call("/api/extract/experience/update", {"extract_id": eid, "profile": edited})
check("5 编辑保存成功", d3.get("ok") and d3["visitor_profile"]["summary"] == edited["summary"])
check("6 保存后 experiences 是列表", isinstance(d3["visitor_profile"]["experiences"], list) and len(d3["visitor_profile"]["experiences"]) == 2)
check("6b 保存后 visitor_speaker=B", d3["visitor_profile"].get("visitor_speaker") == "B")

# 6. 重新提取（真实 LLM，mock 对话）
d4 = call("/api/extract/experience", {"extract_id": eid})
vp2 = d4.get("visitor_profile") or {}
check("7 重新提取成功且非空", d4.get("ok") and vp2.get("summary"), str(vp2)[:200])

# 7. 创建 persona（注入经历）
d5 = call("/api/agent/persona", {
    "segments": segs, "speaker": "A", "scene": "微信找我算命，问事业",
    "extra_style": "", "visitor_profile": vp2})
check("8 persona 创建成功", d5.get("ok"))
persona = d5.get("persona", "")
check("8b persona 首段为「口语化模仿」最高优先级", "最高优先级：口语化模仿" in persona)
check("8c persona 含分话题反应模式", "思维方式与反应模式" in persona)
sa = d5.get("style_analysis") or {}
print("   catchphrases:", (sa.get("catchphrases") or [])[:5])
print("   topics:", [t.get("topic") for t in (sa.get("topic_reactions") or [])])
check("9 persona 含「你的经历」段", "你的经历（人物背景）" in persona)
check("10 persona 含画像内容", vp2.get("summary", "")[:10] in persona)
sid = d5.get("sid", "")

# 8. 会话持久化含画像
d6 = call(f"/api/agent/session/{sid}")
check("11 会话返回 visitor_profile", (d6.get("visitor_profile") or {}).get("summary"))

# 9. 对话（真实 LLM）
d7 = call("/api/agent/send", {"sid": sid, "message": "师傅你好，我最近事业上遇到些麻烦，想请您帮我看看"})
check("12 agent 回复成功", d7.get("ok") and d7.get("agent_msg", {}).get("content"))
print("   agent:", d7.get("agent_msg", {}).get("content", "")[:80])

# 10. ASR 语音转文字（mock：完整口播逐字稿 → 分段 + 说话人 + 画像）
# 10a. Key 通过中央设置路由 /api/settings/asr 保存（集中化，替代页内输入框）
d8 = call("/api/settings/asr", {"api_key": "mock-test-key"})
check("13 ASR Key 中央保存成功", d8.get("ok") and d8.get("has_key"))
# 10b. 兼容路由 /api/extract/asr/settings（GET）读到已配置状态（key_masked）
d9 = call("/api/extract/asr/settings")
check("14 ASR Key 后端读取一致", d9.get("ok") and d9.get("has_key") and d9.get("key_masked") and d9.get("key_masked") != "sk-test-mock-key")
# 10c. 中央总览 /api/settings 聚合三组服务且 ASR 已配置
d9b = call("/api/settings")
check("14b /api/settings 聚合三组（llm/asr/tts）",
      isinstance(d9b.get("asr"), dict) and d9b["asr"].get("has_key")
      and isinstance(d9b.get("tts"), dict) and isinstance(d9b.get("llm"), dict))


def _wait_asr(payload):
    """提交 ASR 任务 → 轮询状态 → 返回最终 result（适配后台任务模式）。"""
    submit = call("/api/extract/asr/transcribe", payload)
    if not submit.get("ok"):
        return submit
    import time
    result = None
    for _ in range(120):  # 最多 120 秒
        st = call("/api/extract/asr/status").get("state") or {}
        if not st.get("running"):
            result = st.get("result")
            if st.get("last_error"):
                return {"ok": False, "error": st["last_error"]}
            break
        time.sleep(0.5)
    return result or {"ok": False, "error": "轮询超时"}


d10 = _wait_asr({"url": "https://v.douyin.com/mock_asr/"})
check("15 ASR 转录成功", d10.get("ok"), str(d10.get("error", ""))[:200])
# 15a. 回归：整段抖音分享文案（前缀+链接+后缀）也能被解析转录
d10b = _wait_asr({"url": "9.41 EHi:/ 03/05 d@A.gO :8pm 财太重了，这个兄弟 https://v.douyin.com/mock_asr2/ 复制此链接，打开Dou音搜索，直接观看视频！"})
check("15a ASR 整段分享文案可解析", d10b.get("ok"), str(d10b.get("error", ""))[:200])
asr_segs = d10.get("segments") or []
check("16 ASR 分段足够多（长视频多句）", len(asr_segs) >= 15, f"实际 {len(asr_segs)} 句")
check("17 ASR speaker 全部 A/B", all(s.get("speaker") in ("A", "B") for s in asr_segs),
      str([s.get("speaker") for s in asr_segs[:10]]))
check("18 ASR 记录标记 source=asr", d10.get("source") == "asr")
check("18b ASR 结果含 extract_id（供前端改说话人）", bool(d10.get("extract_id")), str(d10.get("extract_id")))
# 18c. 回归：ASR 转录结果也能改说话人（之前 extract_id 缺失导致按钮无效）
asr_eid = d10.get("extract_id", "")
d10c = call("/api/extract/segment", {"extract_id": asr_eid, "seg_idx": 0, "speaker": "B"})
check("18c ASR 结果改说话人成功", d10c.get("ok") and d10c["segments"][0]["speaker"] == "B",
      str(d10c.get("segments", [{}])[0].get("speaker")))
asr_vp = d10.get("visitor_profile") or {}
check("19 ASR 画像含年份事件", any("年" in str(e) for e in (asr_vp.get("experiences") or [])),
      str(asr_vp.get("experiences"))[:200])
d11 = call("/api/extract/latest")
check("20 latest 已被 ASR 结果替换", (d11.get("source") == "asr") and d11.get("segments"))

n_ok = sum(1 for _, c in PASSED if c)
print(f"\n== {n_ok}/{len(PASSED)} PASS ==")
sys.exit(0 if n_ok == len(PASSED) else 1)
