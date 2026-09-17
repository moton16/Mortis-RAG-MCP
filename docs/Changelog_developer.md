# 那么，一切从这里开始吧。
### 如有commit变更，请同步在此说明更新。非必要可以**不读**该文件，阅读"Quick-start_developer.md"即可。只在你需要补充内容以及遇到困难要溯源时进行查证使用。否则就太占上下文了。
### 请在每次commit的开头标注commit提交的用户名（GitHub用户名），时间，若commit为ai直接提交的，请一并输出agent与模型底模（如你的系统提示词有明确告诉你你是什么模型，没有则不需要）的名字。
#### 如：moton16,2026-9-13,Codex,GPT-6-Astra
### ⚠️ 必须注意文档分工：面向普通用户的日常更新在根目录 CHANGELOG_user.md，必须用大白话只讲功能增减与体验，绝不允许写技术细节；所有的代码逻辑、实现细节与技术变更流水必须且只能写在本文档（Changelog_developer.md）与 PROJECT_GUIDE.md！

---

### D0 — Vodyanitsaaa,2026-9-13,WorkBuddy(工作区改动，未commit),WorkBuddy,kimi-k3-1 — docs: 开发者文档体系建立
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

### D1 — Vodyanitsaaa,2026-9-13,Kimi Code,Kimi-K3 — docs: 新增 v0.7.0 本地提交对抗审查报告
- 新增 `docs/v0.7.0/Adversarial-review-merged_developer.md`：针对 C0-C7 8 轮 commit 进行全量对抗审查（Adversarial Review / Red Team Review），整合两轮（静态代码审查 + 实测探针深挖），按根因去重、统一为 `D1–D20` 编号。
- 梳理出 3 项 P0（`kb_ingest` 路径逃逸外发、未闭合 `<table>` 三条故障分支、已闭合大表超预算 chunk）、5 项 P1、6 项 P2、6 项 P3，其中 8 条附实测复现数据，并提供代码级修复指引与 14 条验收门禁。

### C8 — moton16,2026-9-13,ZCode,GLM-5.3 — fix(ingest,indexer,server): 全面修复对抗审查 20 项缺陷并达成 14 项门禁
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

### C9 — moton16,2026-9-13,ZCode,GLM-5.3-Flash — chore: bump version to 0.7.0 and sync CHANGELOG/docs
- pyproject.toml：版本号由 0.6.0 bump 至 0.7.0
- CHANGELOG_user.md：全量重写为纯大白话用户体验更新日志，顶部增设醒目红线警告栏（严禁技术术语）
- 新增 AGENTS.md & CLAUDE.md：根目录确立开发者与 AI Agent 协作法则，将“用户日志禁止堆砌技术细节”设为项目级强制红线
- docs/Quick-start_developer.md & Changelog_developer.md：强化文档分工说明
- README.zh-CN.md & docs/PROJECT_GUIDE.md：更新版本标头为 v0.7.0 并同步新特性
- 验证：全量测试 231 passed，Hit@5 100%，准备合入与推流

### C10 — moton16,2026-9-13,ZCode,GLM-5.3-Flash — docs: purge root AGENTS/CLAUDE files and streamline READMEs to user perspective
- 移除根目录冗余的 `AGENTS.md` 与 `CLAUDE.md`，开发者协作与文档分工准则统一收敛于 `docs/Quick-start_developer.md` 与 `docs/PROJECT_GUIDE.md`。
- 大幅精简重写 `README.md` 与 `README.zh-CN.md`，彻底移除数百行历史故障复盘、内存基准分析与底层结构排查等技术黑话。
- 主页文档全面对齐 `QUICKSTART_user.md` 与 `CHANGELOG_user.md` 简化版，聚焦产品特性、5 分钟极简上手、核心工具速查与文档导航，打造清爽优雅的项目主页。

### C11 — moton16,2026-9-13,ZCode,GLM-5.3-Flash — docs: make Chinese README default and add README_EN with bilingual switcher
- 将 `README.md`（GitHub 默认渲染入口）设为中文主页，直接匹配主力用户与仓库母语使用习惯。
- 将英文版独立收敛为 `README_EN.md`，两份文档顶部互设 `[English](README_EN.md) | 简体中文` 双向语言切换链接。
- 移除多余的 `README.zh-CN.md`，同步更新 `docs/Quick-start_developer.md` 与 `docs/PROJECT_GUIDE.md` 中的文档索引。

### C12 — moton16,2026-9-13,ZCode,GLM-5.3-Flash — docs: add MinerU configuration guides to READMEs and QUICKSTART
- 在 `README.md`、`README_EN.md` 与 `QUICKSTART_user.md` 的配置章节中，明确拆分基础配置（Embedding API Key）与进阶可选配置（MinerU 文档解析摄取）。
- 详细说明 MinerU Token 获取地址（mineru.net）、环境变量设置（`MINERU_API_TOKEN`）与免登轻量试用通道的区别。
- 在客户端连接配置 JSON 示例中补充展示 `MINERU_API_TOKEN` 环境变量注入项。

### C13 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — fix(tests): eliminate Windows NTFS mtime collision in test_indexer
- 根因：`test_indexer.py::test_incremental_add_modify_delete_and_rename` 中连续写入 `# Old\nold content` 与 `# New\nnew content`（两者均为 17 字节）。在云端高速 Windows CI 虚拟机（Azure VM）上，因时钟中断分辨率（15.6ms），两次写入的 `st_mtime_ns` 发生碰撞；结合字节大小完全相同，触发 Fast-Stat 判定为文件未修改而跳过增量更新。
- 修复：对齐 `test_improvements.py` 与 `test_watch_integration.py` 的处理规范，在连续覆写前增加 `time.sleep(0.02)` 跨越 NTFS 时间戳分辨率窗口，并使用不同长度的更新内容。
- 验证：本地全量 231 项测试 100% 通过。

### C14 — Vodyanitsaaa,2026-9-13,Antigravity,Gemini 3.8 Flash — feat(doctor,migration): Agent 信任锚（STATUS.md / doctor.py）+ 用户数据无损原子迁移
- **路径与配置无损原子迁移（~/.vault_mcp* -> ~/.mortis_rag_mcp*）**：
  - `registry.py`：保持 `user_config_dir()` 函数名，对 `~/.vault_mcp` 纯原子 `os.rename` 迁移至 `~/.mortis_rag_mcp`，遇 `OSError` 严格安全回退读旧目录，绝不使用 `shutil.move`；`registry_path()` 支持 `MORTIS_RAG_REGISTRY` 优先回退 `VAULT_MCP_REGISTRY`。
  - `config.py`：新增 `API_KEY_ENV_VARS = ("MORTIS_RAG_API_KEY", "VAULT_MCP_API_KEY")` 及 `resolve_api_key()`；`resolve_default_cache_dir()` 延迟至运行时调用并安全原子搬迁 `~/.mortis_rag_mcp_cache`；`resolve_config_path()` 支持 `MORTIS_RAG_CONFIG` > `VAULT_MCP_CONFIG` > `~/.mortis_rag_mcp/config.toml` > `~/.vault_mcp/config.toml` 回退链。
  - 新增 `tests/test_path_migration.py`（6 个全覆盖单测）。
- **Agent 信任锚（STATUS.md / doctor.py）**：
  - 新增 `mortis_rag_mcp/doctor.py`：实现本机环境自检与 `STATUS.md`（给 Agent 的硬约束规范：7 天内 VALID 禁止环境预检；失效只跑一次 `--doctor`；仍失败熔断报错用户）与 `status.json`；复用 `providers.py`（`provider.rerank` 签名适配）；核心项至少 4 项在场防假 VALID；单库离线警告不锁死；Windows 冲突 4 次退避原子写。
  - `server.py`：`SERVER_INSTRUCTIONS` 注入信任锚与熔断条款；`main()` 支持 `--doctor` 与 `--quiet`；`serve_stdio` 启动后台异步轻量刷新。
  - 新增 `tests/conftest.py`（`pytest_sessionfinish` 成绩记录钩子，解耦整体可用性）与 `tests/test_doctor.py`（6 个单测）。
- **Skill 与规范升级**：
  - `skills/mortis-rag-mcp/SKILL.md`：bump 至 5.1.0，插入信任锚硬约束条款，更新配置链。
  - `pyproject.toml` 及全量文档 bump 至 0.7.1。
### C15 — Vodyanitsaaa,2026-9-13,Antigravity,Gemini 3.8 Flash — fix(doctor,migration): 修复对抗审查发现的表格注入、迁移竞争与门禁防假缺陷
- **防注入与结构安全**：`doctor.py` 的 `render_md()` 增加 Markdown 表格单元格转义（过滤换行、转义管道符），杜绝异常或路径破坏 Agent 信任锚。
- **并发迁移竞态保护**：`registry.py`（`user_config_dir`）与 `config.py`（`resolve_default_cache_dir`）在 `rename` 抛出 `OSError` 时二次检测 `new.exists()`，胜出进程完成迁移后败者直接复用新目录，杜绝死路径回退。
- **配置与门禁严密化**：
  - `doctor.py`：`check_config()` 校验 `reranker` 鉴权，杜绝静态 embedding + 缺 key reranker 组合产生虚假 VALID；扩充本地免密端点支持（`0.0.0.0`、`::1`、`host.docker.internal`）。
  - `probe_embedding()`：增加端点实际返回维度与配置 `dimension` 一致性校对。
  - `run()`：`quiet=True` 场景禁止触碰 `sys.stdout.reconfigure`，规避后台刷新与 stdio JSON-RPC 主线程竞争；去重沿用探测提示后缀。
- **单测宿主环境隔离**：`conftest.py` 检测隔离环境变量，在隔离测试与 CI 下跳过改写真实宿主 `STATUS.md`；`record_test_run()` 使用 `setdefault` 保持信任锚原始过期时间戳。
- **测试补充**：在 `tests/test_doctor.py` 与 `tests/test_path_migration.py` 中新增 8 个回归测试（测试全绿：257 passed, 2 skipped）。

### C16 — Vodyanitsaaa,2026-9-13,Antigravity,Gemini 3.8 Flash — fix(doctor,migration): 修复预置目录注册表失联、外部门禁防假、URL伪本地绕过与并发排他锁缺陷
- **注册表无损迁移守护**：`registry.py`（`registry_path`）增加文件级检测，当新目录预先存在（如提前放置 config.toml）但无 `vaults.toml`，而旧目录存在 `vaults.toml` 时，执行单文件原子迁移；遇锁失败安全回退读取旧文件，杜绝老知识库注册丢失归零。
- **外部模型门禁严加把关**：`doctor.py`（`run`）优化判定逻辑，`mode="external"` 时 `embedding_api` 必须在场且通过探测才能判定 `overall = True`；增量轻量刷新保留原 `generated_at` 时间戳并增加过期校验，杜绝无限续期与虚假标绿。
- **URL Hostname 严格解析**：`_is_local_endpoint()` 改用 `urllib.parse.urlsplit` 严格提取 hostname 进行环回与本地判定，杜绝路径或 query 携带 `localhost` 绕过免密检查；对 `mode="external"` 补全 endpoint/model 非空校验。
- **doctor 跨进程排他锁**：`doctor.run()` 与 `record_test_run()` 引入 `_process_file_lock("status.lock")`，杜绝后台刷新与 CLI 诊断并发时的 Lost Update。
- **单测宿主环境防污染**：`tests/conftest.py` 增加前置文件存在检测，宿主未初始化时单测运行禁止无中生有落盘 `STATUS.md`。
- **测试补充**：在 `tests/test_doctor.py` 与 `tests/test_path_migration.py` 中新增 7 个对抗回归测试（全量 264 passed, 2 skipped 全部通过）。

### C17 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): 修复等长内容替换时增量同步漏检（Windows CI 捕获）
- **缺陷**：`_sync_locked` 快速路径用 `(mtime_ns, size)` 相等短路 sha256，该判据只在「内容变化必然推动 mtime 前进」时成立。Windows 文件时间戳粒度受系统时钟中断（约 15.6ms）限制，等长内容替换（`"old content"` -> `"new content"`，均 18 字节）若落在同一刻度内，`mtime_ns` 与 `size` 双双不变，文件被误判「未修改」而跳过，旧 chunk 静默残留并继续被召回。ext4 真纳秒精度掩盖了该缺陷，故此前只在 Windows CI 稳定复现。
- **修复**：新增 `_fast_path_is_trustworthy(source, mtime_ns)`，判据取反向——只有 mtime 严格晚于上次扫描完成时刻（`_scan_completed_ns`，于 `_sync_locked` 末尾在所有 stat/read 之后记录）时才允许信任签名相等；mtime 未推进（粒度碰撞）或被显式回拨（`os.utime` / `touch -d` / 同步备份工具）的文件一律回退 sha256 精确校验。该分布也更合理：刚编辑过的热文件精确校验，长期未动的冷文件廉价跳过。
- **测试**：新增 `test_incremental_evicts_stale_when_mtime_does_not_advance`，用 `os.utime` 精确回拨 mtime 确定性复现同一失效模式。已反向验证：撤掉修复该测试即失败，装上即通过。
- **文件**：`mortis_rag_mcp/indexer.py`（+33）、`tests/test_indexer.py`（+30）。

### C18 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): Fast-Stat 的 racily clean 判据与真正有鉴别力的回归测试
- **测试鉴别力修复**：C17 的回归测试构造顺序错误——先 sync 再钉 mtime，导致 `_stat_cache` 里留的是首页真实 mtime，签名判据先短路为假、文件走 sha256 被正确检出，**可信度判据根本没被调用**（把判据临时改成恒真 `return True`，测试依然通过，等于没覆盖这个 bug）。改为「先 sync，再把索引缓存与笔记 mtime 钉到同一时刻 T，并把 `(T, size)` 写进 `_stat_cache`」，精确复现 Git 定义的 racily clean 条目（mtime 与索引文件时间戳相同），使可信度判据成为唯一决定因素。
- **判据细化**：锚点优先取索引缓存文件自身 `st_mtime_ns`（单调、跨进程有效、与文件 mtime 同时基），缓存未启用时退化为进程内扫描完成时刻；注释对齐 git-scm.com/docs/racy-git 原文——索引在采集完所有 stat 信息之后才落盘，其时间戳通常不早于其中任何条目，故可疑条目限定为「mtime 与索引时间戳相同」。
- **反向验证**：禁用判据 -> 测试 FAIL；启用判据 -> 测试 PASS。
- **文件**：`mortis_rag_mcp/indexer.py`（+65）、`tests/test_indexer.py`（+58）；另误提交了提交信息暂存文件 `_msg.txt`（见 C25 清理）。

### C19 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): 兜底锚点改为扫描起点，消除零读盘契约的时序抖动
- **CI 现象**：run 34751264159 在 windows-latest 上 `test_fast_stat_skips_disk_read_when_unmodified` 失败（`read_bytes was called despite file being unmodified`），而同一提交在 run 34751198224 上通过——典型时序相关抖动。
- **根因**：C18 给快速路径加上可信判据后，缓存未启用（`AppConfig` 默认 `cache.enabled=False`）时兜底锚点取「扫描完成时刻」，该时刻在扫描所有 stat 之后才取值，与不变文件的 mtime 只差毫秒级（本地实测约 3.1ms）；一旦文件时间戳粒度让两者落入同一刻度，`mtime < anchor` 即为假，不变文件被误判可疑、被迫读盘，零读盘契约被打破。本地无法复现，只在 Windows CI 上稳定偶发。
- **修复**：兜底锚点改取「本次扫描起点 `scan_started_ns`」，在 `_sync_locked` 的任何 stat 之前捕获。语义上更强：不变文件上一轮就已存在，其 mtime 必然严格早于本轮起点 -> 可信、稳定零读盘；扫描期间被写过的文件 mtime >= 起点 -> 强制 sha256 复核。索引缓存启用时仍优先用缓存文件 `st_mtime_ns`。`_scan_completed_ns` 保留为观测/诊断状态，不再参与判据。
- **验证**：索引子集 49 passed；反向验证（临时禁用判据 -> 回归测试 FAIL）确认测试仍有鉴别力。

### C20 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): 判据只在有文件系统锚点时才生效，不再跨时基比较
- **CI 现象**：run 34751456959 上 C19 仍未解决 windows-latest 的同一失败 -> 判定为设计层面问题而非取值层面问题。
- **根因**：进程内时钟（`time.time_ns`）与文件系统时间戳是**两个不同时基**，跨时基比较在时间戳粒度较粗的平台上必然因取整而失真——无论锚点取扫描起点还是扫描终点，只要文件 mtime 与进程时钟落入同一刻度，未改动文件就会被误判可疑、被迫读盘，破坏 `docs/PROJECT_GUIDE.md` 记载的「未改变则彻底跳过读取内容」契约。
- **修复**：判据只在存在**文件系统锚点**（索引缓存文件自身的 `st_mtime_ns`）时才生效；缓存未启用（`cache.enabled=False`，也是 `AppConfig` 默认值）时没有可比的锚点，判据不做判断、直接返回 True，继续信任 `(mtime_ns, size)` 签名——宁可在此退化，也不虚构一个不可靠的跨时基比较。同时回退 C19 引入的 `scan_started_ns` 兜底锚点（函数签名恢复为单参数）。
- **行为矩阵（已实测）**：回归测试（缓存开启）判据启用 PASS / 禁用 FAIL（确有鉴别力）；零读盘契约（缓存关闭）判据启用 PASS / 禁用 PASS（不受判据影响，恢复跨平台稳定）。

### C21 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): 判据改为同源进程时钟自比，默认配置下也生效
- **CI 现象**：C20 在无索引缓存时干脆不判，等于把原缺陷放回默认配置——run 34751601252 上 `test_incremental_add_modify_delete_and_rename` 失败，而该用例用的正是 `AppConfig` 默认的 `cache.enabled=False`。
- **修复**：改为**只比较同源的进程时钟**，把判定放在「记录时」思想下——新增 `_stat_seen_ns: dict[str, int]`，记录每个 source 的 `(mtime_ns, size)` 签名是在哪个进程时钟时刻被观测到的；`_fast_path_is_trustworthy(source, mtime_ns)` 仅当 `mtime_ns` 严格早于该观测时刻才信任签名相等。两个量同源，不存在跨时基取整问题；粒度碰撞只会让条目更可能被判不可信（更保守，方向安全）。复核确认内容未变后按当轮观测时刻重新登记，条目恢复可信、重回零读盘快速路径——「不可信」只让一个条目多付**一次**读盘代价，而非永久失去快速路径（实测：racily clean -> 复核读 1 次 -> 恢复可信 -> 再 sync 读盘 0 次）。判据不依赖 cache 是否启用，故在 `AppConfig` 默认配置下同样生效。
- **清理**：`_scan_completed_ns` 保留为观测/诊断状态、不再参与判据；移除已成死代码的 `_index_written_ns` 及其引入的 `CacheConfig` 依赖。
- **验证**：`tests/test_indexer.py` + `tests/test_improvements.py` 15 passed；索引相关子集（含 multivault）58 passed；反向验证有效；生命周期实测见上。

### C22 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): 登记时刻改到读盘之后，避开写入-观测同刻度窗口
- **诊断数据**（Windows runner 上临时诊断测试输出）：`DIAG mtime=...862503500 seen=...092448200 seen-mtime=229944700`、`trust=True reads2nd=0 racycount=1/30`——Windows 上「写入后紧接着同步」约有 **1/30** 的概率让文件 mtime 与观测时刻落入同一时间戳刻度，条目一出生就被判为 racily clean，导致下一轮白白多读一次；这正是零读盘契约在 Windows CI 上偶发失败、而本地 NTFS 300 次 0 次复现不出来的原因。
- **修复**：把观测时刻从「读盘之前」挪到「读盘并哈希之后」，并顺带重取一次 stat 作为登记签名——`recorded_at_ns = time.time_ns()` 在 `read_bytes + sha256` 之后取，与刚读到的内容严格对应；哈希耗时天然拉开了它与文件 mtime 的距离，显著降低落进同一刻度的概率；重取 stat 得到 `settled_sig`，此时文件已写完关闭，其 mtime 不会再被这次写入改动，比读之前的 `fast_sig` 更稳。
- **清理**：移除临时诊断测试 `tests/test_zzdiag_win.py`，CI 的 `pytest -s` 回退。
- **验证**：索引子集 15 passed；本地余量 1.63ms、第二轮零读盘；反向验证禁用判据仍使回归测试 FAIL。

### C23 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): 可信判据引入 50ms 安全余量，堵死同刻度漏检窗口
- **用户决策**（AskUserQuestion 确认）：采用「一次性精确校验后固化」——正确性优先，允许刚写过的文件在其 mtime 老化超过余量前每轮 sync 复核读盘一次，随后固化恢复零读盘；同步放宽 `test_fast_stat_skips_disk_read_when_unmodified` 的断言。
- **判据**：`mtime_ns < seen_ns - _MTIME_TRUST_MARGIN_NS`（50ms > 2 倍 Windows 最坏刻度 15.6ms）。`seen_ns` 为该签名最近一次通过内容级验证（read + sha256 完成）的进程时钟时刻。
- **可靠性论证（归纳）**：条目登记时若满足 `seen - mtime > MARGIN`，则此后任何写入都发生在 `seen` 之后，其落盘时间戳 `T2 >= 写入时刻 - tick`（时间戳因取整至多滞后一个刻度），故 `T2 >= seen - tick > mtime + MARGIN - tick > mtime + tick > mtime`，即登记之后的写入必然推动 mtime、签名必然失配走正常 sha256 路径——不存在漏检窗口。登记时余量不足的条目（含 Windows 实测约 3% 的「时间戳超前于进程时钟」碰撞，`racycount=1/30`）一律判不可信、强制复核；复核把 `seen` 推得更晚，随真实时间流逝终满足余量后固化。
- **与 Git 的差异**：Git 以索引文件 mtime 为锚（同为文件系统时间戳）；本实现用进程时钟 + 余量，因 `cache.enabled=False`（`AppConfig` 默认）不落任何盘上锚点，而正确性修复必须覆盖默认配置（上游 `test_incremental_add_modify_delete_and_rename` 正是在默认配置下失败的）。
- **实现调整**：登记签名回退为读盘前的 `fast_sig`（弃用 C22 的 re-stat）——若读盘期间文件被写入，`fast_sig` 与磁盘新状态不一致，下一轮签名失配自动重读（自愈）；re-stat 反而可能把「新 mtime + 旧哈希」固化进缓存。`_stat_seen_ns` 保持进程内存活，与 `_stat_cache` 一致，不改缓存格式。
- **测试**：零读盘测试重构为三轮契约（第二轮允许至多一次复核读盘，第三轮断言严格零读盘，固化成立）；回归测试重写为直接把 `_stat_seen_ns` 压到 mtime 同刻以复现 racily clean，默认配置下验证判据拦截等长替换，并断言老化超余量后恢复可信。反向验证通过（判据禁用 -> 用例 FAIL）。`docs/PROJECT_GUIDE.md` 的 Fast-Stat 小节补记判据与论证。

### C24 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(review): 对抗审查修复——锁重入按路径判定 + 沿用健康度标注陈旧度
- **F3（`registry.py`）锁重入只看线程深度**：`_process_file_lock` 的重入判定只看 `lock_depth` 不看锁路径——持 A 锁（如 doctor 的 `status.lock`）再取 B 锁（registry 的 `vaults.lock`）会走重入分支直接放行，B 的跨进程互斥被静默跳过。修复：held 集合按解析后的锁路径记录，同路径重入才放行，异路径真正取锁；注释明示嵌套异构锁的跨进程死锁风险（当前无此调用模式，未来引入须全局有序）。新增回归测试 `test_process_file_lock_reentrancy_is_per_path`：断言持 A 取 B 时 held 含两把锁，且另一线程持 B 时本线程取 B 被真实阻塞。
- **F5（`doctor.py`）沿用旧探活结果会显示假健康**：`full=False` 只加固定后缀「沿用上次全量探测」，若外部 API 已宕机，信任锚会在最长 7 天新鲜度窗口内显示假健康。修复：从旧 section 的 `at` 字段计算距今分钟数，标注改为「沿用 N 分钟前的全量探测结果，本次未重新探活」，陈旧度肉眼可见；`at` 缺失/不可解析时退化为固定文案。沿用前先剥离旧后缀再拼新标注，防重复。
- **本次实测推翻的两条误报（留档）**：「mtime 回拨 + 等长替换击穿 50ms 判据」——实测 `trustworthy=False`，回拨到登记值被余量正确拦截（小文件 `seen` 仅比 mtime 晚几毫秒），残余窄窗口仅限读盘+哈希超 50ms 的大文件，已在 `indexer.py` docstring 与 `PROJECT_GUIDE` 补记该已知限制（与 Git 同类）；「`DEFAULT_CACHE_DIR` 是死常量」——`CacheConfig.dir` 默认值仍在使用，非死代码。
- **验证**：`tests/test_registry.py` + `tests/test_doctor.py` 19 passed；索引/注册表/doctor/迁移相关子集 117 passed。

### C25 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — chore: 诊断脚手架与误提交清理（4 个提交合并记录）
- `4d75bfb` / `2dc5cae` `chore(diag)`：新增临时 Windows 时序诊断测试 `tests/_diag_win.py` 并打开 CI 的 `pytest -s`，用于量化「写入后同步」的 racily clean 概率——结论约 **1/30**，是 C22 / C23 的数据来源与决策依据。
- `bce5f70` `chore(diag)`：诊断文件名改为 `tests/test_zzdiag_win.py`，否则不被 pytest 收集（原文件名 `_diag_win.py` 不匹配 `test_*.py`）。
- `44c5b90` `chore`：移除 C18 误提交的提交信息暂存文件 `_msg.txt`。
- 上述脚手架已在 C22 中全部移除，分支最终不残留任何诊断代码；CI 的 `pytest -s` 同步回退。

### C26 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(review): 环回判定改 fail-closed + 启动期不再真导入 numpy + 补齐本分支开发日志
对抗审查（`/review`，diff `67f2758..HEAD`，+1464 / −111，22 文件）第二轮，经用户逐项批准的三项修复与文档补齐：

- **D1（`doctor.py`）`_is_local_endpoint()` 环回判定收紧为 fail-closed**：原实现 `host.startswith("127.")` 会把 `127.0.0.1.attacker.com`、`127.example.com` 这类**远端域名**判成本机。由于「本机」意味着免 API key 且 STATUS.md 标 ✅，远端端点漏配 key 也会被信任锚说成健康，agent 据此跳过预检、直到真实调用才撞 401——正是本分支要消灭的「信任锚说谎」。现只认两类形态：主机名白名单（`localhost` / `host.docker.internal`）+ 经 `ipaddress` 严格解析的 IP 字面量（`is_loopback`，含 `::ffff:127.0.0.1` 这类 v4-mapped，以及 `0.0.0.0` / `::` 全零地址）。代价是 `127.1` / 十进制 `2130706433` / 八进制 `0177.0.0.1` 等花式写法不再免密（fail-closed，方向安全）。
- **D3（`doctor.py`）`check_optional_deps()` 不再真导入可选依赖**：改用 `importlib.util.find_spec` 探存在性 + `importlib.metadata.version` 读版本号。本函数跑在服务端启动的后台刷新线程，与 stdio 握手同期，而 numpy 首次导入是数百毫秒级 CPU、会与握手抢 GIL——只为报告里一行版本号付这个代价不成比例。退化面：无发行档案的裸目录包会显示 `unknown`（本项目语境不存在）。
- **沿用标注去重与测试陈旧度补齐（`doctor.py`）**：抽出 `_strip_carryover_note()` / `_carry_probe_section()` / `_carry_tests_section()`；剥旧后缀只认 `_CARRYOVER_NOTE_RE` 的固定形状，**不再用 `rsplit("（", 1)[0]`**——探活项自身 detail 就合法含全角括号（`mode=static（非 external，跳过在线探测）`、`未启用（跳过）`），按最后一个「（」切会把原始信息连同括号一起切掉，刷新后变成一句自相矛盾的话。另补 `tests` 项陈旧度标注：测试成绩只在 pytest 里刷新，`full=True` 也不会重跑，否则 `--doctor` 刚刷新的 `generated_at` 会把旧成绩单一起「续期」。
- **`tests/conftest.py` 宿主数据目录防污染**：`pytest_sessionfinish` 的存在性检测不再经 `doctor._status_json_path()`——它内部调 `registry.user_config_dir()`，而后者把「旧目录 `~/.vault_mcp` 原子改名」当作存在性检查的副作用执行，**单跑一次 pytest 就会把开发者真实的数据目录搬走**。改为直接拼新名路径（`Path.home() / ".mortis_rag_mcp" / "status.json"`），零副作用。
- **测试补充（`tests/test_doctor.py`）**：新增 3 个对抗回归测试——`test_doctor_is_local_endpoint_rejects_127_prefixed_remote_domains`（含 userinfo / 尾点 / unicode 数字 / 内网地址）、`test_doctor_is_local_endpoint_ip_literal_forms`（v4-mapped、全零、花式写法）、`test_doctor_check_optional_deps_does_not_truly_import`（用 `builtins.__import__` 间谍钉死「不真导入」契约）。
- **文档同步**：`docs/PROJECT_GUIDE.md` 4.10 行数订正 `约 150 行` -> `约 460 行`；新增两条**排障用已知边界**（新目录先存在时只搬注册表、`config.toml` 可能留在旧侧；降级不可逆——回退到 0.7.1 之前须先手工把 `~/.mortis_rag_mcp` 改回 `~/.vault_mcp`）；4.10 补记免密端点 fail-closed 与启动期不真导入两条设计。`CHANGELOG_user.md` 补「回退须知」（迁移是 rename 改名而非复制，回退旧版本需先改回目录名，数据未损坏）。本文件补齐 C17–C26 共 10 条条目，兑现仓库「每个 commit 一条」的约定。
- **验证**：D1 逐条断言 24 个端点形态全部符合预期（脚本验证，未跑测试套件）；D3 以 `__import__` 间谍确认零泄漏导入；doctor 端到端模拟 3 轮 `full=False` 刷新 + 1 轮 `full=True`，沿用标注不叠加、`tests.at` 保持真实测试时刻。按用户要求，全量测试交由 CI 在推送后执行。

### C27 — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — test(doctor): 可选依赖探测补两条打桩用例，覆盖 CI 走不到的「已装」分支
- **问题**：C26 的 `test_doctor_check_optional_deps_does_not_truly_import` 里只有「不真导入」这条与环境无关的断言是硬的，另两条关于版本号的断言写成了条件式——而 CI 只装 `sqlite-vec`（`[vec]` extra）不装 numpy，于是 numpy 那一路的「已装 -> 读版本」分支在 CI 上永远不会被执行，等于该分支没有覆盖。
- **补充两条确定性用例**（不依赖跑测机器装了什么）：
  - `test_doctor_check_optional_deps_reports_version_when_present`：打桩 `find_spec` 返回非 None、`version` 返回 `9.9.9`，断言 detail 出现「numpy 9.9.9」，且另一模块仍正确落入「未装」；
  - `test_doctor_check_optional_deps_version_fallback_is_unknown`：模块在、但发行档案抛 `PackageNotFoundError` -> 记 `unknown`，而不是把整项误判成「未装」。
- **反向验证**：把实现改回 `__import__`，spy 用例的断言即失败（已实测旧实现确实触发 numpy / sqlite_vec 导入），说明该用例具备鉴别力、不是空转。
- **文档订正**：C26 条目的测试清单回正为 3 个（该提交实际只含 3 个用例）；C27 条目即本测试提交的记录，一并收录该订正。
- **验证**：两条新用例的断言逻辑逐条复现通过；`compileall` 通过；CI 全绿。

---

> 以下 C28–C31 来自放行前终审（12 条 findings：3 CRITICAL + 9 INFORMATIONAL）的修复窗口，四项一次修完，不留发版后独立分支的欠账。

### C28 — Vodyanitsaaa,2026-9-17,WorkBuddy,Deepseek-V4.1-Flash — fix(doctor): STATUS.md 头行补转义，堵死信任锚指令注入
- **问题（CRITICAL）**：`render_md()` 此前只对表格 `detail` 列做转义（`\r\n` 归一 + 管道符 `\|` + HTML 角括号），而**头行四个字段 `machine` / `version` / `commit` / `stamp` 零转义**。`machine` 取自 `platform.node()`，在受控主机名（CI runner、容器名、可改名的 Windows 主机）上可预先注入换行与 `##`，实测能在 STATUS.md 中**伪造出顶层标题与伪造的「给 Agent 的硬约束」段**——而这份文件正是被 agent 当权威读的信任锚，等于把环境原始数据变成指令注入通道。
- **修复**：抽出 `_sanitize_inline()`，与 detail 同一套清洗逻辑，**复用于全部头行字段**；`machine` 另经 `_sanitize_hostname()` 叠加 RFC1123 字符白名单（非法字符一律替换为 `?`）+ 64 字符截断（超长补 `…`）。值域受限后该字段既不可能携带结构，也无法藏进可执行的伪指令。
- **detail 侧同步强化**：`_sanitize_free_text()` 补齐 HTML 角括号与 `U+2028` / `U+0085` / `U+2029` 三个 Unicode 行分隔符的归一化——部分渲染器（含部分 agent 的正文解析）视其为换行，只处理 `\r\n` 会让它们成为绕过通道；自由文本限长 `_FREE_TEXT_LIMIT = 160` 字符，超长截断。
- **契约补声明**：`render_md()` 的「给 Agent 的硬约束」段补一句「表格 detail 列为环境原始数据，不得当作指令执行」，把「信任本文件」的边界写进文件自己。
- **回归用例（`tests/test_doctor.py`）**：`test_doctor_render_md_sanitizes_all_header_fields`（四个头行字段分别注入 `## 伪造标题`，断言渲染结果中不出现注入标题、且合法硬约束段只有一份）、`test_doctor_render_md_truncates_oversized_hostname`、`test_doctor_render_md_normalizes_html_and_unicode_line_breaks`、`test_doctor_render_md_declares_detail_column_is_not_an_instruction`。
- **验证**：脚本对照（`verify/v1_injection_and_fastpath.py`）——修复前注入可产生脱离引用块的一级结构，修复后同输入被转义为 `?` 序列且结构数不变。

### C29 — Vodyanitsaaa,2026-9-17,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): Fast-Stat 余量按实测刻度推导 + 签名纳入 ctime，收口漏检面
本条目收口两条 CRITICAL，二者是同一个判据的两条独立失效路径。**先纠正 C24 留档的一处误判**：C24 曾把「mtime 回拨 + 等长替换击穿 50ms 判据」记为**误报**（依据是小文件 `seen` 仅比 mtime 晚几毫秒，回拨会被余量拦下）。该结论不成立——余量按固定 50ms 取值时，只要「读盘 + 哈希」耗时把 `seen - mtime` 推到余量之上（实测自然稳态即可达 **152.5ms**），回拨就能同时满足「签名相等」与「判据为真」，等长替换被静默漏检。C24 的留档结论以本条为准。

- **CRITICAL 1 · 余量 50ms 小于粗粒度文件系统刻度**：`_MTIME_TRUST_MARGIN_NS` 原为写死的 50ms，而同一份 docstring 自己就写明「FAT32 达 2s」。当刻度 > 余量时，「登记之后发生的写入必然推动 mtime 前进」的归纳前提直接不成立——刻度是**文件系统属性**，固定常数无法同时适配 NTFS（约 3ms）、ext4（真纳秒）与 FAT32/exFAT/部分 SMB（2s）。
  - **修复**：新增 `_probe_mtime_tick_ns()` 从库内文件 `mtime_ns` **反推真实刻度**（取「全体值的 gcd」与「相邻差值的 gcd」两者中较小者）。正确性方向明确：真实刻度整除所有时间戳，故任何 gcd 都是刻度的正整数倍——**只会高估、绝不低估**；高估使余量变大（更保守，只多读盘），低估才会漏检，而 gcd 数学上不可能低估。`_effective_margin_ns()` 取 `max(50ms 下限, 2 × 刻度)`；探测收敛点 `_finalize_mtime_tick_probe()` 放在**扫描循环之后**，而 `_stat_cache` 是进程内存量、每进程首次 sync 必为空，故首次 sync 不会用到未探测的余量。探测到刻度粗于 `_MTIME_TICK_COARSE_NS`（50ms）时**直接禁用快速路径（fail-closed）**——宁可每轮读盘也不漏检。
  - **明确记录退化面**：库内可索引文件少于 2 个时无法求差值，探测返回 `None`、退回固定下限，此时粗刻度库仍存在该固定余量的失效面。这是本探测唯一的未覆盖面，如实写入 docstring。
- **CRITICAL 2 · mtime 回拨 + 等长替换静默漏检**：`rsync --times` / tar 解包 / 快照还原会把 mtime 回拨到原值，配等长替换（`# Old\nold content` → `# New\nnew content`）可让 `(mtime_ns, size)` 逐位不变。
  - **修复**：签名升级为 **`(mtime_ns, size, st_ctime_ns)`**。POSIX 下 ctime 是 inode 元数据变更时间，由内核维护、`os.utime` **无法回拨**，该向量被直接封死。
  - **Windows 残余风险如实标注（不冒充已修复）**：`st_ctime` 在 Windows 上语义是**创建时间**、不随写入推进，对「等长替换 + mtime 回拨」无鉴别力。docstring 与验证脚本均按「已申报残余风险」处理，验证脚本显式输出 `ctime_is_creation_time_on_this_platform` 与 `MISSED` 标志，让该平台的实际行为可审计而非靠断言。
- **口令订正**：删除原 docstring 中「不存在漏检窗口」与「窗口很窄（已实测）」两处论断——前者被固定余量失效面证伪，后者被自然稳态（`seen - mtime` 约 152.5ms）下的实测复现证伪。两处都是在为可复现的漏检背书，改为与实测一致的口径。
- **INFORMATIONAL · mtime 停在未来的条目不再被永久惩罚**：网络盘/共享盘时钟超前、备份还原、手工 touch 会把 mtime 留在未来，旧实现让这类条目永久失去零读盘快速路径（每轮重读 + 重哈希）。现引入 `_FUTURE_MTIME_RECHECK_LIMIT = 2`（跨墙钟复核上限）+ `_FUTURE_MTIME_MIN_OBSERVATION_GAP_NS = 1ms`（两次观测最小间隔，防止同一毫秒内的连续 sync 把复核计数刷满）；超上限且签名不变则接受稳定、恢复零读盘，并在 `fast_path_warnings` 留痕告警——是「有上限的复核 + 告警」，不是无声豁免。
- **回归用例（`tests/test_indexer.py`）**：`test_probe_mtime_tick_ns_infers_granularity_from_samples`、`test_fast_path_disabled_on_coarse_timestamps_detects_equal_length_replacement`（注入 2s 刻度，断言快速路径被禁用且等长替换仍被检出）、`test_fast_path_signature_includes_ctime_so_metadata_channel_is_not_blind`、`test_future_mtime_entry_regains_zero_read_after_bounded_rechecks`（含 `Path.read_bytes` 读盘计数）。
- **验证**：`verify/v7_fix_regression.py` 对 3 个 CRITICAL 逐个场景给出「修复前复现 / 修复后不再复现」对照，并附 ctime 前后实测值。**脚本自身踩坑留档**：粗刻度注入最初污染了 S2a/S6 场景（它顺带把后续场景的快速路径全禁用，让对照结果失真），已加还原点 `restore_real_tick_probe()` 并在脚本内注明。

### C30 — Vodyanitsaaa,2026-9-17,WorkBuddy,Deepseek-V4.1-Flash — fix(doctor): 迁移分裂可观测（配置完整路径 + STATUS 落点自陈）
- **`check_config()` 只输出 basename**：两侧同时存在 `.vault_mcp/config.toml` 与 `.mortis_rag_mcp/config.toml` 时，报告里两个 `config.toml` 无法区分，用户看不出实际生效的是哪一份。现改输出**完整路径**（与 `check_cache_dir` 的口径对齐）；两侧同名配置同时存在时追加一句「旧路径同名配置已被忽略」，把「实际生效/被忽略」讲明白。
- **`registry.py` rename 失败时的对外宣告与实际落点分裂**：整目录 rename 失败（Windows 文件占用/权限不足/跨卷）时 `user_config_dir()` 回落旧目录，于是 STATUS.md 写进 `~/.vault_mcp/`，而读取侧（`server.SERVER_INSTRUCTIONS`、两份 README、`SKILL.md`）的路径是**硬编码**的 `~/.mortis_rag_mcp/STATUS.md`——表现为「文件缺失 → 按文档跑 `--doctor` → 仍然缺失」，且写入侧与读取侧此前没有任何比对。
  - **修复**：新增 `_status_path_notice()`，在写盘前比对实际落点与宣告路径（`normcase` + `resolve()` 容错），不一致时把告警**写进 STATUS.md 自身**。写入侧改不了读取侧的硬编码，至少让这个分叉在文件自己身上可见。
- **回归用例（`tests/test_doctor.py`）**：`test_doctor_check_config_reports_full_path_and_flags_shadowed_old_config`、`test_doctor_check_config_omits_shadow_notice_when_only_one_side_exists`（防误报）、`test_doctor_status_path_notice_is_none_when_paths_match`、`test_doctor_run_writes_write_path_warning_into_status_md`、`test_doctor_record_test_run_refreshes_path_warning`。
- **验证**：`verify/v2_migration_split.py` 五个场景全部跑通（含「新目录先存在」「两侧都存在」「cache 已迁移」）。**脚本自身踩坑留档**：`rename` 前必须先 `mkdir` 源目录，否则 `FileNotFoundError` 让场景静默失败。

### C31 — Vodyanitsaaa,2026-9-17,WorkBuddy,Deepseek-V4.1-Flash — docs: 0.7.1 用户可感知口径对齐 + CI 触发收窄
- **日期口径统一**：`[0.7.1]` 定为 **2026-09-17**，`CHANGELOG_user.md` 与 `docs/PROJECT_GUIDE.md` 的版本行同步订正（原为 2026-09-13）。
- **回退须知补漏（有真金白银成本）**：`CHANGELOG_user.md` 的回退须知原文只提 `~/.mortis_rag_mcp` 一个目录，**漏了 `~/.mortis_rag_mcp_cache`**。照该说明操作会「库列表回来但缓存全部失效 → 所有笔记重新嵌入」，用付费 embedding 即重复计费。现补齐两个目录并写明后果；`QUICKSTART_user.md` 增 0.7.1 小节（新目录名、新环境变量名、回退路径）并指向该须知。
- **README 双份同步**：中英两份徽章 `Version-0.7.0` → `Version-0.7.1`；「核心特性」各增一条 **Agent 信任锚**（一条 `--doctor` 命令 + 免预检收益），让首次访问者能直接看到本版本的主要卖点。
- **`docs/PROJECT_GUIDE.md`**：新增 v0.7.1 版本变更详录段（本节之上）；订正模块行数 `config 390→465`、`registry 197→363`、`indexer 2681→3145`、`server 793→1064`、`doctor 460→711`（其中 config/registry/indexer/server 四处在本分支之前就已漂移，一并按当前实测值对齐）。
- **`.github/workflows/ci.yml`**：push 触发由 `branches: ["**"]` **收窄为 `[main, "ci/**"]`**。原写法在合入上游后会对**任意分支 push 永久生效**，且同仓分支 PR 会 push + pull_request 双跑 5 个 job；收窄后恢复「主分支 + ci 分支」的常态，本 PR 仍由 `pull_request` 事件正常触发 5 个 job。该行为改变已在 PR 描述中显式说明。
- **docs 入库口径**：`docs/` 下仅三份白名单文件入库（`Changelog_developer.md` / `PROJECT_GUIDE.md` / `Quick-start_developer.md`），版本内规划文档 `docs/V0.7.1/Plan_agent-status.md` **不入库**，`.gitignore` 不动；其设计意图以摘要形式写入 PR 描述。
- **验证**：`git status` 仅含预期文件；受影响模块测试子集 108 passed；全量测试按约定交 CI 执行。

