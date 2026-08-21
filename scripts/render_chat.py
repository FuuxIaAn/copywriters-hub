# -*- coding: utf-8 -*-
"""
微信群聊界面渲染器

把 Markdown 讨论记录渲染成仿微信群的聊天页面：
  - 顶部群名 + 成员列表
  - 头像（彩色圆形 + 名字首字）
  - 消息气泡按轮次出现
  - "正在输入…" 动画 + 逐块打字机效果
  - 终稿以特殊卡片展示

用法:
  python render_chat.py  (渲染 output/ 下最新的讨论记录)
  python render_chat.py --file discussion_xxx.md
"""
import argparse
import datetime
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 专家头像配色（可按需扩展；未知专家给灰色）
AVATAR_COLORS = {
    "阿沁": "#E8576A",
    "老周": "#3B82F6",
    "阿爆": "#F59E0B",
    "小黄": "#10B981",
    "爆哥": "#8B5CF6",
    "阿证": "#0D9488",
    "阿骨": "#B45309",
    "阿导": "#E11D48",
    "安先生": "#E8576A",
    "薛辉": "#3B82F6",
    "三把刀": "#F59E0B",
    "社恐小黄": "#10B981",
    "阿审": "#7C3AED",
}
DEFAULT_COLOR = "#94A3B8"

RE_ROUND1 = re.compile(r"^##\s+(.+?)（(.+?)）·\s*第一轮独立分析\s*$")
RE_ROUND2 = re.compile(r"^##\s+(.+?)（(.+?)）·\s*讨论回应\s*$")
RE_FINAL = re.compile(r"^###\s+(.+?)（(.+?)）终稿\s*$")


def parse_messages(md: str) -> list:
    """把 Markdown 讨论记录解析成结构化消息序列。"""
    sections = []
    cur = None
    for line in md.split("\n"):
        m1, m2, m3 = RE_ROUND1.match(line), RE_ROUND2.match(line), RE_FINAL.match(line)
        if m1:
            cur = {"type": "round1", "name": m1.group(1).strip(), "title": m1.group(2).strip(), "text": []}
            sections.append(cur)
            continue
        if m2:
            cur = {"type": "round2", "name": m2.group(1).strip(), "title": m2.group(2).strip(), "text": []}
            sections.append(cur)
            continue
        if m3:
            cur = {"type": "final", "name": m3.group(1).strip(), "title": m3.group(2).strip(), "text": []}
            sections.append(cur)
            continue
        if line.strip().startswith("## 原稿"):
            cur = {"type": "script", "name": "", "title": "", "text": []}
            sections.append(cur)
            continue
        if cur is not None:
            cur["text"].append(line)

    for s in sections:
        s["text"] = "\n".join(s["text"]).strip()

    # 组装成消息序列
    n_round1 = sum(1 for s in sections if s["type"] == "round1")
    messages = []
    messages.append({"type": "system", "text": "群聊开始 · 口播文稿专家讨论群"})
    for s in sections:
        if s["type"] == "script":
            messages.append({"type": "script", "text": s["text"]})
            messages.append({"type": "system", "text": f"Round 1 · {n_round1} 位专家独立分析"})
        elif s["type"] == "round1":
            messages.append({"type": "message", "name": s["name"], "title": s["title"], "text": s["text"], "round": 1})
        elif s["type"] == "round2":
            if not any(m.get("round") == 2 for m in messages):
                messages.append({"type": "system", "text": "Round 2 · 群聊互评交锋"})
            messages.append({"type": "message", "name": s["name"], "title": s["title"], "text": s["text"], "round": 2})
        elif s["type"] == "final":
            if not any(m.get("type") == "final" for m in messages):
                messages.append({"type": "system", "text": "Round 3 · 各自给出终稿"})
            messages.append({"type": "final", "name": s["name"], "title": s["title"], "text": s["text"]})
    messages.append({"type": "system", "text": "讨论结束"})
    return messages


def _strip_name_prefix(text: str, name: str) -> str:
    for sep in ("：", ":"):
        if text.startswith(name + sep):
            return text[len(name) + 1:].strip()
    return text


def render_md(text: str) -> str:
    """轻量 Markdown 渲染：加粗 / 引用 / 列表 / 小标题 / 段落。"""
    text = html.escape(text)
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                block.append(lines[i])
                i += 1
            out.append('<div class="code">' + "<br>".join(html.escape(l) for l in block) + "</div>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            quote = [line.lstrip()[1:].strip()]
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append('<div class="quote">' + "<br>".join(render_inline(q) for q in quote) + "</div>")
            continue
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append("<li>" + render_inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append('<ul class="ul">' + "".join(items) + "</ul>")
            continue
        if re.match(r"^\s*(\d+)[.、]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*(\d+)[.、]\s+", lines[i]):
                items.append("<li>" + render_inline(re.sub(r"^\s*\d+[.、]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append('<ol class="ul">' + "".join(items) + "</ol>")
            continue
        if re.match(r"^#{2,4}\s+", line):
            out.append('<div class="subtitle">' + render_inline(re.sub(r"^#{2,4}\s+", "", line)) + "</div>")
            i += 1
            continue
        if re.match(r"^[-—]\s*$", line):
            out.append('<div class="hr"></div>')
            i += 1
            continue
        para = [render_inline(line)]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{2,4}|\s*[-*]\s|\s*\d+[.、]|\s*>|\s*```)", lines[i]):
            para.append(render_inline(lines[i].rstrip()))
            i += 1
        out.append('<p class="p">' + "<br>".join(para) + "</p>")
    return "\n".join(out)


def render_inline(line: str) -> str:
    line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
    line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
    return line


def build_html(md: str, title: str = "口播文稿 · 专家讨论群") -> str:
    messages = parse_messages(md)

    # 群成员（去重，保留顺序）
    members = []
    for m in messages:
        if m.get("name") and m["name"] not in members:
            members.append(m["name"])

    # 每条消息的展示时间（递增）
    base = datetime.datetime.now().replace(second=0, microsecond=0)
    clock = [base + datetime.timedelta(seconds=30 * i) for i in range(len(messages))]
    msgs_js = []
    for i, m in enumerate(messages):
        name = m.get("name", "")
        color = AVATAR_COLORS.get(name, DEFAULT_COLOR)
        if m["type"] == "system":
            item = {"type": "system", "text": m.get("text", "")}
        else:
            item = {
                "type": m["type"],
                "name": name,
                "title": m.get("title", ""),
                "html": render_md(_strip_name_prefix(m.get("text", ""), name)),
                "color": color,
                "time": clock[i].strftime("%H:%M"),
            }
        msgs_js.append(item)

    import json as _json
    # 转义 "</"：防止 AI 消息文本里出现 </script> 提前闭合 script 标签
    payload = _json.dumps(msgs_js, ensure_ascii=False).replace("</", "<\\/")
    members_html = "".join(
        f'<span class="member"><span class="avatar" style="background:{AVATAR_COLORS.get(n, DEFAULT_COLOR)}">{n[0]}</span>{n}</span>'
        for n in members
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:#dfe2e6; }}
.phone {{ max-width:520px; margin:0 auto; min-height:100vh; background:#ededed; display:flex; flex-direction:column; box-shadow:0 0 24px rgba(0,0,0,.18); }}
.header {{ position:sticky; top:0; z-index:10; background:#f7f7f7; border-bottom:1px solid #e2e2e2; padding:14px 16px 10px; }}
.header .gname {{ font-size:16px; font-weight:600; color:#111; }}
.header .members {{ margin-top:8px; display:flex; flex-wrap:wrap; gap:6px; }}
.member {{ display:inline-flex; align-items:center; gap:4px; background:#fff; border:1px solid #e5e5e5; border-radius:999px; padding:2px 10px 2px 3px; font-size:12px; color:#333; }}
.avatar {{ width:22px; height:22px; border-radius:50%; color:#fff; font-size:12px; font-weight:600; display:inline-flex; align-items:center; justify-content:center; flex:none; }}
.chat {{ flex:1; padding:14px 12px 20px; overflow-y:auto; }}
.sys {{ text-align:center; font-size:11px; color:#8c8c8c; background:#e5e5e5; border-radius:6px; padding:4px 10px; display:inline-block; margin:10px auto; }}
.sys-wrap {{ text-align:center; }}
.card {{ background:#fdf4e3; border:1px solid #f0d9a8; border-radius:10px; padding:10px 12px; margin:8px 0; font-size:13px; color:#4a3a1a; line-height:1.7; }}
.card .card-title {{ font-weight:600; color:#a26a00; margin-bottom:6px; font-size:13px; }}
.msg {{ display:flex; gap:10px; margin:14px 0; align-items:flex-start; }}
.msg .avatar.big {{ width:40px; height:40px; font-size:17px; }}
.msg .bubble {{ max-width:calc(100% - 60px); background:#fff; border-radius:2px 12px 12px 12px; padding:10px 12px; font-size:14px; line-height:1.75; color:#222; box-shadow:0 1px 1px rgba(0,0,0,.04); }}
.msg .bubble .who {{ font-size:12px; color:#999; margin-bottom:2px; font-weight:500; }}
.msg .bubble .time {{ float:right; font-size:10px; color:#bbb; margin-top:6px; }}
.bubble .p {{ margin:4px 0; }}
.bubble strong {{ color:#111; }}
.bubble .quote {{ border-left:3px solid #d8d8d8; background:#fafafa; padding:6px 10px; margin:8px 0; color:#555; border-radius:0 6px 6px 0; }}
.bubble .ul {{ margin:6px 0 6px 18px; }}
.bubble li {{ margin:3px 0; }}
.bubble .subtitle {{ font-weight:600; color:#333; margin:8px 0 4px; }}
.bubble .code {{ background:#f5f5f5; border-radius:6px; padding:8px 10px; font-family:Consolas,monospace; font-size:12px; margin:6px 0; }}
.bubble .hr {{ border-top:1px dashed #ddd; margin:10px 0; }}
.typing {{ display:flex; gap:10px; margin:14px 0; align-items:flex-start; }}
.typing .bubble {{ display:flex; align-items:center; gap:4px; background:#fff; border-radius:2px 12px 12px 12px; padding:12px 14px; }}
.typing .dot {{ width:7px; height:7px; border-radius:50%; background:#bbb; animation:blink 1.2s infinite; }}
.typing .dot:nth-child(2) {{ animation-delay:.2s; }}
.typing .dot:nth-child(3) {{ animation-delay:.4s; }}
@keyframes blink {{ 0%,80%,100% {{ opacity:.25; transform:translateY(0); }} 40% {{ opacity:1; transform:translateY(-2px); }} }}
.final {{ background:#f0f7ff; border:1px solid #bcd7f3; border-radius:12px; padding:14px 14px 10px; margin:16px 0; }}
.final .final-head {{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
.final .badge {{ background:#3b82f6; color:#fff; font-size:11px; font-weight:600; border-radius:4px; padding:2px 8px; }}
.final .who {{ font-size:13px; font-weight:600; color:#1e4e8c; }}
.final .bubble {{ background:#fff; border-radius:10px; padding:10px 12px; font-size:14px; line-height:1.8; color:#222; }}
.typing-final {{ font-size:12px; color:#888; margin:10px 0; text-align:center; }}
.foot {{ text-align:center; font-size:11px; color:#aaa; padding:10px 0 18px; }}
</style>
</head>
<body>
<div class="phone">
  <div class="header">
    <div class="gname">口播文稿 · 专家讨论群 ({len(members)})</div>
    <div class="members">{members_html}</div>
  </div>
  <div class="chat" id="chat"></div>
  <div class="foot">讨论记录由 DeepSeek 生成 · 已保存至 output/</div>
</div>
<script>
const MESSAGES = {payload};
const chat = document.getElementById('chat');
const sleep = ms => new Promise(r => setTimeout(r, ms));

function scrollBottom() {{ chat.scrollTop = chat.scrollHeight; }}

function sysHtml(t) {{ return '<div class="sys-wrap"><span class="sys">' + t + '</span></div>'; }}

function cardHtml(t) {{ return '<div class="card"><div class="card-title">待讨论 · 口播文稿</div>' + t + '</div>'; }}

function bubbleHtml(m, inner) {{
  return '<div class="msg"><span class="avatar big" style="background:' + m.color + '">' + m.name[0] + '</span>' +
    '<div class="bubble"><div class="who">' + m.name + ' · ' + m.title + ' <span class="time">' + m.time + '</span></div>' +
    inner + '</div></div>';
}}

function typingHtml(m) {{
  return '<div class="typing" id="typing"><span class="avatar big" style="background:' + m.color + '">' + m.name[0] + '</span>' +
    '<div class="bubble"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div></div>';
}}

function finalHtml(m) {{
  return '<div class="final"><div class="final-head"><span class="avatar" style="background:' + m.color + '">' + m.name[0] + '</span>' +
    '<span class="badge">终稿</span><span class="who">' + m.name + ' · ' + m.title + '</span>' +
    '<span class="time" style="margin-left:auto;font-size:11px;color:#aaa">' + m.time + '</span></div>' +
    '<div class="bubble">' + m.html + '</div></div>';
}}

async function revealBubble(m, node) {{
  node.innerHTML = m.html;
  const ps = node.querySelectorAll('.p');
  ps.forEach(p => p.style.opacity = 0);
  for (const p of ps) {{
    p.style.opacity = 1;
    scrollBottom();
    await sleep(140 + Math.min(420, p.textContent.length * 2));
  }}
}}

async function run() {{
  for (const m of MESSAGES) {{
    if (m.type === 'system') {{
      chat.insertAdjacentHTML('beforeend', sysHtml(m.text));
      scrollBottom();
      await sleep(500);
      continue;
    }}
    if (m.type === 'script') {{
      chat.insertAdjacentHTML('beforeend', cardHtml(m.html));
      scrollBottom();
      await sleep(400);
      continue;
    }}
    chat.insertAdjacentHTML('beforeend', typingHtml(m));
    scrollBottom();
    await sleep(900 + Math.random() * 900);
    document.getElementById('typing')?.remove();
    if (m.type === 'final') {{
      const node = document.createElement('div');
      node.className = 'final';
      node.innerHTML = finalHtml(m);
      chat.appendChild(node);
      const bubble = node.querySelector('.bubble');
      await revealBubble(m, bubble);
    }} else {{
      const wrapper = document.createElement('div');
      wrapper.innerHTML = bubbleHtml(m, '<div class="inner"></div>');
      const inner = wrapper.querySelector('.inner');
      chat.appendChild(wrapper.firstElementChild);
      await revealBubble(m, inner);
    }}
    scrollBottom();
  }}
  const done = document.createElement('div');
  done.className = 'typing-final';
  done.textContent = '—— 讨论结束 · 各专家终稿见上方卡片 ——';
  chat.appendChild(done);
  scrollBottom();
}}

run();
</script>
</body>
</html>"""


def render_chat_html(md: str, output_dir: str, stem: str = "") -> str:
    """渲染讨论记录为微信群聊 HTML 文件，返回文件路径。"""
    os.makedirs(output_dir, exist_ok=True)
    if not stem:
        stem = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"{stem}_chat.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(md))
    return path


def main():
    parser = argparse.ArgumentParser(description="把讨论记录渲染成微信群聊界面")
    parser.add_argument("--file", default=None, help="讨论记录 md 文件路径（默认取 output/ 最新）")
    args = parser.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(base, "output")
    if args.file:
        md_path = args.file
    else:
        candidates = sorted(
            [f for f in os.listdir(output_dir) if f.startswith("discussion_") and f.endswith(".md")],
            reverse=True,
        )
        if not candidates:
            print("未找到讨论记录，请用 --file 指定")
            sys.exit(1)
        md_path = os.path.join(output_dir, candidates[0])
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    stem = os.path.splitext(os.path.basename(md_path))[0]
    out = render_chat_html(md, output_dir, stem)
    print(f"群聊界面已生成: {out}")


if __name__ == "__main__":
    main()
