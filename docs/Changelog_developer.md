# 那么，一切从这里开始吧。
### 如有commit变更，请同步在此说明更新。非必要可以**不读**该文件，阅读"Quick-start_developer.md"即可。只在你需要补充内容以及遇到困难要溯源时进行查证使用。否则就太占上下文了。
### 请在每次commit的开头标注commit提交的用户名（GitHub用户名），时间，若commit为ai直接提交的，请一并输出agent与模型底模（如你的系统提示词有明确告诉你你是什么模型，没有则不需要）的名字。
#### 如：moton16,2026-9-13,Codex,GPT-6-Astra

---

### D0 — WorkBuddy(工作区改动，未commit),2026-9-13,WorkBuddy,kimi-k3-1 — docs: 开发者文档体系建立
- **新增 `docs/Execution-plan_developer.md`**：v0.7.0 代码级执行方案。P0 检索评测 harness（scripts/eval_search.py + 金标准查询集）→ P1 定向检索路由（registry 加 description 字段 + kb_describe 工具 + MCP initialize instructions + kb_search 描述路由纪律 + fan-out hint + SKILL.md 5.0 判定表化重写）→ P2 PDF/Office 摄取层（**默认关闭**、按需异步 kb_ingest、内嵌 MinerU 双通道客户端 v4/Agent免登、产物收 `.mortis-parsed/`、HTML 表格原子块保护 + 小表转 pipe、pymupdf 兜底）→ P3 可选增强（title/alias boost、per-source 限流，eval 数据决定是否做）。每步含可直接粘贴的代码、测试清单、commit 切分与验收标准。
- **补全 `docs/Quick-start_developer.md`**（原为空胚）：项目概况、架构分层图、索引/检索/tools-call 三条数据流、8 个模块职责与改动坑位表、缓存布局、测试约定、开发约定（文档分工/原子写/fail-closed/Breaking 流程）、常见任务食谱（加工具/改检索/加配置）、上手 checklist。
- 方案依据：MinerU 官方 API 文档（mineru.net/apiManage/docs）已核实全量端点/参数/错误码（v4 精准 + Agent 轻量免登双通道）；同类项目调研（proofsh/obsidian-notes-rag 链接图谱、gbrain per-page max-pool/title boost/CRAG、parent-document retrieval 业界数据）。
- 未动：`CHANGELOG_user.md`（按分工只在 release 时改）、`QUICKSTART_user.md`（PDF 章节文案已写入 Execution-plan P2-8，随功能 commit 一并落地，不提前记录不存在的功能）。
- 验证：docs 纯文档改动，无代码变更；方案内代码均对照现行源码核实（VaultEntry 字段、save() json.dumps 序列化、MarkdownIndexer 构造签名、_fanout_search 返回结构、initialize 响应位置）。

### C0 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — test(eval): 检索评测 harness（Hit@K 金标准回归脚本）
- 新增 `scripts/eval_search.py`（Hit@K 回归）、`tests/eval/golden_queries.json` 骨架
- 动机：P1-P3 都会动检索行为，先立度量尺
- 验证：占位查询跑通（MISS 正常捕获，耗时 0.00s）；真实 vault 抽测跑通（0.09s）；Hit@5 基线 = 0.0%

### C1 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — feat(registry,server): vault description + kb_describe + instructions + 路由纪律
- registry.py：VaultEntry.description / set_description（load/save 兼容老 toml）
- server.py：kb_init 收 description；_list_vaults 返回 description；新增 kb_describe 工具；kb_search 与 path_prefix 描述加入路由纪律；initialize 响应加 instructions
- 测试：tests/test_registry_server.py（roundtrip/老 toml 回退）、tests/test_mcp_stdio.py（instructions/kb_describe schema 与调用）
- 验证：pytest 9 passed (1.92s)；eval Hit@5 = 0.0%（基线维持）

### C2 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — feat(server): fan-out hint
- server.py：_fanout_search 在横跨多个库（len(searched) > 1）时，在返回 dict 中注入 hint，提示调用方下次携带 vault_path 或 path_prefix 定向检索
- 测试：tests/test_registry_server.py 追加跨库 fan-out 结果包含 hint 验证
- 验证：pytest 6 passed (1.24s)

### C3 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — docs(skill): SKILL.md 5.0 重写
- skills/mortis-rag-mcp/SKILL.md：5.0.0 重写，改为 5 级判定表，删除冗余 schema 描述，增加行为反模式与摄取层管理机制说明

### C4 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — feat(config,ingest): IngestConfig + MinerU 双通道客户端
- config.py / app.toml.example：新增 IngestConfig（enabled 默认 False，纯 opt-in）、配置文件样例注释
- mortis_rag_mcp/ingest/mineru.py：纯标准库 urllib 实现 MinerU v4 精准与 Agent 免登双通道客户端，含 429 Retry-After 重试、致命错误码不重试、zip 提取
- tests/test_ingest_mineru.py：通道路由、错误重试判定、解包、轮询状态机测试（全部 mock 网络层）
- 验证：pytest tests/test_ingest_mineru.py 15 passed (0.23s)
