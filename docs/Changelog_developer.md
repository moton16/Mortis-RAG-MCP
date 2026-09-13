# 那么，一切从这里开始吧。
### 如有commit变更，请同步在此说明更新。非必要可以**不读**该文件，阅读"Quick-start_developer.md"即可。只在你需要补充内容以及遇到困难要溯源时进行查证使用。否则就太占上下文了。
### 请在每次commit的开头标注commit提交的用户名（GitHub用户名），时间，若commit为ai直接提交的，请一并输出agent与模型底模（如你的系统提示词有明确告诉你你是什么模型，没有则不需要）的名字。
#### 如：moton16,2026-9-13,Codex,GPT-6-Astra
### ⚠️ 必须注意文档分工：面向普通用户的日常更新在根目录 CHANGELOG_user.md，必须用大白话只讲功能增减与体验，绝不允许写技术细节；所有的代码逻辑、实现细节与技术变更流水必须且只能写在本文档（Changelog_developer.md）与 PROJECT_GUIDE.md！

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

### C5 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — feat(ingest): 任务管理器 + 表格处理
- mortis_rag_mcp/ingest/tables.py：iter_table_blocks 块识别与未闭合兜底、convert_small_tables 规整小表转 pipe、split_large_table 巨型表分片包裹
- mortis_rag_mcp/ingest/worker.py：IngestManager 异步任务队列、.mortis-parsed/ 镜像落盘、sha256 增量与幂等、原子写状态、pymupdf 本地兜底
- tests/test_ingest_tables.py、tests/test_ingest_worker.py：表格识别/转换单测、worker 异步生命周期与幂等单测
- 验证：pytest 13 passed (0.14s)

### C6 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — feat(server,indexer): kb_ingest + hint + 表格保护
- server.py：新增 kb_ingest 工具（submit/status/pending 三态）；kb_init / kb_init_solo 增加 PDF/Office 文档探测与启用提醒 hint；异步触发索引同步
- indexer.py：iter_table_blocks 表格原子块保护，表格内部不触发标题切分、不截断 chunk；超 2*chunk_size 巨型表分片保护；_cache_meta 增加 table_guard 缓存代际
- tests/test_ingest_server.py、tests/test_indexer.py：工具分发、三态行为、未开启拦截、原子表格保护单测
- 验证：pytest 50 passed (2.47s)

### C7 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — docs: README/QUICKSTART_user 增补摄取章节
- README.zh-CN.md：增补"PDF / Office 文档摄取（默认关闭）"小节，阐明两步开启方式与 .mortis-parsed/ 落盘设计
- QUICKSTART_user.md：配置段末尾追加第 6 条摄取说明

### D1 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — docs: 新增 v0.7.0 本地提交对抗审查报告
- 新增 `docs/v0.7.0/Adversarial-review-merged_developer.md`：针对 C0-C7 8 轮 commit 进行全量对抗审查（Adversarial Review / Red Team Review），整合两轮（静态代码审查 + 实测探针深挖），按根因去重、统一为 `D1–D20` 编号。
- 梳理出 3 项 P0（`kb_ingest` 路径逃逸外发、未闭合 `<table>` 三条故障分支、已闭合大表超预算 chunk）、5 项 P1、6 项 P2、6 项 P3，其中 8 条附实测复现数据，并提供代码级修复指引与 14 条验收门禁。

### C8 — moton16,2026-9-13,Antigravity,Gemini — fix(ingest,indexer,server): 全面修复对抗审查 20 项缺陷并达成 14 项门禁
- **P0 缺陷彻底清零**：
  - D1: `_validate_safe_source` 纵深校验前移至 `submit` 与 `_run_job` 双重防御，严防路径穿越与任意文件上传。
  - D2: `iter_table_blocks` 排除围栏代码块，对未闭合表格设行数与字符双重界限；`convert_small_tables` 严格要求已闭合 `</table>`，根除正文被删隐患。
  - D3: 表格按字符预算动态装箱，长单元格细粒度切分，确保所有 chunk 严格不超过 `chunk_size`。
- **P1 缺陷全面加固**：
  - D4: `split_large_table` 保留同行的 `<tr>` 与表头，`split_table_into_chunks` 直出物理行号区间，杜绝行号漂移。
  - D5: 单行超长表按行与单元格拆包重构，保证每个分片均为闭合合法 `<table>` 片段。
  - D6: 引入 `.ingest.lock` 跨进程文件锁，`_ingest_manager_for` 补齐双检锁，杜绝多进程与多线程竞争。
  - D7: `retryable=True` 错误维持 failed 状态并支持 force 重新入队，PyMuPDF 兜底仅对 PDF 生效且显式 close()。
  - D8: `scan_pending` 与 `_count_vault_docs` 改用 scandir 剪枝并跳过全量哈希，根除服务假死。
- **P2 / P3 完备性与可观测性**：
  - D9: `IngestManager` 支持 `on_job_finished` 回调，解析落盘后自动拉起增量索引同步。
  - D10 / D11: 启动自愈 zombie parsing 任务，submit 去重防止额度浪费。
  - D13 / D14: 配置防御与告警，SearchFilter 与 FTS 支持原目录前缀定向召回 `.mortis-parsed` 产物。
  - D15 / D16: 移除全局盲替换，优化 status 汇总可观测性 (`done_full` / `done_fallback`)。
  - D17 / D18 / D19 / D20: 统一版本号 0.7.0 与 15 个工具计数，eval_search 支持 MRR 与无外网模式，fanout hint 采用 VaultEntry.name，231 项全真测试 100% 通过。

### C9 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — chore: bump version to 0.7.0 and sync CHANGELOG/docs
- pyproject.toml：版本号由 0.6.0 bump 至 0.7.0
- CHANGELOG_user.md：全量重写为纯大白话用户体验更新日志，顶部增设醒目红线警告栏（严禁技术术语）
- 新增 AGENTS.md & CLAUDE.md：根目录确立开发者与 AI Agent 协作法则，将“用户日志禁止堆砌技术细节”设为项目级强制红线
- docs/Quick-start_developer.md & Changelog_developer.md：强化文档分工说明
- README.zh-CN.md & docs/PROJECT_GUIDE.md：更新版本标头为 v0.7.0 并同步新特性
- 验证：全量测试 231 passed，Hit@5 100%，准备合入与推流

### C10 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — docs: purge root AGENTS/CLAUDE files and streamline READMEs to user perspective
- 移除根目录冗余的 `AGENTS.md` 与 `CLAUDE.md`，开发者协作与文档分工准则统一收敛于 `docs/Quick-start_developer.md` 与 `docs/PROJECT_GUIDE.md`。
- 大幅精简重写 `README.md` 与 `README.zh-CN.md`，彻底移除数百行历史故障复盘、内存基准分析与底层结构排查等技术黑话。
- 主页文档全面对齐 `QUICKSTART_user.md` 与 `CHANGELOG_user.md` 简化版，聚焦产品特性、5 分钟极简上手、核心工具速查与文档导航，打造清爽优雅的项目主页。
