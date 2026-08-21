# SESSION-STATE

## Current Task

修复配音工坊说话人分割两大问题：①一段长视频被切 100+ 段全部标成 A ②只有标题被当成 1 段对话。已落地并打包（commit a721f20，下次跑 `pyinstaller copywriters_chat.spec` 即可出新 exe）。

## Non-negotiable Product Boundaries

- 洗稿工坊与配音工坊是两条独立链路，不能串联。
- 洗稿问题只报告洗稿代码与洗稿测试；不要引用配音测试作为洗稿完成证明。
- 配音相关状态、音色、任务队列不得影响洗稿流程。
- 所有文案产出面向「口播」；对标视频 → 口播逐字稿 → 洗稿/专家口播改写，全程不碰配音工坊。

## Current Decisions

- 分享入口先从整段文本提取 URL，再校验域名。
- 支持 `douyin.com`、`iesdouyin.com`、`xiaohongshu.com`、`xhslink.com` 单条分享链接。
- 自动提取后进入可编辑校对页；用户取消或未确认时不创建洗稿任务。
- 小红书使用独立页面解析分支，不复用抖音 F2 抓取器。
- 抖音短视频描述常常就是一段短文案加话题标签，不再因为文本短或带 `#` 而被判为"标题/口令"过滤掉；只有分享口令、纯标签串、去掉话题后无实质内容才判弱。

## Completed

- 2026-08-22（装备配置会话）：**新增第 4 份技能方法论 `knowledge/skills_methods/viral-topic-methods.md`**（爆款四基因 + 8 维打分卡 + 传播锚点，来源：WorkBuddy 专家「爆款炼金师」蒸馏；不与已有 title-formulas 重复，拼齐「选题→标题→改写→合规」四段链路）。config.json `skill_methods.max_chars` 3200→4200，验证 4 份方法论共 3967 字完整加载无截断。注入路径为 Agent.say（群聊+洗稿共用），**未触碰任何配音/TTS 配置**，两条链路边界不变。
- 2026-08-22（装备配置会话）：**git 版本管理落地**：`git init`（main 分支，仓库级身份 linsh），75 文件 33581 行首次入库（commit def694f）。.gitignore 隔离 config.json（API key）、构建产物（*.exe/dist*/build.bak*）、运行数据（output/mine/knowledge_digests/logs）、依赖库快照（_buildlib/.tools/.tools2）。后续改动请正常 commit，不再手动 .bak 备份。
- 2026-08-22（装备配置会话）：WorkBuddy 侧配置 3 个自动化：抖音对标账号日报（每日 09:00，账号待配置，读取 output/monitor/accounts/ 与 workslib/accounts.json 的 douyin_id）、爆款晨报·公众号10w+（每日 08:30）、抖音飙升榜·选题雷达（每日 10:00）。
- 2026-08-22（内建强化会话）：**选题潜力打分列**（commit 8f0eace）：对标监控高赞榜新增「潜力」列，纯前端四基因（情绪钩子/信息差/身份标签/行动触发）规则 + 互动数据加权出 1-10 分徽章 + 命中基因标签，零 LLM 零后端，仅增强选题目录。
- 2026-08-22（内建强化会话）：**内置选题雷达·晨报**（commit 7730a42）：新增 `scripts/radar_server.py`，聚合对标监控高赞榜 + 撞车检测 + LLM 分析，生成「今日值得跟选题 Top5 + 撞车警示 + 口播切入角度」日报落盘 output/monitor/radar/。server.py 注册 /api/radar/generate、/api/radar/latest。前端对标监控视图新增「🎯 选题雷达」卡片。LLM 未配置时优雅兜底输出原始数据。.gitignore 补 build/。
- 2026-08-22（内建强化会话）：**口播文案强化**（commit e7827b7）：对标视频弹窗（monShowDesc/monAcctOpenVid）新增「💬 专家口播改写」按钮，新增 newWorkFlowWithScript 把逐字稿预填进「新建口播文稿」一键送 8 位专家群聊重做口播；修复新增函数时误删 bindSession 签名。整文件 JS 语法校验通过。
- 2026-08-22（配音工坊修复会话）：**说话人分割修复**（commit a721f20）：`_presegment_text` 切太碎（156 段→7 段）、`_looks_like_dialogue` 扩展命理/咨询特征词、新增 `_looks_like_title_only` 识别标题兜底、works_library 标题兜底改为返回失败。现有 unittest 4/4 通过；冒烟测试 fallback 正确交替 A/B。**已用 Python 3.12 + PyInstaller 6.22.2 重新打包**，产出 `dist/靓仔文案工作台.exe`（95MB）并覆盖桌面快捷方式；`--probe` 自检通过。
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
