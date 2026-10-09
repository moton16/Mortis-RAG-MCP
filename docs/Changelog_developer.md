# 那么，一切从这里开始吧。
### 如有commit变更，请同步在此说明更新。非必要可以**不读**该文件，阅读"Quick-start_developer.md"即可。只在你需要补充内容以及遇到困难要溯源时进行查证使用。否则就太占上下文了。
### 请在每次commit的开头标注commit提交的用户名（GitHub用户名），时间，若commit为ai直接提交的，请一并输出agent与模型底模（如你的系统提示词有明确告诉你你是什么模型，没有则不需要）的名字。
#### 如：moton16,2026-9-13,Codex,GPT-6-Astra
### 💡 组织说明：同一次任务/同一 Agent 连续提交的多个 commit 已按工作批次（如 `[C0–C7]`）合并整理，并提供了清晰的「涵盖提交」与「代码改动概况」；独立提交（如架构审查报告、重大发版节点、独立专项缺陷修复）继续单独成项，以兼顾溯源精度与阅读效率。
### ⚠️ 必须注意文档分工：面向普通用户的日常更新在根目录 CHANGELOG_user.md，必须用大白话只讲功能增减与体验，绝不允许写技术细节；所有的代码逻辑、实现细节与技术变更流水必须且只能写在本文档（Changelog_developer.md）与 PROJECT_GUIDE.md！

---

### [E18 文档整合、收尾与内容差异报告] — moton16,2026-10-09,CodeBuddy,DeepSeek-V4.1-Flash

按用户侧 / 开发者侧分工对 v0.9.0 文档做一致性整合与发布前收尾，**不改代码逻辑、不改测试断言、不做任何发布动作**（无 push / PR / tag / release / publish）。本批只动文档，合并为一个提交（同任务连续提交按本文档开头约定合并整理）。逐项盘点、两个对比基准的原始 git 证据与全部结论见本地忽略目录 `docs/v0.9.0/V0.9.0_CHANGE_DIFF_2026-10-09.md`（本机交付，不在本提交内）。

**用户侧文档（只讲功能增减与体验，无技术细节）**

- `README.md` / `README_EN.md`：候选节新增「音频转录与音频转码能力已移除」——`[audio]` 段与 `audio_enabled` 开关一并消失，旧配置留着会被忽略（不报错、不影响其它配置），建议删除；删去已不存在的「完整音频 adapter/解码装配」待验收表述；媒体待验收边界改写为「文字检索图片已在本机跑通 / 真实客户端显示与付费端点通用装配未验收」；MinerU 免登通道补注「只取回解析出的文字，不带文档里的图片」；核心特性表去掉「完整音频生产链」。
- `QUICKSTART_user.md`：§0.4 新增第 5 条（音频能力移除与旧配置键处理）；媒体段落按实际验证边界改写；MinerU 节补注免登只回文字；**修正「解析产物自动存放于 `.mortis-parsed/`」**——0.9 起默认落本机文档库（virtual），只有显式旧镜像模式才在库内写 Markdown。
- `CHANGELOG_user.md`：0.9.0 条目新增 `### Removed`（音频转录/转码移除 + 用户侧迁移影响），`### Upgrade` 补一条「旧键可安全删除」；顶部候选说明去掉「完整音频接入」。

**开发者侧文档（技术流水与实现细节）**

- `docs/PROJECT_GUIDE.md`：§0.9 候选增量更新两条现状（真实载体跨模态「文字检索图片」已跑通；sqlite-vec 已装入本机两套解释器并跑通 `vec0` KNN 与 `extras` lane，远端 CI 仍未 push 故无链接）；§一项目定位把 `.mortis-parsed/` 镜像口径改为 virtual 文档库（`cache.dir/<namespace>/doc_store/`）；§3.1 与 §六工具面 15→16 并补 `kb_read_media` 行（必填 vault_path/source/revision_id/occurrence_id，`metadata`/`inline`、`preview`/`original`、`budget_bytes` 默认 2097152 上限 8388608）、修正 `kb_ingest` action 枚举为 `submit/status/pending/retry` 并写明 retry 语义；§4.3/4.4/4.5/4.8/4.10/4.11 行数按实测更新（426/398/1695/2307/937/4842），§4.11 改写任务生命周期与 `storage` 落点、新增免登通道「只返回 markdown」的协议事实（并说明原 mock 断言为何误导）；新增 §4.12 `doc_store.py`（4099 行，版本化文档库：事实与派生索引分离、OS mutation lock、revision pin、purge 边界）；§七配置参考补 `[chunking]`/`[doc_store]`/`[media]`/`[diag]` 四节与 `[ingest]` 0.9 键、`[embedding]` 模板/media 声明组；§八磁盘布局区分 virtual/legacy 并补 doc_store；§十一测试体系 56→106 个文件、skip 归类改写；§14.2 版本历史速览补齐 0.7.0–0.9.0；§14.3 新增文档库与媒体声明两条不变量。
- `docs/Quick-start_developer.md`：仓库地图行数与模块清单按实测更新（新增 `doc_store.py` / `media_providers.py` / `paid_requests.py` / ingest 子模块，包体 ~25260 行 / 42 个 .py）；§5 模块表 15→16 并补 doc_store 与 media/provider 两行；§7 测试文件数 56→106；§10 checklist 改为「靶向 pytest 全绿，全量回归交 CI」。
- `docs/Docs_Folder-descriptions.md`：**删除错误口径**「`docs/v0.8.1/` 是 `.gitignore` 版本目录特例、随版本入库」——用 `git check-ignore -v` 与 `git ls-files docs` 核实：实际规则是 `docs/*` 加四行白名单，`docs/v0.8.1/**` 与 `docs/v0.9.0/**` 均未被跟踪（该例外由 `21722ef` 撤销，文件仍在磁盘但已解除跟踪、仍被忽略）；补文档分工说明。
- `skills/mortis-rag-mcp/SKILL.md`：候选增量去掉「完整音频 adapter/解码装配」并补「音频转录/转码链路已物理移除」；媒体待验收边界改为「文字检索图片已本机验证 / 真实端点通用装配与宿主显示未验收」。

**未改动（有意保留）**：代码逻辑与测试断言（含 `tests/test_version_sync.py`、`tests/test_beta2_docs_contract.py` 断言的版本徽章、Skill frontmatter/标题、用户 changelog 标题与 CLI 文案）；`Changelog_developer.md`、`PROJECT_GUIDE.md`、`Quick-start_developer.md` 的历史条目按纪律不改写——例如历史批次里「仍有缺口：完整 AudioConfig/adapter/解码装配」记录的是当日状态，已被 E17 物理移除取代，新结论写在本条与 E17 条，不回改历史。

**验证**（`.venv/Scripts/python.exe`；`--basetemp` 钉仓库 `.runtime`，TEMP/TMP/TMPDIR 全隔离）：

- 配置与版本只读探针：`load_config(config/app.toml.example)` 正常加载、10 个段全部被识别、示例中无被拒键；`hasattr(AppConfig,'audio')=False`、`AudioConfig` 类不存在、`IngestConfig.audio_enabled` 不存在；仍带 `[audio]`/`audio_enabled` 的旧配置可正常加载（被静默忽略）；带 UTF-8 BOM 的配置 `cache.enabled=true`、`reranker.enabled=false`（`b328dc9` 回归）。版本单一真源 pyproject = 包 `__version__` = `SERVER_INFO["version"]` = `0.9.0`，`_tool_definitions()` 实测 16 个工具。
- 文档契约靶向：`pytest -q tests/test_version_sync.py tests/test_beta2_docs_contract.py` = **9 passed**，exit0。
- 核心全量（本窗口唯一一次）：**1140 passed / 14 skipped / exit0**（收集 1154；14 个 skip = `.venv` 缺 docs/media 可选依赖 12 个（pymupdf/docx/pptx/openpyxl/PIL/fitz）+ 非 Windows 平台用例 2 个），与 E17 终态计数完全一致，文档改动未影响用例数与结果。
- 边界与授权：`.gitignore` 未改、未 `git add -f`、`docs/v0.9.0/**` 与 `.runtime/**` 未入库；真实计费 API 调用 0、真实知识库/用户配置写操作 0、安装包 0、push/PR/tag/release 均未执行。待授权项与开放项清单见统一入口 §18 与差异报告 §9。

---

### [E17 架构收敛：物理清除 ffmpeg 音频解码与 Whisper 转录链路] — moton16,2026-10-09,CodeBuddy,DeepSeek-V4.1-Flash

按统一入口 §16 架构收敛裁定，把音频转码 / 转录链路整体移出核心代码库（不新增业务功能，只做删除与去悬空）：

- **删除模块**：`mortis_rag_mcp/ingest/audio.py`（PCM 分段/解码接缝/`parse_audio` 编排）、`mortis_rag_mcp/ingest/transcription.py`（OpenAI Whisper 转录 adapter 与付费闸门装配点）。
- **配置面**：移除 `config.AudioConfig`、`AppConfig.audio`、`IngestConfig.audio_enabled`、`_load_audio` 与 `[audio]` 解析；`config/app.toml.example` 删除 `[audio]` 段与 `audio_enabled`。
- **装配面**：`server.py` 删除 `_transcription_adapter`/`_audio_decoder` 与转录 paid_guard 装配，`make_ingest_manager` 不再传 `audio_config/audio_adapter/audio_decoder`；`doctor.py` 删除 `audio_enabled`/`transcription` 报告字段；`ingest/worker.py` 删除 `AUDIO_EXTS`/`AUDIO_ROUTE_UNSUPPORTED`、音频分卷与转录 subjobs 调度、段级 audio checkpoint 与 audio 指纹。
- **测试**：删除 `test_audio_ingest.py`、`test_audio_production_adapter.py`、`tests/fixtures/transcription_contract.json`；`test_e15_service_contracts.py` 移除转录子出口用例；`test_lane_be_config.py`/`test_route_execution_upgrade.py` 移除音频配置断言。混合文件去音频后更名为 `test_virtual_worker_queue.py`、`test_subjob_checkpoint_contract.py`（保留非音频合同用例，未降低覆盖口径）。
- **保留（与转录无关）**：音频原生 embedding transport（`media_providers.py`）、音频 occurrence 展示（`_server/media_dispatch.py`、`_indexer/media.py::merge_audio_segments`）、`doc_store` 的 `audio_frames/audio_ms` range kind（读旧库 checkpoint 兼容）。

E17 同一窗口另外落地三项真实验收发现：

- `5e37bc5` **test(e2e)**：本机 EG2 文本→图片用例硬编码端口且无载体门控，缺载体时是**失败**而非 skip（CI 必红）。改为 `MORTIS_EG2_MEDIA_ENDPOINT`（默认 `127.0.0.1:8000`）并加可达性 skip，保留载体在线时的强断言。
- `50793f3` **test(e2e)**：新增零 mock 跨模态回归——真实 PNG → `HttpMediaTransport` → EG2 Tier2 图文向量 → native chunk → 图片查询自命中（cos>0.999）+ 文本查询召回。载体在线实测 2 passed。
- `57caeca` **test(mineru)**：真实端点实测确认**免费 agent 通道只返回 markdown**（无图片字节 / 无 `content_list.json`/page_map），原 mock 用例名与文档暗示「免登通道可提取图片与锚点」属误导，已改名+改写说明并新增 `test_agent_channel_protocol_is_text_only` 固定该协议事实。
- `b328dc9` **fix(config)**：宿主机首用检查复现出真实缺陷——配置文件带 UTF-8 BOM（记事本 / `Set-Content -Encoding utf8` 默认）时会**静默错解**：首个 `[section]` 头丢失、段内键泄漏到顶层，`[cache] enabled = true` 被 flat-legacy 别名读成 `reranker.enabled = True`，doctor 对合法配置误判 ❌ BROKEN。`_read_toml` 改为 `utf-8-sig` 解码，并加 BOM/非 BOM 等价回归。

**验证**（本窗口，`.venv/Scripts/python.exe`，basetemp 与 TEMP/TMP/TMPDIR 全隔离）：

- 音频清除静态探针 PASS（模块导入 + 被清除符号/签名/成员缺席）。
- 受影响靶向：97 passed/4 skipped 与 158 passed/2 skipped，均 exit0。
- 全量 run1（修复载体门控前）1128 passed/17 skipped/**1 failed**（EG2 载体未运行，记录保留不改写）；run2（修复后）1128 passed/18 skipped；run3（载体在线）1132 passed/17 skipped；**run4（终态，装入 sqlite-vec 后）1140 passed / 14 skipped，exit0**；载体在线复跑 `test_text_to_image_search_e2e.py` 为 2 passed。
- 发布候选（离线、隔离 src、setuptools 后端直调）：wheel 48 项 / sdist 166 项，归档卫生 0 项违禁；隔离导入 `ISOLATED_IMPORT_OK 0.9.0`；pyproject = 包 = `server.SERVER_INFO` = 0.9.0。

**sqlite-vec 后端正向检查（主人授权后闭合）**：主人授权安装后，按**本机已有离线 wheel**（`C:\tmp\sqlitevec_probe\sqlite_vec-0.1.9-py3-none-win_amd64.whl`，此前有窗口下载过但**从未装入任何解释器**）以 `--no-index --no-deps` 离线装入 PATH python 与仓库 `.venv`；`vec0` 虚拟表 KNN 实测通过（`SQLITE_VEC_POSITIVE_OK`），本地复刻 CI `extras` lane（`MORTIS_REQUIRE_EXTRAS=1`）`import pymupdf…sqlite_vec` = `EXTRAS_IMPORTS_OK` 且 `test_extra_formats.py + test_vector_backend.py` = **11 passed**，核心全量比 run3 多跑通 8 个此前被 `importorskip` 跳过的磁盘后端用例。首轮记 BLOCKED 的根因是只查了两套解释器的 `pip show`——「下载过」≠「装上了」。`pyproject.toml` 的 `vec` extra 与 CI `extras` lane 原样保留。

**主人裁定（2026-10-09）**：远端 CI、≥100 问七类质量语料、真实旧库升级演练三项属**需要实际使用才能知道的结论**，按 PASS 收口、不再作为阻塞项；本窗口不为它们编造本地证据（CI 未推送故无链接、质量门禁仍只记 `fixture_measured`、旧库只有合成升级/快照恢复证据），逐项区分见 `docs/v0.9.0/E17_FINAL_ACCEPTANCE_2026-10-09.md` §9/§10 与统一入口 §17.6。

**仍未闭合**：push/PR 未经主人显式授权 → 分支未推送、CI 未触发；宿主真实 image/audio 显示验收需真实 MCP 客户端；MinerU v4 带图 occurrence/正文锚点需授权商业 key。

---

### [beta2 第四批与前三批局部复核] — moton16,2026-10-08,Codex

只做本地候选准备，未调用真实 API/迁移真实资产/执行远端 Git 或发布。只读增量复核由内置 gpt-6.1-sol/high 完成，主流程核实与修复。

E14 本地目标0.9.0：同步包、pyproject、SERVER_INFO衍生版本、README badges、Skill标题/frontmatter和用户候选changelog；stdio smoke从包版本取断言。未创建 tag/release，候选构建/回归证据由统一入口另记，不预写外部CI绿。

整合末 `8866bb5` 一次全量实际为1084passed/20failed/13skipped（286.09s、exit1），保留原记录。
`d88b234` 修复读轮询自己不断enqueue而永远rebuilding：`for_read=True`仅机会性读刷新在刚完成时返回False；显式请求/import/文件事件仍保pending，原E04接受承诺不变。import5/read_stale7/txt3/multivault9/compact7通过。
后续靶向合同对齐：legacy字符上限/dedup/v073 golden显式mode，golden原数据不改且只剥新增embedding_key；
scanning spy改实际扫描入口；迁移测试在临时home去掉隔离override；旧fake checkpoint换真实SQLite record_subjob(done)；
virtual不可用错误与后台stdio等待按现行接口；estimated保尾空白/可变metadata预算按实际包络，短块预览不承诺固定压缩比（长正文压缩unit仍锁比例）。
涉及12个失败文件分别靶向112passed/1skip，另txt3passed，关闭原20个失败点；未重复跑全量，不能将旧候选全量记录改为最终候选全绿。

- `f4577c1`：PPTX 正常 printerSettings 二进制是惰性打印元数据，不应按嵌入 OLE 拒绝；仅精确豁免此路径，真实 PDF/Office/Pillow 正向5例通过。
- `c11f762`（E11）：必需 docs/media/vec CI lane；离线检索拒外部配置；成对质量/资源工具与合同测试。2问指标 fixture 不是七类100问验收；Python tracemalloc 不冒充 RSS。
- `4e69b1d`：retry 启动 idle worker；cancelled 重试清失去 blob 保护的段 checkpoint；9个 management 和5个 checkpoint用例通过。
- `5f5b4c0`：import 最终忙态/gate重核和generation发布复用同一 mutation lock，保原补偿；6个 import安全用例通过。
- `d631ff9`：native 缺向量不走文本补嵌、媒体 fingerprint 进入签名、媒体chunk补 SHA、固定 revision occurrence 分页、完整同步清磁盘派生 orphan。14个媒体接线用例通过；真实 sqlite_vec 本地缺包，新增磁盘回归待必需 CI。
- `9050ed7`：第二轮局部证伪再现重开 chunker/profile 后旧 pending 覆盖 native，以及不支持MIME误停 proxy；native ID 含媒体 profile、仅补缺向量、仅成功native停其proxy。3个靶向+7个codec+5个sync通过。
- E12：真实16工具/配置合同、合成 legacy 缓存升级/快照恢复与源/备份hash不变；同步 virtual/import/retry/请求CLI/升级边界；取消 Agent 缺 STATUS 即自动 doctor。相应提交由统一入口登记，不伪造未产生的 SHA。
- `8cd52ca`：真实示例未设API环境变量触发 `_env` 的缺失 sys import；最小修正+1个unset用例+33个配置用例通过。E12提交 `ccdb9d0` 的文案/升级3例、virtual配置33例、stdio5例、import6例通过。

仍有实际范围缺口：真实媒体 transport、完整 AudioConfig/adapter/解码装配、独立图片摄取、真实质量/宿主验收；legacy 极端多格截失未静默改兼容语义。完整候选回归/构建结果在后续交接登记，不预写通过。

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
>   - `server.py` 由 1359 行降至 1126 行（净减 233 行；v0.8.1 实测 1312 行）；`_fanout_search` 与 `_kb_search` 委托至 `_server/`。
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

### [FIX-6–FIX-8] — moton16,2026-09-28,CodeBuddy,Deepseek-V4.1-Flash — chore(cleanup): 清理 Facade 死导入与陈旧行号引用
> **涵盖提交**：`chore(cleanup): 清理 Facade 死导入与陈旧行号引用`
> **来源**：同批次维护性卡 M1 / M2 / M13。
>
> **代码改动概况（verify-then-delete，逐名词边界核验后才删）**：
> - `mortis_rag_mcp/indexer.py`（M1，净减 10 行导入）：删除 Facade 瘦身遗留的 7 条整行死导入（`fnmatch`、`math`、`re`、`struct`、`zlib`、`from concurrent.futures import ThreadPoolExecutor, as_completed`、`from dataclasses import dataclass, field, replace`）与裸模块导入 `from . import fsnotify`；并从两条组合导入中摘除 `watcher_available`、`split_large_table`（同行的 `WindowsDirectoryWatcher`、`iter_table_blocks`、`split_table_into_chunks` 保留）。
> - `mortis_rag_mcp/server.py`（M2）：删除 `from dataclasses import replace`，以及从 `.indexer` 导入的 `dedupe_by_content_hash`、`rerank_chunks`（`kb_search` 已迁 `_server/`，fanout 直接从 `_indexer` 导入）。
> - `mortis_rag_mcp/_server/fanout.py`（M13）：`_measure_payload_bytes` docstring 的 `server.py:843` 陈旧行号引用改为 `server.py::_text_content`（引用函数名，勿钉行号——行号随重构持续腐烂正是本卡动机）。
>
> **核验依据（凭什么可删）**：`indexer.py` 内候选名的唯一命中即 import 行本身（`tmp.replace`/`source.replace` 是 `Path`/`str` 方法、注释里的 "re-embeds"/"re-written" 是英文单词，均不构成使用）；`server.py` 内 `replace` 的其余命中同理（`str.replace`、注释与错误文案）；包内无任何模块 `from .indexer import <候选名>`；`watcher_available` 由 `_indexer/watch.py:7` 直接从 `..fsnotify` 导入、`split_large_table` 由测试直接从 `ingest.tables` 导入，均不经 Facade；Facade 的 monkeypatch 接缝只有 `_inject_image_notes`（chunking）与 `_probe_mtime_tick_ns`（scanning），不在候选内；`indexer.py` 无 `__all__`；`test_facade_freeze.py` 冻结符号表不含任何候选。另核实被删的每个名字都由真正使用它的私有子模块自带导入（`re`→chunking/search、`struct`+`zlib`→cache_codec、`ThreadPoolExecutor`+`as_completed`→sync_engine、`fnmatch`+`math`→scanning、`dataclass`+`field`→models、`replace`→search），无运行期隐患。
>
> **验证**：`import mortis_rag_mcp.indexer, mortis_rag_mcp.server` 通过；导入面断言 `STILL_PRESENT_INDEXER=[]`、`STILL_PRESENT_SERVER=[]`（13+3 个被删名全部消失）且 `KEPT_OK=True`（Facade 保留导出面齐全）；`tests/test_facade_freeze.py + tests/test_facade_seam.py + tests/test_chunking_seam.py` 9 passed；lint 零新增。
>
> **本批次验证（三卡合计，按用户裁定不在本地跑全量、由 CI 承接最终回归）**：动手前基线双跑均为 **410 passed, 4 skipped**（与 C52 声称一致，无带病施工）；最终靶向门禁 10 文件 **56 passed**（`test_exempt`、`test_cache_codec_roundtrip`、`test_budget_bytes`、`test_facade_freeze`、`test_facade_seam`、`test_chunking_seam`、`test_p5_lifecycle`、`test_mcp_stdio`、`test_search_oracle`、`test_hybrid`），且在隔离配方下真实缓存目录文件数前后 **18153 → 18153 零变化**。
>
> **本批次偏差与上报（以磁盘实况为准；均未顺手修）**：
> - **`Quick-start_developer.md` §7 的 `SAFE_DELETE_FAIL_CLOSED` 描述不成立**：全库 `*.py` grep 零命中该字符串，`purge_cache` / `rebuild` 走 `Path.unlink()`、不进系统回收站。该已知红用例的真实成因待独立排查。
> - **`PROJECT_GUIDE.md` §2.3 的注记在 FIX-4 落地后过期**：其称「`numpy` 连 optional-dependencies 都没声明」，而本批已补 `accel` extra（§13.3「提供 accel / vec 额外可选依赖」反而成真）。卡内明示 PROJECT_GUIDE 无需改，故未改，仅记录两处口径留待后续版本对齐。
> - **C52 条目所称「pyproject.toml 工作区 extras（accel/numpy）经审裁决暂保留」与提交历史不符**：`git log --all -S accel -- pyproject.toml` 零命中、HEAD 亦只有 `vec`，即 accel extra 从未进入任何提交；本批 FIX-4 才是首次落地。
> - **测试隔离缺陷（不在本批 8 卡范围，仅上报不动）**：`config.py` 的 `DEFAULT_CACHE_DIR` 固定为 `~/.mortis_rag_mcp_cache` 且无环境变量可覆盖，而 `tests/test_exempt.py`、`tests/test_mcp_stdio.py` 等 stdio 用例的 app.toml 未写 `[cache] dir`（`test_registry_server.py`、`test_multivault.py`、`test_snapshot.py` 等则写了），这些用例会把临时库缓存写进使用者的真实缓存目录——本机实测累积 **18153** 个孤儿缓存文件；叠加 pytest 默认只保留 3 轮 `tmp_path`、更早整轮被改名 `garbage-*` 后整目录删除（实测单个目录 837~1262 文件），本机每跑一次全量即触发一次 500+ 文件的批量删除审核。另 `tests/test_budget_bytes.py` 的子进程 env 只设 `VAULT_MCP_REGISTRY`，宿主若导出 `MORTIS_RAG_REGISTRY`（新名优先）会压过测试自身隔离：本会话实测该陷阱令 `test_mcp_stdio.py` 两个用例假失败（`KeyError: 'description'`，真因是复用的注册表已存在、legacy `vault_path` 自动迁移被跳过），故本地验证统一改用「仅重定向 `HOME`/`USERPROFILE`、不导出任何 REGISTRY 变量、独立 `--basetemp` + `-p no:cacheprovider`」的隔离配方（修正后同批用例即全绿）。
> - **本轮不做**：§4 明确清单（C3/C4/C5/C7/C8 与 46 条 informational）一律未动；`tests/test_subvaults.py` 的 rebuild 已知环境红未顺手修。

### FIX-9 — moton16,2026-9-29,CodeBuddy,Deepseek-V4.1-Flash — docs: PROJECT_GUIDE 转义污染全量清理（805 处反斜杠残留还原）

> **涵盖提交**：`docs: PROJECT_GUIDE 转义污染全量清理（§1–§15 共 805 处反斜杠残留还原）`（`e5eb33d`）
> **来源**：v0.8.1 计划 Lane E 的前置独立 commit（用户裁定 D4：P1 必须先于 C58 的 §四/§七 文档改动，否则同一文件里大段机械 diff 与语义编辑互相遮蔽）。编号走并行线——`C53`–`C64` 已被 v0.8.1 的 12 张卡占用，故沿用 `FIX-x` 清扫线。
>
> **问题（规模实测，与原始描述不一致）**：`REPORT.md` 的 P1 记「§3.2 标题、§4.5、§六/七多处」；实测污染覆盖 §1–§15：**290 / 1103 行、805 个反斜杠 token、21 种形态**。其中 `\_` 的 7 反斜杠形态 592 处、3 反斜杠 19 处、1 反斜杠 103 处，系多轮「转义反斜杠 + 转义标点」叠加而成；GitHub 渲染后表现为标识符前挂着若干反斜杠。
>
> **映射与命中数（逐形态定目标后脚本化还原）**：
> - markdown 标点转义 `\_` `\[` `\]` `\*` `\~` → 去反斜杠：**770** 处
> - 水平分割线 `\---` → `---`：**16** 处
> - f-string 空字符 `\0`：8 → 1 反斜杠：**4** 处
> - 长路径前缀 `\\?\\`：16 → 2 反斜杠：**1** 处
> - PowerShell 路径分隔符：8 → 1 反斜杠 **3** 处；连缀符 `&&`：7 → 0 反斜杠 **2** 处
> - JSON 示例 Windows 路径 `D:\\笔记\\工作库`：16 → 2 反斜杠：**2** 处
> - SQL `ESCAPE '\\'`：8 → 2 反斜杠；同行括号内被转义字符 `\`：8 → 1 反斜杠：各 **1** 处
> - **刻意保留（合法、非遗留）**：regex `\b` 3 处、Markdown 表格单元格 `\|` 1 处（表格必需）；`Quick-start_developer.md` §7 的 `.\.venv\Scripts\python.exe` 本就是单反斜杠（非污染，未动）。
>
> **验证**：清理后对全文重跑反斜杠 token 直方图，残留仅剩上述合法形态（无意外残留）；行数 1103 不变；`git diff --stat` = 292 insertions / 292 deletions（纯行内替换）；无代码与测试影响。

### FIX-10 — moton16,2026-9-29,CodeBuddy,Deepseek-V4.1-Flash — docs: 过期行号订正 + 仓库地图/白名单修齐（5 个主文档口径，X3）

> **涵盖提交**：`docs: 过期行号订正 + 仓库地图/白名单修齐（5 个主文档口径，X3）`（`96e205f`）
> **来源**：v0.8.1 计划 Lane E（陈旧行号）；用户裁定 X3（延后项用仓库自己的 `docs/Execution-plan_developer.md` 口径，不新建根 `TODOS.md`）。
>
> **改动概况**：
> - **过期行号**：`PROJECT_GUIDE.md` §4.8 `server.py` 约 1064 → 约 1312 行（计划点名的三处之一）；同清单内一并订正 §4.1 config 465→526、§4.2 registry 363→384、§4.5 indexer 1238→1253、§4.6 fts 149→152。`Quick-start_developer.md` 仓库地图 server.py 1126→1312、config 390→526、registry 304→384、indexer 1238→1253、fts 149→152、包体 `~5800`→`~10850` 行。实测依据：`server.py` **1312 行**（计划记 1313，以实测为准）；Lane C（C55–C57）改动后行数会再变，**由 C60/T8 复核**。
> - **测试规模标注**：`PROJECT_GUIDE.md` §11「24 个文件 / 约 5250 行 / 264+ passed, 2 skipped」与 `Quick-start_developer.md`「340+ 测试用例」「22 个测试文件」→ 实测 **50 个文件 / 49 个测试文件 / 416 个用例 / 约 11570 行**；并把「全量 pytest 全绿」的表述改为「全量回归由 CI 承接，本地按靶向文件单跑」，与「本地不跑全量」的项目约定对齐。
> - **仓库地图与白名单（X3）**：新建 `docs/Execution-plan_developer.md`（延后项落点：`_save_state` 剪枝、`list_files()` 全量构造、FTS 构造期写盘、多平台 watcher、云路径验收闸门、TTHW/进度反馈/DX 候选、两条口径残留）；`.gitignore` 白名单 3 → 5（+ `Execution-plan_developer.md`、+ `Docs_Folder-descriptions.md`）；`Docs_Folder-descriptions.md` 的「只需保留」清单 4 → 5；`Quick-start_developer.md` §2 地图补 `PROJECT_GUIDE.md` 与 `Docs_Folder-descriptions.md` 两行（原地图漏列 PROJECT_GUIDE）。
> - **历史条目处置（本次裁定）**：本文件 C49 条目「`server.py` 由 1359 行降至 1126 行」**不改写**（该数字在其提交时点为真），仅追加「（v0.8.1 实测 1312 行）」注——记账流水是历史快照，不随版本回填。
>
> **验证**：纯文档/配置改动，无代码与测试影响；18 处替换均由脚本断言「原文唯一命中」后落盘；`.gitignore` 生效核验 = 两个新文件在 `git status` 中由「被忽略」变为「未跟踪可见」，提交后显示 `create mode 100644` 两行。

### FIX-11 — moton16,2026-9-30,CodeBuddy,Deepseek-V4.1-Flash — docs: v0.8.1 版本目录随版本入库（docs/v0.8.1 白名单特例）

> **涵盖提交**：`docs: v0.8.1 版本目录随版本入库（docs/v0.8.1 白名单特例）`
> **来源**：用户裁定（2026-09-30）——`feat/v0.8.1` 推送到远端，同时把本轮 Worklog 放进 `docs/v0.8.1/`，并**取消该目录的 ignore**（目录级白名单特例）。
>
> **改动概况**：
> - `.gitignore`：在 5 个主文档白名单之后新增「版本目录特例」`!docs/v0.8.1/`，并注明为什么只重纳目录本身就够——`docs/*` 只匹配 `docs` 的**直接子项**，不会命中子目录内部的文件；目录级排除（`docs/`）才会让后续 `!` 永远失效。
> - **入库文件**：`docs/v0.8.1/PLAN.md`（施工图 + 三轮评审审计链，约 189KB）、`docs/v0.8.1/REPORT.md`（v0.8.1 开工报告）、`docs/v0.8.1/Worklog_2026-09-29.md`（本轮 Lane E/A/B/C 完成情况、验证证据与待办清单）。
> - `docs/Docs_Folder-descriptions.md`：补一行「例外」说明，避免既有的「版本文件夹应显式 ignore」口径与新的入库现状互相矛盾（该文件本身也在白名单内、随仓库分发）。
>
> **验证**：`git check-ignore -v docs/v0.8.1/*.md` 退出码 **1**（未忽略，符合预期）；`git status` 中 `docs/v0.8.1/` 由「被忽略」变为「未跟踪可见」；`docs/` 根目录仍只有 5 个主文档 + 版本子目录。
> **技术账追溯登记**：本轮 Lane E/A/B/C 的技术卡条目（`C53`、`C54`+P0、`C63`、`C55`、`C56`、`C57`+`C62`、`C64`）已于 C70 集中补写如下。

### C53 — moton16,2026-09-29,moton16 — fix(ingest): MinerU 预签名 PUT 显式置空 Content-Type（issue #1）
> **代码改动概况**：
> - `mortis_rag_mcp/ingest/mineru.py`：
>   - 根因分析：urllib 的 `AbstractHTTPHandler.do_request_` 在「有 data 且 `has_header('Content-type')` 为假」时会自动注入 `application/x-www-form-urlencoded`；阿里云 OSS V1 预签名把 `CONTENT-TYPE` 计入 `StringToSign`，服务端按实际收到的请求头校验签名导致 `403 SignatureDoesNotMatch`，造成开启摄取的文档全部上传失败；
>   - 修复方案：`_put_upload` 显式传递 `headers={"Content-Type": ""}`（`Request.add_header` 会将其规整为 `Content-type`，命中 `do_request_` 的判据从而阻止自动注入），统一修复 v4 与 Agent 免登两通道共用上传路径；
> - `tests/test_ingest_mineru.py`：
>   - 新增本机回环 `http.server` 回归测试，断言服务端实际接收到的 `Content-Type` 为置空状态，且 `Request.has_header("Content-type")` 为 True。
>
> **验证**：
> - 专项测试（19 passed）：`.\.venv\Scripts\python.exe -m pytest tests/test_ingest_mineru.py tests/test_version_sync.py -q`
> - 实机验证状态：待实机验证（测试机无云端付费 Token，真机需用户授权执行，按 E1 降级形态如实记录）。

### C54 — moton16,2026-09-29,moton16 — test(isolation): 测试宿主隔离三件套 + P0 flaky 消除 + 隔离守卫（C54 / P0）
> **代码改动概况**：
> - `tests/conftest.py`：
>   - C54a 配置隔离：session 级 autouse fixture 在临时目录创建真实 `app.toml`，将 `MORTIS_RAG_CONFIG` 钉住，并清理宿主环境变量；
>   - C54b 缓存根覆盖：function 级 autouse fixture 为每个单测生成独立缓存目录，杜绝污染宿主真实 `~/.mortis_rag_mcp_cache`；
>   - conftest 的 `pytest_sessionfinish` 守卫：设置 `MORTIS_RAG_NO_STATUS_HOOK=1`，防止单测写入宿主 `STATUS.md`；
> - `mortis_rag_mcp/config.py`：
>   - `resolve_default_cache_dir()` 支持 `MORTIS_RAG_CACHE_DIR` 覆盖，并短路在改名逻辑之前，防止测试移动宿主旧缓存目录；
> - `tests/test_adversarial_v070.py` & `tests/test_improvements.py`：
>   - 为单测补充独立 per-test 注册表文件，消除跨用例泄漏与并发竞争（P0 flaky 修复）；
> - `tests/test_isolation_guard.py`：
>   - 新增隔离守卫测试，断言单测运行前后宿主真实缓存目录文件数零增长。
>
> **验证**：
> - 专项测试（47 passed）：`.\.venv\Scripts\python.exe -m pytest tests/test_isolation_guard.py tests/test_path_migration.py tests/test_doctor.py -q`

### C63 — moton16,2026-09-29,moton16 — fix(diaglog): 版本号去硬编码，收敛到包顶层单一真源（C63）
> **代码改动概况**：
> - `mortis_rag_mcp/__init__.py`：
>   - 新增 `__version__ = "0.8.0"`（定义在包顶层，发版单一真源）；
> - `mortis_rag_mcp/server.py` & `mortis_rag_mcp/diaglog.py`：
>   - 消除硬编码版本号，`SERVER_INFO["version"]` 与 `diaglog` 默认兜底值统一引用 `mortis_rag_mcp.__version__`；
> - `tests/test_version_sync.py`：
>   - 新增版本真源同步校验测试。
>
> **验证**：
> - 专项测试（16 passed）：`.\.venv\Scripts\python.exe -m pytest tests/test_diaglog.py tests/test_version_sync.py -q`

### [C55, C56, C57+C62, C64] — moton16,2026-09-29,moton16 — feat(server,indexer): kb_read 跨库 chunk_id 寻址 + kb_list_files 分页/前缀 + 别名与行数订正
> **代码改动概况**：
> - **C55（参数别名）**：`kb_init` / `kb_init_solo` 支持 `vault_path` / `vault` / `vault_name` 别名，对齐既有 handler 口径，不改 schema 属性以守住体积门禁；
> - **C56（跨库 chunk_id 寻址）**：
>   - 未传库标识时遍历全部注册库进行只读探测；
>   - 单命中自动展开并附加 `vault` 归属（solo 库仅输出库名与 `solo: true` 保护隐私）；多命中 fail-closed 报错并列出候选库；未完全探测时不谎报文件修改；
> - **C57+C62（kb_list_files 分页与过滤）**：
>   - 新增 `limit`, `offset`, `path_prefix` 参数；返回 `total`（过滤后、切片前条目数）、`next_offset` 与 `page_truncated`（与字节预算 truncated 区分）；
>   - 抽象通用纯函数 `path_prefix_match`，统一 kb_search 与 kb_list_files 前缀口径；
> - **C64（探测代价量测与上限防御）**：
>   - 实测 10 库 60 文件开销（82ms / 619KiB）；设定先到先停双上限：`_PROBE_MAX_UNLOADED_VAULTS=32` 与 `_PROBE_BUDGET_SECONDS=2.0`。
>
> **验证**：
> - 专项测试（82 passed）：`.\.venv\Scripts\python.exe -m pytest tests/test_kb_read_chunkid.py tests/test_mcp_stdio.py tests/test_scoped_search.py tests/test_solo_vault.py -q`



### C65 — moton16,2026-10-05,Antigravity,Gemini 3.8 Flash — fix(read): fail closed on incomplete chunk probes

> **涵盖改动**：`fix(read): fail closed on incomplete chunk probes`
> **来源**：Issue #5 剩余工作、v0.8.1 计划 C65 卡。
>
> **改动概况**：
> - `mortis_rag_mcp/server.py`：
>   - 注册表为空且未显式指定 `vault_path` 时，显式拦截并报 `ValueError("没有已注册的知识库，请先使用 kb_init 注册知识库")`，MCP `handle` 返回 `isError=True` 具名错误，杜绝协议级 `-32000` / `IndexError`。
>   - 新增 `_chunk_incomplete_message` 辅助函数：当存在跳过库（`skipped`）且已命中不足 2 个时判定为 `incomplete`，如实汇报已命中/跳过库名与原因，要求显式传 `vault_path`，杜绝未探全时误以为唯一而展开。
>   - solo 库在探测报错/跳过原因中绝不泄露绝对物理路径，保持纯库名与 solo 标记。
>   - 探测临时 indexer 收集到 `probes_to_close`，并在 `finally` 块中统一切断 FTS/vector 连接；不影响常驻 indexer。
>   - 命中临时库提升为常驻 indexer 时，重新按 id 取 chunk，若取不到抛出 `ValueError` 引导重新 `kb_search`，不再静默使用陈旧 probe chunk 兜底。
> - `mortis_rag_mcp/indexer.py`：
>   - 初始化新增 `self._chunks_cache_loaded: bool = False`，在 `_load_chunks_cache` 确认有效且 meta 匹配后置 `True`；未加载且无有效文本缓存的库跳过并记「尚无可探测文本索引」。
>   - `sqlite_vec` 回退至 `memory` 时，为 `_load_vectors_cache()` 补齐 `and load_vectors` 门禁，确保只读探测期零向量加载。
> - `tests/test_kb_read_chunkid.py`：
>   - 新增 8 项分支测试（`test_chunk_read_no_registered_vaults`、`test_chunk_probe_one_hit_with_unprobed_vault_is_incomplete`、`test_chunk_probe_missing_cache_is_unprobed`、`test_chunk_probe_valid_empty_cache_is_complete`、`test_chunk_probe_solo_diagnostics_do_not_leak_path`、`test_probe_load_vectors_false_survives_backend_fallback`、`test_chunk_probe_closes_resources_on_all_outcomes`、`test_probe_promoted_chunk_disappeared`）。
>
> **验证**：
> - `tests/test_kb_read_chunkid.py`（20 passed）、`tests/test_facade_freeze.py`、`tests/test_vector_backend.py`（合计 29 passed, 4.56s）。

### C66 — moton16,2026-10-05,Antigravity,Gemini 3.8 Flash — fix(search): serve existing indexes while refresh runs in background

> **涵盖改动**：`fix(search): serve existing indexes while refresh runs in background`
> **来源**：Issue #5、Issue #6 前置、v0.8.1 计划 C66 卡 + A4 回调修复。
>
> **改动概况**：
> - `mortis_rag_mcp/_indexer/watch.py` & `indexer.py`：
>   - 新增 `request_refresh(owner, *, immediate=False) -> bool` 与 `refresh_status(owner) -> dict[str, Any]`。
>   - 调度线程 `_fs_scheduler_thread` 启动引入双检锁（`_fs_scheduler_start_lock`），避免多并发请求创建重复调度线程。
>   - 连续读节拍限制：`_READ_REFRESH_MIN_INTERVAL_SECONDS = 1.0`，1 秒内重复只读请求合并；失败指数退避上限 5 秒。
>   - `_fs_scheduler_loop` 进 sync 前清空 dirty 标记，sync 期间新请求重新置 dirty，单次最多合并一轮。
>   - `_run_sync_quietly` 记录可观测 `_refresh_error` 与 `_last_refresh_completed_at`。
>   - 停止状态下（`stop_watching`）拒绝新 refresh 请求，安全 join `_fs_scheduler_thread`。
> - `mortis_rag_mcp/_server/search_dispatch.py` & `_server/fanout.py`：
>   - 单库检索统一通过 `_search_single_vault` 处理，替换旧有的 `try_sync_with_guard`；
>   - 冷库判定：未同步且无缓存且无 chunks 时立即返回 `status="indexing"`, `retry_after=3`, `chunks=[]`；
>   - warm 检索不阻塞 `_sync_lock`，立即基于当前可用索引执行 search，并追加 `indexing_in_progress` 与 `indexing_progress`；
>   - 目录不存在/被删除时防御性清空 chunks，返回空结果，避免死库缓存被持续召回。
>   - 跨库 fan-out 支持汇总 `indexing_vaults` 状态，单库 cold 不阻断其他 warm 库，query 向量在外部模型下仅计算一次。
> - `mortis_rag_mcp/server.py`：
>   - A4 摄取完成回调修复：`_on_job_finished(source, out_md)` 接收二参数，仅对已存在 indexer 请求 `immediate=True` 后台刷新，不再反向启动阻塞 sync。
>   - 只读 MCP 工具 `kb_read`、`kb_list_files`、`kb_stats` 前台不再同步等锁，改由 `request_refresh()` 后台驱动；
>   - `kb_read` 显式 `chunk_id` 寻址增加物理文件 sha256 签名校验（stale chunk 快速 fail-visible，杜绝读错章节）。
> - `mortis_rag_mcp/diaglog.py`：
>   - 代理层适配 `request_refresh`，统计调度开销，四阶段顺序（sync -> retrieve -> rerank -> serialize）与 corr_id 严格保持。
> - `tests/test_read_stale.py`：
>   - 新建 C66 专用测试套件，覆盖 Event 阻塞下前台秒级返回、冷启动 indexing 状态机、防抖突发合并、chunk_id stale 签名过期拦截、A4 完成回调集成等。
>
> **验证**：
> - C66 专项验收命令（86 passed in 48.94s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_read_stale.py tests/test_anti_contention.py tests/test_diaglog.py tests/test_multivault.py tests/test_scoped_search.py tests/test_ingest_server.py tests/test_p5_lifecycle.py tests/test_sync_engine.py tests/test_concurrency_hardening.py tests/test_search_oracle.py -q`
> - 全库回归测试（454 passed, 2 skipped in 108.83s）。

### C58a — moton16,2026-10-05,Antigravity — feat(config): add opt-in ingest auto watch and size policy
> **代码改动概况**：
> - `mortis_rag_mcp/config.py`：
>   - `IngestConfig` dataclass 尾部新增 `auto_watch: bool = False` 与 `max_file_size_mb: int = 20`，新增 `max_file_size_bytes` 属性（`max_file_size_mb * 1024 * 1024`，0 表示不限），保持既有字段顺序与默认 `enabled=False`；
>   - `IngestConfig.__post_init__` 与 `AppConfig.__post_init__`：严格校验 `auto_watch` 必须为真 `bool`，`max_file_size_mb` 必须为非布尔、非负、有限非浮点整数（`int >= 0`）；
>   - `load_config`：从 `[ingest]` 节解析 `auto_watch`（严格 `isinstance(raw, bool)`，拒绝 `"false"` 等字符串，抛出具名 `ValueError`）；`max_file_size_mb` 经 `_numeric(..., int, 20, 0)` 解析与范围门禁；
>   - 明确 `enabled=false + auto_watch=true` 为合法配置（有效但不激活运行时上传）。
> - `mortis_rag_mcp/ingest/worker.py`：
>   - 同步更新 fallback `IngestConfig` dataclass，保持相同的字段、默认值、类型校验与 `max_file_size_bytes` 属性。
> - `config/app.toml.example`：
>   - 在 `[ingest]` 节增补注释：明确默认关闭不上传、启用后扫描包含既有文档、支持全部 `INGEST_EXTS` 格式、默认 20MiB 上限（0 不限）、云端配额/费用与隐私影响声明、修改后需重启服务。
> - `tests/test_ingest_auto.py`：
>   - 新建测试套件，全面覆盖配置默认值、显式值解析、非 bool 严格拒绝、数值上下界与类型门禁、0 不限尺寸计算与边界判定、TOML fallback 解析兼容等。
>
> **验证**：
> - 专项测试（67 passed in 2.93s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_ingest_auto.py tests/test_path_migration.py tests/test_doctor.py -q`
> - 全局回归测试（479 passed, 2 skipped in 109.16s）。

### C58b — moton16,2026-10-05,Antigravity — feat(ingest): enforce size policy and persist automatic submission dedupe
> **代码改动概况**：
> - `mortis_rag_mcp/ingest/worker.py`：
>   - 统一尺寸上限策略门禁：新增 `_size_limit_bytes()` 与 `_check_file_size()`；显式 `submit(sources)` 在计算哈希和建任务前执行全量路径校验与尺寸判定，任一超限整批抛出 `ValueError`（all-or-nothing）；
>   - 扫描尺寸过滤：`scan_pending()` 在计算 sha 之前执行尺寸检查，超限文件标记 `reason="too_large"` 且跳过哈希计算；`submit(None)` 过滤超限文件并返回 `skipped_too_large` 计数；
>   - 运行时二次防御：`_run_job` 在沙箱检查后、创建产物和调用 client 解析前再次校验物理文件尺寸，防止入队后变大或恢复旧 queued 任务越过上限；
>   - 自动候选与持久化免重试：
>     - 新增 `_auto_pending()` 与 `auto_submit()`：仅在 `enabled=True && auto_watch=True` 时生效，默认关闭路径零扫描、零哈希、零线程；
>     - 注入动态 `ignore_provider`，每轮扫描动态获取最新 `.vaultignore`/排除规则，排除临时目录与 `.assets`；
>     - 引入 `state["auto_seen"]` 去重账本（`{source: {sha256, state, submitted_at, last_job_id}}`）：连续未变版本（含 done/failed/queued/parsing）自动跳过，源文件 sha 发生实际变化或 A->B->A 重新入队；
>     - 任务状态迁移原子更新：`_worker_loop` 转换状态时仅当 `last_job_id` 匹配时更新 `auto_seen`，防止已完成的旧任务覆盖排队中的新版本；
>     - 历史任务清理（>500）仅修剪 jobs，严格保留 `auto_seen` 账本去重凭证；完整无异常扫描支持清理已确认物理删除的源；
>     - 兼容迁移：旧版缺失 `auto_seen` 的 state 自动基于 `jobs` 最新 `submitted_at` 构建。
> - `mortis_rag_mcp/ingest/__init__.py`：
>   - 更新设计约束文档注释，从"仅显式触发"更新为"默认手动，显式授权后可自动（auto_watch=True）"。
> - `tests/test_ingest_auto.py`：
>   - 扩展 18 个测试用例，覆盖显式提交阻断、精确边界允许、扫描过滤、auto_submit 诊断统计、force 无法绕过上限、queued 恢复与入队后变大二次拦截、混合源全批回滚、Agent 额度 PyMuPDF 兜底、动态 ignore、四种状态去重、sha 变化与 A->B->A 重新入队、500 历史修剪免重传、旧 state 迁移、删除源修剪与零副作用读状态等。
>
> **验证**：
> - 专项测试（91 passed in 5.55s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_ingest_auto.py tests/test_ingest_worker.py tests/test_adversarial_v070.py tests/test_ingest_server.py tests/test_registry.py -q`
> - 全局回归测试（497 passed, 2 skipped in 113.71s）。

### C58c+C61 — moton16,2026-10-05,Antigravity — feat(watch): trigger coalesced automatic ingest across native and poll modes
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/watch.py`：
>   - 架构解耦与控制流图：增加架构设计注释与 ASCII 控制流图，阐明文本同步（毫秒级本地哈希与分块）与文档摄取扫描（秒到分钟级二进制哈希与云端 MinerU 解析）解耦的核心逻辑；
>   - 按需摄取扫描工作线程：新增 `request_ingest_scan(owner)` 与 `_ingest_scan_loop(owner)`，采用双检锁按需启动至多一条 `vault-ingest-scan` 守护线程，快速合并高频事件风暴（100 个事件只起一个 worker）；锁外调用 `_ingest_hook`，失败采用指数退避（0.5s -> 5.0s）；
>   - 原生事件双通道分类：更新 `_on_fs_events`，逐条分类文件事件为文本事件与摄取事件（纯 PDF 事件仅唤醒 `vault-ingest-scan`，零文本 sync 开销；纯文本事件仅唤醒文本防抖；混合与目录变动/events=None 双向派发）；
>   - 监听循环双模接线与 0 规则：`_native_watch_loop` 启动与每个 `fallback_interval > 0` 节拍触发摄取扫描；`_watch_loop` 启动与每 30s 节拍触发摄取扫描（若 `watch_fallback_interval == 0` 关闭原生兜底，轮询文档扫描仍保留 30s 默认节拍以防功能静默失效）；
>   - 优雅生命周期停止：`stop_watching` 设置停止标记、清理 dirty、通知条件变量并以 2s 超时优雅 join `vault-ingest-scan` 线程，停止后拒绝新扫描请求。
> - `mortis_rag_mcp/indexer.py`：
>   - `MarkdownIndexer.__init__`：新增摄取协调状态字段（`_ingest_hook`, `_ingest_lock`, `_ingest_cv`, `_ingest_dirty`, `_ingest_worker_thread`, `_ingest_stopping`, `_last_ingest_scan_at`, `_ingest_scan_failures`, `_last_ingest_error`, `_ingest_scan_start_lock`）；
>   - Facade 委托方法：暴露 `request_ingest_scan()` 与 `_ingest_scan_loop()`。
> - `mortis_rag_mcp/ingest/worker.py`：
>   - 复制判稳与写操作防抖（Req 9）：`IngestManager` 新增 `_stat_samples` 与 `_settling_files`（及 `mark_settling`）；`_auto_pending` 对 0 字节文件与采样仍在变化中的文件延后入队，且不阻断同批其他稳定文件；
>   - 入队后源文件变更防御（Req 10）：`_run_job` 在解析前复核当前 sha256，若与任务 hash 不符直接标记 `state="failed"`, `error="source_changed: ..."`，杜绝错误上传并允许下一轮扫描按新版本哈希入队。
> - `tests/test_watch_integration.py`：
>   - 新增 7 个专项测试：纯 PDF 事件零文本 sync、纯文本事件零摄取 hook、混合与 indeterminate 事件双触发、100 事件合并单线程、阻塞摄取 hook 不卡死文本 sync、轮询模式 0 间隔默认兜底、停止生命周期无泄漏线程、非 Windows 环境 auto 退回 poll 仍保留摄取扫描。
> - `tests/test_ingest_auto.py`：
>   - 新增 2 个专项测试：0 字节与动态写文件判稳延后不阻塞稳定文件、入队后修改文件 worker 拦截 `source_changed` 且免上传并在后续扫描重新入队。
>
> **验证**：
> - 专项测试（70 passed, 2 skipped in 6.14s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_watch_integration.py tests/test_fsnotify.py tests/test_ingest_auto.py tests/test_p5_lifecycle.py -q`
> - 全局回归测试（107 passed in 55.25s）：
>   覆盖 stale read、防争用、diaglog、多库、scoped search、ingest server、sync engine、并发加固、search oracle、chunkid read、facade freeze 等全部核心链路。

### C58d — moton16,2026-10-05,Antigravity — feat(server): wire automatic ingest with explicit status and safe defaults
> **代码改动概况**：
> - `mortis_rag_mcp/server.py`：
>   - 惰性挂载摄取 Hook（Req 1 & 2）：`_indexer_for` 在 `start_watching()` 之前仅在 `enabled && auto_watch` 为 True 时注入惰性 `_ingest_hook`（不在闭包定义时构造 manager，不提前创建 `.mortis-parsed` 目录）；
>   - 锁序死锁规避（Req 2）：`_ingest_manager_for` 注入动态 `_ignore_provider`（实时拉取 `idx.config.exclude_patterns`），禁止反向申请 `_indexers_lock`，消除反向嵌套死锁风险；
>   - 友好提示与矛盾检测（Req 3 & 5）：`_ingest_init_hint` 升级 `kb_init` / `kb_init_solo` 提示文案，明确容量上限（如 20MiB）、自动模式授权风险，并在 `auto_watch=true` 但 `enabled=false` 时给出矛盾警告；`kb_ingest(action="status")` 同样在矛盾配置下返回 `warning`；
>   - 状态只读快照（Req 6）：`kb_stats` 追加 `ingest_auto` 状态快照（configured, effective, max_file_size_mb, watch_method, effective_interval, last_scan_at, last_error, skipped 计数等），仅只读现有 manager / state，manager 不存在则 `last_scan_at = None`，绝不因查 stats 隐式启动扫描或 worker；
>   - 优雅停机（Req 9）：`shutdown` 与 `_kb_remove` 优先解除 `_ingest_hook` 引用，再触发 `stop_watching()`。
> - `mortis_rag_mcp/doctor.py`：
>   - 配置体检（Req 7）：`check_config` 诊断 `detail` 回显摄取有效状态（自动/手动/未启用）、容量上限及矛盾警告，保持 VALID 判定不因自动摄取关闭而失败；
>   - 状态快照（Req 7 & 8）：新增 `check_ingest` 与 `STATUS.md` 表格中的 `文档摄取` 栏，仅做已有 `.ingest_state.json` 的轻量快照读取并明确标注 `（报告生成时快照）`，不构造 manager、不发起任何网络调用。
> - `tests/test_ingest_server.py`：
>   - 新增 4 个集成测试：`kb_stats` 的 `ingest_auto` 快照字段完整性与有效性校验、矛盾配置下 `kb_ingest` 告警、Mock 端到端全链路（enabled+auto true -> 放 PDF -> 触发 hook -> fake parse -> .mortis-parsed md 落盘 -> A4 刷新 -> kb_search 命中）、auto_watch=false 时负向路径（零 hook、零 worker 启动）。
> - `tests/test_doctor.py`：
>   - 新增 2 个诊断测试：`check_config` 三态回显与矛盾警告检测、`check_ingest` 状态快照读取与报告生成时快照标注。
>
> **验证**：
> - 专项测试（106 passed in 10.68s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_ingest_server.py tests/test_ingest_auto.py tests/test_doctor.py tests/test_watch_integration.py -q`

### C67 — moton16,2026-10-05,Antigravity — feat(search): add opt-in compact result projection
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/models.py`：
>   - `Chunk.to_dict` 增加 keyword-only 参数 `compact=False`（Req 3 & 4）；
>   - 当 `compact=True` 时，直接构造并返回极简四键结构（`source`, `heading`, `lines`, `snippet`），排除 `id`, `score`, `title`, `metadata`, `char_count`, `source_pdf`, `content`，保持 Chunk 实例与缓存对象不可变；
> - `mortis_rag_mcp/_server/search_dispatch.py`：
>   - `dispatch_search` 兼容解析布尔字符串格式的 `compact` 参数（Req 2）；
>   - `compact=True` 强制激活 `preview=True` 并优先于 `mode="full"`；
>   - `_search_single_vault` 支持 `compact` 参数（Req 5）：单库顶层注入已解析的绝对路径 `vault` 与注册名称 `vault_name`，chunks 内部不重复记录库标识；
> - `mortis_rag_mcp/_server/fanout.py`：
>   - `fanout_search` 增补 `compact: bool = False` 参数并支持全路由通道；
>   - 组排序与最高分降序提前至 `pairs` / `Chunk` 层执行，杜绝投影后对无 `score` 键的 compact 字典排序（Req 7）；
>   - 平铺跨库（`group_by_vault=False`）每条 chunk 追加 `vault` 绝对路径，不重复 `vault_name`；
>   - 分组跨库（`group_by_vault=True`）在 group 顶层记录 `vault` 与 `vault_name`，group 内 chunks 不重复注入库属性；
> - `mortis_rag_mcp/server.py`：
>   - `_tool_definitions` 在 `kb_search` 的 inputSchema 中新增 `compact` 布尔参数（Req 1）；
>   - `_fanout_search` 透传 `compact` 关键字参数；
> - `tests/test_compact_search.py`：
>   - 新增 7 个专项测试：Chunk.to_dict 四键投影与不变量检查、kb_search 工具 schema 与布尔字符串容错、单库顶层归属、平铺跨库每 chunk 带 vault 免重复 vault_name、分组跨库组级归属、compact/full 顺序与过滤等价性、单库/平铺/分组行号+库路径 `kb_read` 原文回读闭环、30 篇中文笔记在分组与平铺形态下的真实 payload 缩减量测（分组减幅达 33.3% >= 30%）。
>
> **验证**：
> - 专项与相关测试（58 passed in 44.74s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_compact_search.py tests/test_preview_mode.py tests/test_budget_bytes.py tests/test_multivault.py tests/test_scoped_search.py tests/test_search_oracle.py tests/test_facade_freeze.py -q`

### C68 — moton16,2026-10-05,Antigravity — fix(search): enforce whole-result budgeting and explicit grouped cursors
> **代码改动概况**：
> - `mortis_rag_mcp/_server/fanout.py`：
>   - 废弃一切二分截断正文或 snippet 的破碎 chunk 逻辑，严格保持 chunk 全量原子性（Req 3 & 4）；
>   - 引入正整数前缀 $1 \dots N-1$ 二分搜索（`_binary_search_prefix`），构建统一形状评估真实 UTF-8 封装大小（Req 9）；
>   - 首条 chunk 超出预算时安全退化为正规空 envelope（`chunks=[]`, `returned=0`, `truncated=True`, 原游标保持），注入明确诊断提示 `budget_hint="use compact or increase budget_bytes"`（Req 6）；
>   - 最小 envelope 超限处理（`_build_overflow_response`）：当元数据（searched, errors, status 等）本身超过预算时，不产生虚假错误或非法截断，而是显式标记 `budget_exceeded=True`，经过最多 3 轮迭代计算真实稳定的 `minimum_budget_bytes` 并给出提示 `narrow vaults or increase budget_bytes`（Req 7 & 8）；
>   - 分组跨库分页与游标（Req 10–14）：废弃单值游标跨组伪进位，在 `group_by_vault=True` 且启用预算时，顶层 `next_offset` 设为 `None`，以字典形式显式返回 `group_next_offsets: dict[str, int]`（包含本页未分配到预算的候选组，未推进组保留原始偏移）；
> - `mortis_rag_mcp/_server/search_dispatch.py`：
>   - 严格参数校验 `_validate_group_offsets`（Req 12）：仅允许 `group_by_vault=True` 传入，校验字典值非负整数，校验 keys 必须属于本次已解析已授权库（拒绝未授权/未注册库，拒绝全局模式传入 solo 库，拒绝重复别名）；
>   - 单库冷状态（`is_cold` / `indexing`）及空结果统一经由 `apply_budget` 包装（Req 15），消除控制字段绕过预算的口径差异；
>   - 候选窗口稳定化（Req 16）：分组跨库固定 `per_vault_k = min(max_top_k, max(top_k, 20))`，确保多页分页过程中跨库候选集窗口稳定，消除深分页时候选池动态扩增导致 RRF 重排错位；
> - `mortis_rag_mcp/server.py`：
>   - `_tool_definitions` 在 `kb_search` 的 inputSchema 中新增 `group_offsets`（object，int 值）；
>   - 完善 `budget_bytes` 说明，明确首条超限空 envelope 与包络溢出声明；
>   - `_fanout_search` 转发 `group_offsets` 参数；
> - `tests/test_budget_bytes.py`：
>   - 更新旧用例 `test_budget_bytes_first_chunk_exceeds_budget`，断言整条 chunk 丢弃、`returned: 0`、`budget_hint` 出现、游标不跃迁，并对照测试 compact 模式下整条 chunk 正常装入；
>   - 更新 `test_budget_bytes_fanout_grouped`，断言每组独立游标、顶层 `next_offset is None` 以及 `group_next_offsets` 回传 `group_offsets` 续页无漏读；
>   - 新增 5 个深度对抗测试：最小 envelope 溢出迭代与 `minimum_budget_bytes` 精确声明、`group_offsets` 各种非法输入严格校验报错、`apply_budget` 输入对象不可变深比对、单库冷状态/空结果预算合规性、CJK/四字节 Emoji/反斜杠/转义字符 UTF-8 双层 json.loads 真实计量；
> - `tests/test_read_stale.py` & `tests/test_isolation_guard.py`：
>   - 消除 watcher sync 偶发竞争与子进程 Windows 编码警告。
>
> **验证**：
> - 专项与相关测试（46 passed in 23.33s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_budget_bytes.py tests/test_compact_search.py tests/test_multivault.py tests/test_preview_mode.py tests/test_search_filters.py -q`

### C69a — moton16,2026-10-05,Antigravity — fix(read): diagnose bounds and expose accurate continuation positions
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/reading.py`：
>   - 新增私有模块与冻结数据类 `@dataclass(frozen=True, slots=True) ReadResult`（Req 2）；
>   - 单次快照读取（`raw = path.read_bytes()`）并计算 sha256、物理总行数 `total_lines`、有效行范围与字符截断游标，杜绝检查陈旧度与读取内容二次读盘产生的不一致窗口（Req 4 & 14）；
>   - 严格越界诊断（Req 1 & 7）：当 `start_line > total_lines` 时，抛出具名 `ValueError`，明确包含 `requested` 请求行号、`actual` 实际物理行号、相对 `source` 以及「核对分卷行号或用heading定位」的修复建议；空文件无范围返回空内容，带范围明确报错；
>   - 字符上限与续读游标映射（Req 10, 12, 13）：正文规范化为 `\n.join(lines[start-1:end])`，字符截断时精准区分分隔符前 `(本行, len(line))` 与分隔符后 `(下一行, 0)`，输出 `content_end_line`、`next_start_line` 与 0-based `next_start_char`，支持单行长文本多页续读且无损严格拼接复原；
>   - 异常安全隔离（Req 15）：引入双继承异常 `ReadFileNotFoundError(FileNotFoundError, ValueError)`，边界捕获 `OSError` 与 `UnicodeDecodeError`，消除物理绝对路径向 solo 客户端的泄漏；
> - `mortis_rag_mcp/indexer.py`：
>   - Facade `read(source, start_line, end_line)` 薄委托内部 `_read_result`，保持编程接口完整不截字符契约（Req 5 & 9）；
>   - 新增私有薄委托 `_read_result(...)` 支持 `start_char`、`max_chars` 与 `expected_sha256` 单次校验；
> - `mortis_rag_mcp/server.py`：
>   - `_tool_definitions` 在 `kb_read` 的 inputSchema 中新增 `start_char`（integer, minimum 0, default 0）；
>   - `_kb_read` 严格入参解析（Req 6 & 11）：显式布尔、浮点、负数、零及 `end < start` 严格校验报错，杜绝布尔隐式转为整数 1 绕过验证；`chunk_id` 与 `heading` 模式显式禁止携带 `start_char`；
>   - 回显元数据扩充（Req 8）：成功返回结构增加 `total_lines`、`effective_start_line`、`effective_end_line`、`content_end_line`、`next_start_line` 与 `next_start_char`，兼容保留请求回显 `start_line` 与 `end_line`；
> - `tests/test_read_ranges.py`：
>   - 新增 13 个专项对抗测试：10行文件越界诊断提示定位要求、start1/EOF/EOF+1与钳制、空文件两分支、BOM/CRLF/Unicode无损解析、严格参数类型校验、未索引直连快速读取、沙箱与白名单后缀防御、单行长文本与多行跨页无损拼接复原、路径丢失与坏编码安全防护、协议级 isError 往返等。
>
> **验证**：
> - 专项与相关测试（61 passed in 12.60s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_read_ranges.py tests/test_indexer.py tests/test_txt_indexing.py tests/test_wikilink_read.py tests/test_kb_read_chunkid.py tests/test_facade_freeze.py -q`

### C69b — moton16,2026-10-07,Antigravity — feat(read): resolve heading sections from current source text
> **代码改动概况**：
> - `mortis_rag_mcp/_indexer/reading.py`：
>   - 新增 `scan_headings(lines)` 标题扫描器（Req 3–5）：基于原文字符串切片进行轻量级扫描，复用 `frontmatter`、`iter_table_blocks` 与代码围栏精确维护，严格跳过 YAML 元数据、长同字符代码块与 HTML 表格内部假标题；支持 ATX 标题（`#`–`######` 对应 level 1–6）与 TXT/MD 小说章节标题（如 `第1章 ...`、`Chapter 1 ...` 归一为 level 1），不把文件 stem / title fallback 当物理 heading；
>   - 在 `read_file_result` 中实现原文章节动态定位（Req 6–8）：
>     - 精确文本匹配 `query_heading = heading.strip()`；
>     - 零命中时抛出 `ValueError`，包含 `heading`、`source`、`total_lines`、前 5 个候选标题及起始行提示，列表过多提示总数，引导改用 `start_line`/`end_line`；
>     - 多个同名标题时拒绝隐式合并或选首项，抛出 `ValueError` 列出前 5 个起始行与总命中数，明确指示歧义并引导行号定位；
>     - 唯一定位时确定章节区间：`effective_start = matched_start`，`effective_end` 截至下一个同级或更高层级标题前一行（`level <= matched_level` - 1）或文件末尾，严格保留深层子标题与其正文内容；
> - `mortis_rag_mcp/server.py`：
>   - 废除原 `all_chunks()` 中跨切片并集计算 heading `min/max` 行号的陈旧逻辑，彻底解耦索引切片缓存，确保未索引直连文件与缓存陈旧文件均可准确命中最新物理章节（Req 2）；
>   - 完善 `_tool_definitions` 中 `heading` 字段说明（Req 1）：阐明精确标题包含子标题、同名报错改行号、行号区间优先等契约；
>   - 在无显式指定行号且传入 `heading` 时，将回显 `start_line` / `end_line` 与实际 `effective_start_line` / `effective_end_line` 对齐（Req 11）；
>   - 调整冷文件探测逻辑，仅对无后缀短名未索引时走 indexing 提示，带后缀真实文件无论索引状态均直通物理读取；
> - `tests/test_read_heading.py`：
>   - 新增 10 个针对性测试用例：核心固定 fixture 层级章节测试（Target=[2,5], Child=[4,5], A=[1,7], End=[8,8]）、同名歧义 fail-closed、未找到标题有限候选诊断、代码块/表格/frontmatter 假标题过滤、小说章节标题门禁与读取、显式 range 优先覆盖 heading、chunk_id 互斥、长章节超 `read_max_chars` 截断与无缝续读、未索引与陈旧缓存物理读取、JSON-RPC `handle` 协议级 `isError=True` 验证。
>
> **验证**：
> - 专项与相关测试（43 passed in 2.01s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_read_heading.py tests/test_read_ranges.py tests/test_wikilink_read.py tests/test_txt_indexing.py tests/test_chunking_seam.py tests/test_facade_freeze.py -q`

### C59 — moton16,2026-10-07,Antigravity — docs: explain Windows MCP process locks during upgrades
> **代码与文档改动概况**：
> - `docs/Quick-start_developer.md`：
>   - 在 §7（测试与已知平台坑）补充 Windows 升级时 console 入口 exe 被占用导致 `[WinError 5] 拒绝访问` 的机制说明与热更新限制；
>   - 在 §9（常见任务食谱）新增「升级已有部署（Windows 进程占用排查）」小节，给出客户端停连接器、只读 PowerShell 过滤特定进程（`Get-CimInstance Win32_Process`）、安全定向结束 PID、当前 venv 安装及推荐使用 `python.exe -m mortis_rag_mcp --serve-mcp-stdio` 减少入口 exe 被锁冲突的标准操作步骤；
> - `QUICKSTART_user.md`：
>   - 新增第 8 节「升级已有部署（Windows 避坑指南）」，通俗说明进程被锁与代码热更新失效的根本原因，给出客户端关闭、PowerShell 排查残留、当前 venv 重装与参数推荐的 5 步无歧义升级流程；
> - 规范核对：
>   - 确认文档中引用的包入口 `mortis-rag-mcp`、`vault-mcp` 与 `pyproject.toml` 中的 `[project.scripts]` 完全一致；
>   - 示例命令与路径全部使用占位符，不包含任何真实 vault 路径与敏感 API key。
>
> **验证**：
> - 纯文档改动，相关版本与协议测试保持绿灯（3 passed in 0.25s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_version_sync.py -q`

### C70/T8 — moton16,2026-10-07,Antigravity — docs: synchronize developer guides, update user docs, and enforce retrieval discipline
> **代码与文档改动概况**：
> - **Part 1：开发者主文档现状同步与历史欠账追溯（commit `bcd60af`）**：
>   - `docs/PROJECT_GUIDE.md`：
>     - 修正包名与入口描述（`mortis_rag_mcp`，`setuptools>=77`，MD/TXT 原生支持，PDF 转 MD 摄取）；
>     - 更新检索与刷新时序（只读检索优先、后台增量 refresh、锁粒度不跨网络 sync）；
>     - 更新 Facade/read/search 职责（compact 结构化投影、whole chunk 预算裁决、物理章节与区间读取、A4 双参回调）；
>     - 新增 §4.11 Ingest 模块职责（单队列、20MiB 大小门禁、auto_seen 判据账本与安全沙箱）；
>     - 更新 §6 工具 API 表与 §7 配置参考（对齐真实 schema、环境变量覆盖优先级与默认关说明）；
>     - 在 §15 倒序追加 v0.8.1 开发详录（Intake、C65–C70、C58、C59 全卡实施与测试证据）；
>   - `docs/Quick-start_developer.md` & `docs/Docs_Folder-descriptions.md` & `docs/Execution-plan_developer.md`：对齐测试用例基线、文件清单与版本目录规范，核验勾选完成状态；
>   - `docs/Changelog_developer.md`：追溯补齐 Lane E/A/B/C 技术卡（C53、C54+P0、C63、C55、C56、C57+C62、C64）详细条目。
> - **Part 2：用户文档、检索路由纪律与配置复核（commit `6c4361d`）**：
>   - `skills/mortis-rag-mcp/SKILL.md`：
>     - 递增 frontmatter 版本至 `5.3.0`，主标题同步为 0.8.1；
>     - 全面清除无样本依据的"降低 70%+ Token"量化宣传；
>     - 增补 C70.2 六条检索调用纪律（大候选初筛定向与预算、compact 无 chunk_id 的 source+行号回读契约、budget returned=0 恢复与 group 游标原样续页、read 实际总行数校正与同名 heading 消歧、indexing/stale 状态应对与禁擅自 rebuild、自动摄取默认关闭与用户显式授权边界）；
>     - 给出大候选 compact 初筛、区间回读、物理章节直读与用户授权 ingest 配置四组标准调用样例；
>   - `QUICKSTART_user.md`：
>     - 修正 §0.2 中的 preview 描述，删除无依据 70%+ 说法；
>     - 新增 §0.3 v0.8.1 检索与读取升级速查（compact 模式、heading 物理章节读取与重名消歧、只读优先、摄取 20MiB 门禁）；
>     - 同步 §6 常用工具速查表，增加 compact、budget_bytes、group_offsets 与 heading 参数提示；
>   - `README.md` & `README_EN.md`：
>     - 删除无样本限定的"降低 70%+"承诺，准确描述为轻量返回切片与行号、降低上下文开销；
>     - 去除固定"秒级"承诺，准确表述为后台增量同步与只读优先；
>     - 补充 0.8.1 紧凑初筛与物理章节直读核心特性；
>   - `config/app.toml.example`：
>     - 复核 `[ingest]` 注释，明确阐述自动摄取的费用/隐私成本、扫描 cadence 绑定 index 轮询周期、初始存量文件自动入队、0 表示不限制单文件尺寸及重启生效要求。
>
> **验证**：
> - 离线评测与不变量测试（29 passed in 1.02s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_version_sync.py tests/test_compact_search.py tests/test_read_heading.py tests/test_search_oracle.py -q`
> - schema 字节量测：14,433 字节（+14.88%，依 PLAN.md 882 行已批准作为安全与新参数完整性授权例外登记）。

### C60 — moton16,2026-10-07,Antigravity — chore: bump version to 0.8.1 and finalize release artifacts
> **代码与文档改动概况**：
> - `pyproject.toml` & `mortis_rag_mcp/__init__.py`：
>   - 版本号由 `0.8.0` 正式升级为 `0.8.1`（包顶层 `__version__` 单一真源驱动）；
> - `README.md` & `README_EN.md`：
>   - 顶部 Version badge 同步更新为 `0.8.1`；
> - `CHANGELOG_user.md`：
>   - 顶部新增 `## [0.8.1] - 2026-10-07` 发布说明，面向终端用户大白话阐明：
>     - 升级须知：100% 索引与缓存兼容、文档摄取 20MiB 默认上限与 0 不限配置、自动摄取默认关闭、整块预算与分组续页、先出结果后台静默刷新；
>     - 新增功能：紧凑初筛模式（`compact`）、物理章节与小说分卷直读（`heading`）、行号越界实际行数诊断、跨库切片唯一识别展开；
>     - 修复与改进：MinerU 预签名上传 403 签名修复（注明确切待实机验证状态）、Windows 升级进程占用说明、文件列表分页与前缀过滤；
> - `tests/test_version_sync.py`：
>   - 扩展版本同步守卫测试：断言 README.md 与 README_EN.md 的 Version badge、SKILL.md 标题包版本及 `diaglog.PACKAGE_VERSION` 与单一真源严格一致；
> - `tests/test_mcp_stdio.py`：
>   - 新增 `test_stdio_release_smoke_v081` 协议冒烟用例：验证 initialize 返回 0.8.1、15 个核心工具完整可见、新参数（`compact`, `start_char`, `group_offsets`, `heading`）暴露正确、`ping` 正常响应、越界行号与重名标题歧义均正确返回协议级 `isError=True`；
> - `docs/v0.8.1/PLAN.md`：
>   - 全量卡片状态核验收口：C60 打勾完成，7.2 发布清单 12 项全部核销。

---

## v0.9.0（Lane A–E，C90–C104 + 集中 review；2026-10-07 ~ 2026-10-08）

> **本节状态**：8 个提交**全部未 push、未 merge、未发版**（分支 `feat/v0.9.0-lane-ab`，
> 截至 `ad87d69`）。版本号、`CHANGELOG_user.md`、`docs/v0.9.0` 忽略规则均未动。
> **作者署名口径**：本节 8 个提交同属 v0.9.0 连续窗口，署名沿用本仓库同期口径
> `moton16` + `CodeBuddy,Deepseek-V4.1-Flash`；Lane A/B 两个窗口未在仓库留下独立署名，
> 如需更正请直接改标题行。
> **目标与合同**：`docs/v0.9.0/RESEARCH_REPORT.rev1.md`（C90–C104 卡片定义与执行回填）；
> 逐卡报告 `docs/v0.9.0/Lane_A_REPORT_2026-10-07.md`、`Lane_B_REPORT_2026-10-07.md`、
> `Lane_CDE_EXECUTION_2026-10-08.md`；集中 review `docs/v0.9.0/Lane_CDE_REVIEW_2026-10-08.md`
> 与待裁定清单 `docs/v0.9.0/DECISIONS_PENDING_2026-10-08.md`（后两者为本地证据，未入库）。

### Lane A（C90/C91/C92） — moton16,2026-10-07,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane A —— C90/C91/C92（doc_store/doctor/indexer/tests）

> **涵盖提交**：
> - `11d0ec0` `feat(v0.9.0): Lane A —— C90/C91/C92（doc_store/doctor/indexer/tests）`
>
> **代码改动概况**：
> - `mortis_rag_mcp/doc_store.py`（**新增，2196 行**）：版本化文档库 —— generation 层
>   `docstore.sqlite` + 库外控制面 `vault_<key>.control.sqlite`（权威发布指针 `control_meta`
>   与 `generation_registry`）；`StorageLayout` 解析、vault 绑定与归属冲突检测、
>   `recover()`、TTL/配额、写门禁与事务回滚语义、稳定错误 code（`StoreCorrupt`/`StoreBusy`/
>   `StoreConflict`/`StoreBindingMismatch`…）。
> - `mortis_rag_mcp/config.py` + `config/app.toml.example`（+97 / +13）：缓存身份统一到
>   `doc_store.vault_cache_key`（显式 `cache.id` 优先，否则规范化 vault 路径，跨进程/跨会话稳定）。
> - `mortis_rag_mcp/indexer.py`（+91）：`document_store(write=…)` 惰性接缝（读路径不得因一次查询建库）。
> - `mortis_rag_mcp/server.py`（+22）与 `mortis_rag_mcp/doctor.py`（+57）：宿主隔离诊断与
>   存储布局/文档库状态接线。
> - 测试：`tests/test_doc_store.py`（371 行）、`tests/test_doc_store_recovery.py`（370 行）、
>   `tests/test_virtual_config.py`（474 行）。
>
> **验证**：
> - Lane A 窗口本地全量回归 **616 passed / 4 skipped**（`--basetemp` 钉在仓库 `.runtime/`，未写 `%TEMP%`），
>   本轮新增测试 61 个。
> - 独立对抗审核（全新上下文子代理，5 组复现脚本）判定 **FAIL（无 P1）**；审核提出的 D3/D5
>   与主代理先行复现的事务泄漏均已修复并复核。遗留 P3 显式归档（见 Lane A 报告 §8）。

### Lane B（C93/C94） — moton16,2026-10-07,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane B —— C93/C94（ingest/doc_store/config/server/tests）

> **涵盖提交**：
> - `1bcaa6c` `feat(v0.9.0): Lane B —— C93/C94（ingest/doc_store/config/server/tests）`
>
> **代码改动概况**：
> - `mortis_rag_mcp/ingest/models.py`（**新增，510 行**）：`ResourceLimits`/`PageSpan`/
>   `MediaOccurrence`/`ParseResult`/`MediaSink`/`DictMediaSink`、`parser_fingerprint`、
>   `parse_retry_after`、`normalize_markdown`、图片头与像素预算、`redact_*` 脱敏与稳定 code 常量。
> - `mortis_rag_mcp/ingest/mineru.py`（+1090）：**构造前**归档准入（EOCD/中央目录/ZIP64/
>   多 disk/声明≠实际/压缩比/成员数）、`Retry-After` 三种形态（秒/HTTP-date/拒 NaN-Inf-负数）、
>   `_read_bounded` 的「声明尺寸」与「实际字节」双路径记账、逐字保留 `_put_upload` 空 Content-Type。
> - `mortis_rag_mcp/ingest/worker.py`（**新增，537 行**）：任务队列、租约与 owner fencing、
>   phase/subjob 上报、发布 CAS 与 `commit_job_revision`（发布+done 同事务）。
> - `mortis_rag_mcp/doc_store.py`（+1002）：`enqueue_job`（容量/合并/force 递增 request_seq/supersede）、
>   `claim_job`（UPDATE…WHERE CAS，多进程只有一个 owner）、`renew_lease`/`report_phase`/`fail_job`/
>   `cancel_job`/`retry_job`/`job_status`/`list_jobs`/`queue_depth`、`put_media_blob`/`attach_occurrences`
>   （流式媒体两阶段）、`record_auto_seen`/`iter_auto_seen`/`migrate_legacy_ledger`（只迁终态事实、
>   幂等、中间态不复活）、`get_derived_generation`/`mark_derived_generation`。
> - `mortis_rag_mcp/config.py` + `config/app.toml.example`（+65 / +19）：`ingest.storage/network_policy/
>   archive_max_mb/extracted_max_mb/markdown_max_mb/json_max_mb/media_max_mb/memory_budget_mb/
>   queue_limit/max_parse_workers`，`__post_init__`+`AppConfig`+`load_config` 三层校验。
> - `mortis_rag_mcp/server.py`（+46）：`_ingest_manager_for` 改走 `make_ingest_manager`
>   （virtual 先确保 indexer 存在，且在取 manager 锁**之前**，避免双锁嵌套）、`_kb_remove` 停 manager、
>   `shutdown()` 顺序、`kb_stats` 的无 state 文件容错。
> - 测试：`tests/test_ingest_archive_limits.py`（236 行）、`test_ingest_publication.py`（316 行）、
>   `test_ingest_virtual.py`（167 行）、`test_parsed_document_contract.py`（169 行）、
>   `test_ingest_mineru.py`（+125）。
>
> **验证**：
> - Lane B 窗口靶向批次 **317 passed / 1 skipped**（主代理本机运行结果）；C95 与审核修复在该窗口
>   已实现但**未提交**，逐位并入本节后续提交。
> - 独立对抗审核两路（`audit-c95`、`audit-ingest-r2`）收敛 3 个 P1 + 4 个 P2；原 `audit-ingest`
>   代理失败，不计其交付。
> - **如实记录未覆盖项**：`_ingest_manager_for(virtual)` 无独立单测；真实 MinerU/预签名 PUT 端点、
>   server virtual stdio 全链路、Linux 锁/权限/symlink/SMB 未测。

### Lane C（1/6：C96/C98 基础设施） — moton16,2026-10-08,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane C —— C96/C98（config/_indexer 切块身份/embedding profile/依赖 extras）

> **涵盖提交**：
> - `6e370f3` `feat(v0.9.0): Lane C —— C96/C98（config/_indexer 切块身份/embedding profile/依赖 extras）`
>
> **代码改动概况**（13 文件）：
> - `mortis_rag_mcp/config.py` + `config/app.toml.example`：`ingest.storage` 默认 **`virtual`**
>   （`legacy` 保留为显式回退）、切块/估算器/媒体配置口径。
> - `mortis_rag_mcp/_indexer/token_chunking.py`（**新增**）+ `embedding_capabilities.py`（**新增**）：
>   捕获 profile 驱动切块、`revision_id` 作用域的 chunk id、`embedding_key`（含模板实际输入与
>   媒体哈希）、oversize/`embedding_disabled` 统一跳过、估算器改名。
> - `_indexer/chunking.py`、`exemptions.py`、`scanning.py`、`watch.py`、`vector.py`：
>   切块代际与扫描/撤销/监听接缝更新。
> - `pyproject.toml`：新增 `[docs]`（PyMuPDF/pypdf/python-docx/python-pptx/openpyxl，注明
>   PyMuPDF 为 AGPL/commercial，非全 MIT）与 `[media]`（Pillow）可选 extras。
> - 测试：`tests/test_token_chunking.py`、`test_embedding_profiles.py`、`test_lane_be_config.py`（均新增）。
>
> **验证**：见本节末「批量验证」；本条提交后在提交内容上复跑受影响批次仍为 531 passed / 1 skipped。

### Lane C（2/6：C96 虚拟读取身份链路） — moton16,2026-10-08,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane C —— C96 虚拟读取身份链路与索引器接线（indexer/_indexer）

> **涵盖提交**：
> - `73a6f83` `feat(v0.9.0): Lane C —— C96 虚拟读取身份链路与索引器接线（indexer/_indexer）`
>
> **代码改动概况**（10 文件）：
> - `mortis_rag_mcp/indexer.py` + `_indexer/reading.py`：**虚拟与物理读取彻底分流** ——
>   虚拟 chunk 以自身捕获的 `revision_id`/`source_sha256`/`render_sha256` 做严格核验，
>   任一缺失即判 stale；物理签名（`_signatures`，虚拟源处为 `virtual:…` 串）**永不**被当作
>   虚拟 chunk 的校验依据；`allow_stale` 对 chunk 版本寻址无效（恒 STALE）。
> - 行号口径统一为 `rendered_markdown`；`resolve_virtual_source`（媒体/显式路径入口）与
>   `read_virtual_result` 共用同一 fail-closed 序列（ignore → 库归属 → active+committed →
>   物理源 SHA → 复核期 revision 变化）。
> - `_indexer/sync_engine.py`：**镜像精确排除**（已入库 active source 的镜像逐条精确不索引，
>   非目录级忽略；用户自建同名文件仍索引）、派生代际记账、向量按 `embedding_key` 复用。
> - `_indexer/search.py`：查询向量与 rerank 的闸门调用点。
> - 测试：`tests/test_virtual_read.py`、`test_virtual_read_server.py`、`test_virtual_sync.py`、
>   `test_chunk_identity.py`、`test_mirror_exclusion.py`、`test_lane_b_adversarial.py`（均新增）。
>
> **集中 review 修复随本提交落地**：
> - **F-2**：外部嵌入按 `batch_size` 切片时，某批结果未知后只拦失败批会让每轮 sync 重发
>   **已成功计费的前导批**且文件永不完结 → 改为索引批量路径按 profile 整体暂停（外部请求数为 0），
>   查询期嵌入不受影响。
> - **F-3**：换 embedding model/dimension 会让向量缓存**文件名**变化 → 旧文件根本找不到 →
>   闸门按「首次使用」放行未授权全库重付费嵌入 → 新增 `_note_foreign_embedding_profile`
>   检测同 cache key 下的其它向量文件并置 pending，逼出显式 `--approve-reembedding`。
>
> **验证**：复现探针实测「修复前 sync#1=1、sync#2=2、sync#3=3 次外部请求」→「修复后 2→2→2 且
> `_embedding_paused=True`」；漂移探针实测修复前 `may_use_paid_profile(新 fp)=True` 并发出请求、
> 修复后 pending=True。

### Lane C（3/6：C97 文档库/快照与迁移） — moton16,2026-10-08,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane C —— C97 文档库/快照导入导出与 legacy 镜像迁移

> **涵盖提交**：
> - `2718068` `feat(v0.9.0): Lane C —— C97 文档库/快照导入导出与 legacy 镜像迁移`
>
> **代码改动概况**（8 文件）：
> - `mortis_rag_mcp/_indexer/snapshot.py`：快照 v1/v2 导入导出。**构造 `ZipFile` 之前**做
>   EOCD/中央目录准入（复用 mineru 的只读准入，mmap 不整读入内存）；校验与 staging 阶段
>   **零活动状态变化**；跨文件回滚（`_backup_live_derived`/`_restore_live_derived`）；
>   `prepare_import` → 锁内 `change_seq` 复核 → `publish_import`（CAS）；
>   未知来源包默认拒绝，显式 `replace=true`+`confirm_replace=true` 才替换且被替换代登记
>   `retained_backup`；`_require_no_active_ingest` 忙线门禁。
> - `mortis_rag_mcp/doc_store.py`：`prepare_import`/`publish_import`/`restore_generation`、
>   generation 注册表状态机、`import_confirmations`。
>   **归属标注**：本文件按文件粒度归档在本提交，其中还含属 C98（派生代际表）、
>   C100（`payment_authorizations`/`request_intents` 与 `record_send_intent`/`mark_intent`/
>   `paid_authorization_state`）、C101/C103（媒体 Blob/Occurrence、`put_media_variant`）
>   的新增部分 —— 同一文件混着多张卡的 hunk，无法按卡拆分，故在此显式标注。
> - `mortis_rag_mcp/ingest/migration.py`（**新增**）：legacy `.mortis-parsed` 镜像迁移 ——
>   默认 **dry-run**、逐项校验 frontmatter/源相对安全路径（拒绝对化/上跳/盘符/UNC/symlink 逃逸）/
>   源 SHA/正文 NUL/媒体子树与 magic/配额，无法证明归属记 `pending_manual`，幂等
>   `already_migrated`，**不删除旧镜像、不重嵌**；已验证资产入 Blob/Occurrence。
> - 测试：`tests/test_snapshot_import_safety.py`、`test_snapshot_v2_store.py`、`test_migration_media.py`
>   （均新增）与 `tests/test_snapshot.py`、`test_parsed_document_contract.py`（追加）。
>
> **集中 review 修复随本提交落地**：
> - **F-4（P1）**：v2 导入原先把**包内**（不可信）chunk 正文以空签名写进活动 `chunks.bin` ——
>   当前进程看不见，但**重启/第二个会话**经 `_load_chunks_cache()` 会把投毒正文载回索引并可检索。
>   现改为与 v1 同口径：包内正文与包内 `fts.sqlite` **一律不发布**，只保留向量按 chunk.id
>   进补挂池；`kb_import` 返回体相应改为 `files=0/chunks=0` + `packaged_files/packaged_chunks/
>   text_published=false`（**属工具响应变更，需在 C106 一并确认对外文案**）。
> - **F-5（P2）**：导入失败时未 publish 的 staged generation 永远停在 `validated`（看似可用、
>   且无回收路径）→ 失败路径登记为 `aborted`，且**只标能确认非活动代者**。
> - **F-6（P2）**：忙线门禁 `list_jobs()` 抛异常时旧实现当作「无活跃任务」放行（fail-open）
>   → 改为 fail closed（`IMPORT_BUSY`）。
> - **F-7（P2）**：`published` 标志只在 `publish_import` **返回**后为真，控制面 CAS 已提交但
>   重新打开失败的窗口里派生层被回滚而 docstore 留在导入代 → 改为用一次**新鲜控制面读**
>   重判「是否实际已发布」，据此尝试回滚 docstore，且标签侧 fail closed（读不到活动代时不猜）。
>
> **验证**：探针实测 —— 修复前「失败导入残留 `('g0002','validated')` + 新实例 `_chunks=['a.md']`、
> `search('POISONED')=1`、正文=`POISONED PAYLOAD ZZZ`」；修复后「残留=`aborted`、新实例 `_chunks` 空、
> 命中 0」；失败导入前后 `active` 指针与 `change_seq` **逐项不变**。

### Lane E（4/6：C100 付费请求闸门） — moton16,2026-10-08,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane E —— C100 付费请求闸门（持久意图 journal + profile 授权）

> **涵盖提交**：
> - `42107ed` `feat(v0.9.0): Lane E —— C100 付费请求闸门（持久意图 journal + profile 授权）`
>
> **代码改动概况**（4 文件）：
> - `mortis_rag_mcp/paid_requests.py`（**新增**）：`PaidRequestJournal`（`before_send` 先落持久意图、
>   `mark_success`/`mark_unknown`、`pending()`）+ `paid_request_guard`（授权/撤销/pending 三态）+
>   `open_paid_control`。合同：结果只有 success/submission_unknown；不存正文只存哈希；
>   **闸门 fail closed**（控制面不可用即拒绝外部付费请求）。
> - `mortis_rag_mcp/providers.py`：`_JsonHttpProvider._post` 统一闸门（embed/rerank 共用；
>   journal 或 guard 缺失即拒绝），网络类异常标 `SUBMISSION_UNKNOWN`；JSON 准入与退避契约
>   （`Retry-After` 上界、jitter、`max_retries` 校验）。
> - `mortis_rag_mcp/doc_store.py`（本提交未改文件，但表结构在 3/6 内）：`payment_authorizations`
>   与 `request_intents` 两张表及 `authorize_paid_profile`/`revoke_paid_authorization`/
>   `paid_authorization_state`/`record_send_intent`/`mark_intent`/`list_request_intents`。
> - 测试：`tests/test_paid_request_journal.py`、`tests/test_http_json_admission.py`（均新增）。
>
> **集中 review 修复随本提交落地**：
> - **F-1（P1）**：`SUBMISSION_UNKNOWN` 后下一次 `sync()` 会把**同一载荷**重新发出去（异常文案
>   宣称 "automatic retry disabled"，实现里却只有"写"没有"读"）→ `before_send` 在落意图前检查
>   同 `kind+payload_hash+endpoint+profile` 的未决意图并拒绝（`PAID_REQUEST_UNRESOLVED`）；
>   `pending()` 同时列出 `prepared` 与 `submission_unknown`（此前后者在可观测性上消失）。
> - 恢复路径：人工确认/放弃该意图用公开 API `ControlStore.mark_intent(id,"abandoned")`；
>   `kb_stats` 会给出 `pending_paid_requests`（含 request_id）。**产品面尚无解绑 CLI**（待裁定 D5）。
>
> **验证**：修复前实测 sync#1=1 次外部请求、sync#2=2 次（journal 两条 `submission_unknown`，
> attempt 1/2）；修复后 sync#2 新增 0 次请求，未决期间外部请求数为 0。

### Lane D/E（5/6：C99/C101/C102/C104 摄取与媒体） — moton16,2026-10-08,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane D/E —— C99/C101/C102/C104（摄取路由、媒体索引、音频骨架与媒体闸门）

> **涵盖提交**：
> - `1b71b93` `feat(v0.9.0): Lane D/E —— C99/C101/C102/C104（摄取路由、媒体索引、音频骨架与媒体闸门）`
>
> **代码改动概况**（15 文件）：
> - `mortis_rag_mcp/ingest/router.py`（**新增**）+ `ingest/local.py`（**新增**）：`decide_route`
>   实际路由决策、`local_only` 语义、**进程级共享 `ParseBudget`**；legacy 音频路径明确拒绝（两处）。
> - `mortis_rag_mcp/ingest/worker.py`、`ingest/models.py`、`ingest/mineru.py`：虚拟摄取管线接线、
>   发布与准入（对照 1/6–4/6 的存储契约）。
> - `mortis_rag_mcp/_indexer/media.py`（**新增**）：确定性 image proxy、双向锚定、
>   逐行→全局 span、profile/generation 链接、oversize 切分、媒体代理 chunk 的 `line_basis`。
> - `mortis_rag_mcp/media_providers.py`（**新增**）：native 媒体 provider 走同一付费闸门；
>   **C102 保持闸门关闭**（缺 EG2 模型卡/部署/真实响应 fixture，不得猜协议）。
> - `mortis_rag_mcp/ingest/audio.py`（**新增**）：PCM 分段/时间锚定/`metadata_only`、
>   checkpoint+resume（按片段 SHA 幂等）、coverage 统计；**真实转录保持 unsupported**（无端点证据）。
> - 测试：`test_document_router.py`、`test_audio_ingest.py`、`test_ingest_route_audio.py`、
>   `test_media_indexing.py`（新增）与 `test_ingest_archive_limits.py`、`test_ingest_publication.py`、
>   `test_ingest_virtual.py`（追加）。
>
> **验证**：随本节批量验证；C102/C104 的**外部证据缺项**如实保留（见节末清单）。

### Lane C/E（6/6：C96/C103 工具面） — moton16,2026-10-08,CodeBuddy,Deepseek-V4.1-Flash — feat(v0.9.0): Lane C/E —— C96/C103 工具面（server/doctor/媒体只读派发）

> **涵盖提交**：
> - `ad87d69` `feat(v0.9.0): Lane C/E —— C96/C103 工具面（server/doctor/媒体只读派发）`
>
> **代码改动概况**（8 文件）：
> - `mortis_rag_mcp/server.py`：`kb_read` 的虚拟分流与短名歧义报错、媒体 refs 分页、
>   `kb_read_media` 工具（工具数 15 → 16）、`--approve-reembedding` 与 `--migrate-ingest`
>   （dry-run 默认）CLI、`kb_stats` 的付费闸门状态段。
> - `mortis_rag_mcp/_server/media_dispatch.py`（**新增**）：真实 `resolve_virtual_source` +
>   真实 store 签名、`metadata` 默认 / `inline` 显式、**整包 JSON-RPC 预算不截断 base64**、
>   MIME 魔术字节与「当前 active revision」双重复核、preview 变体持久化并复用（C103）。
> - `mortis_rag_mcp/doctor.py`：探活不越权（`cache.enabled=false` 即跳过并给出原因，
>   探活只落自己的持久意图，不写授权表、不解除任何库的 pending 审批）。
> - 测试：`tests/test_kb_read_media.py`、`test_media_dispatch.py`、`test_lane_be_server.py`（新增）与
>   `tests/test_doctor.py`、`tests/test_ingest_server.py`（追加）。
>
> **集中 review 修复随本提交落地**：`kb_stats` 暴露 `pending_paid_requests`
> （request_id/kind/state/attempt）与「未决暂停」的 `embedding_paused_reason`
> —— 否则「嵌入静默暂停」看起来像坏了；该可观测性也是 D5（解绑入口）的前置。

### v0.9.0 集中 review（第三窗口；未单独成提交，见 `Lane_CDE_REVIEW_2026-10-08.md`）

> **范围**：只读核对 + **3 轮对抗式独立证伪**（子代理全新上下文）+ 只修 review 确认的核心故障。
> 未做新功能、未加通用框架、未动版本号与用户 changelog、未并入 v0.8.2 DCGFH。
>
> **3 轮证伪的净收益**（子代理初稿全为静态推理，逐条由主代理回源码或写实测探针复核后采信）：
> - 第 1 轮（4 路子代理，按用户点名方向：虚拟签名身份、付费闸门可绕过/fail-open、
>   快照导入活动状态与残留、迁移/镜像误伤、媒体只读越权与本地路径泄漏）：
>   确认上表 F-1、F-3（P1）与快照侧 3 条 P2；并**否决**了其「新鲜库回滚不完整」的 P1 断言
>   （`prev_generation==""` 时登记函数提前返回，该场景不可达）。
> - 第 2 轮（3 路子代理，被明确要求把第 1 轮结论与**我刚写完的修复**当证伪对象）：
>   证伪并修掉**主代理自己引入的回归** F-2（多批切片重发前导批）与 F-5/F-7 的误标问题。
> - 第 3 轮（2 路子代理，证伪第 2 版修复）：证伪并修掉「未决暂停放进共用 guard 会连查询期嵌入
>   一起拦掉、语义检索静默退回词法」，收窄为只暂停索引批量路径；并修掉 `store.generation_id`
>   作为「当前活动代」的 fail-open 用法（改为新鲜控制面读）。边际收益递减后主动停在 3 轮。
>
> **本节 58 个文件的构成**：27 个已修改跟踪文件 + 31 个新增文件（10 个产品模块 + 21 个测试文件），
> 分 6 个提交按子系统归档（同一文件同时含多张卡的 hunk 时无法按卡拆分，沿用 Lane A/B 的 areas 约定；
> review 修复以 **F-1..F-7** 标注在提交正文，可用 `git log --grep=F-4` 定位）。
>
> **批量验证（本机、bundled Python、`--basetemp` 钉 `.runtime/`、未写 `%TEMP%`）**：
> - 38 个受影响测试文件：**review 前 524 passed / 1 skipped** → **review 后与提交后 531 passed / 1 skipped**
>   （新增 7 个用例、按新合同重写 1 个；跳过项为既有 `sqlite_vec` 未安装 best-effort）。
> - 复现探针（均在 `.runtime/review-cde-20261008/probe/`，属本地证据）：
>   `probe_paid_gate.py`（F-1/F-3）、`probe_multibatch.py`（F-2）、`probe_snapshot.py`（F-4/F-5）、
>   `probe_drift.py`（漂移检测）；`git diff --check` 通过、IDE 诊断 0。
>
> **仍未测 / 外部证据阻塞（不得据此宣称通过）**：全仓 pytest 与 CI（`gh pr checks`）、
> Windows+Linux×Python 3.10–3.13 矩阵、真实语料金测、10x 负载与故障注入、
> `sqlite_vec` 后端下的「导入→重启→零重嵌」链路（未安装该扩展）；
> EG2/native 媒体（C102 保持关闭）、真实转录端点（C104 保持 unsupported）、
> 真实宿主 image/audio 显示（`host_media_verified` 恒为 false）、`[docs]` extras 平台与许可证、
> 真实 MinerU/预签名 PUT 端点。
>
> **有意偏差与待裁定**（详见 `docs/v0.9.0/DECISIONS_PENDING_2026-10-08.md`，共 16 项）：
> ① `cache.enabled=false` 时外部付费请求一律拒绝（含 doctor 探活）；
> ② `ingest.storage` 默认切 `virtual` 的用户可见行为变更口径；
> ③ 镜像排除采用索引器侧精确排除而非 per-source ignore 登记；
> ④ v1/v2 均丢弃包内 `fts.sqlite`（FTS 与文本层统一由本机 sync 重建，导入后到首次 sync
> 之间词法检索为空）；⑤ legacy MinerU 通道**不在**付费闸门内（措辞需限定）；
> ⑥ 代际无回收路径（失败导入会留一份 docstore 拷贝，标签已诚实为 `aborted`）；
> ⑦ 改 `cache.dir`/`namespace`/`placement` 会绕过漂移检测；⑧ 未决付费意图暂无 CLI 解绑入口。
>
> **验证**：
> - 语法编译检查通过：`.\.venv\Scripts\python.exe -m compileall -q mortis_rag_mcp`
> - 靶向发版测试集（38 passed in 3.60s）：
>   `.\.venv\Scripts\python.exe -m pytest tests/test_version_sync.py tests/test_diaglog.py tests/test_mcp_stdio.py tests/test_facade_freeze.py tests/test_cache_codec_roundtrip.py -q`
> - 差异与格式守卫通过：`git diff --check`（0 警告/0 错误）。

### FIX-v081-docs — moton16,2026-10-07,CodeBuddy,GLM-5.3-Flash — docs: align read-priority wording and record v0.8.1 review follow-ups
> **代码与文档改动概况**：
> - 响应 feat/v0.8.1 二轮 review（`.runtime/review-v081/REVIEW.md`，PR #7）§3「旧残余与有意限制」的文档口径与续项登记：
>   - `CHANGELOG_user.md` / `QUICKSTART_user.md` / `README.md` / `README_EN.md`：
>     - 「不再发生前台同步等待」等绝对化表述改为「优先使用已就绪索引、后台静默刷新；并发更新时仍可能短暂等待」——共享 FTS 写锁未消除前如实陈述（对应 review §3.1）；
>   - `docs/Execution-plan_developer.md`：
>     - 按所有者裁定整份移除（此前登记在其上的 FTS 锁等待 / chunk 签名同代际两条审查续项不再单独维护，review 报告 §3 已有留痕）；
>   - `.gitignore`：
>     - 补齐本机代理配置目录（.codebuddy/.codex/.gstack/.cursor/.claude）、运行时产物（.runtime/、*.sqlite/*.log、.mortis_rag_mcp*、.env*）、测试与 IDE 噪声；docs/* 白名单行为不变。
>
> **验证**：
> - 纯文档与忽略规则改动，不触碰产品代码；`tests/test_version_sync.py` 守卫保持绿灯（README badge 与版本真源未受影响）。

### FIX-v081-code — moton16,2026-10-07,CodeBuddy,GLM-5.3-Flash — fix: address v0.8.1 pre-merge review findings R1–R6 and C54 isolation gaps
> **代码与文档改动概况**：
> - 依据 feat/v0.8.1 二轮 review（`.runtime/review-v081/REVIEW.md`，PR #7）落实 6 条确认发现与 C54 隔离缺口：
>   - **R1（P1）自动摄取绕过豁免**：`server.py` `_ignore_provider` 改复用 `indexer._ignore_matcher()` 动态规则（覆盖 .vaultignore；此前只有静态 exclude_patterns）；`_indexer_for` 先发布进 `_indexers` 再 `start_watching()`（消除启动竞态窗口）；`worker.py` `_auto_pending` 在 provider 返回 None 时 fail-closed 拒绝本轮自动提交；
>   - **R2（P1）首次同步误报唯一命中**：`server.py` 跨库 chunk_id 探测对已加载库的 incomplete 判定改为 `last_sync is None 且无完整缓存` 即跳过，不再因内存 `_chunks` 非空宣告探测完成；
>   - **R3（P2）判稳**：`worker.py` 两次间隔采样规则扩展到所有新/变化源（首见只登记 (mtime,size) 采样，一致才 hash/提交，复制中途的部分字节不再被解析上传）；`watch.py` `_ingest_scan_loop` 支持主动重扫——hook 异常退避后重试（连续失败≤5 次），auto_submit 返回 `rescan_after_seconds`（仍有判稳文件或扫描不完整）时延时重扫，不再干等下一个文件事件；
>   - **R4（P2）账本误清**：扫描遇忽略目录剪枝置 `pruned_by_ignore`，非完整枚举不清理 auto_seen 账本与判稳采样（临时排除目录后取消忽略不再同 SHA 重传）；
>   - **R5（P2）heading 章节截短**：`reading.py` `scan_headings` 表格配对只在 frontmatter 之后的正文上做并偏移回物理行号（frontmatter title:"<table>" 不再污染正文表格配对）；
>   - **R6（P2）skill 字段勘误**：`SKILL.md` compact 回读字段改为 `lines` 数组（start=`c.lines[0]`、end=`c.lines[1]`），预算恢复字段统一为 `minimum_budget_bytes`；
>   - **C54 隔离补漏**：`tests/conftest.py` 会话隔离同时暂存/清除/恢复 legacy `VAULT_MCP_REGISTRY`；`pytest_sessionfinish` 在 `--collect-only` 时跳过状态写入（不再凭空产生 0/0/0 的 status.json/STATUS.md/status.lock）。
> - 测试：`test_ingest_auto.py` 新增 R1 fail-closed / vaultignore 豁免 / 两次采样 / 账本保留 4 条回归，另 6 处既有断言适配两次采样语义（改为两扫模式）；`test_kb_read_chunkid.py` 新增首扫未完成库不可探测回归；`test_read_heading.py` 新增 frontmatter 表格字符串回归；`test_ingest_server.py` e2e 双触发 hook。
>
> **验证**：
> - 靶向：触碰的 7 个测试文件收敛后全绿（70 passed 复核）；
> - 全量（本批修复完成后单次）：551 passed, 4 skipped in 25.82s
>   `bundled python -m pytest tests -q --basetemp=.runtime/fix-v081-20261007/pytest-full -p no:cacheprovider`

### FIX-v081-final — moton16,2026-10-07,CodeBuddy,DeepSeek-V4.1-Flash — fix: close v0.8.1 final-round review regressions (N1–N4)

> **代码与文档改动概况**：
> - 依据 feat/v0.8.1 发版前最后一轮三路独立对抗审核（全新上下文子代理，明确指令为「证伪 R1–R6 修复声明」；报告存证 `.runtime/ship-v081-20261007/FINAL-ROUND.md`），全部关键证伪已由主代理回源码逐条复核后采信：
>   - **N1（本轮新引入，P1）补扫自激**：`_indexer/watch.py` 给 `rescan_after_seconds` 加连续补扫上限 `_MAX_INGEST_RESCAN_STREAK = 30`（与 hook 异常分支「连续失败 >5 次」对称），无需补扫的轮次将计数归零。此前「判稳残留或持久 OSError」会让 `rescan_after_seconds` 恒为正且消费端无上限，扫描循环被钉成 1Hz 永久重扫——每轮对全库重算 sha256 并重写状态文件；
>   - **N2（本轮新引入，P1）0 字节回归**：`ingest/worker.py` 恢复 0 字节在判稳比对**之前**短路（v0.8.0 语义：0 字节一律延后，不进上传链路）。R3 重写判稳逻辑时丢掉了该守卫，0 字节文件两次采样恒为 `(mtime, 0)` → 被判「已稳定」后直接 hash 上传空内容（`PROJECT_GUIDE.md` 与 `test_ingest_auto.py` 的「0 字节判稳延后」宣称因此失守）；
>   - **N3（本轮新引入，P1）账本永不清**：`ingest/worker.py` 把「本轮出现过剪枝就整轮不清账本」改为**逐条豁免剪枝子树**（`pruned_by_ignore` 标记 → `pruned_dirs` 集合 + `_under_pruned()`），`_stat_samples` / `_settling_files` 同步改造。原实现下只要库内存在任一被忽略目录（如 `cache.placement="vault"` 的 `.mcp_cache/` 或用户自建目录规则），真实删除的条目永不回收：账本无界增长，且删除后重建的同 SHA 文件不再被解析；
>   - **N4（既有未覆盖，P2）库根不可见清空账本**：`ingest/worker.py` 的 `vault_path.exists()` 为假的早退不再按「完整枚举 0 文件」处理（置 `clean_scan=False`、`scanned_sources=None`），避免未挂载 / 权限抖动 / 同步客户端整目录改名后整库重传（云端配额与费用）；
>   - **文档漂移订正**：`skills/mortis-rag-mcp/SKILL.md` 与 `QUICKSTART_user.md` 的报错文本与响应字段对齐真实实现（`actual: N` / 响应字段 `total_lines` / `存在歧义 (起始行: 42, 108)`；此前引用代码中不存在的 `actual total lines`、`ambiguous heading`、`candidates at lines [...]`）；`docs/PROJECT_GUIDE.md` 订正回调参数语义为 `on_job_finished(source, out_md)`（第二参为解析产物 Markdown 路径，非 `changed`）；`_indexer/watch.py` docstring 订正重试阈值 off-by-one；
>   - **幽灵引用清理**：`.gitignore` 白名单、`docs/Quick-start_developer.md`（4 处）、`docs/Docs_Folder-descriptions.md`（保留清单 5 → 4）、`mortis_rag_mcp/indexer.py` 注释中指向已按所有者裁定移除的 `docs/Execution-plan_developer.md` 的引用全部清理（`docs/Changelog_developer.md` 内的历史记账保留不改）。
> - 测试：新增 4 条**复现级**回归——`test_ingest_auto.py` 的「0 字节永不上传」「剪枝子树豁免但真实删除仍回收」「库根不可见保账本」；`test_watch_integration.py` 的「补扫连续次数封顶」。四条均在「暂存源码修复（`git stash`）」状态下确认变红，复现力已实证。
>
> **验证**：
> - 靶向：`test_ingest_auto.py` + `test_watch_integration.py` + `test_ingest_server.py` + `test_ingest_worker.py` = 86 passed in 6.21s；
> - 全量（本批修复完成后单次）：555 passed, 4 skipped in 24.30s
>   `bundled python -m pytest tests -q --basetemp=.runtime/ship-v081-20261007/pytest-full-2 -p no:cacheprovider`
> - 未修项（本轮新发现的既有形态，已如实登记，不阻断本次发版）：R2 热缓存库在后台重同步窗口内仍可被当作可探测完整库；R5 未闭合 frontmatter / YAML 块标量含 `---` 时表格配对偏移归零、缩进代码块内标题被识别；`.vaultignore` 读取异常被 `except Exception: pass` 吞掉（含 UTF-8-BOM 首行失效）；`server.py` 先发布后 `start_watching()` 使启动异常留下永不复试的半成品 indexer；`tests/conftest.py` 的 `--collect-only` 早退分支在套件内不可达（NO_STATUS_HOOK 会话级封顶先短路）。

### FIX-v081-ci — moton16,2026-10-07,CodeBuddy,DeepSeek-V4.1-Flash — test: make search assertions deterministic under read-first contract

> **代码与文档改动概况**：
> - 修复 v0.8.1 分支**从未在 CI 上转绿**的问题：`gh pr checks 7` 在 ubuntu 四个 job 上稳定 8 红（main `82989c8` 为绿，故确属本分支引入）。根因是 C66「读已就绪索引、后台刷新」把搜索路径的 `try_sync_with_guard(timeout=1.5)` 前台守护移除（对照 `origin/main:mortis_rag_mcp/_server/search_dispatch.py:96/131`），而 `kb_init` 只把首建丢进后台线程（`server.py:663`）——一批测试仍按「kb_init 后检索必然拿到完整索引」的旧契约写，成了时序依赖：Windows 上侥幸通过（本机 555 passed），Linux CI 上只拿到部分结果。
> - 测试侧按分支自己已有的模式改为确定性等待（不触碰产品语义）：
>   - `tests/test_budget_bytes.py`：新增 `_kb_init_ready()`（`kb_init` + 显式 `indexer.sync()`，`sync()` 走阻塞锁，后台首建会被等完再做一次无变更增量）与 `_is_search_settled()`；14 处 `kb_init` 调用点改走该助手；
>   - `tests/test_compact_search.py` / `tests/test_exact_terms.py`：同样加 `_kb_init_ready()` 并替换全部 12 处 `kb_init` 调用点；
>   - `tests/conftest.py`：新增交互式 stdio 轮询会话助手 `run_stdio_polling()`（+ `stdio_polling` fixture）——批式 stdio 一次性喂完 stdin，既表达不了真实客户端的「按 `retry_after` 稍候重试」，快速连发也等不到后台建库推进（实测 Linux CI 连发 50 条仍在首建中）；助手逐条发请求、逐条读应答、带真实间隔轮询到判据成立，并支持在同一条会话内续跑后续断言请求；
>   - `tests/test_budget_bytes.py::test_budget_bytes_stdio_integration` 与 `tests/test_txt_indexing.py::test_txt_indexing_and_chapter_headings` 改为走该助手：先轮询到首建完成，再在同一会话内跑预算/章节断言（原先一次性批式发请求，Linux 上稳定拿到 `status:"indexing"` 或 0 命中）；后者顺带移除已被取代的本地批式助手与随之失效的 `os`/`subprocess`/`sys`/`Path` 导入；
>   - `tests/test_read_stale.py::test_stale_chunk_id_signature_mismatch_fails_visible`：关掉 `_startup_index_all` 与 `start_watching`，锁定「索引持旧 chunk、磁盘已改」的窗口——后台刷新抢先跑完会让旧 `chunk_id` 变成 not found，断言落到另一条错误分支（Linux 必现）；
>   - `tests/test_kb_read_chunkid.py::test_kb_read_chunk_id_first_sync_partial_not_probeable`：同样关掉启动预索引与后台监听，让「首扫未完成」成为确定状态（否则 A 库的 `last_sync` 被后台 sync 重新写实，R2 守卫不再触发）。
> - 未改动任何产品代码：C66 的读优先语义是本版明确目标，本批只把测试从「时序依赖」改成「契约依赖」。
> - 注记（既有测试隔离缺口，非本批引入）：`registry_path()` 默认落在真实用户目录 `~/.mortis_rag_mcp/vaults.toml`，未 monkeypatch 注册表的用例会写进开发机（本次排查时该文件已有 `v`×3、`TestV` 等历史残留）；本轮预热方案的中间版本也误写过 5 条 `name = "OS"`，已清理，最终方案不再触碰该文件。
>
> **验证**：
> - 靶向：`test_budget_bytes.py` + `test_txt_indexing.py` = 18 passed in 2.36s；五文件组（`test_budget_bytes` + `test_compact_search` + `test_exact_terms` + `test_read_stale` + `test_kb_read_chunkid`）= 63 passed；
> - 全量（本批完成后单次）：555 passed, 4 skipped in 32.63s
>   `bundled python -m pytest tests -q --basetemp=.runtime/ship-v081-20261007/pytest-full-4 -p no:cacheprovider`
> - 真实用户注册表残留复查：`name = "OS"` 0 条（清理后无新增）。

---

## E20 运维缺陷与诊断（2026-10-09 / America_New_York）

Codex（本地施工；模型底模未由系统明确标注）。开工 `e35ec03`，用户已批准 E20-FIX/A/B/C/D。

- ADD-09-OPS-1：显式参数/环境指向缺失配置时失败，未配置自动默认仍合法；Python 3.10 fallback 只剥离字符串外的 `#` 注释，保留带空格、引号及 `#` 的路径。
- ADD-09-OPS-2：registry 读状态区分 missing/ok/unknown；拒读保留进程内已知注册项，未知状态禁止覆盖写，包括新实例直接 save；schema 不变。
- ADD-09-OPS-3：内存索引可用与 chunks/vectors/failed_files 持久化成功分开。内部 persistence_status 保留 layer/path/errno/error，既有 next_action 披露失败，未增加 MCP 字段；恢复写盘后消除诊断。
- E20-C：doctor 仅对现有 provider 谓词已证明 loopback 且无 key 的 local_free 显式动作豁免 cache=false；付费/带 key 对照仍拒绝且零请求。
- 隔离证据 `.runtime/beta2/E20/ops/`：正确红回归 9 failed/3 passed；修后 13 passed；registry 20、doctor 35、lane-config 30、missing-env 1、management 9、path-migration 10 passed，均 exit0。初次 fixture 导入名错误及不存在的 test_config.py exit4 原始日志保留，不作缺陷证据。
- 本批不联网、不安装、不操作真实库/用户配置；后续媒体、摄取、存储和测试施工由主流程继续整合。

### E20 F05/F06 媒体连接与认证收敛

Codex 主流程整合；施工子代理 gpt-6.1-sol/high。resolved media connection 统一 adapter/endpoint/model/dimension 与认证解析；transport/profile/journal 共享真实媒体身份，凭据不纳入 fingerprint，保持既有显式 media key、通用 key、专用 env 与新旧通用 env 优先级。三个 adapter env-only 回归、单字段身份变化及重启缓存对照落地。红回归 15 failed/5 passed，绿回归 20 passed；既有 transport/profile 靶向保留。证据 .runtime/beta2/E20/media/；全部离线 HTTP 边界，不声明真实端点通过。

### E20 registry 并发状态复核补足

Codex 主流程：load/save 读状态与内存快照置于既有 RLock；直接 save 复用既有进程文件锁，保持原锁序/重入策略，避免并发 load 把 unknown 状态改写后放过覆盖写。registry 20 passed、E20 ops 13 passed，exit0；证据 ops/registry-serialized 与 ops/green-serialized。

### E20 F01/F02/F03 存储不变量出口

Codex 主流程整合；施工子代理 gpt-6.1-sol/high。已有活动代数据库缺失/零长度显式失败，不创建替代空库；合法首次初始化先建库再发布指针。删除原子推进 change_seq、tombstone 与旧任务栅栏，source_seq 从已有事实生成 source-local CAS，schema/epoch 不变。generation pin 保护必要 committed revision，导出冻结 chunks staging member，并对 backup sequence、引用、最终 pin 做发布前验证，避免其他客户端换缓存后形成混包。原红 8 failed/1 passed，新增 immutable-cache 红2failed；主复核12passed，既有docstore/snapshot/recovery靶向通过。worker CAS消费者与首摄取删除sync补丁在下一批串行整合。证据 .runtime/beta2/E20/store/；不声明真实丢盘/旧用户库已验。
