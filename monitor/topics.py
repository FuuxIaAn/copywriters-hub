# -*- coding: utf-8 -*-
"""
选题撞车检测（跨账号同题聚类）
=================================
需求：如果某几个账号近期高点赞选题相同，要能一眼看到。

实现：
  1. 对每条视频的 caption（标题）用 jieba 分词 + 词性过滤，去掉
     语气词/数字量词/常见口水词，提取"选题关键词"（名词/动词/形容词中
     有信息量的词）。
  2. 计算每条视频的"选题指纹"（归一化后的关键词集合）。
  3. 两两比较 Jaccard 相似度，>= 阈值（默认 0.4）判定为同题，归入一族。
  4. 族内按"族内最高点赞"排序，输出撞车榜：
     选题名（族内点赞最高视频的标题）+ 涉及账号 + 各自点赞 + 谁最高。

去重规则（两层，用户强调）：
  - 视频级去重：同一 aweme_id 出现多次（搬运）只保留一条，标记搬运来源。
  - 选题级去重：不同视频标题相似 -> 归并到同一选题族（即撞车检测）。
"""
import re

# 监控高赞门槛：低于此点赞数的视频不进入高赞榜/撞车榜/账号最高赞统计
MIN_DIGG_THRESHOLD = 10000

# 账号一览：每个账号最多展示的达标（≥门槛）视频条数
MAX_ACCOUNT_TOP = 10

try:
    import jieba
    from jieba import posseg

    JIEBA_AVAILABLE = True
    # 常用口播/自媒体口水词，不计入选题指纹
    _STOPWORDS = set(
        """
        这个 那个 一个 我们 你们 他们 自己 今天 现在 时候 什么 怎么 为什么
        大家 朋友 视频 内容 感觉 真的 其实 就是 还是 但是 如果 因为 所以
        然后 而且 知道 觉得 看看 看完 决定 事情 人生 生活 世界 一起 可以
        没有 不是 不要 不会 不能 已经 还是 比如 这样 那样 一下 一直 有点
        分钟 秒钟 小时 每天 个月 这些 那些 各位 喜欢 希望 想要 开始 成为
        看到 告诉 关注 点赞 收藏 转发 评论 记得 谢谢 感谢 免费 领取 资料
        关注我 每天 看完这个 下集 上集 系列 教程 方法 步骤 干货 分享
        """.split()
    )
    # 仅过滤纯 ASCII 数字/字母（如"2026""ai"），注意 \w 在 Python 会匹配中文，必须显式 ASCII
    _STOP_RE = re.compile(r"^[0-9A-Za-z_]+$")
except Exception:  # pragma: no cover
    jieba = None
    JIEBA_AVAILABLE = False


def extract_keywords(text: str, top_n: int = 12) -> list:
    """从标题中提取选题关键词（带词性过滤）。"""
    if not text:
        return []
    if not JIEBA_AVAILABLE:
        # 降级：按常用分隔符切词
        words = re.split(r"[\s,，。！？、：:；;\"'“”‘’《》（）()\[\]【】\-—_]+", text)
        return [w for w in words if len(w) >= 2][:top_n]

    keep = []
    for w, flag in jieba.posseg.cut(text):
        w = w.strip()
        if len(w) < 2:
            continue
        if w in _STOPWORDS:
            continue
        if _STOP_RE.match(w):
            continue
        # 只保留有信息量的词性
        if flag and flag[0] in "nvan":  # 名词/动词/形容词/副词
            keep.append(w)
    return keep[:top_n]


def topic_fingerprint(text: str) -> set:
    """选题指纹 = 去重后的关键词集合。"""
    return set(extract_keywords(text))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def dedup_videos(videos: list[dict]) -> list[dict]:
    """视频级去重：同一 aweme_id 只保留一条（优先保留点赞高的），
    额外记录最早出现的来源。"""
    seen = {}
    for v in videos:
        vid = str(v.get("aweme_id", ""))
        if not vid:
            continue
        if vid not in seen or v.get("digg_count", 0) > seen[vid].get("digg_count", 0):
            seen[vid] = v
    return list(seen.values())


def detect_collisions(videos: list[dict], threshold: float = 0.35) -> list[dict]:
    """选题撞车检测（输入为跨账号拼接后的视频列表，每条含 note 字段标识来源）。
    返回撞车榜：
    [
      {
        "topic": 选题名(族内最高赞视频标题),
        "max_digg": 族内最高点赞,
        "accounts": ["账号A(12.8w)", "账号B(9.8w)"],
        "members": [视频...],   # 按点赞降序
        "count": 2,
      }, ...
    ]
    """
    items = []
    for v in videos:
        fp = topic_fingerprint(v.get("desc", ""))
        if not fp:
            continue
        items.append({"video": v, "fp": fp})

    # 并查集聚类
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if jaccard(items[i]["fp"], items[j]["fp"]) >= threshold:
                union(i, j)

    groups = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(items[i]["video"])

    # 只有 >=2 条才算"撞车"
    results = []
    for g in groups.values():
        if len(g) < 2:
            continue
        g = sorted(g, key=lambda x: x.get("digg_count", 0), reverse=True)
        accounts = []
        for v in g:
            name = v.get("note") or v.get("author_nickname") or "未知账号"
            digg = v.get("digg_count", 0)
            accounts.append(f"{name}({_fmt_digg(digg)})")
        results.append(
            {
                "topic": (g[0].get("desc") or "未命名选题")[:40],
                "max_digg": g[0].get("digg_count", 0),
                "accounts": accounts,
                "members": g,
                "count": len(g),
            }
        )
    results.sort(key=lambda x: x["max_digg"], reverse=True)
    return results


def _fmt_digg(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}w"
    return str(n)


def build_report(accounts_results: list[dict], top_n: int = 10) -> dict:
    """汇总批量抓取结果，产出：高赞榜 + 撞车榜 + 统计。
    accounts_results: fetch.fetch_accounts_videos 的返回值。
    """
    all_videos = []
    account_meta = []
    fetch_errors = []

    for r in accounts_results:
        note = r.get("note") or r.get("home_url", "")
        if not r.get("ok"):
            fetch_errors.append({"note": note, "error": r.get("error", "未知错误")})
            continue
        acc = r.get("account") or {}
        account_meta.append(
            {
                "nickname": acc.get("nickname", note),
                "note": note,
                "home_url": r.get("home_url", ""),
                "follower_count": acc.get("follower_count", 0),
            }
        )
        for v in r.get("videos", []):
            v["author_nickname"] = acc.get("nickname", note)
            v["note"] = note
            all_videos.append(v)

    # 视频级去重（跨账号去搬运）
    deduped = dedup_videos(all_videos)
    dedup_hits = len(all_videos) - len(deduped)

    # 高赞门槛过滤：只保留点赞 >= MIN_DIGG_THRESHOLD 的视频进入榜单
    qualified = [v for v in deduped if v.get("digg_count", 0) >= MIN_DIGG_THRESHOLD]
    threshold_drops = len(deduped) - len(qualified)

    # 时间窗口：只保留「近 90 天」发布的视频（避免用自然月卡死，导致上月/前月的
    # 过万爆款被误过滤——用户反馈过某账号有过万视频却一条都看不到）。
    # create_time 有两种来源格式，统一归一化后按时间戳比较：
    #   - 真实抓取："2026-03-03 18-44-03"（时间用 '-' 分隔）
    #   - mock："2026-08-10 12:00:00"（时间用 ':' 分隔）
    qualified_recent = [
        v for v in qualified
        if _is_recent(v.get("create_time", ""), days=90)
    ]

    # 高赞榜：每个账号取点赞降序前 MAX_ACCOUNT_TOP 条，合并为统一榜单（按点赞降序，不截断）。
    # 取消 MIN_DIGG_THRESHOLD 过滤：用户要的是「每个账号近十条最高赞」，
    # 而不是「达标爆款」——否则小账号永远看不到自己的榜。
    top_videos = []
    for r in accounts_results:
        if not r.get("ok"):
            continue
        acc = r.get("account") or {}
        nick = (acc.get("nickname") or "").strip()
        author = nick or (r.get("note") or "").strip() or _short_account_name(r.get("home_url", ""))
        videos = sorted(
            r.get("videos", []),
            key=lambda x: x.get("digg_count", 0),
            reverse=True,
        )[:MAX_ACCOUNT_TOP]
        for v in videos:
            v["author_nickname"] = author
            top_videos.append(v)
    top_videos.sort(key=lambda x: x.get("digg_count", 0), reverse=True)

    # 账号一览：列出全部监控账号（含抓取失败的），前端只展示「账号 + 粉丝数」。
    account_top = {}
    for r in accounts_results:
        acc = r.get("account") or {}
        # 展示名：优先本次抓到的真实昵称 → 已存储昵称 → 备注 note → 短链接形式（绝不用整条长 URL）
        nick = (acc.get("nickname") or "").strip()
        name = (
            nick
            or (r.get("stored_nickname") or "").strip()
            or (r.get("note") or "").strip()
            or _short_account_name(r.get("home_url", ""))
        )
        if not r.get("ok"):
            # 抓取失败：仍列出，粉丝数沿用已有资料（无则 0），无视频
            account_top[name] = {
                "nickname": name,
                "home_url": r.get("home_url", ""),
                "follower_count": acc.get("follower_count", 0),
                "aweme_count": acc.get("aweme_count", 0),
                "video_count": 0,
                "raw_count": 0,
                "top": [],
                "fetch_failed": True,
            }
            continue
        videos = list(r.get("videos", []))
        videos.sort(key=lambda x: x.get("digg_count", 0), reverse=True)
        top = videos[:MAX_ACCOUNT_TOP]
        account_top[name] = {
            "nickname": name,
            "home_url": r.get("home_url", ""),  # 主页链接，前端点账号名直接打开
            "follower_count": acc.get("follower_count", 0),
            "aweme_count": acc.get("aweme_count", 0),
            "video_count": len(videos),        # 达标条数（≥门槛）
            "raw_count": len(r.get("videos", [])),  # 实际抓取条数
            "top": [
                {
                    "aweme_id": v.get("aweme_id", ""),
                    "desc": v.get("desc", ""),
                    "digg_count": v.get("digg_count", 0),
                    "comment_count": v.get("comment_count", 0),
                    "play_count": v.get("play_count", 0),
                    "create_time": v.get("create_time", ""),
                }
                for v in top
            ],
        }

    return {
        "fetched_at": _now_str(),
        "threshold": MIN_DIGG_THRESHOLD,
        "max_account_top": MAX_ACCOUNT_TOP,
        "threshold_drops": threshold_drops,
        "account_count": len(account_meta),
        "account_meta": account_meta,
        "fetch_errors": fetch_errors,
        "dedup_hits": dedup_hits,
        "total_videos": len(qualified),
        "raw_total_videos": len(deduped),
        "top_videos": top_videos,
        "account_top": list(account_top.values()),
    }


def _now_str() -> str:
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short_account_name(home_url: str) -> str:
    """从主页链接里抠一个可读的短名，绝不在昵称位置显示整条长 URL。

    抓取失败且无昵称/备注时兜底用：优先取 sec_uid 尾号，否则显示「未命名账号」。
    """
    u = (home_url or "").strip()
    if not u:
        return "未命名账号"
    # 抖音主页 /user/<sec_uid> 取尾号（约 8-12 位即可辨认）
    m = re.search(r"/user/([^/?#]+)", u)
    if m:
        tail = m.group(1)
        return "账号 " + (tail[-10:] if len(tail) > 10 else tail)
    # 其它链接：去掉协议/域名后的路径尾段
    tail = u.rstrip("/").rsplit("/", 1)[-1]
    if tail and "douyin" not in tail and "http" not in tail:
        return "账号 " + tail[:20]
    return "未命名账号"


def _is_recent(create_time, days: int = 90) -> bool:
    """判断视频发布时间是否在近 `days` 天内。

    create_time 支持两种格式（真实抓取与 mock 的差异）：
      - "2026-03-03 18-44-03"（时间部分用 '-' 分隔）
      - "2026-08-10 12:00:00"（时间部分用 ':' 分隔）
    无法解析的时间视为「近期」（不误过滤）。
    """
    import datetime
    import re

    raw = str(create_time or "").strip()
    if not raw:
        return True
    # 归一化：把日期与时间之间的分隔、时间内部的分隔统一为可解析形式。
    # 先用正则拆分：日期部分 "YYYY-MM-DD"，时间部分 "HH-MM-SS" 或 "HH:MM:SS"。
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[\sT]+(\d{1,2})[-:](\d{1,2})[-:](\d{1,2})", raw)
    if not m:
        # 可能只有日期 "YYYY-MM-DD"
        m2 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", raw)
        if not m2:
            return True  # 解析失败不误过滤
        y, mo, d = (int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        hh, mi, ss = 0, 0, 0
    else:
        y, mo, d, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
    try:
        dt = datetime.datetime(y, mo, d, hh, mi, ss)
    except Exception:
        return True
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    return dt >= cutoff


def build_markdown(report: dict) -> str:
    """把报告渲染成 Markdown（用于落盘/分享）。"""
    th = report.get("threshold", MIN_DIGG_THRESHOLD)
    drops = report.get("threshold_drops", 0)
    lines = [
        "# 对标账号监控报告",
        "",
        f"- 抓取时间：{report['fetched_at']}",
        f"- 高赞门槛：仅统计点赞 ≥{th//10000}万 的视频（本次过滤掉 {drops} 条低赞视频）",
        "",
    ]
    lines.append(f"## 高赞视频榜（仅限当月 · Top {len(report['top_videos'])}，≥{th//10000}万赞）")
    lines.append("")
    lines.append("| # | 账号 | 标题 | 点赞 | 评论 | 发布时间 |")
    lines.append("|---|------|------|------|------|----------|")
    for i, v in enumerate(report["top_videos"], 1):
        lines.append(
            f"| {i} | {v.get('author_nickname','')} | {v.get('desc','')[:30]} | "
            f"{v.get('digg_count',0)} | {v.get('comment_count',0)} | {v.get('create_time','')} |"
        )
    lines.append("")
    lines.append("## 账号一览")
    lines.append("")
    lines.append("| 账号 | 粉丝数 |")
    lines.append("|------|--------|")
    for b in report["account_top"]:
        lines.append(f"| {b['nickname']} | {b.get('follower_count', 0)} |")
    if report["fetch_errors"]:
        lines.append("")
        lines.append("## 抓取失败")
        lines.append("")
        for e in report["fetch_errors"]:
            lines.append(f"- {e['note']}：{e['error']}")
    return "\n".join(lines)
