# CODEBUDDY.md · 靓仔文案工作台改代码规范

> 本文件是 AI 改「靓仔文案工作台」代码时必须遵循的约定。任何改动前先读本文件。
> 核心目的：**让改代码既有前端/后端专业度，又有产品/用户视角把关，绝不破坏现有稳定功能。**

---

## 一、项目结构速览

```
copywriters-hub/
├── web/index.html          # 前端唯一入口（单文件 ~8800+ 行：HTML+CSS+JS 全在一处）
├── scripts/                # 后端服务（Flask 单 app）
│   ├── server.py           # 主路由入口（@app.route 全部在这，导入各 *_server.py）
│   ├── extract_server.py   # 链接→口播逐字稿 + 说话人分割
│   ├── works_library_server.py  # 作品库（对标账号视频抓取/提取）
│   ├── monitor_server.py   # 对标监控（高赞榜/撞车榜/5分钟轮询）
│   ├── tts_server.py       # 配音工坊（IndexTTS）← 默认不要碰
│   ├── rewrite_flow.py     # 洗稿流水线
│   ├── agent_chat_server.py# 专家群聊引擎
│   ├── radar_server.py     # 选题雷达（内建晨报）
│   └── ...其他 *_server.py
├── monitor/                # 对标监控包（fetch/store/topics/realtime_store）
├── knowledge/skills_methods/  # 技能方法论（注入专家 prompt）
├── config.json             # 配置（API/专家/方法论开关）★ 含密钥，勿提交
├── copywriters_chat.spec   # PyInstaller 打包配置
└── SESSION-STATE.md        # 会话进度 + 已完成记录
```

---

## 二、产品硬约束（改代码必须守住，碰都不能碰）

1. **洗稿工坊 ≠ 配音工坊，两条链路永不串联**。洗稿/文案改动不得影响 tts_server.py 任何逻辑；同理配音改动不碰洗稿。
2. **所有文案产出面向「口播」**（20-30 岁年轻女性、玄学命理垂类）。
3. **反 AI 幻觉**：爆款学习原文摘录必须逐字校验，编造即丢弃。
4. **复盘不编造因果**：数据不足写「不足以判断」。
5. **玄学合规红线**：禁止绝对化承诺、封建迷信敏感词、恐吓式表述、医疗断言（阿证专家专职核查）。
6. **API Key 不随 exe 发布**（config.json 已被 .gitignore 隔离）。

---

## 三、前端改代码约定（web/index.html）

- **单文件大改要克制**：index.html 是 8800+ 行单文件，改动要精准、局部，禁止大段重排。
- **改完必须做 JS 语法校验**：抽取内联 `<script>` 块用 `node --check` 验证（377K 字符级别）。
- **新增前端函数**：命名遵循现有风格（如 `monGenRadar`、`scoreTopic`、`newWorkFlowWithScript`），放对应功能区块附近。
- **新增「视图」是重活**：只有确有必要才加新导航视图；优先在现有视图内加卡片/区块（融入强化，不颠覆）。
- **交互要有用户反馈**：按钮点击要有 loading 态、成功/失败 toast，不能静默失败。
- **工具函数复用**：`esc()`、`fmtNum()`、`$()`、`post()`、`openModal()`（fields 支持 value 预填）、`switchView()`、`showToast()` 已存在，优先复用不重复造。

---

## 四、后端改代码约定（scripts/*.py）

- **结构**：业务逻辑放 `*_server.py`（可独立测试），`server.py` 只做薄路由转发 + `jsonify`。新增服务也照此。
- **新增路由**：`server.py` 顶部 `import` 对应模块，路由用 `@app.route`，返回 `jsonify(模块.函数(...))`。
- **运行时路径**：`DATA_DIR`/`OUTPUT_DIR` 由 `server.py` 定义（AppData 优先），新模块要能拿到；参考 `monitor_server` 的模式（BASE_DIR 推导 + 独立可导入）。
- **LLM 调用**：复用 `_make_client`/`_resolve_member_provider` 模式（config.api 主模型 / config.deepseek 专家），带超时+重试（参考 agent_chat_server）。
- **改完必须验证**：
  - `python -m py_compile scripts/<改动文件>.py`
  - 跑相关单元测试（`scripts/test_*.py`）
  - 涉及业务逻辑做冒烟测试（用 mock 数据）
- **不要动测试/临时脚本**：`test_*`、`e2e_*`、`_verify_*`、`_repro_*`、`_patch_*` 前缀文件是验证/草稿，正常运行不带，勿改勿删勿提交进 exe。
- **新增后端文件要进打包**：`copywriters_chat.spec` 的 `_runtime_script_datas()` 会自动带 scripts/ 下非 test 前缀的 .py，确认新文件满足命名规范即可自动打包。

---

## 五、产品 / 用户视角审查清单（功能改动前必过）

任何功能改动，动手前先自问（产品经理视角）：

1. **这个改动对用户意味着什么？** 解决什么痛点？措辞用用户的话，不堆术语。
2. **会不会破坏现有流程？** 改动是否会让已跑通的功能（洗稿/配音/监控/群聊）受影响？
3. **边界明确吗？** 洗稿/配音链路边界守住没？合规红线守住没？
4. **是否"融入强化"而非"推倒重来"？** 优先增强现有能力，不无谓新增视图/模块。
5. **空数据/失败时体验？** 无账号、无数据、LLM 未配置、抓取失败时，要给用户明确提示，不能白屏/静默。
6. **改动可回滚吗？** 项目已 git 化，改动要 commit，重要改动写 SESSION-STATE.md。

---

## 六、提交与文档

- **git 已启用**（main 分支），改完代码要 `git commit`，不要手动 .bak 备份。
- **重要改动记入 `SESSION-STATE.md`**（Current Task / Completed 追加）。
- **打包 exe**：`pyinstaller copywriters_chat.spec`（用 Python 3.12，见记忆）；改完要出桌面版就重新打包。
