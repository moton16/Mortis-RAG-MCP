# 那么，一切从这里开始吧。
### 如有commit变更，请同步在此说明更新。非必要可以**不读**该文件，阅读"Quick-start_developer.md"即可。只在你需要补充内容以及遇到困难要溯源时进行查证使用。否则就太占上下文了。
### 请在每次commit的开头标注commit提交的用户名（GitHub用户名），时间，若commit为ai直接提交的，请一并输出agent与模型底模（如你的系统提示词有明确告诉你你是什么模型，没有则不需要）的名字。
#### 如：moton16,2026-9-13,Codex,GPT-6-Astra
### 💡 组织说明：同一次任务/同一 Agent 连续提交的多个 commit 已按工作批次（如 `[C0–C7]`）合并整理，并提供了清晰的「涵盖提交」与「代码改动概况」；独立提交（如架构审查报告、重大发版节点、独立专项缺陷修复）继续单独成项，以兼顾溯源精度与阅读效率。
### ⚠️ 必须注意文档分工：面向普通用户的日常更新在根目录 CHANGELOG_user.md，必须用大白话只讲功能增减与体验，绝不允许写技术细节；所有的代码逻辑、实现细节与技术变更流水必须且只能写在本文档（Changelog_developer.md）与 PROJECT_GUIDE.md！

---

### D0 — Vodyanitsaaa,2026-9-13,WorkBuddy(工作区改动，未commit),WorkBuddy,kimi-k3-1 — docs: 开发者文档体系建立
- **新增 `docs/Execution-plan_developer.md`**：v0.7.0 代码级执行方案。P0 检索评测 harness（scripts/eval_search.py + 金标准查询集）→ P1 定向检索路由（registry 加 description 字段 + kb_describe 工具 + MCP initialize instructions + kb_search 描述路由纪律 + fan-out hint + SKILL.md 5.0 判定表化重写）→ P2 PDF/Office 摄取层（**默认关闭**、按需异步 kb_ingest、内嵌 MinerU 双通道客户端 v4/Agent免登、产物收 `.mortis-parsed/`、HTML 表格原子块保护 + 小表转 pipe、pymupdf 兜底）→ P3 可选增强（title/alias boost、per-source 限流，eval 数据决定是否做）。每步含可直接粘贴的代码、测试清单、commit 切分与验收标准。
- **补全 `docs/Quick-start_developer.md`**（原为空胚）：项目概况、架构分层图、索引/检索/tools-call 三条数据流、8 个模块职责与改动坑位表、缓存布局、测试约定、开发约定（文档分工/原子写/fail-closed/Breaking 流程）、常见任务食谱（加工具/改检索/加配置）、上手 checklist。
- **方案依据**：MinerU 官方 API 文档（mineru.net/apiManage/docs）已核实全量端点/参数/错误码（v4 精准 + Agent 轻量免登双通道）；同类项目调研（proofsh/obsidian-notes-rag 链接图谱、gbrain per-page max-pool/title boost/CRAG、parent-document retrieval 业界数据）。
- **未动**：`CHANGELOG_user.md`（按分工只在 release 时改）、`QUICKSTART_user.md`（PDF 章节文案已写入 Execution-plan P2-8，随功能 commit 一并落地，不提前记录不存在的功能）。
- **验证**：docs 纯文档改动，无代码变更；方案内代码均对照现行源码核实（VaultEntry 字段、save() json.dumps 序列化、MarkdownIndexer 构造签名、_fanout_search 返回结构、initialize 响应位置）。

### [C0–C7] — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — feat: v0.7.0 执行方案全链路落地（P0 评测 + P1 定向路由 + P2 摄取层）
> **涵盖提交**：
> - `C0` `test(eval): 检索评测 harness（Hit@K 金标准回归脚本）`
> - `C1` `feat(registry,server): vault description + kb_describe + instructions + 路由纪律`
> - `C2` `feat(server): fan-out hint`
> - `C3` `docs(skill): SKILL.md 5.0 重写`
> - `C4` `feat(config,ingest): IngestConfig + MinerU 双通道客户端`
> - `C5` `feat(ingest): 任务管理器 + 表格处理`
> - `C6` `feat(server,indexer): kb_ingest + hint + 表格保护`
> - `C7` `docs: README/QUICKSTART_user 增补摄取章节`
>
> **代码改动概况**：
> - `scripts/eval_search.py` & `tests/eval/golden_queries.json`：新增 Hit@K 金标准评测脚本与测试集骨架。
> - `mortis_rag_mcp/registry.py`：新增 `VaultEntry.description` 与 `set_description`，兼容旧版 TOML 序列化。
> - `mortis_rag_mcp/server.py`：`kb_init` 接收 description，`_list_vaults` 返回 description；新增 `kb_describe` 工具；`kb_search` 与 `path_prefix` 加入路由纪律；`_fanout_search` 注入定向收窄 hint；新增 `kb_ingest` 工具（submit/status/pending 三态）与文档探测提醒。
> - `skills/mortis-rag-mcp/SKILL.md`：重写为 5.0 5 级判定表，明确反模式与摄取机制。
> - `mortis_rag_mcp/config.py`：新增 `IngestConfig`（默认关闭 `enabled=False`，纯 opt-in）。
> - `mortis_rag_mcp/ingest/mineru.py`：标准库 urllib 实现 MinerU v4 精准与 Agent 免登双通道客户端，含 429 重试与 zip 解包。
> - `mortis_rag_mcp/ingest/tables.py` & `worker.py`：表格块识别与转换（`iter_table_blocks`、小表转 pipe、大表分片）；`IngestManager` 异步队列与 `.mortis-parsed/` 镜像增量落盘（含 pymupdf 兜底）。
> - `mortis_rag_mcp/indexer.py`：表格原子块保护，表格内不截断 chunk，`table_guard` 缓存代际升级。
> - 文档同步：`README.zh-CN.md` 与 `QUICKSTART_user.md` 增补摄取章节说明。

- **核心实现与设计细节**：
  - **P0 检索评测度量衡**：立项先立尺，新增 `eval_search.py`，支持 Hit@K 占位与真实 vault 回归抽测，建立 0.0% 基线。
  - **P1 定向检索路由**：注册表支持描述字段，服务端注入路由纪律与跨库 `fan-out hint`，提示调用方下次携带 `vault_path` 或 `path_prefix`。
  - **SKILL 5.0 重写**：改为 5 级判定表，删除冗余 schema 描述，增加行为反模式与摄取层管理机制说明。
  - **P2 摄取客户端与表格防护**：纯标准库实现 MinerU 双通道客户端，支持网络重试；设计表格原子保护机制，超长表格按 2*chunk_size 分片装箱，防止 chunk 被切碎。
  - **异步任务队列**：`IngestManager` 维护解析任务状态，`.mortis-parsed/` 增量幂等落盘，本地集成 PyMuPDF 离线兜底。
- **验证**：单测由 9 passed 逐步扩展至 50 passed (2.47s)；eval Hit@5 = 0.0%（基线维持）。

### D1 — Vodyanitsaaa,2026-9-13,Kimi Code,Kimi-K3 — docs: 新增 v0.7.0 本地提交对抗审查报告
- 新增 `docs/v0.7.0/Adversarial-review-merged_developer.md`：针对 C0-C7 8 轮 commit 进行全量对抗审查（Adversarial Review / Red Team Review），整合两轮（静态代码审查 + 实测探针深挖），按根因去重、统一为 `D1–D20` 编号。
- 梳理出 3 项 P0（`kb_ingest` 路径逃逸外发、未闭合 `<table>` 三条故障分支、已闭合大表超预算 chunk）、5 项 P1、6 项 P2、6 项 P3，其中 8 条附实测复现数据，并提供代码级修复指引与 14 条验收门禁。

### C8 — moton16,2026-9-13,ZCode,GLM-5.3 — fix(ingest,indexer,server): 全面修复对抗审查 20 项缺陷并达成 14 项门禁
> **代码改动概况**：
> - `mortis_rag_mcp/ingest/worker.py` & `server.py`：前移路径穿越纵深校验；引入 `.ingest.lock` 跨进程文件锁与双检锁；任务完成自动触发增量索引同步。
> - `mortis_rag_mcp/ingest/tables.py` & `indexer.py`：表格按字符预算动态装箱，长单元格细粒度切分；保留物理行号区间；单行超长表拆包重构保持闭合。
> - `mortis_rag_mcp/indexer.py` & `server.py`：scandir 剪枝消除全量哈希假死；前缀召回 `.mortis-parsed` 镜像产物。

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

### [C9–C12] — moton16,2026-9-13,ZCode,GLM-5.3-Flash — chore & docs: v0.7.0 发版收尾、文档体系重构与主页双语规范化
> **涵盖提交**：
> - `C9` `chore: bump version to 0.7.0 and sync CHANGELOG/docs`
> - `C10` `docs: purge root AGENTS/CLAUDE files and streamline READMEs to user perspective`
> - `C11` `docs: make Chinese README default and add README_EN with bilingual switcher`
> - `C12` `docs: add MinerU configuration guides to READMEs and QUICKSTART`
>
> **代码改动概况**：
> - `pyproject.toml`：版本号由 0.6.0 bump 至 0.7.0。
> - `CHANGELOG_user.md`：全量重写为纯大白话用户日志，顶部增设醒目红线警告栏（严禁技术术语堆砌）。
> - 根目录规范化：移除根目录冗余的 `AGENTS.md` 与 `CLAUDE.md`，开发者协作准则收敛于 `docs/Quick-start_developer.md` 与 `docs/PROJECT_GUIDE.md`。
> - `README.md` / `README_EN.md`：将 `README.md` 设为主力中文主页，独立收敛 `README_EN.md`，互设双向切换链接；移除 `README.zh-CN.md`；彻底剔除技术黑话与底层排查记录，聚焦极简上手与产品特性。
> - 摄取指引：在主页文档与 `QUICKSTART_user.md` 中拆分基础与 MinerU 高级配置，详解 API Token 获取、环境变量注入及免登通道差异。

- **验证**：全量测试 231 passed，Hit@5 100%，准备合入与推流。

### C13 — moton16,2026-9-13,Antigravity,Gemini 3.8 Flash — fix(tests): eliminate Windows NTFS mtime collision in test_indexer
> **代码改动概况**：
> - `tests/test_indexer.py`：在连续覆写前增加 `time.sleep(0.02)` 跨越 NTFS 时间戳分辨率窗口，并使用不同长度的更新内容。

- **根因**：`test_indexer.py::test_incremental_add_modify_delete_and_rename` 中连续写入 `# Old\nold content` 与 `# New\nnew content`（两者均为 17 字节）。在云端高速 Windows CI 虚拟机（Azure VM）上，因时钟中断分辨率（15.6ms），两次写入的 `st_mtime_ns` 发生碰撞；结合字节大小完全相同，触发 Fast-Stat 判定为文件未修改而跳过增量更新。
- **修复**：对齐 `test_improvements.py` 与 `test_watch_integration.py` 的处理规范，在连续覆写前增加 `time.sleep(0.02)` 跨越 NTFS 时间戳分辨率窗口，并使用不同长度的更新内容。
- **验证**：本地全量 231 项测试 100% 通过。

### [C14–C16] — Vodyanitsaaa,2026-9-13,Antigravity,Gemini 3.8 Flash — feat & fix(doctor,migration): Agent 信任锚（STATUS.md / doctor.py）+ 用户数据无损原子迁移及安全加固
> **涵盖提交**：
> - `C14` `feat(doctor,migration): Agent 信任锚（STATUS.md / doctor.py）+ 用户数据无损原子迁移`
> - `C15` `fix(doctor,migration): 修复对抗审查发现的表格注入、迁移竞争与门禁防假缺陷`
> - `C16` `fix(doctor,migration): 修复预置目录注册表失联、外部门禁防假、URL伪本地绕过与并发排他锁缺陷`
>
> **代码改动概况**：
> - `mortis_rag_mcp/registry.py`：`user_config_dir()` 实现对 `~/.vault_mcp` 的安全原子改名迁移，`OSError` 回退；并发竞态下二次检测 `new.exists()`；`registry_path()` 增加单文件原子迁移，防止预置目录注册表失联；引入 `vaults.lock`。
> - `mortis_rag_mcp/config.py`：新增 `API_KEY_ENV_VARS`；运行时原子迁移 `~/.mortis_rag_mcp_cache`；建立四级配置路径回退链。
> - `mortis_rag_mcp/doctor.py`：新增模块，实现环境自检、`STATUS.md`（7天免检硬约束）与 `status.json`；Markdown 表格防注入转义；外部模型门禁严格校验 embedding 在场与维度；URL 严格解析 hostname 防伪本地绕过；引入 `status.lock` 进程排他锁。
> - `mortis_rag_mcp/server.py`：`SERVER_INSTRUCTIONS` 注入信任锚与熔断条款；支持 `--doctor` 与 `--quiet`；启动后台异步轻量刷新。
> - `tests/conftest.py` & 单测：`pytest_sessionfinish` 钩子记录成绩，增加宿主前置检测防污染；新增 `tests/test_doctor.py` 与 `tests/test_path_migration.py`（单测增至 27 个）。
> - 规范与版本：`SKILL.md` 升级至 5.1.0；`pyproject.toml` bump 至 0.7.1。

- **核心实现与对抗加固细节**：
  - **路径与配置无损原子迁移（~/.vault_mcp* -> ~/.mortis_rag_mcp*）**：纯原子 `os.rename` 迁移，绝不使用 `shutil.move`；并发竞争下败者直接复用胜者新目录；当新目录预先存在但缺少 `vaults.toml` 时，单文件安全迁移老注册表，杜绝知识库注册归零。
  - **Agent 信任锚（STATUS.md / doctor.py）**：给 Agent 的硬约束规范（7 天内 VALID 禁止环境预检；失效只跑一次 `--doctor`；仍失败熔断报错用户）；表格生成时过滤换行、转义管道符，防 Markdown 表格注入；外部模型 `mode="external"` 强制探活并校对返回维度，杜绝虚假标绿；`urllib.parse.urlsplit` 严查环回 hostname，封堵路径带 `localhost` 伪装免密；引入 `status.lock` 防止 CLI 与后台刷新并发写入冲突。
  - **单测宿主环境隔离与防污染**：`tests/conftest.py` 检测隔离环境变量，宿主未初始化时单测禁止无中生有落盘 `STATUS.md`；`quiet=True` 场景禁止触碰 `sys.stdout.reconfigure` 规避主线程竞争。
- **验证**：单测套件由 12 个扩充至 27 个对抗回归测试，全量 264 passed, 2 skipped 全部通过。

### [C17–C23] — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix(indexer): Windows CI Fast-Stat 增量同步漏检排查与可信判据收敛（50ms 安全余量）
> **涵盖提交**：
> - `C17` `fix(indexer): 修复等长内容替换时增量同步漏检（Windows CI 捕获）`
> - `C18` `fix(indexer): Fast-Stat 的 racily clean 判据与真正有鉴别力的回归测试`
> - `C19` `fix(indexer): 兜底锚点改为扫描起点，消除零读盘契约的时序抖动`
> - `C20` `fix(indexer): 判据只在有文件系统锚点时才生效，不再跨时基比较`
> - `C21` `fix(indexer): 判据改为同源进程时钟自比，默认配置下也生效`
> - `C22` `fix(indexer): 登记时刻改到读盘之后，避开写入-观测同刻度窗口`
> - `C23` `fix(indexer): 可信判据引入 50ms 安全余量，堵死同刻度漏检窗口`
>
> **代码改动概况**：
> - `mortis_rag_mcp/indexer.py`：实现并收敛可信判据 `_fast_path_is_trustworthy(source, mtime_ns)`；引入进程内字典 `_stat_seen_ns: dict[str, int]` 记录观测时刻；确立安全余量 `_MTIME_TRUST_MARGIN_NS = 50ms`；登记签名使用读盘前 `fast_sig` 保障自愈。
> - `tests/test_indexer.py`：零读盘契约测试重构为三轮严格断言（第 2 轮允许至多一次复核读盘，第 3 轮严格断言零读盘）；构造确定性 racily clean 回归测试。
> - `docs/PROJECT_GUIDE.md`：Fast-Stat 小节补记判据演进与数学论证。

- **缺陷根因与排查演进（7 轮递进收敛）**：
  - **初始缺陷（C17）**：Windows 文件时间戳粒度受系统时钟中断（约 15.6ms）限制。等长内容替换（如 18 字节 `"old content"` -> `"new content"`）若落在同一刻度内，`mtime_ns` 与 `size` 双双不变，Fast-Stat 快速路径误判为「未修改」而跳过，旧 chunk 静默残留。
  - **测试鉴别力与锚点尝试（C18–C20）**：C18 修正回归测试顺序，以索引缓存文件 `st_mtime_ns` 为锚点精确复现 Git racily clean；C19 改兜底锚点为扫描起点但在 CI 产生时序抖动；C20 证实进程内时钟（`time.time_ns`）与文件系统时间戳属**两个不同时基**，跨时基比较在粒度较粗时必然因取整失真。
  - **同源时钟自比与刻度量化（C21–C22）**：C21 放弃跨时基，改为同源进程时钟自比（引入 `_stat_seen_ns`）；C22 通过临时诊断测试捕获 Windows 下「写入后紧接同步」约有 **1/30** 概率让 mtime 与观测时刻落入同刻度（一出生即被判 racily clean），并将记录时刻推迟至 `read_bytes + sha256` 之后。
  - **终局方案（C23 - 用户决策裁决）**：采用「一次性精确校验后固化」——引入 `_MTIME_TRUST_MARGIN_NS = 50ms`（> 2 倍 Windows 最坏刻度 15.6ms）。
- **终局方案与可靠性论证**：
  - **判据公式**：`mtime_ns < seen_ns - _MTIME_TRUST_MARGIN_NS`，`seen_ns` 为最近一次通过内容级验证（read + sha256）的进程时钟时刻。
  - **归纳论证**：若条目登记时满足 `seen - mtime > MARGIN`，则此后任何写入落盘时间戳必然满足 `T2 >= seen - tick > mtime`，写入必然推动 mtime、导致签名失配走正常校验，彻底杜绝漏检。登记时余量不足的条目强制复核读盘，复核将 `seen` 推后，随时间流逝满足余量后自动固化恢复零读盘。
  - **与 Git 的差异**：Git 依赖索引文件 mtime 锚点，Mortis 依靠进程时钟 + 余量，确保在 `cache.enabled=False`（默认配置）下完全生效。
- **验证**：索引子集 15 passed，多库索引 58 passed；反向验证（禁用判据则回归测试必 FAIL）确认测试具备高度鉴别力。

### [C24–C27] — Vodyanitsaaa,2026-9-14,WorkBuddy,Deepseek-V4.1-Flash — fix & chore(review): 对抗审查修复（锁重入/fail-closed/不真导入）与分支清理
> **涵盖提交**：
> - `C24` `fix(review): 对抗审查修复——锁重入按路径判定 + 沿用健康度标注陈旧度`
> - `C25` `chore: 诊断脚手架与误提交清理（4 个提交合并记录）`
> - `C26` `fix(review): 环回判定改 fail-closed + 启动期不再真导入 numpy + 补齐本分支开发日志`
> - `C27` `test(doctor): 可选依赖探测补两条打桩用例，覆盖 CI 走不到的「已装」分支`
>
> **代码改动概况**：
> - `mortis_rag_mcp/registry.py`：修复 `_process_file_lock` 锁重入判定，按规范化路径记录 `held` 集合，杜绝跨进程互斥被静默跳过。
> - `mortis_rag_mcp/doctor.py`：沿用探活结果标注陈旧分钟数并用正则剥离旧后缀；环回判定收紧为 fail-closed（白名单 + `ipaddress` 严格解析，封死 `127.attacker.com` 绕过）；`check_optional_deps()` 改用 `find_spec` + `version`，消除启动期导入 numpy 抢 GIL 的数百毫秒开销。
> - `tests/conftest.py`：宿主数据目录防污染，取消间接调用改名副作用，直接拼装目标路径。
> - 清理与单测：移除临时 Windows 时序诊断测试与 `_msg.txt`；新增 5 个专项回归与打桩测试。

- **核心修复细节与推翻误报**：
  - **F3 锁重入按路径判定**：原实现只看 `lock_depth` 不看锁路径，导致持 A 锁再取 B 锁直接放行。修复后按路径隔离重入。
  - **F5 沿用健康度陈旧标注**：标明「沿用 N 分钟前的全量探测结果，本次未重新探活」，tests 项陈旧度同步标注。
  - **D1 环回判定收紧为 fail-closed**：原 `host.startswith("127.")` 会误判 `127.attacker.com` 为本机。现仅白名单或经 `ipaddress` 严格解析的 loopback/全零地址才视为本机免密。
  - **D3 启动期不真导入可选依赖**：后台刷新线程改用 `importlib.util.find_spec` 探测，避免 numpy 导入抢 GIL 影响 stdio JSON-RPC 握手。
  - **实测推翻的两条误报留档**：「mtime 回拨 + 等长替换击穿 50ms 判据」实测被余量拦截；「DEFAULT_CACHE_DIR 是死常量」实测仍在使用。
- **验证**：24 个端点形态全部符合预期；`__import__` 间谍确认启动期零模块泄漏导入；doctor 沿用标注不叠加。

---

> 以下 C28–C31 来自放行前终审（12 条 findings：3 CRITICAL + 9 INFORMATIONAL）的修复窗口，四项一次修完，不留发版后独立分支的欠账。

### [C28–C31] — Vodyanitsaaa,2026-9-17,WorkBuddy,Deepseek-V4.1-Flash — fix & docs: v0.7.1 终审 12 条 findings 一次收口（Fast-Stat实测刻度/ctime签名/信任锚转义/迁移分裂可观测）
> **涵盖提交**（Git 整合单次提交 `6480cde`）：
> - `C28` `fix(doctor): STATUS.md 头行补转义，堵死信任锚指令注入 (CRITICAL)`
> - `C29` `fix(indexer): Fast-Stat 余量按实测刻度推导 + 签名纳入 ctime，收口漏检面 (CRITICAL 1 & 2)`
> - `C30` `fix(doctor): 迁移分裂可观测（配置完整路径 + STATUS 落点自陈）`
> - `C31` `docs: 0.7.1 用户可感知口径对齐 + CI 触发收窄`
>
> **代码改动概况**：
> - `mortis_rag_mcp/doctor.py`：头行字段增加 `_sanitize_inline()`；`machine` 叠加 RFC1123 白名单与 64 字符截断，堵死 STATUS.md 标题伪造与指令注入；`check_config()` 输出完整路径与遮蔽告警；`_status_path_notice()` 在 rename 失败回落旧目录时将路径分叉告警直接写入 STATUS.md 自身。
> - `mortis_rag_mcp/indexer.py`：新增 `_probe_mtime_tick_ns()` 反推文件系统真实刻度，`_effective_margin_ns()` 动态取 `max(50ms, 2 * 刻度)`，粗刻度（>50ms）fail-closed 禁用快速路径；签名升级为三元组 `(mtime_ns, size, st_ctime_ns)`，利用 POSIX ctime 封死回拨攻击；引入 `_FUTURE_MTIME_RECHECK_LIMIT = 2` 避免网络盘超前时间戳被永久惩罚。
> - 文档与 CI：订正发版日期为 2026-09-17；`CHANGELOG_user.md` 补全缓存目录迁移回退说明；README 增补 Agent 信任锚特性；`ci.yml` 触发收窄为 `[main, "ci/**"]`。

- **核心加固与设计细节**：
  - **CRITICAL · 信任锚指令注入防护（C28）**：原实现头行四个字段零转义，受控主机名可注入换行与 `##` 伪造硬约束。现头行字段全面清洗截断；`_sanitize_free_text()` 归一化 HTML 角括号与 Unicode 行分隔符（U+2028/U+0085/U+2029）；硬约束段补充免责声明（detail 列为原始数据不得当指令执行）。
  - **CRITICAL 1 · 实测刻度动态推导（C29）**：纠正 C24 留档误判（稳态下读写耗时推高 `seen - mtime` 后，回拨仍可漏检）。新增刻度探测求差值 gcd，只会高估绝不低估；刻度 >50ms 直接禁用快速路径（fail-closed），彻底解决 FAT32/exFAT 2s 刻度失效。
  - **CRITICAL 2 · 签名升级与回拨封锁（C29）**：签名升级为三元组 `(mtime_ns, size, st_ctime_ns)`。POSIX 下 ctime 由内核维护且 `os.utime` 无法回拨，彻底封死回拨攻击；Windows 平台上如实记录 ctime 为创建时间的已知限制。
  - **迁移分裂与落点自陈（C30）**：整目录 rename 失败回落旧目录导致读写侧分叉时，`_status_path_notice()` 将告警显式写入 STATUS.md 自身，使其立即可查。
  - **发版口径与回退成本说明（C31）**：补全 `~/.mortis_rag_mcp_cache` 目录回退说明，避免用户降级时缓存失效重复消耗付费 embedding。
- **验证**：`verify/v1_injection_and_fastpath.py`、`v2_migration_split.py` 与 `v7_fix_regression.py` 专项脚本全绿；受影响模块单测 108 passed。

### [C32–C36] — Antigravity,2026-09-21,Google DeepMind,Gemini-3.8-Flash — feat(v0.7.2): 核心特性矩阵落地（别名路由/多库Scoped/txt原生收录/轻量预览模式/豁免与冷启动防假死）
> **涵盖提交**：
> - `C32` `feat(registry,server): 库名别名自然路由 (I6) 与 Scoped 定向多库检索 (I1)`
> - `C33` `feat(indexer,server): 纯文本 .txt 原生收录与未收录资产感知 (I4) 对齐 3元组 Fast-Stat`
> - `C34` `feat(indexer,server): 检索 Payload 瘦身与轻量高光预览模式 preview=True (I5)`
> - `C35` `feat(indexer,server): 豁免规则联动清理与落盘 (I3/F-02) 及冷启动首检防假死真守护 (I2/F-03/04)`
> - `C36` `docs(skill,tests): 同步 SKILL.md 5.2 二段式检索纪律与 v0.7.2 全量回归通过`
>
> **代码改动概况**：
> - `mortis_rag_mcp/registry.py` & `server.py`：`get_by_name` 大小写不敏感解析，`_resolve_vault_path` 别名优先，消除拼接 Windows 漫长物理路径的摩擦；`_fanout_search` 支持 `target_vaults` 数组/逗号字符串精准圈定多库；更新 Solo 契约（显式点名合法召回，仅盲搜排除）；抹平 Windows 盘符大小写；完善 `kb_search` Schema。
> - `mortis_rag_mcp/indexer.py`（.txt 收录与章节标题）：扩展 `_INDEXABLE_TEXT_EXTS = frozenset({".md", ".txt"})`，统一分块与 FTS 索引；引入章节标题门禁（<=60 字符，无句末标点，无逗号/引号，防小说正文误判）；缓存版本 chunker 升至 5；沙箱放行 `.txt`；`stats()` 返回 `skipped_unsupported` 统计。
> - `mortis_rag_mcp/indexer.py` & `server.py`（轻量预览与二段式检索）：实现 `_extract_snippet` 高光窗口提取算法；`Chunk.to_dict()` 扁平化外露 `heading`, `start_line`, `end_line`；`preview=True` 时仅返回高光摘要与字符数，单块 payload 压缩 70%+；`kb_search` 全面透传 `preview`。
> - `mortis_rag_mcp/indexer.py` & `server.py`（豁免解耦与冷启动守护）：`add_exemption_pattern` 彻底解耦同步阻塞（80s -> <20ms），即时内存剪枝（弹出 chunks/signatures/stat_cache/failed_files）、清理 FTS 与向量并写盘；引入进度状态机 `_sync_state` 与 `_sync_progress`；实现 `try_sync_with_guard(timeout=1.5)` 守护机制，主线程禁止同步承担重度构建，返回带 `progress` 与 `retry_after: 3` 的友好响应；`kb_read` 读取降至 2ms。
> - `skills/mortis-rag-mcp/SKILL.md`：升级至 5.2.0，重构 Agent 交互纪律（别名路由、多库定向、preview 初筛与二段式精读、后台 indexing 状态识别）。

- **验证**：全量单测套件由 279 扩充至 **293 passed, 4 skipped in 20.16s**；黄金检索评测集 `scripts/eval_search.py` 维持 **Hit@5: 100.0%, MRR@5: 1.000**，检索质量零衰退。

### C37 — moton16,2026-09-21,Antigravity,Gemini 3.8 Flash — chore(release): 版本号 bump 至 0.7.2 + 版本一致性守卫 + 用户侧四处文档同步
> **代码改动概况**：
> - `pyproject.toml` 与 `server.py`（`SERVER_INFO`）：统一由 `0.7.1` bump 至 `0.7.2`。
> - `tests/test_version_sync.py`：新增测试，静态守卫 `pyproject.toml`、`SERVER_INFO` 与 `CHANGELOG_user.md` 之间的版本强一致性。
> - 用户文档同步：`CHANGELOG_user.md` 增设 `[0.7.2]` 条目；`README.md` 与 `README_EN.md` 同步徽章与特性速查；`QUICKSTART_user.md` 增设新用法速查。

### [C38–C41] — moton16,2026-09-21,Antigravity,Gemini 3.8 Flash — fix: v0.7.2 缺陷收口与边界加固（Scoped容错/异步豁免/txt可见性/中文高光与进度状态机）
> **涵盖提交**：
> - `C38` `fix(server): vault_paths 字符串形态不再静默回落全局盲搜（F-01 收口）`
> - `C39` `fix(indexer): kb_exempt 剩余 3 个动作解除全量同步阻塞（I3 收口）`
> - `C40` `fix(indexer,server): .txt 豁免可见性 / skipped_unsupported 排除可摄取格式 / 描述文案订正`
> - `C41` `fix(indexer): 中文查询高光窗口与进度状态机（I5/I2 收口）`
>
> **代码改动概况**：
> - `mortis_rag_mcp/server.py`：`_parse_vault_targets` 兼容逗号分隔字符串，显式传空时抛出 `ValueError` 报错，杜绝因类型不匹配静默降级为全库盲搜的高危扩权；更新 `kb_list_files` 与 `kb_read` 工具描述。
> - `mortis_rag_mcp/indexer.py`：
>   - 抽取公共剪枝方法 `_prune_ignored_sources`，将 `remove_exemption_pattern` 与 `set_file_exemption` 转为非阻塞后台静默执行（前台耗时 <20ms，返回体追加 `"sync": "background"`）；
>   - 新增 `_iter_vault_text_files`，使 `.txt` 豁免状态完全可见，返回体新增 `total_text_files`；
>   - `stats()` 的 `skipped_unsupported` 显式排除 `INGEST_EXTS`，消除与 `kb_init` 可摄取统计打架；
>   - 中文高光候选词算法 `_candidate_terms`（2-gram 滑窗提取候选词并匹配正文最高频词居中），解决中文长句无空格导致预览窗口退化为首部 150 字的问题；
>   - 完善 `_sync_progress` 状态机，新增 `phase` 字段与 `_sync_state` 同步，扫描循环实时递增 `files_done`，避免冷启动时前端观测进度恒为 0。
> - 测试补充：在 `test_scoped_search.py`、`test_anti_contention.py`、`test_txt_indexing.py` 与 `test_preview_mode.py` 中增设 8 组专项回归断言。

### [C42–C43] — moton16,2026-09-22,Antigravity,Gemini 3.8 Flash — fix & release(0.7.3): 多库/Solo 场景参数解析修复 (GitHub Issue #2) 与 v0.7.3 发版
> **涵盖提交**：
> - `C42` `fix(server,registry): 修复多库/solo场景定向检索与状态查询参数失效缺陷（GitHub Issue #2）`
> - `C43` `chore(release): 版本号 bump 至 0.7.3 + 用户侧与开发者文档同步`
>
> **代码改动概况**：
> - `mortis_rag_mcp/server.py`：
>   - MCP 协议传参多层解包：在 `handle` 与 `call_tool` 中兼容 `input`、`args`、`parameters`、顶层扁平参数以及 JSON 字符串形态参数；
>   - 新增 `_camel_to_snake` 转换，全面兼容客户端 CamelCase 参数（`vaultPath` 等）；
>   - `_parse_vault_targets` 优先级平滑回退（`vault_paths` 传空时自动回退 `vault_path`）；
>   - 单库工具别名兜底：`_indexer_for` 提取 `vault`、`vault_name`、`path` 等别名，彻底修复多库环境下调用 `kb_stats` 误报 `multiple vaults registered` 以及定向检索 solo 库失败的问题。
> - `mortis_rag_mcp/registry.py`：`get_by_name` 剥离首尾引号斜杠，支持按目录物理名称（`Path(e.path).name`）自动回退匹配。
> - 发布与文档：`pyproject.toml` 与 `SERVER_INFO` bump 至 0.7.3；`CHANGELOG_user.md` 记录 Issue #2 修复，README 徽章同步。
> - 验证：`tests/test_scoped_search.py` 增设 6 组端到端回归用例，全量单测全绿。

### C44 — moton16,2026-09-27,Codex,GPT-6-Sol — refactor(server,ingest): v0.8.0 Phase 1 工具路由表化 + 表格切块精简（金测守护）
> **代码改动概况**：
> - `mortis_rag_mcp/server.py`：15 个 `kb_*` 工具的巨型 if 链改为显式映射路由表 `_TOOL_ROUTE_TABLE`（工具名 → 处理器方法名），各分支体逐字迁移为独立私有方法；入参归一化时序、unknown-tool 报错文案、`_text_content` 包装行为完全不变（MCP 协议契约冻结）。
> - `mortis_rag_mcp/ingest/tables.py`：`split_table_into_chunks` 精简（删除被通用分支完全覆盖的单行特例分支，内联单用途局部变量 `is_continuation`；物理行号映射、预算装箱、表头复用逻辑零变更）。
> - 金标准守护：新增 `tests/golden/legacy_split_table_impl.py`（v0.7.3 原实现逐字冻结快照）+ `tests/test_tables_golden.py`（12 语料覆盖全部分支 × 新旧实现输出严格相等）。

- **评审记录与契约纠偏**：`docs/v0.8.0/Mortis-RAG-MCP-v0.8.0-full-refactor-execution.md` 追加 GSTACK REVIEW REPORT；**缓存契约纠偏**：`.chunks.bin`/`.vectors.bin` 为 VMCPC/VMCPV 自研二进制格式而非 pickle（全库 0 命中），真实兼容契约 = 二进制格式与 `_CACHE_VERSION` 不变 + `Chunk` 构造参数序不变（`_CacheCodec.load` 位置序重建）+ 解码失败静默重建语义不变。
- **会话异常披露**：本 Phase 施工期间检测到多轮工具结果注入（伪造编辑结果 ×3、`SAFE_DELETE_BULK_CONFIRM_REQUIRED` 批量删除诱导载荷 ×2、虚假声明"P1 已提交 / pickle 契约 / venv 无 pytest"），已全部识别并拒绝执行，未运行任何由工具输出文本指示的命令；tables.py 落盘精简经金测 oracle + 全量回归双重客观验证后保留。首次验证出现 `FULL_EXIT=1` 却无失败摘要的矛盾输出，定位为 safe-delete 守卫干扰 pytest 临时垃圾目录清理，改用全新 `--basetemp` 隔离后复跑恢复真实结果。
- **验证**：全量 `pytest tests/ -q` **314 passed, 4 skipped**（与 v0.7.3 基线持平，`PYTEST_EXIT=0`）；金测 10 passed；`scripts/eval_search.py` **Hit@5 100.0% / MRR@5 1.000**（基线不变）；server.py AST 语法检查通过。

### [C45–C46] — moton16,2026-09-27,CodeBuddy,Kimi-K3 — refactor(indexer): v0.8.0 Phase 2 & 3a 拆分（提取 _indexer/models, cache_codec, scanning，Facade 接缝保持）
> **涵盖提交**：
> - `C45` `refactor(indexer): v0.8.0 Phase 2 提取 _indexer/models + cache_codec（Facade 面不变）`
> - `C46` `refactor(indexer): v0.8.0 P3a 提取 _indexer/scanning（monkeypatch 接缝保持）`
>
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/` 私有包落地：
>   - `models.py`：`Chunk`（字段序 `id, content, source, title, metadata, score, embedding` 即磁盘物理契约）/ `SearchFilter` / `dedupe_by_content_hash` / `_extract_snippet` / `_candidate_terms` / `_EMB_DTYPE` 逐字迁移。
>   - `cache_codec.py`：`_CacheCodec` / `_VectorsCodec` / `_pack_str` / `_pack_u32` 逐字迁移，`VMCPC`/`VMCPV` 格式与版本号不变。
>   - `scanning.py`：`IgnoreMatcher` / `_probe_mtime_tick_ns` / Fast-Stat 常量块 + `scandir_indexable_files` / `ignored_name` / `source_rel` 纯函数。
> - `mortis_rag_mcp/indexer.py` 转为 Facade 面并持续瘦身：
>   - 行数由 3487 经过两轮受控切除降至 2975 行（P2 切除 371 行，P3a 切除 171 行 + 插桩 19 行）；
>   - 公开导入面 100% 保持（通过显式 re-export，外部调用零断裂）；
>   - 原位保留 Facade 包装函数与薄委托（`_probe_mtime_tick_ns` 作为唯一调用路径，保障测试 monkeypatch 依然能被内部观察到）；
>   - 判据族方法因操作实例状态留守 Facade。
> - 金测体系与接缝锁闸：
>   - 新增 `tests/fixtures/golden_vault/` 5 文件金标准 + `tests/test_golden_v073.py` 快照比对（守护重构全程切块行为一致性）；
>   - 新增 `tests/test_cache_codec_roundtrip.py` 4 个专项闸门（Chunk 字段序锁、双层缓存字节级恒等、损坏缓存静默兜底）；
>   - 新增 `tests/test_facade_seam.py` 2 个接缝测试（验证模块属性补丁能被底层逻辑感知）。

- **契约纠偏与手术安全**：
  - 原计划 pickle `__module__` 条款经双声部评审证伪弃用（全库 0 命中 pickle）；真实兼容契约 = `VMCPC`/`VMCPV` 二进制格式 + `_CACHE_VERSION` 不变 + **`Chunk` 位置序构造**（`_CacheCodec.load` 按位重建）+ 解码失败静默重建语义，由新测试逐条锁定。
  - 机械切除脚本带边界锚点断言（任何一行与读取快照不符即中止，杜绝静默错切），切除后 AST 语法检查通过；自查修复 scanning.py 遗漏的 `import math`。
- **验证**：全量单测由 314 增至 **322 passed, 4 skipped**（基线 314 + 6 P2 + 2 接缝，`PYTEST_EXIT=0`）；eval **Hit@5 100.0% / MRR@5 1.000**（基线不变）；金测快照 5 文件 11 chunks 与改造前逐字一致；切除行数算术闭合（3127 − 171 切 + 19 插桩 = 2975）。

### C47 — moton16,2026-09-27,Antigravity,Gemini 3.8 Flash — refactor(indexer): v0.8.0 P3b 提取 _indexer/chunking（接缝注入与薄委托）
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/chunking.py` 落地：
>   - 模块级正则与常量：`_HEADING_RE`、`_BLOCK_IGNORE_START/END`、`_MD_IMAGE_RE`、`_WIKI_IMAGE_RE`、`_FENCE_START_RE`、`_FENCE_RE`、`_IMAGE_EXTS`、`_INDEXABLE_TEXT_EXTS`、`_CHAPTER_HEADING_RE`。
>   - 模块级纯函数：`_is_chapter_heading`、`_image_note`、`_image_notes_for_line`、`_inject_image_notes`。
>   - 切块管线算法：`frontmatter`、`is_frontmatter_exempt`、`strip_ignored_blocks`、`clean_heading`、`extract_title`、`overlap_tail`、`new_chunk`、`make_chunks`、`chunk_file`。
> - `mortis_rag_mcp/indexer.py` Facade 瘦身与接缝维护：
>   - 行数由 2975 行降至 2647 行（净减 328 行）；
>   - 显式 re-export 保持 `from mortis_rag_mcp.indexer import _inject_image_notes, _HEADING_RE, ...` 导入兼容；
>   - `_chunk_file` Facade 委托在调用时将 `_inject_image_notes` 动态注入底层函数，保障既有测试对 Facade 模块符号的 monkeypatch 依然可达（D2 决策）；
>   - 原位保留 `_chunk_file`/`_frontmatter`/`_is_frontmatter_exempt`/`_strip_ignored_blocks`/`_clean_heading`/`_title`/`_make_chunks`/`_overlap_tail`/`_new_chunk` 薄委托方法。
> - 接缝回归测试：
>   - 新增 `tests/test_chunking_seam.py`（3 个测试：re-export 符号完备性、monkeypatch 可达性、Facade 薄委托方法签名兼容）。
> - **验证**：全量单测增至 **325 passed, 4 skipped**（基线 322 + 3 接缝，`PYTEST_EXIT=0`）；金测 `test_golden_v073.py` 严格相等；eval **Hit@5 100.0% / MRR@5 1.000**。

### C48 — moton16,2026-09-27,Antigravity,Gemini 3.8 Flash — refactor(indexer): v0.8.0 P3c 提取 _indexer/sync_engine（Eng协议边界与锁序同一性保障）
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/sync_engine.py` 落地（D7 用户裁决最高风险项受控切除）：
>   - 核心自由函数 `run_sync(owner)` 承接 `_sync_locked_impl` 增量同步对账主逻辑（前置条件：调用方已持 `_sync_lock`）；
>   - 辅助纯函数与向量处理逻辑搬移：`ensure_disk_vectors_migrated`、`flush_vectors_to_disk`、`chunk_has_vector`、`reuse_vectors_by_content_hash`、`embed_missing`、`embedding_changed_state`、`embed_one_file`、`_to_emb`；
>   - 状态管理严格遵守 Eng B.3 协议：22 个可变状态属性留守主类实例，引擎经 `owner.*` 访问并就地变异；运行时零反向依赖 Facade（仅 TYPE_CHECKING 导入）。
> - `mortis_rag_mcp/indexer.py` Facade 关键边界留守与瘦身：
>   - 行数由 2647 行降至 2289 行（净减 358 行）；
>   - 坚守 Facade 留守底线：`sync()`/`try_sync_with_guard()` 锁获取与首启线程不变；全部 `_cache_lock` 获取点不变；Fast-Stat 判据族与 `_fts_*` 家族不变；
>   - 原位保留 `_sync_locked_impl`、`_ensure_disk_vectors_migrated`、`_flush_vectors_to_disk`、`_chunk_has_vector`、`_reuse_vectors_by_content_hash`、`_embed_missing`、`_embedding_changed_state`、`_embed_one_file` 薄委托方法。
> - 专项回归测试守护：
>   - 新增 `tests/test_sync_engine.py`（4 个测试：`owner._chunks` 与 `_stat_cache` 就地变异字典同一性断言、`_sync_lock -> _cache_lock` 严格锁序拦截断言、线程名契约校验、薄委托兼容调用）。
> - **验证**：全量单测增至 **329 passed, 4 skipped**（基线 325 + 4 P3c 专项，`PYTEST_EXIT=0`）；eval **Hit@5 100.0% / MRR@5 1.000**；金测 `test_golden_v073.py` 严格相等。

### C49 — moton16,2026-09-27,Antigravity,Gemini 3.8 Flash — refactor(indexer,server): v0.8.0 Phase 4 提取 _indexer/search + _server/fanout 与 search_dispatch（检索语义与Oracle恒等）
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/search.py` 落地：
>   - 单库检索引擎主逻辑 `search_single_vault` 与 `SearchEngine` 只读组件抽象；
>   - 搬迁检索引擎底层组件：`rerank_chunks`、`_to_emb`、`cosine`、`query_tokens`、`fts_query`、`hybrid_rank`、`semantic_rank`；
>   - 检索常量集中管理：`_RRF_K`、`_WORD_RE`、`_ASCII_RE`、`_CJK_RE`、`_SHORT_STOPWORDS`；numpy 保持惰性导入与标量余弦安全降级。
> - `mortis_rag_mcp/_server/` 私有包落地：
>   - `search_dispatch.py`：承接单库、Scoped 多库及全局检索的入参归一化、参数类型校验、冷启动守护与分发调度；
>   - `fanout.py`：承接跨库聚合、单次 query embedding 计算、库级权重乘算与恢复、跨库去重、全局与分组截断分页；
>   - 坚守活实例契约：直接操作 `server._indexers`，实时观测 `_chunks` 与 `_sync_progress`，绝无静态快照脏读。
> - `mortis_rag_mcp/indexer.py` 与 `server.py` 瘦身：
>   - `indexer.py` 由 2289 行降至 2035 行（净减 254 行）；原位保留 `search`、`_fts_query`、`_hybrid_rank`、`_query_tokens`、`_semantic_rank`、`_cosine` 薄委托，显式 re-export `rerank_chunks` 与 `_to_emb`；
>   - `server.py` 由 1359 行降至 1126 行（净减 233 行）；`_fanout_search` 与 `_kb_search` 委托至 `_server/`。
> - Oracle 等价与回归测试：
>   - 新增 `tests/test_search_oracle.py`（9 个测试：常驻 Chunk `score` 原地不修改断言、SearchEngine 与 Facade 产物 `(id, score, source)` 严格恒等、server 端到端委托校验、向后兼容静态方法测试）。
> - **验证**：全量单测增至 **338 passed, 4 skipped**（基线 329 + 9 P4 专项，`PYTEST_EXIT=0`）；eval **Hit@5 100.0% / MRR@5 1.000**；金测 `test_golden_v073.py` 严格相等。

### C50 — moton16,2026-09-27,Antigravity,Gemini 3.8 Flash — refactor(indexer): v0.8.0 Phase 5 提取 _indexer/snapshot + exemptions + watch（生命周期解耦与死代码清理）
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/snapshot.py` 落地：
>   - 搬迁快照打包与恢复引擎：`export_snapshot`、`_export_snapshot_locked`、`import_snapshot`、`_import_snapshot_locked`、`_import_chunks_member`、`_import_vectors_bin_member`、`_import_vectors_sqlite_member`、`_decode_member`、`_replace_live_file`、`_recreate_fts`；
>   - 常量与安全门禁：`_SNAPSHOT_FORMAT`、`_SNAPSHOT_VERSION`、`_SNAPSHOT_MEMBERS` 白名单精确匹配（天然防御 Zip Slip 穿越）、解压炸弹与压缩比上限拦截、SQLite 向量后端异常安全回滚。
> - `mortis_rag_mcp/_indexer/exemptions.py` 落地：
>   - 搬迁豁免规则管理：`iter_vault_text_files`、`get_exemptions`、`_prune_ignored_sources`、`add_exemption_pattern`（唯一活版本）、`remove_exemption_pattern`、`check_exemption`、`set_file_exemption`；
>   - 状态联动清理：集中清理 `_chunks`、`_signatures`、`_stat_cache`、`_stat_seen_ns`、`_stat_confirmations`、`fast_path_warnings`、FTS、向量记录及 `failed_files`；
>   - 剔除历史遗留缺陷：彻底删除原 `indexer.py:1120` 无 return 的死代码版 `add_exemption_pattern`，锁定返回 dict（含 `sync: background`）活版本契约。
> - `mortis_rag_mcp/_indexer/watch.py` 落地：
>   - 搬迁目录事件监听与防抖调度控制器：`start_watching`、`stop_watching`、`_start_fs_scheduler`、`_fs_scheduler_loop`、`_on_fs_events`、`_fs_event_matters`、`_run_sync_quietly`、`_native_watch_loop`、`_watch_loop`、`_quick_signatures`；
>   - 调度常驻与线程名契约：保持 "vault-watch-native"、"vault-fs-debounce"、"exempt-sync" 线程名不变；优雅停启无孤儿线程残留。
> - `mortis_rag_mcp/indexer.py` 终极瘦身：
>   - 行数由 2035 行降至 1238 行（净减 797 行，累计较 v0.7.3 3487 行降低 64.5%）；
>   - 原位保留薄委托方法与常量 re-export，保留线程名契约锚点。
> - 生命周期专项回归测试：
>   - 新增 `tests/test_p5_lifecycle.py`（3 个测试：快照导入 Zip Slip 路径穿越白名单拦截、豁免增删返回值契约与 8 项状态级联清理、Watcher 启动与优雅停启无孤儿线程）。
> - **验证**：全量单测增至 **341 passed, 4 skipped**（基线 338 + 3 P5 专项，`PYTEST_EXIT=0`）；eval **Hit@5 100.0% / MRR@5 1.000**；金测 `test_golden_v073.py` 与缓存 roundtrip 严格恒等。

### C51 — moton16,2026-09-27,Antigravity,Gemini 3.8 Flash — chore & docs: v0.8.0 收口、Facade 导出面冻结、全量文档同步与版本发版
> **代码改动概况**：
> - 接口与错误边界全面收口：
>   - 确认 `_normalize_call_arguments` 保持为 15 个工具入参归一与数值夹取的唯一合法入口；
>   - 审计全库无裸 `except Exception: pass` 吞异常逻辑，关键降级点（FTS、向量后端、reranker）返回明确回退与可观测记录；
>   - `_safe_path` 严格执行后缀白名单与 root 越界拦截 fail-closed。
> - Facade 导出面冻结测试：
>   - 新增 `tests/test_facade_freeze.py`（4 个测试：`mortis_rag_mcp.__all__` 严格维持 7 项不变、Facade 公开 API 与 30+ 历史测试锚点全员在场、MarkdownIndexer 核心方法契约完整、私有子模块 `_indexer/` 与 `_server/` 运行时零反向导入 Facade）。
> - 全量文档体系同步：
>   - `docs/PROJECT_GUIDE.md`：第 4.5 节重构为 Facade 与私有子包体系说明，订正各模块行数；第 15 节追加 v0.8.0 架构解耦重构专章；
>   - `docs/Quick-start_developer.md`：更新第 1、2、5 节仓库地图与模块职责表；
>   - `CHANGELOG_user.md`：遵循用户规范，以纯大白话记录 v0.8.0 架构、并发与稳定性升级，严禁技术黑话。
> - 版本号发布 Bump（v0.8.0）：
>   - `mortis_rag_mcp/server.py`：`SERVER_INFO.version` bump 0.7.3 -> 0.8.0；
>   - `pyproject.toml`：`project.version` bump 0.7.3 -> 0.8.0（保留工作区 extras 改动）。
> - **验证**：全量单测增至 **345 passed, 4 skipped**（基线 341 + 4 冻结专项，`PYTEST_EXIT=0`）；eval **Hit@5 100.0% / MRR@5 1.000**；金测与缓存 roundtrip 严格恒等。

### C52 — moton16,2026-09-27,CodeBuddy,GLM-5.3-Flash — fix(review): 终审修复——检索 oracle 冻结对照、sync 接缝回归 owner 路由、chunk 兜底对齐与反向导入守卫加固
> **代码改动概况**：
> - `tests/golden/legacy_search_impl.py` 落地：C44（v0.7.3）检索主路径逐字冻结（`search`/`_fts_query`/`_hybrid_rank`/`_query_tokens` + `_RRF_K` 等常量与 `dedupe_by_content_hash`/`rerank_chunks`），状态经 `__getattr__` 穿透到活实例（同 `legacy_split_table_impl.py` 模式）。
> - `tests/test_search_oracle.py`：旗舰测试原为"Facade vs SearchEngine"——两条路径汇聚同一个 `search_single_vault`，结构上恒真（同义反复，守不住回归）。改为"Facade vs 冻结旧实现"真 oracle 对照；并开启 `cache.enabled=True` 使 FTS/RRF 混合路由真实纳入对照（裸 `AppConfig` 编程构造默认 `enabled=False`，旧测试静默退化为纯词法路径对照），辅以 FTS 硬断言防静默退化；score 不可变测试同步扩大覆盖面。
> - `mortis_rag_mcp/_indexer/sync_engine.py`：13 处引擎直呼模块函数改回经 Facade 薄委托（`owner._embed_missing`/`_flush_vectors_to_disk`/`_ensure_disk_vectors_migrated`/`_chunk_has_vector`/`_reuse_vectors_by_content_hash`/`_embedding_changed_state`/`_embed_one_file`），对齐 C44 `self._` 调用形态与 `snapshot.py` 现状，monkeypatch 接缝在 sync 路径恢复可达；未打补丁时行为零变化。
> - `mortis_rag_mcp/_indexer/chunking.py`：`chunk_file` 的 `getattr` 兜底 800/120 对齐 `AppConfig` 真实默认 1200/0，消除缺属性 config 时的静默错块潜伏差异（Facade 生产路径不可触达）。
> - `tests/test_facade_freeze.py`：反向导入 AST 守卫补 `alias.name` 检查（`from mortis_rag_mcp import indexer` 不再漏报）并按 `level` 区分相对导入（`from ._indexer import` 不再被潜在误报）。
> - pyproject.toml 工作区 extras（accel/numpy）：经审裁决暂保留，与 PROJECT_GUIDE 2.3 设计决策的矛盾文档补录留待后续版本处理。

- **验证**：全量 `pytest tests/ -q` **345 passed, 4 skipped**（oracle 测试就地强化，总数与 C51 持平）；oracle 判别力抽检：FTS 开启下无扰动恒等、人为扰动冻结 `_RRF_K=59` 后分数立即分歧；`mortis_rag_mcp` 包内 sync 函数族直呼 grep 清零；受影响 6 测试文件单跑 25 passed；lint 零新增。

### [FIX-1–FIX-2] — moton16,2026-09-28,CodeBuddy,Deepseek-V4.1-Flash — fix(security): kb_exempt check 路径沙箱 + 缓存解码截断防御
> **涵盖提交**：`fix(security): kb_exempt check 路径沙箱 + 缓存解码截断防御`
> **来源**：v0.8.0 Combo-C `/review` 终审（54 findings）Option A 已批准修复范围的前两张卡（C1/C2）；施工指导 `docs/v0.8.0/Combo-C-review-handoff_developer.md` §3。
>
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/exemptions.py`（C1 · P1 安全）：`check_exemption` 原以 `owner.vault_path / source_posix` 直拼接路径，而 `source` 来自 MCP 工具参数（LLM 直控），`../` 或绝对路径可探测 vault 外文件的存在性与豁免状态。现改为 `owner._safe_path(source_posix)` 沙箱（与该文件 `set_file_exemption` 早已存在的正确防线对齐），并把 `_safe_path` 的「抛 ValueError 拒绝」折叠为 `None`，与「文件不存在」走同一响应分支——**库外与不存在同响应，存在性 oracle 被封死**。响应键集（`source/is_exempt/reason/has_block_ignores/indexed_chunks`）保持不变，不加新键、不破坏 MCP 返回契约；`get_exemptions()` 对 scandir 派生的库内真实文件循环调用，行为零变化。
> - `mortis_rag_mcp/_indexer/cache_codec.py`（C2 · P1 健壮性）：`_CacheCodec.load()` 与 `_VectorsCodec.load()` 的 `version = raw[pos]` 位于两段 try 之外，magic 匹配但总长不足 6 字节的截断文件会抛 `IndexError`，违反模块 docstring「解码失败（magic/版本/截断/损坏）一律静默返回 None」的契约。两处条件行各追加 `or len(raw) <= len(MAGIC)`，最小补丁、不动控制流。
>
> **测试**：
> - `tests/test_exempt.py` 新增 `test_check_exemption_rejects_paths_outside_vault`：库外**真实存在**的文件（`../outside.md`）、绝对路径（`C:/x.md`）与 `../../outside.md` 三项均断言 `is_exempt=False` + `reason="file not found on disk"` + 响应键集冻结，库内文件仍正常回报。
> - `tests/test_cache_codec_roundtrip.py` 新增 `test_short_payload_after_zlib_returns_none_without_indexerror`：两个 codec 各构造「解压后恰好 5 字节 = magic」的载荷（刻意用 `zlib.compress` 而非裸写 magic——后者会先撞 `zlib.error` 分支，测不到新增守卫），外加 magic+版本 6 字节的边界档。
>
> **验证**：`tests/test_exempt.py` 7 passed、`tests/test_cache_codec_roundtrip.py` 5 passed；修复前复现对照——同一载荷走旧逻辑实测抛 `IndexError: index out of range`（`decompressed_len 5 / magic_match True`），修复后返回 `None`；lint 零新增。

### [FIX-3–FIX-5] — moton16,2026-09-28,CodeBuddy,Deepseek-V4.1-Flash — chore(release): 徽章/SKILL 头部对齐 0.8.0 + 补 accel extra
> **涵盖提交**：`chore(release): 徽章/SKILL 头部对齐 0.8.0 + 补 accel extra`
> **来源**：同批次 Option A 修复卡 C6 / A1 / A2。
>
> **代码改动概况**：
> - `README.md` / `README_EN.md`：版本徽章 `0.7.3` → `0.8.0`（仅第 5 行徽章；正文 v0.8.0 特性宣传本就正确，其余内容不动）。
> - `pyproject.toml`：`[project.optional-dependencies]` 在 `vec` 之后补 `accel = ["numpy>=1.24"]`。PROJECT_GUIDE §13.3 早已声称「提供 accel / vec 额外可选依赖」，此前工作区 extras 经审裁决暂缓（见 C52 条目），本卡落地后该声明成真。`dependencies = []` 零运行时依赖红线不变：numpy 仍是「装了就快、没装也对」的软依赖（`_indexer/search.py` 内 try-import，缺失自动回退标量余弦）。
> - `QUICKSTART_user.md`：用户侧 extras 安装段（原本已提 `mortis-rag-mcp[vec]`）按现有格式补 accel 一行，标注与裸 `pip install numpy` 等价；`README.md` / `README_EN.md` 未提 extras，按卡内规则不动。
> - `skills/mortis-rag-mcp/SKILL.md`：头部标题版本 `0.7.2` → `0.8.0`。frontmatter `version: 5.2.0` 是 SKILL 文档规范版本号（Combo-C 未声明 bump），**刻意不动**；正文的 Combo-C 新参数（chunk_id/expand_lines、budget_bytes、exact_terms、aliases、.txt、双链、solo 语义、15 工具清单）此前已同步，本卡只修头部。
>
> **验证**：`grep "0\.7\.3" README.md README_EN.md` 零命中（其余 0.7.3 命中均在 `CHANGELOG_user.md` 与 docs 历史条目内，按事实保留与卡内规则不动）；`grep "0\.7\.2" skills/mortis-rag-mcp/SKILL.md` 零命中；用项目自带 `config._read_toml()`（本机 `.venv` 为 Python 3.10、无 `tomllib`，自动走 fallback 子集解析器）断言 `extras == ['accel','vec']`、`accel == ['numpy>=1.24']`、`dependencies == []`。
> **实装校验留白（如实记录）**：本机 `.venv` 未安装 numpy（实测 `ModuleNotFoundError`），故 `accel` 下限按保守 `>=1.24` 声明、不随意下调；`pip install -e ".[accel]"` 的实装验证未在本会话执行（避免改动使用者 venv 且本批最终回归交由 CI），留待 CI 安装阶段覆盖。





