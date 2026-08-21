# SESSION-STATE

## Current Task

修复洗稿工坊的分享链接入口：支持整段抖音/小红书转发文本，自动提取文案和互动数据，提取后必须先校对，确认后才启动洗稿。

## Non-negotiable Product Boundaries

- 洗稿工坊与配音工坊是两条独立链路，不能串联。
- 洗稿问题只报告洗稿代码与洗稿测试；不要引用配音测试作为洗稿完成证明。
- 配音相关状态、音色、任务队列不得影响洗稿流程。

## Current Decisions

- 分享入口先从整段文本提取 URL，再校验域名。
- 支持 `douyin.com`、`iesdouyin.com`、`xiaohongshu.com`、`xhslink.com` 单条分享链接。
- 自动提取后进入可编辑校对页；用户取消或未确认时不创建洗稿任务。
- 小红书使用独立页面解析分支，不复用抖音 F2 抓取器。
- 抖音短视频描述常常就是一段短文案加话题标签，不再因为文本短或带 `#` 而被判为"标题/口令"过滤掉；只有分享口令、纯标签串、去掉话题后无实质内容才判弱。

## Completed

- 2026-08-22：修复 `scripts/extract_server.py` 中 `_looks_like_body_text` 与 `_is_weak_plain_text` 过度过滤短视频描述的问题，让带话题标签的真实短文案（如 `这个日主多出高智商 #癸水 #癸水男 #癸水女`）能作为原稿正文被提取并进入校对页。
- 2026-08-22：洗稿入口校对弹窗优化——
  - `.modal-box` 加 `max-height:85vh; overflow-y:auto`，长文案弹窗可滚动，底部「确认」按钮始终可见。
  - `openModal` 扩展支持 `type:'checkbox'` 字段。
  - 校对弹窗（自动提取版 + 手动补录版）新增「默认洗稿后完整字数为 550-600 字」复选框，默认勾选。
  - 提交时 `_mergeLimitAndRequirements` 把勾选状态拼进 requirements（勾选 + 自填要求合并为 `默认洗稿后完整字数为 550-600 字。另：<自填要求>`）。
  - 保留「洗稿要求（可选）」输入框，用户仍可自填要求。
- 2026-08-22：洗稿入口改为**三步分步向导** `wizardRewrite()`：
  - 第1步 校对原稿文案（可编辑 textarea）→ 下一步
  - 第2步 校对互动数据（点赞/评论/转发/收藏，2 列网格）→ 下一步
  - 第3步 洗稿要求（字数勾选默认550-600 + 自填要求 textarea）→ 确认并开始洗稿
  - 顶部步骤指示器（1 校对原稿 / 2 校对数据 / 3 洗稿要求），支持「上一步」返回。
  - `newRewriteFromLink` 提取成功直接进向导；`newRewriteManual` 无 pre 先补录原稿再进向导，有 pre（爆款素材一键带入）直接进向导。
  - 每步内容精简，底部按钮始终可见，不再一屏塞满。
  - JS 语法校验通过（node new Function 0 错误）。
- 2026-08-22：**融合技能方法论进洗稿/群聊**（方法论文本注入，非工具脚本内嵌）：
  - 新增 `scripts/skill_methods.py` 加载器：从 `knowledge/skills_methods/*.md` 合并方法论文本，config 控制开关/字符上限。
  - 新增 3 份方法论：`de-ai-rewrite.md`（去AI味）、`title-formulas.md`（爆款标题/钩子）、`compliance-redlines.md`（平台违禁词/玄学合规红线）。
  - config.json 新增 `skill_methods` 配置（enabled/max_chars=3200/files=[]）。
  - `agents.py` Agent 新增 `methods` 字段，注入 `_system_prompt` 的「技能方法论档案」；专家群聊 + 洗稿（都用 Agent.say）自动带上。
  - `server.py` build_agents / build_single_agent 加载 methods 并传给 Agent。
  - `copywriters_chat.spec` datas 加入 `knowledge/skills_methods` 打包进 exe。
  - Python 语法检查通过；方法论加载验证完整（3011 字未截断）。

## Pending Verification

- 用用户提供的抖音短链 `https://v.douyin.com/h8Su_xrxwbU/` 在桌面 EXE 中验证：能识别、能提取到原稿、进入三步向导、字数勾选默认生效。
- 在真实已登录的小红书页面验证正文和互动数据字段。
- 验证方法论注入生效：群聊/洗稿的专家输出是否更"去AI味"、更注意合规。
- 打包桌面 EXE 并确认桌面版本包含本次洗稿修复 + 三步向导 + 技能方法论融合。
