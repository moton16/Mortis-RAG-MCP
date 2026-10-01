<!-- /autoplan restore point: C:\Users\14166\.gstack\projects\moton16-Mortis-RAG-MCP\main-autoplan-restore-20260929-080941.md -->

# v0.8.1 执行计划 — GitHub Issue #5 全量落地（含 auto_watch RFC）

> 2026-09-29 · 承接 v0.8.0（HEAD `82989c8`）与同目录 `REPORT.md`。
> 来源：[issue #5](https://github.com/moton16/Mortis-RAG-MCP/issues/5)（作者 Vodyanitsaaa，v0.8.0 实机实测报告）。
> 本文档是**动工前的施工图**：逐条给出核验结论、改动点、测试与验证命令、提交切分。
> 2026-09-29 增补：本文档经一次**独立子代理审阅**（只读源码与测试、不读本文件、不跑全量测试）。3 处结论被修订、2 处被确认、2 处被加固（另挖出 1 个更严重的测试污染源）；修订痕迹见文末「独立审阅记录」。
> 注意：`docs/*` 在 `.gitignore:19` 被整体忽略（只白名单放行 3 个主文档），本文件与 `REPORT.md` 一样是本地草稿，不进仓库。

## 意图

issue #5 里唯一会让用户直接踩死的是 MinerU 预签名上传 403（开了 PDF 摄取的人一个文件都传不上去）；其余为测试宿主隔离、两处 DX 契约、一处分页、一条 Windows 文档注记，外加一条与既有硬性设计冲突的 auto_watch RFC。本计划把 7 条**全部**落地，auto_watch 按用户裁定做成**显式 opt-in、默认关闭**，并保持 MCP 工具契约向后兼容（只加可选参数与新键，不改既有语义）。

版本策略（已裁定）：**一次发 0.8.1**，不拆补丁版——仓库没有「修复必须独立发版」的成文条文，`CHANGELOG_user.md` 本身按 Added/Fixed 混排，拆版的额外成本是可数的（版本三件套 + README×2 + SKILL 头 + 多一轮全量 CI）。若 C53 的实机验收久拖不决，再单独讨论补丁版。

## 已核验事实（动工前对照，勿重复调研）

| # | issue 声称 | 核验结论 | 关键证据 |
|---|---|---|---|
| 1 | `_put_upload` 触发 OSS 403 SignatureDoesNotMatch | **真**：本机回环探针实测旧代码发出 `Content-Type: application/x-www-form-urlencoded`（urllib 在 `data=` 且无 Content-Type 时注入），修法后发出空 Content-Type | `mortis_rag_mcp/ingest/mineru.py:79-81`；探针 2026-09-29 |
| 2 | test_diaglog 两个用例在配了 config.toml 的宿主上固定红 | **条件为真**：conftest 未 pin `MORTIS_RAG_CONFIG`，`VaultMcpServer()` 回落到 `~/.mortis_rag_mcp/config.toml`；仅 `embedding.mode == "external"` 且慢于 30ms / 1.5s 阈值才红 | `tests/conftest.py:28-34`、`server.py:376`、`config.py:342-362`、`_indexer/search.py:339-342`、`_server/search_dispatch.py:96-104`、`tests/test_diaglog.py:408,146` |
| 2b | （issue 未提）宿主注册表同理未隔离 | **真**：`tests/test_improvements.py:218`、`tests/test_adversarial_v070.py:260` 的 `VaultMcpServer()` 仍读真实 `~/.mortis_rag_mcp/vaults.toml` | 代码扫描 |
| 2c | （独立审阅发现）测试往真实缓存根写文件 | **真，且量级最大**：缓存根 `DEFAULT_CACHE_DIR` 无 env 可覆盖，而部分 stdio 用例的 app.toml 未写 `[cache] dir` → 缓存落真实 `~/.mortis_rag_mcp_cache`；本机实测累积 **18153** 个孤儿文件 | `docs/Changelog_developer.md:418`；`config.py:436-437` |
| 3 | `kb_init`/`kb_init_solo`/`kb_remove` 参数名不一致 | **部分成立**：`kb_remove`(`server.py:709-716`) 早已兜 `vault_path`/`vault`/`vault_name`；只有 `_kb_init`(612)、`_kb_init_solo`(652) 缺别名 | `server.py` |
| 4 | chunk_id 具有天然唯一性，可跨库自动寻址 | **前提有误**：id = `sha1(source \0 index \0 content)`，同文件复制进两库会撞 id；现有硬报错是设计且有测试锁定 | `_indexer/chunking.py:296`、`tests/test_kb_read_chunkid.py:279`、`server.py:562-563` |
| 5 | `kb_list_files` 无分页/子树过滤，大库输出数万字符 | **真** | schema `server.py:238-241`；`indexer.list_files()` `indexer.py:961-962` |
| 6 | Windows 下 exe 被占用导致 `pip install -e .` WinError 5 | **真**（属通用 Windows 行为），文档未提 | `QUICKSTART_user.md:109` 推荐 .exe 连接器；`docs/Quick-start_developer.md:192-203` 无相关说明 |
| 7 | 建议新增 `[ingest] auto_watch` + `max_file_size_mb` | **与既有硬性设计冲突，需 opt-in 实现**；且 per-channel 硬闸门（v4 200MB / agent 10MB，错误码 `-60005`/`-30001`）已存在，新键是**策略层前置拦截**（省额度、提前失败），不是补安全缺口 | `mortis_rag_mcp/ingest/worker.py:1-4`「不做 watcher 自动摄取」；`config.py:115-129` 现无这两个键；`ingest/mineru.py:25-32,107-122` |

## Scope

- **In**：issue #5 的 7 条全部；auto_watch 做成默认关闭的显式 opt-in；配套测试、文档、版本收口。
- **Out**：
  - `docs/v0.8.1/REPORT.md` 的 P0（`test_gate_9_retryable_mineru_error` 竞态）/ P1（PROJECT_GUIDE 转义清理）/ P2（`Changelog_developer - 副本.md` 处置）——并列候选，是否并入见「仍待裁定」。
  - 不引入任何第三方依赖（`dependencies = []` 铁律）；不改动已有 MCP 工具的必填参数与返回语义。

## Action items

### [ ] C53 — fix(ingest): MinerU 预签名 PUT 显式置空 Content-Type（issue #1）

- 改动：`mortis_rag_mcp/ingest/mineru.py:79-81`，`_put_upload` 改为
  `urllib.request.Request(url, data=payload, headers={"Content-Type": ""}, method="PUT")`，
  注释写明原因链：urllib `AbstractHTTPHandler.do_request_` 会在有 `data` 且无 Content-Type 时注入
  `application/x-www-form-urlencoded`；OSS V1 预签名把 CONTENT-TYPE 计入 StringToSign（服务端用**实际请求头**重算），
  故 403 `SignatureDoesNotMatch`。v4(`:159`) 与 Agent(`:215`) 两通道共用本函数，一处修全局生效。
- 需要知道的事实（评审补充）：`http.client` 会发出 `Content-type: `（**空值头**），不是「不发这个头」；urllib
  无法真正不发头（传 `None` 会 `ValueError`）。OSS V1 的 StringToSign 下空值与缺省等价，所以修法可用。
- 回归测试（`tests/test_ingest_mineru.py` 新增 1 条）——**两条断言都要**：
  ① 本机回环 `http.server`（端口 0）捕获 PUT 头，断言收到的 `Content-Type` **不是** `application/x-www-form-urlencoded`，且 200 不抛错；
  ② **`req.has_header("Content-type")` 为真**。第二条锁的是**回归安全**（防止未来重构退回 urllib 的自动注入），不是当前行为——
  只断言①等于「空值或缺省都放行」，没有锁定任何东西。
- 验证：`.\.venv\Scripts\python.exe -m pytest tests/test_ingest_mineru.py -q`
- **未覆盖项（发版必填字段，E1 降级形态）**：真实阿里云 200 需维护者带 MinerU key 实机确认一次。
  本卡完成时必须在 `docs/Changelog_developer.md` 的条目里二选一写明：**「实机验证：已通过（日期）」或「待实机验证（原因）」**。
  禁止留空——这正是 v0.8.0 那次「旗舰功能 100% 坏、3 天无人察觉」的结构化成因。载体沿用
  `docs/PROJECT_GUIDE.md:764-769` 的发布 4 步 + `Changelog_developer.md` 的「验证：」记账先例（C13/C14）。
  可选（不阻塞）：`scripts/smoke_mineru.py` 手动脚本；`ci.yml:10` 已有 `workflow_dispatch`，需要时可挂手动 job，
  **不进自动 CI**（单 job、零 secret 通道、且每次运行烧付费额度）。
- 残余风险（如实申报）：空 Content-Type 在其他 S3 兼容服务（非阿里云 OSS）上的行为**未验证**；本卡只对 OSS V1 预签名有证据。

### [ ] C54 — test: 测试隔离三件事（issue #2 / 2b；审阅修订后范围）

- **C54a 配置隔离（必做）**：`tests/conftest.py`
  - 新增 **session 级** fixture 生成一个真实存在的 app.toml（内容仅 `[cache] dir = "<session tmp>/cache"`，
    保持 `enabled=true` 语义不变、把缓存落点搬出真实 home）；
  - 新增 **function 级 autouse** fixture：`monkeypatch.setenv("MORTIS_RAG_CONFIG"/"VAULT_MCP_CONFIG", ...)`；
  - 会话启动时 `os.environ.pop("MORTIS_RAG_REGISTRY", None)`：宿主若导出新名会压过用例自设的旧名
    （`registry.py:126-127` 新名优先），历史上造成过 `KeyError: 'description'` 假失败（`Changelog_developer.md:418`）。
- **C54a' 缓存根 env 覆盖（新增，用户裁定 D3）**：`mortis_rag_mcp/config.py`
  - 新增 **`MORTIS_RAG_CACHE_DIR`**（新名优先 / 旧名 **`VAULT_MCP_CACHE_DIR`** 兼容），语义与 `MORTIS_RAG_*` 系列一致
    （`MORTIS_RAG_API_KEY` / `MORTIS_RAG_CONFIG` / `MORTIS_RAG_REGISTRY` 已是这个模式）；`config/app.toml.example` 补注释。
  - **位置是硬要求（评审 N3）**：覆盖必须**短路在 `resolve_default_cache_dir()` 的目录改名逻辑之前**。
    `config.py:72-84` 在「新目录不存在且旧目录存在」时会 `os.rename` 原子搬迁 `~/.vault_mcp_cache`；
    若覆盖落在其后，跑测试会触发宿主的目录搬家。降级说明：这不是本版新引入的风险——
    `config.py:437` 显示该函数**今天已在每次 `load_config()` 生效**，且 rename 是 v0.7.1 设计内的原子迁移
    （`CHANGELOG_user.md:70,86-87`），不是数据销毁。env 短路严格更优，故做，但不阻塞发版。
- **C54b 配置补齐（必做；污染的真正来源）——⚠ 机制与清单均已修正**：
  - **修正后的机制**（原判据「`_init_cache_paths` 无条件 mkdir，与 `cache.enabled` 无关」**是错的**）：
    `indexer.py:195` 是 `if self.config.cache.enabled and self.config.cache.dir:`，`198-203` 另有 `except OSError` 降级。
    真实机制是：**`cache.enabled` 经 `load_config` 默认 true，`dir` 未显式设置时回落到真实 home**
    → 所以**任何「没有 `[cache]` 段」的 app.toml 都会往真实缓存根写**；而写了 `[cache] enabled = false` 的
    （如 `tests/test_search_filters.py:182`）**本来就安全**。
  - **修正后的可执行清单**（原计划写「逐一排查所有 stdio 用例」，指令本身完整，以下是省去逐个排查的清单）：
    15 个 stdio 用例里只有 3 个写了 `[cache] dir`（`test_solo_vault.py:40-43`、`test_registry_server.py:31-34`、
    `test_multivault.py:63-66`）；**缺的 12 个**：`test_wikilink_read.py:47`、`test_ingest_server.py:202`、
    `test_txt_indexing.py:51,119`、`test_subvaults.py:99,123`、`test_scoped_search.py:34`、`test_exempt.py:223`、
    `test_preview_mode.py:106`、`test_diaglog.py:333-345`、`test_mcp_stdio.py:28`、`test_kb_read_chunkid.py:89,260`、
    `test_budget_bytes.py`（8 处）、`test_anti_contention.py:133`。另**进程内**用例同样需要（不受 C54a 的 env pin 影响，
    因为显式 `config_path` 优先于 env）。`test_snapshot.py:211-217` 已写但它是**进程内**用例，不是 stdio。
- **C54c 注册表隔离（可选，单独评估）**：消除 2b（`tests/test_improvements.py:218`、`tests/test_adversarial_v070.py:260`
  读真实注册表）。注意：**必须 per-test 文件**，共享 session 文件会造成跨用例状态泄漏；并评估 pytest tmp 目录膨胀。
- 必须遵守的坑（**已修正**）：
  1. pin 的路径必须是**真实文件**（`resolve_config_path` 对「env 指定但不存在」的路径会继续回落 home，
     `config.py:346-355`，假 pin 会静默失效且可能把宿主的 external embedding + 真 key 引进测试）；用 session 级单文件
     而非每用例 `mktemp`，避免加剧 tmp 碎片。
  2. **function 级 fixture 必须显式声明依赖 session 级 fixture**。pytest 只按依赖图 + 作用域排序，autouse 的
     function 级 fixture **不会**自动继承 session 级 → env 可能指向尚未创建的文件，触发上一条的静默回落。
     （`autouse=True` 写在 session 级 fixture 上是同样有效的替代写法，一行即可根治。）
  3. **`os.environ.pop("MORTIS_RAG_REGISTRY", None)` 必须配套由 fixture 自设一个临时注册表**。`tests/conftest.py:15`
     的 `pytest_sessionfinish` 守卫读的是 `os.getenv`——pop 之后若不再 set，守卫失效；
     虽然 `conftest.py:29` 还有一层「宿主已存在 `status.json` 才更新」的封顶，但方向是错的，不要制造这个依赖。
     （**更正**：原计划把 pop 当作必做项，评审指出它只有在「pop 后不再 set」时才有害。）
  4. `cache.enabled = false` 的配置是安全的，不要为了统一而给它加 `dir`。
- 影响面（已核验）：31 处显式传 `config_path` 的构造点不受影响；15 个子进程用例全部 `env={**os.environ, ...}`
  且显式传 `--app-config`，pin 会被继承但其行为由 explicit 决定；唯二需要 delenv 的用例
  （`tests/test_path_migration.py:133,136`、`tests/test_doctor.py:442-443`）已自带 delenv，不会红——`test_doctor.py:446`
  会先调 `_stub_home` 删掉两个变量，已逐行核对（439-454 行）。
- 验证：`pytest tests/test_diaglog.py tests/test_path_migration.py tests/test_doctor.py -q`，
  再单跑 `tests/test_improvements.py`、`tests/test_adversarial_v070.py`、`tests/test_exempt.py`、`tests/test_mcp_stdio.py`。

### [ ] C55 — fix(server): kb_init / kb_init_solo 支持 vault_path 别名（issue #3）

- 改动（handler 层，保留）：`_kb_init`(server.py:612-613)、`_kb_init_solo`(server.py:661-663) 取参改为
  `path or vault_path or vault or vault_name`，与 `kb_remove`(`:709-716`)、`_indexer_for`(`:565-572`)、
  `_kb_describe`(`:744-750`)、`kb_ingest`(`:778-784`)、`kb_read`(`:907-913`) 的五处既有口径对齐。
- **诚实评估（评审 N2 终裁，必须知道）**：handler 别名**有效但不可发现**。
  - `kb_init_solo` 的 `inputSchema`（`server.py:192-195`）只有 `path` / `name` 属性 + `required: ["path"]`。
    模型读的是 schema → 它不会知道可以传 `vault_path`。
  - 但 MCP 客户端通常**不拒收** properties 之外的参数（未禁 `additionalProperties`），所以别名对
    「模型受其余 15 个工具的 `vault_path` 习惯影响而误传」是**有效的**——那正是 issue #3 的痛点。
  - **不改 schema 是刻意的取舍**：`docs/PROJECT_GUIDE.md:1099` 有「`tools/list` schema 体积增量 ≤10%」的门禁。
  - 另：`kb_remove` 的四别名（`:709-716`）同样**没有任何测试用别名**，`PROJECT_GUIDE.md:496-500` 也只文档化了 `path`
    → 这是既存的口径不一致，不是本卡新引入的。
- **本卡真正该补的（评审三轮的共识）**：
  1. `kb_init` / `kb_init_solo` 的 schema **`description` 里写明**「`vault_path` 亦可，等价于 `path`」——
     这是零成本的可发现性修复，不触碰体积门禁。
  2. `server.py:615`（`path is required for kb_init`）与 `:663`（solo 的同类报错）的**报错文案**要提示别名可用。
- 测试：`tests/test_scoped_search.py` 追加：以 `vault_path` 调 `kb_init_solo` 成功；旧的 `path` 路径回归。
- 验证：`pytest tests/test_scoped_search.py tests/test_solo_vault.py -q` + **复测 `tools/list` 的 schema 体积增量 ≤10%**

### [ ] C56 — feat(server): kb_read(chunk_id) 跨库自动寻址（issue #4）

- 事实纠正：chunk_id = `sha1(source \0 index \0 content)`（`_indexer/chunking.py:296`），不是纯内容哈希；
  同一文件复制进多库（同相对路径 + 同切片序号 + 逐字节同内容）会撞 id。另外 `kb_read` 只有 `vault_path`，
  **没有 `vault_paths`**（`_indexer_for` 只认四个单值键，`server.py:566-572`）。
- 设计（`_kb_read` 重构：先看 chunk_id，再决定是否解析 indexer）：
  - 探测范围必须覆盖**未加载的库**：`_indexers` 是惰性字典、可能缺条目，只探它会谎报「未找到」。已加载的直接读
    `all_chunks()`；未加载的用临时 `MarkdownIndexer(path, config)` 探测。**绝不可走 `self._indexer_for`**
    （那会起 watcher 线程 + 持目录句柄 + 注册 atexit）。遍历源用 `registry.load()`。
    探测只读现成内存/缓存态：不触发 sync、不触发 embedding。
  - **⚠「轻量构造」并不轻（三轮评审一致确认，原描述的「可接受」是错的）**：`indexer.py:195` 门控通过后，
    `_init_cache_paths()`（`:249-282`）会 ① `_load_chunks_cache()` **全量加载文本层**；② `_load_vectors_cache()`
    全量加载向量（`:280-281`，仅当 `vector.backend != "sqlite_vec"`）；③ 构造 `FtsIndex`（`:205-218`，`fts.py:43-50`
    **会真建 sqlite 文件**）并可能调 `_fts_ensure_populated()` **写盘**。无网络、无线程、无 atexit——这几点原来判断正确。
  - **修法（改两行，不新增抽象）**：给 `MarkdownIndexer.__init__` 加 `load_vectors: bool = True` 开关，
    探测时传 `False`，省掉第 ② 步。
  - **⚠ 残余（评审盲点 B4）**：`load_vectors=False` **盖不住第 ① 步**——chunks 层仍全量加载。本卡先修向量部分；
    chunks 部分的代价评估见新增 **C64**。（原判据的「20 库 × 20MB ≈ 400MB」**无实测依据**，已撤回；真实量级需实测。）
  - **错误路径必须显式**（原计划未指定）：逐库探测时 `OSError` / `zlib.error` / `FileNotFoundError`
    （注册表有条目但目录已删）→ **跳过该库并计入「未探测库」**，不得中断整个 `kb_read`，也不得计入「已探测」。
  - **solo 库是否参与探测必须明确**：`kb_search` 的 fan-out 跳过 solo 库并列出 `excluded_solo`（`server.py:440` 一带）。
    `kb_read(chunk_id)` 是显式寻址而非 fan-out，建议**参与探测**（用户已经指名了具体切片），
    但要在结果与报错文案里说明该库是 solo——否则会出现「搜不到但读得到」的口径矛盾。**这是本卡必须拍板的一处。**
  - **可观测性**：零命中报错里要带上「共 N 个候选库，探测了 M 个，跳过 K 个（列出名字与原因）」——
    否则用户拿到「未找到」无从排障（原计划的「如实说明」缺具体形状）。
  - **单命中**：用该库展开，结果追加库归属（`vault_path` 绝对路径 + `vault` 名）。已核对
    `tests/test_kb_read_chunkid.py:72-78` 是逐键断言而**不是**键集等价断言 → 加键不破现有测试。
  - **多命中**：fail-closed，报错并列出候选 vault（依据：仓库歧义报错口径 `server.py:453-459`、`1084-1087`）。
  - **零命中**：只有在全部候选库都探过之后，才允许沿用现有的 `chunk_id not found: …（文件可能已修改…）`；
    若有库未探测（未加载/未索引），必须如实说明，不得谎报「文件已修改」。
- 契约变更（必须显式记账）：`tests/test_kb_read_chunkid.py:278-280` 现断言「多库 + 无 vault_path → 报错」，
  需改写为「唯一命中自动展开」，并新增撞 id 的 fail-closed 用例；`skills/mortis-rag-mcp/SKILL.md`
  的多库阅读纪律同步改。
- 验证：`pytest tests/test_kb_read_chunkid.py tests/test_mcp_stdio.py -q`

### [ ] C57 — feat(server): kb_list_files 分页与子树过滤（issue #5）

- 改动：schema（server.py:238-241）增 `limit` / `offset` / `path_prefix`；handler `_kb_list_files`(963-966)
  在 `list_files()` 结果上做前缀过滤与切片，返回增加 `total` / `truncated` / `next_offset`
  （不传参数时行为与现在完全一致）。
- **`total` 语义必须钉死**：`total` = **前缀过滤后、切片前的条目数**（不是「已索引文件总数」，也不是「本页条数」）。
  消费方靠它判断是否还有下一页；`next_offset = offset + len(files)`（无更多时为 `None`）。
- `path_prefix` 语义与 `kb_search.path_prefix` 对齐（库内相对 posix 前缀匹配）。
  **注意：这里没有穿越风险**——`models.py:143-166` 是 `chunk.source.startswith(prefix)`，`source` 恒为库内相对路径，
  `../` 或绝对路径只会**零命中**（天生 fail-closed）。所以本项是 **DRY 要求**（避免两套前缀语义漂移），
  **不是补安全缺口**。但两端实现应共用同一函数。
- **⚠ 同名字段、相反缺省（评审盲点 B1，必须处理）**：`kb_search.limit` 的缺省语义是「用 top_k」（`server.py:257`），
  而 `kb_list_files.limit` 的缺省是「返回全量」（现状如此，C57 承诺不变）。同名参数在两个工具里语义相反，
  模型必然误判。**二选一，必须在卡里拍板**：(a) 保持「缺省全量」，并在 schema `description` 里**显式写明**
  「缺省返回全部已索引文件，大库请传 limit」；(b) 给 `limit` 设一个保守缺省（如 200）并把「取全量」变成显式
  `limit=0` 或 `all=true`——**但这与 C57 的兼容性承诺冲突，需要改 plan 的兼容性口径**。建议 (a)。
- **⚠ `truncated` 撞名（评审盲点 B2，必须处理）**：v0.8.0 已有一个 `truncated`，语义是**字节预算截断**
  （`fanout.py:50-58` 的 `apply_budget`）。C57 新增的 `truncated` 是**分页截断**。同一响应体里两个 `truncated`
  会让消费方误读。**建议改名 `page_truncated`**，或在 `description` 里明确区分两者；不要复用同名键。
- **`limit` 上限**：**不加**。理由（第三轮已证伪第二轮的主张）：`kb_search.limit` 虽在 schema 里只有 `minimum`，
  但 handler 侧已被 `_search_filter` 夹取（`server.py:98` 的 `min(limit, max_limit)`，**已实测复核**）；
  `budget_bytes` 是 `kb_search` 的专属可选参数（`:259`；`fanout.py:43-44` 仅非 None 时生效），
  `kb_list_files` 没有这条通道；且 `list_files()` 返回的是摘要 `{source,title,chunks}`（`indexer.py:962`），
  与返回全文 chunk 的 `kb_search` 差几个数量级。给 `limit` 加上限反而会改变默认行为（今天不传就是全量）。
  → **只保留「描述里写明缺省语义」这一条。**
- 测试：`tests/test_mcp_stdio.py`（保持 `files[0]["source"]` 断言成立）+ 新增：切片 / 前缀过滤 / 默认全量一致 /
  `total` 与 `next_offset` 正确 / `offset` 越界返回空 + `total` 仍为过滤后总数。
- 验证：`pytest tests/test_mcp_stdio.py tests/test_preview_mode.py -q`

### [ ] C58 — feat(config,indexer,ingest,server): auto_watch 自动摄取（issue #7 RFC）

- 配置（`mortis_rag_mcp/config.py` + `config/app.toml.example`）：
  - `[ingest] auto_watch = false`（默认关，隐私/额度双闸）；
  - `[ingest] max_file_size_mb = 20`（**⚠ 用户裁定 D2 改判：默认 20，所有路径统一**。原计划的「默认 0 = 不限制」
    已推翻。`0` 仍保留为显式「不限制」；正数走 `_numeric(min=1)` 校验。**这是对存量用户的行为变更**：
    20MB 以上的文件在 `submit(None)` 扫描路径会被跳过——必须写进 `CHANGELOG_user.md` 的升级须知，见 T8）；
  - 矛盾键 `auto_watch=true` + `enabled=false`：**不抛错**，忽略该键并在 doctor/STATUS.md 与 kb_init/kb_ingest 的 hint
    里点明（依据：`config.py:219-273` 全为单键校验、无跨键先例；`doctor.py:449-457` 有「静默忽略但必须点出」的范式；
    抛错会让整个服务起不来，为一个笔误牺牲搜索可用性不值）。
  - 自动路径的生效状态**必须可观测**（评审 A17）：`doctor` / `STATUS.md` 报 `auto_watch` 是否生效、
    最近一次自动触发扫描的时间；否则用户分不清「已关」与「坏了」。
- 接线（不破坏分层）：
  - `server._indexer_for` 创建 indexer 后、`start_watching()` **之前**挂 `indexer._ingest_hook`（默认 None，
    hook 内部 `try/except` 全吞，绝不把 watcher 线程打死），指向 `self._ingest_manager_for(vault).submit(None)`。
  - **⚠ hook 的两条硬约束（评审 A2，缺一不可）**：
    ① **`auto_watch` 关闭时零调用**（早退），否则每个文件事件都白跑一次全库扫描；
    ② **不得在 watcher / 防抖调度线程内同步执行全库扫描**——`_fs_scheduler_loop`（`watch.py:71-93`）是每库唯一的
       防抖线程，在里面同步跑 `scan_pending()`（全库递归 + 首轮每 PDF 做 sha256）会**饿死文本 sync**。
       修法：丢给一次性 worker 线程，或做成可合并的 dirty 标志（合并多次事件为一次扫描）。
  - `_indexer/watch.py` 事件分类从「只认 .md/.txt」扩为两级：文本事件 → 现有 sync 防抖路径（**不变**）；
    可摄取文档（`INGEST_EXTS`）事件 → 单独的 ingest 请求标志。
    **⚠ 原计划的理由错了**：它写「避免 PDF 事件白跑一次全量 sha256 对账」，但 `watch.py:134` 只对
    `_INDEXABLE_TEXT_EXTS = {".md", ".txt"}`（`chunking.py:32`）返回 True —— **PDF 事件今天什么也不触发**，
    不存在「白跑一次全量对账」。改动本身仍然必需（它是把「什么都不做」变成「触发 ingest」），但别按错误模型去设计。
  - **⚠ poll 路径是真空洞（评审 N1，三轮一致确认为 P1）**：原计划写「事件丢失与 `watch_method="poll"` 情况由
    `_native_watch_loop` 的 30s 兜底节拍各补一次 ingest 扫描」——**这是错的**。`watch.py:46-59` 显示：
    poll（或原生 watcher 启动失败）走的是 `owner._watch_loop`（第 57 行），**根本不进 `_native_watch_loop`**（第 51 行）。
    而且 `_fs_scheduler_loop` 虽在 poll 下也启动，但 `_fs_requested` 只由原生事件设置（`:112-117`）→ **poll 下永假**。
    **范围比原判断更严重**：`fsnotify.py:218-242` 在非 win32 上恒返回不可用，而 `config.py:210` 默认
    `watch_method = "auto"` → **Linux / macOS 是 100% 走 poll，auto_watch 在那里完全不会触发（属常态，非极端回退）**。
    → 必须在 `_watch_loop` 里也挂 ingest 节拍（复用 `watch_fallback_interval`），**或**在 poll 模式下明确声明
    不支持 auto_watch 并在 `doctor` / hint 里点明（fail-closed 的表态优于静默失效）。见新增 **C61**。
  - `_recover_zombie_jobs` 会拉起 `.ingest.lock` 跨进程锁并 mkdir `<vault>/.mortis-parsed/`——这是**新增副作用**，
    需记账（A7）。
  - **E2 并入本卡（评审终裁：不单列卡）**：`server.py:638-649`/`687-698` 的 PDF hint **已存在且被测试锁定**
    （`tests/test_ingest_server.py:65,83,99` 断言 `ingestible_docs`），`SKILL.md` 关于摄取只有 `:49`/`:57` 两句。
    增量 = ① hint 里带上 pending 清单与建议动作；② `SKILL.md` 补一句纪律。工作量 S。
    （C58 与 E2 不重叠：C58 是 watcher 自动触发，E2 是 agent 主动触发；纪律不可测、机制可测，故纪律作为本卡子项。）
- 体积闸门（**策略层前置拦截，不是补安全缺口**——per-channel 硬闸门 200MB/10MB 已存在）：
  - **闸门位置必须在 `channel_for` 判定之前**；否则超 200MB 的 PDF 会走 `pymupdf_fallback` 静默降级成本地文本提取
    （`ingest/worker.py:352-365` 的 `except MineruError` → `_pymupdf_fallback`），闸门形同虚设。**这条判断已复核，准确。**
  - 扫描路径（`submit(None)` / `action="pending"`）：跳过超限文件，在 pending 项写 `reason="too_large"`（带 size/limit），
    submit 汇总加 `skipped_too_large` 计数与 hint；**不要**塞进 `kb_stats.skipped_unsupported`——它被测试锁定为
    排除 `INGEST_EXTS`（`tests/test_ingest_server.py:194-218`）；
  - 显式 `sources=[...]` 超限 → **报错**（fail-closed，不静默跳过、也不替用户绕过他自己设的上限），错误文案给出修法。
  - **⚠ 闸门只拦「用户自己配置的 cap」（评审 A1，必须区分）**：无 key 时走 Agent 通道，`mineru.py:118-122` 会强加
    10MB 上限并抛 `-30001`（`retryable=False`）→ 今天会落到 `pymupdf` 本地兜底拿到（粗糙）结果。
    若新闸门把这类「channel 强加的限额」也变成硬失败，则**无 key 用户今天能拿到的兜底结果会被砍掉**——
    这是本卡引入的、计划未申报的行为变更。**修法：只有「用户自己设的 `max_file_size_mb`」被超才硬报错；
    由 channel 强加的限额维持今天的 PyMuPDF 兜底语义。**
    （与 D2 的张力如实申报：D2 裁定「所有路径统一 20MB」，但 D2 讨论的是**默认值**，不是「channel 限额的处置」。
    A1 是对 D2 的细化，不违背 D2。）
  - **⚠ 闸门必须在 `_run_job` 再校验一次（评审 A13）**：只在扫描路径设闸会漏两类——
    ① `_recover_zombie_jobs` 把 `parsing → queued` 恢复的旧 job，会在下次 `submit` 启动 worker 时
    按 `state` 取走（`worker.py:300`，**不看本次 targets**）；② 「入队后文件变大」的 TOCTOU。
  - **⚠ 自动路径必须复用 ignore / exclude 过滤（评审 A8）**：这是**隐私边界**问题——打开 `auto_watch` 后，
    「被上传到第三方云的文件集合」从「用户显式点名的」变成「目录里所有的」。必须复用 `.vaultignore`、
    `config.exclude_patterns`、产物目录排除（`worker.py:48` 的 `_EXCLUDED_DIR_NAMES` 是现成的可复用清单），
    否则会摄取用户明确排除过的文件。
  - `auto_watch` 已开且 cap 仍为 0 → hint 提醒可能一次上传大文件、有额度风险（D2 后默认 20，此情形只会在显式设 0 时出现）。
- 额度安全（**判据已修正，评审 A14**）：
  - **自动模式不得重试 failed 任务**：现 `scan_pending()` 只把 `state == "done"` 当已处理
    （`ingest/worker.py:149-194`），瞬时失败（429/超时）会在下一次事件被反复重提 → 限流与额度风险。
  - **走新函数 `_auto_pending()`，不要改 `scan_pending()`**：后者的返回是 `kb_ingest(action='pending')` 的
    **用户可见契约**，已被 `tests/test_ingest_server.py:184-186`、`tests/test_ingest_worker.py:82-103` 锁定。
  - 自动模式判据：最新 job 状态 ∈ {done, failed, queued, parsing} 即跳过，仅当源文件 sha256 变化才重新入队。
  - **⚠ 两处判据细节（评审指出，原计划自相矛盾）**：
    ① jobs 以 `job_id` 为键，「最新状态」必须**按 `submitted_at` 取 max**，不能取任意一条——
       且 `_save_state` 在超过 500 条时会剪掉旧 `failed`（`worker.py:136-145`），会让「最新」失真；
    ② 「仅 sha256 变化才重入队」与「failed 加最小退避」**对 429 是冲突的**——429 场景下 sha 永远不变，
       所以**退避必须独立于 sha 判定**，不能只靠 sha。
  - `_auto_pending()` 的跳过理由要能被 `hint` 说明（用户看到「为什么这个文件没被自动处理」）。
- 测试（全部 mock MineruClient，禁真实网络）：config 默认值与校验（含 0 与负值、bool 拒收）；闸门位置（`channel_for` 之前）；
  `reason="too_large"` + `skipped_too_large` 计数；显式超限报错文案含修法；**无 key 时保留 PyMuPDF 兜底（A1）**；
  **`_run_job` 的兜底闸门（A13）**；`_auto_pending()` 的 done/failed/queued/parsing 跳过 + sha 变化重入队 + failed 退避；
  **`_save_state` 剪枝后判据仍取最新（按 `submitted_at`）**；hook 接线（monkeypatch `IngestManager.submit` 计数）；
  **`auto_watch=false` 时零调用**；hook 抛异常不死 watcher；**文档事件触发 ingest 标志**；`events=None`（缓冲溢出）路径行为；
  **自动路径复用 ignore/exclude 过滤（A8）**；**poll 路径的 ingest 节拍（N1，见 C61）**。
- 文档同步（**⚠ 工作量被低估，评审 A6**）：`config/app.toml.example`；
  `docs/PROJECT_GUIDE.md` —— **§四 需新增 ingest 模块专节**（现子节为 4.1 config → 4.10 doctor，**没有** ingest）、
  **§七 需新增 `[ingest]` 小节**（现子节为 embedding/reranker/index/vector/cache，**没有** ingest），
  且 §八 的 `PROJECT_GUIDE.md:626` 环境变量优先级清单由**三对扩为四对**（+ 缓存根，见 C54）；
  `QUICKSTART_user.md`（用户向、默认关闭）；`skills/mortis-rag-mcp/SKILL.md`（PDF 摄取章节，现只有 `:49`/`:57` 两句）。
- **模块文档必须同步改写（评审 A7）**：`ingest/worker.py:1-4` 与 `ingest/__init__.py:3-4` 现在明写
  「不做 watcher 自动摄取；只有 submit() 被显式调用才启动后台线程」——C58 落地后这句话就是错的。
  「注释与实现对不上」是本仓库最忌讳的一类问题，必须同批改。
- **ASCII 图（仓库规约 `PROJECT_GUIDE.md:867` 要求）**：C58 的控制流是隐式的（watch 事件 → hook → server → manager，
  4 跳），需在 `_indexer/watch.py` 与 `server.py` 两侧各留一张控制流图。
- 验证：`pytest tests/test_ingest_worker.py tests/test_ingest_server.py tests/test_watch_integration.py tests/test_fsnotify.py -q`

### [ ] C59 — docs: Windows 升级写锁说明（issue #6；评审加强）

- `docs/Quick-start_developer.md` §7 增一条：LSP/IDE 经 stdio 持有 `mortis-rag-mcp.exe` 时，
  `pip install -e .` 覆盖 console script 会 `[WinError 5]`；先退出 MCP 客户端或结束进程再加装。
- **评审加强（DX Pass 1/5）**：只说「先退出客户端」用户无从下手 → 补 ① **怎么确认是哪个进程占用**
  （任务管理器按名称找 `mortis-rag-mcp.exe` / `vault-mcp.exe`）；② **备选升级路径**（先 `pip uninstall` 再装 / 重启终端）。
  错误消息三要素（问题 + 原因 + 修法）缺了「怎么确认」这一环。
- `QUICKSTART_user.md` 升级章节加一句用户向说明（大白话，不写函数名/内部名）。
- 无代码改动、无测试。

### [ ] C60 — chore(release): 0.8.1 收口

- `pyproject.toml` `0.8.0 → 0.8.1`、`SERVER_INFO`、README 徽章、`skills/mortis-rag-mcp/SKILL.md` 头部版本（现 `:4`=5.2.0 / `:7`=0.8.0）；
- **⚠ 新增必改项（评审盲点 B3，主审已复核）**：`mortis_rag_mcp/diaglog.py` 有**两处**硬编码版本号——
  `:195` 的 `version: str = "0.8.0"` 与 `:226` 的 `str(version or "0.8.0")`；另有 `tests/test_diaglog.py:120`。
  原收口清单没列它 → 版本收口会漏掉诊断日志的版本。见新增 **C63**。
- **⚠ 新增必改项**：三处文档过期行号——`docs/PROJECT_GUIDE.md:432`（「约 1064 行」）、
  `docs/Quick-start_developer.md:34`（「1126 行」）、`docs/Changelog_developer.md:320`（C49 的「1359→1126」）
  → `server.py` 实际 **1313 行**。
- `CHANGELOG_user.md` 新增 `## [0.8.1]`（`tests/test_version_sync.py:18-20` 强制「有条目」，
  但**不强制「有升级须知」→ 人工保证**）；`docs/PROJECT_GUIDE.md` §十五 版本详录（倒序追加在顶部）；
- **升级须知必须逐条写清本版的三处行为变更**（DX Pass 5）：① `max_file_size_mb` 默认 20 → 20MB 以上文件在
  `submit(None)` 里被跳过；② `kb_read(chunk_id=...)` 在多库环境的行为从「报错要求显式 vault_path」变为
  「唯一命中自动展开」；③ 新增公开环境变量 `MORTIS_RAG_CACHE_DIR`。`QUICKSTART_user.md` 升级章节同步。
- `docs/Changelog_developer.md` 逐卡记账（编号从 `C53` 起；序列末位现为 `C52`，`FIX-x` 是并行命名线）；
- **E1 降级形态（发版必填字段）**：C53 的「实机验证：已通过（日期）/ 待实机验证（原因）」必须出现在
  Changelog 条目**正文**里，不留空、不留给口头承诺。载体沿用 `PROJECT_GUIDE.md:764-769` 的发布 4 步。
- 版本策略：**一次发 0.8.1，不拆补丁版**（用户裁定 D1=C；本报告不再复议）。
- 验证：`pytest tests/test_version_sync.py tests/test_diaglog.py -q`；全量回归交 CI（遵守「本地不跑全量」约定）。

### [ ] C61 — fix(watch): poll 路径的 auto_watch 节拍（评审 N1；三轮一致确认为 P1）

- 问题：`_indexer/watch.py:46-59` —— poll（或原生 watcher 启动失败）走 `owner._watch_loop`（第 57 行），
  **不进 `_native_watch_loop`**（第 51 行）；而 `_fs_scheduler_loop` 的 `_fs_requested` 只由原生事件设置（`:112-117`）。
  → poll 下 auto_watch 永不触发。**范围**：`fsnotify.py:218-242` 在非 win32 恒不可用 + `config.py:210` 默认 `auto`
  → **Linux / macOS 100% 走 poll，属常态**。
- 修法（二选一，建议前者）：① 在 `_watch_loop` 里同样挂 ingest 节拍（复用 `watch_fallback_interval`）；
  ② 在 poll 模式下明确声明不支持 `auto_watch` 并在 `doctor`/hint 里点明（fail-closed 表态优于静默失效）。
- 文件：`mortis_rag_mcp/_indexer/watch.py`、`mortis_rag_mcp/doctor.py`、`tests/test_watch_integration.py`
- 验证：`pytest tests/test_watch_integration.py tests/test_fsnotify.py -q`

### [ ] C62 — fix(server): kb_list_files 的缺省语义与 truncated 命名（评审盲点 B1/B2）

- B1：`kb_list_files.limit` 缺省=全量，而 `kb_search.limit` 缺省=用 top_k（`server.py:257`）——**同名参数相反缺省**，
  模型必然误判。修法：在 schema `description` 里**显式写明**「缺省返回全部已索引文件，大库请传 limit」（推荐），
  或改缺省值（但与 C57 的兼容性承诺冲突，需改口径）。
- B2：`truncated` 已被 v0.8.0 的字节预算截断占用（`fanout.py:50-58` 的 `apply_budget`）。C57 的分页截断**不要复用同名键**
  → 建议改名 `page_truncated`，或在 `description` 里明确区分。
- 文件：`mortis_rag_mcp/server.py`、`tests/test_mcp_stdio.py`
- 验证：`pytest tests/test_mcp_stdio.py tests/test_preview_mode.py -q`
- 说明：本卡与 C57 同文件同区域，建议**合并进 C57 一次做完**，不单独提交。

### [ ] C63 — fix(diaglog): 版本号去硬编码（评审盲点 B3）

- 问题：`mortis_rag_mcp/diaglog.py:195` 的 `version: str = "0.8.0"` 与 `:226` 的 `str(version or "0.8.0")`
  两处硬编码（主审已复核，两处均存在）；`tests/test_diaglog.py:120` 也依赖该值。
- 修法：改为从 `server.SERVER_INFO["version"]` 或 `mortis_rag_mcp.__version__` 取值（单一真源），
  避免每次发版都要人肉记得改两处。
- 文件：`mortis_rag_mcp/diaglog.py`、`tests/test_diaglog.py`
- 验证：`pytest tests/test_diaglog.py tests/test_version_sync.py -q`

### [ ] C64 — perf(indexer): C56 探测的 chunks 层代价（评审盲点 B4）

- 问题：C56 的 `load_vectors=False` 只省掉向量加载；`_init_cache_paths()` 的第 ① 步
  `_load_chunks_cache()` **仍全量加载文本层**，每个未加载库都要付一次。
- 需要评估（先量测再决定，不要凭感觉优化）：① 只读 chunks 层的轻量入口；② 进程级「库 → chunk_id 集合」索引（带失效）；
  ③ 接受现状并加一个库数上限（超过 N 个库只探测已加载的，并在报错里说明）。
- **前置作业**：先实测「10 个中等库」场景下 `kb_read(chunk_id=...)` 的实际耗时与内存，用数据决定。
  原报告的「20 库 × 20MB ≈ 400MB」**无实测依据，已撤回**。
- 文件：`mortis_rag_mcp/indexer.py`、`mortis_rag_mcp/server.py`
- 验证：靶向单测 + 一次本机量测（记录数值到 Changelog）

## 本地验证策略

- 只跑靶向单文件/单用例（上方各卡的 `pytest` 命令）；全量由 CI 承接，避免 pytest 临时目录碎片膨胀。
- 不新增真实网络依赖：MinerU / embedding / reranker 一律 mock，或走本机回环。
- 每卡的「验证」行给的就是最小靶向集；改动跨模块时按需追加相邻文件。

## 风险与回滚

| 卡 | 风险 | 回滚 |
|---|---|---|
| C53 | 极低（单行） | revert 该行 |
| C54 | 全局夹具改变所有测试的配置来源；C54c 若用共享注册表文件会造成跨用例状态泄漏 | 删 fixture；C54c 独立一步、失败可单独回滚 |
| C56 | 改变 kb_read 既有报错行为（有测试锁定），属契约变更 | 保留旧分支开关（探测全未命中即回落原报错） |
| C57 | 新增键不影响旧消费者 | 移除新参数即恢复 |
| C58 | 打开后会真实消耗 MinerU 额度；cap 默认 0 意味着「默认不设上限」 | 默认关闭；出问题置 `auto_watch=false` 立即止血；文档推荐显式设 20 |

## 已裁定（含 2026-09-29 三轮评审的全部修订）

### 用户裁定（4 项，不得复议）

| # | 裁定 | 说明 |
|---|---|---|
| D1 | **一次发 0.8.1，7 条全落** | 用户明确否决了「拆两批 / 拆三批」的建议。本计划不再复议。 |
| D2 | **`max_file_size_mb` 默认 20，所有路径统一** | 推翻原计划的「默认 0 = 不限制」。`0` 保留为显式不限制。**这是对存量用户的行为变更**，必须进升级须知。 |
| D3 | **C54 采用「缓存根 env 覆盖 + 保留 config pin + 修正后全量清单」** | 见 C54 的 C54a' 与 C54b。 |
| D4 | **P0 搭车 / P1 前置独立 commit / P2 更正为本地清理** | P2 是伪命题：`docs/*` 在 `.gitignore:19` 被整体忽略，那份副本**根本不在仓库内**（`git ls-files docs/` 只有 3 个文件）→ 只需删本地文件，删除动作需单独确认。 |
| X3 | **延后项用仓库自己的 `docs/Execution-plan_developer.md` 口径** | 该文件被 `Quick-start_developer.md:60/215/251` 三处指向，但**不存在且被 gitignore**。落地方式：新建该文件 + 把它与 `docs/Docs_Folder-descriptions.md` 一起加进 `.gitignore` 白名单（现 3 个 → 5 个）+ 修仓库地图与「只需保留」清单。**不新建根 `TODOS.md`**（避免第二套约定）。 |
| D5 | **C56：solo 库参与 `chunk_id` 跨库探测，但只以「库名」标识该库，其 chunk 不进入 `kb_search`** | 2026-09-29 用户裁定：显式寻址（用户已指名具体切片）与 fan-out 搜索是两条独立口径——探测**必须覆盖 solo 库**；命中/报错文案只给**库名**（不展开 solo 库的绝对路径），并点明该库是 solo；`kb_search` 的 `excluded_solo` 语义**完全不变**。据此替代 C56 卡内「本卡必须拍板的一处」。 |

### 技术裁定（三轮评审累积）

1. **#4 多命中** → fail-closed（列候选报错）；单命中追加库归属；探测必须覆盖未加载的库（**不走** `_indexer_for`）。
2. **#4 探测代价** → 用 `load_vectors=False` 开关（两行），**不新增只读层抽象**；chunks 层代价另立 C64 先量测。
3. **`auto_watch=true` + `enabled=false`** → 不抛错，忽略该键并在 doctor/STATUS.md 与 hint 点明。
4. **提交编号** → 从 `C53` 起顺延（主序列末位 `C52`；另有 `FIX-x` 并行命名线，不冲突）。
5. **发版节奏** → 一次发 0.8.1，不拆补丁版（D1）。
6. **体积闸门** → 只拦「用户自己设的 cap」；channel 强加的限额维持 PyMuPDF 兜底语义（A1）。
7. **自动路径判据** → 走新函数 `_auto_pending()`，不改 `scan_pending()` 的用户可见契约（A14）。
8. **C55** → handler 别名保留；重心移到 schema `description` 与报错文案的可发现性；**不改 schema properties**（≤10% 体积门禁）。
9. **C57** → `total` = 过滤后切片前条目数；补 `next_offset`；`truncated` 改名避免与字节截断撞名；`limit` **不加**上限。
10. **C54b** → 判据更正为「`cache.enabled` 默认 true 且未显式设 `dir`」；`enabled = false` 的配置本来就安全。
11. **E1** → 不进 CI（单 job、零 secret、烧额度）；降级为 C53/C60 的**发版必填字段**「实机验证 / 或显式声明未验证」。
12. **E2** → 不单列卡，作为 C58 的子项（hint 带 pending 清单 + SKILL.md 一句纪律）。

## 仍待裁定

- **无。** 原「仍待裁定」的 REPORT.md P0/P1/P2 已由 D4 裁定：
  P0（CI flaky）搭车成卡；P1（PROJECT_GUIDE 转义清理）作前置独立 commit（**必须排在 C58/C59 的文档改动之前**，
  否则同一文件里大段机械 diff 与语义编辑互相遮蔽）；P2 更正为「只删本地文件」（不在仓库内）。
- 本轮新出现的**一处待你拍板**：C56 的 solo 库是否参与 `chunk_id` 跨库探测（见 C56 卡内标注「本卡必须拍板的一处」）。
  评审建议：**参与**（用户已指名具体切片），但在结果与报错里说明该库是 solo。
  → **2026-09-29 已裁定（见上表 D5）**：参与探测；只以「库名」标识该 solo 库（不展开绝对路径）；其 chunk 不进 `kb_search`。

## 独立审阅记录（2026-09-29）

方式：独立子代理**只读源码与测试**、不读本文件、不跑全量测试，对 7 组工程问题各自独立下结论并给证据行号。

- **被修订**（原判断 → 修订后）：
  1. `auto_watch` 矛盾键：抛 `ValueError` → 忽略 + doctor/hint 点明（依据：`config.py:219-273` 全为单键校验、无跨键先例；
     `doctor.py:449-455` 有「静默忽略但必须点出」范式；抛错会让整个服务起不来）。
  2. `max_file_size_mb` 默认 20MB → 0 = 不限制（依据：`batch_size<=0` / `max_age_days=0` / `watch_fallback_interval<=0`
     的既定「0 = 关闭」语义）。
  3. 显式 `sources` 超限「绕过 cap」→「报错」（fail-closed，避免静默上传超限文件或出现半状态）。
- **被确认**：#4 多命中 fail-closed（另获两条歧义报错先例 `server.py:453-459`、`1084-1087`）；仓库无「修复必须独立发版」
  的成文条文 → 一批发更贴既有实践。
- **审阅加固**：探测范围必须覆盖未加载库（`_indexers` 惰性、可能缺条目）；C54 的真实痛点是 stdio 用例缺 `[cache] dir`
  （本机累积 18153 个孤儿文件，`Changelog_developer.md:418`），比宿主 config 影响更大。
- **审阅未证实事项（保留为风险）**：provider 构造期是否绝对不发网络请求（若探活，轻量探测的代价需上调）；
  `docs/PROJECT_GUIDE.md`（103KB）未通读，可能有更细的记账条文；pytest 临时目录膨胀只有历史观测
  （18153 个孤儿缓存 / 单轮 837~1262 文件），本轮无实测量化。

---
---

# GSTACK REVIEW REPORT — /autoplan

> 2026-09-29 · 分支 `main` · HEAD `82989c8` · 标的 `docs/v0.8.1/PLAN.md`
> 还原点：`~/.gstack/projects/moton16-Mortis-RAG-MCP/main-autoplan-restore-20260929-080941.md`
> 流水线：Phase 0（intake）→ Phase 1（CEO）→ ~~Phase 2（Design，无 UI 范围）~~ → Phase 3（Eng）→ Phase 3.5（DX）→ Phase 4（最终门）
> 双声部状态：**Codex CLI 未安装**（`command -v codex` 无输出）→ 全部阶段降级为 `[subagent-only]`。
> 主审 + 三个独立只读子代理（战略声部 / 工程声部 / 事实核验声部，共 165 次工具调用），下文所有代码断言均经实际阅读源码复核。

## 范围检测（Phase 0 Step 2）

| 检测项 | 结果 | 判定 |
|---|---|---|
| UI 范围 | 视图/渲染类词仅 `form` 命中 3 次，全为 `format`/`transform` 类误报；无第二类词命中 | **无** → Phase 2 跳过 |
| DX 范围 | `MCP`×24、`config`×21、`developer`×9、`SKILL.md`×3、`error`×6；且产品本体即开发者工具（MCP 服务器 + CLI + agent skill） | **有** → Phase 3.5 执行 |
| 系统上下文 | 无 `CLAUDE.md`、无 `TODOS.md`、无 `AGENTS.md`；无历史设计文档；工作区仅未跟踪 `review_diff.txt` | 上下文取自 `PROJECT_GUIDE.md` / `Quick-start_developer.md` / `Changelog_developer.md` |
| 先决技能 | 无 design doc → 建议 `/office-hours`。**自动裁决：跳过**（P6 偏向行动；计划已自带前提挑战、替代方案表与一次独立审阅记录，再补 10 分钟设计文档的边际收益低于中断成本） | 机械 |
| 还原点 | 已捕获（21,053 字节），计划文件顶部已写注释标记 | 完成 |

---

## 决策审计轨迹（Decision Audit Trail）

| # | 阶段 | 决策 | 分类 | 原则 | 理由 | 已否决的替代 |
|---|---|---|---|---|---|---|
| D0-1 | Phase 0 | 跳过 `/office-hours` 先决技能 | 机械 | P6 | 计划已含前提挑战与替代方案，且已过一次独立审阅 | 先跑 office-hours |
| D0-2 | Phase 0 | Windows 下 gstack bash 前言/遥测/学习记录脚本不硬跑，改为等价手工探测 | 机械 | P3 | 环境无 `bash` 于 PATH（git bash 存在但前言脚本输出异常），探测目标（分支/slug/设计文档/learnings）已全部手工达成 | 反复调试 bash 桥接 |
| D1 | Phase 1 前提门 | **用户裁定 C：保持一次发 0.8.1，7 条全落** | 用户 | — | 与两声部建议相反，用户方向为准；本报告不复议，仅在风险表保留 C58 真机零验证的敞口 | 拆两批 / 拆三批 |
| D2 | Phase 1 前提门 | **用户裁定 C：`max_file_size_mb` 默认 20，所有路径统一** | 用户 | — | 原计划「默认 0 = 不限制」被推翻；需 release notes 显式声明对存量用户的行为变更 | 手动 0 + 自动自带上限 |
| D3 | Phase 1 前提门 | **用户裁定 A：新增缓存根 env 覆盖 + 保留 config pin + 修正后的全量清单** | 用户 | P2 | 产品级一行修法优于逐文件补丁；原清单漏 10 个 stdio 用例 + 全部进程内用例 | 只按原计划 / 只加 env |
| D4 | Phase 1 前提门 | **用户裁定 A：P0 搭车、P1 前置独立 commit、P2 更正为本地清理** | 用户 | P2 | 本版验证策略押在 CI 上，门禁不可信等于没有门禁；P1 须排在 C58 文档改动之前 | 三条都不并入 / 三条全并入 |
| A1 | Phase 1 | 硬闸门只拦「用户自己配置的 cap」；channel 强加的 10MB 上限维持今天的 PyMuPDF 兜底语义 | 机械 | P5 | 用户 D2 选的是尺寸闸门，不等于「无 key 用户的兜底路径变硬失败」——后者是计划未申报的行为变更 | 统一硬报错 |
| A2 | Phase 1 | C58 的 `_ingest_hook` 接受（显式可注入 + 默认 None），但追加两条硬约束：`auto_watch` 关闭时零调用；hook 不得在 watcher/调度线程内同步执行全库扫描 | 机械 | P5 | 否则每个文件事件都会在防抖线程里跑一次全库扫描，饿死文本 sync | 拒绝 hook / 接受原样 |
| A3 | Phase 1 | C56 的跨库探测必须复用 F5b 短名寻址的「唯一命中→展开 / 多命中→列候选」范式，不新造第三套口径 | 机械 | P4 | 仓库已有一套同形范式（`server.py:1084-1087`） | 新造独立口径 |
| A4 | Phase 1 | 修 `on_job_finished` 参数不匹配。**⚠ 第二轮修正：降为 P2，且撤回「C58 依赖它」的因果判断** | 机械 | P1 | 该回调确实 100% 抛 `TypeError` 并被 `except Exception: pass` 吞掉（`worker.py:395` 传 2 参 / `server.py:766` 收 1 参）；但它是**既有缺陷**，不在 C58 的触发路径上——C58 的 hook 直连 `_ingest_manager_for().submit(None)`，「解析完可检索」由各 `kb_*` 自带的 `try_sync_with_guard` 保证。真正被它影响的是 `server.py:794-795` 那句对用户说谎的 hint | 留作 TODO |
| A5 | Phase 1 | C54b 的判据：**任何「`cache.enabled` 为真且未显式设 `[cache] dir`」的配置**都会写真实缓存根。**⚠ 第二轮修正：原判据「无条件 mkdir，与 cache.enabled 无关」是错的** | 机械 | P1 | `indexer.py:195` 是 `if self.config.cache.enabled and self.config.cache.dir:`，198-203 另有 `except OSError` 降级。正确机制：`cache.enabled` 经 `load_config` 默认 true，`dir` 未设时回落到真实 home。故 **`[cache] enabled = false` 的配置本来就安全**（如 `tests/test_search_filters.py:182`）；有风险的是**完全没有 `[cache]` 段**的文件（默认 enabled=true） | 把「空目录」当「写文件」混为一谈 |
| A6 | Phase 1 | C58 的文档同步从「改 `[ingest]` 段」更正为「**新增** PROJECT_GUIDE §四 ingest 模块专节与 §七 `[ingest]` 小节」 | 机械 | P1 | 实测 §四子节为 4.1 config→4.10 doctor，无 ingest；§七子节为 embedding/reranker/index/vector/cache，无 ingest | 按「修改」估算工作量 |
| A7 | Phase 1 | C58 必须改写 `ingest/worker.py:1-4` 的模块硬设计文档与 `ingest/__init__.py` docstring | 机械 | P1 | 否则代码与「不做 watcher 自动摄取」的成文设计直接矛盾——本项目最忌讳注释与实现对不上 | 只改用户文档 |
| A8 | Phase 1 | 自动模式的跳过必须复用 ignore/exclude 过滤（`.vaultignore`、`exclude_patterns`、产物目录） | 机械 | P1 | 隐私边界变化：打开 auto_watch 后「被上传到第三方云的文件集合」从「用户点名的」变成「目录里所有的」 | 只按 INGEST_EXTS 扫 |
| A9 | Phase 3 | C56 的探测不得用 `MarkdownIndexer(path, config)` 全量构造。**⚠ 第二轮修正：优先采用两行改动，而非新只读层** | 机械 | P5 | 事实成立：`cache.enabled` 为真时 `__init__` → `_init_cache_paths` → 全量加载 chunks/vectors 缓存 → `FtsIndex`（`fts.py:43-50` 会真建 sqlite）→ 可能 `_fts_ensure_populated` 写盘。但原判据的「20 库 ≈ 400MB」**无实测依据**；且「新增只读文本层入口」是重构而非缺陷修复，与报告自称的「过度工程：无」自相矛盾。**最小修法：给 `MarkdownIndexer.__init__` 加一个 `load_vectors=False` 开关** | 「新增只读文本层」这种带新抽象的方案 |
| A10 | Phase 3 | C57 的 `total` 语义钉死为「过滤后、切片前的条目数」，并补 `next_offset` 与 `kb_search` 同形 | 机械 | P5 | 否则消费方无法判断是否还有下一页 | 只加 `total` |
| A11 | Phase 3 | `kb_list_files.path_prefix` 与 `kb_search.path_prefix` 共用同一实现。**⚠ 第二轮修正：撤回「逃出库 = High 安全风险」** | 机械 | P4 | 安全面判据是错的：`models.py:143-166` 是 `chunk.source.startswith(prefix)`，而 `source` 恒为库内相对 posix 路径，「字符串前缀比较」不可能发生路径穿越——`../` 或绝对路径只会**零命中**，即天然 fail-closed。保留的只是 DRY 价值（避免两套前缀语义漂移） | 把它当安全漏洞处理 |
| A12 | Phase 3 | C58 的事件分类改动保留，但**更正计划中的理由**：PDF 事件今天本来就不触发 sync。**⚠ 第二轮新增：poll 模式下 auto_watch 永不触发（真 bug）** | 机械 | P3→**P1** | `watch.py:134` 只对 `{".md",".txt"}` 返 True 成立；poll 模式同样看不见 PDF（`_markdown_files()`）。**但计划写「`watch_method="poll"` 情况由 `_native_watch_loop` 的 30s 兜底节拍补一次 ingest 扫描」是错的**：`watch.py:46-58` 显示 poll（或非 Windows / native 启动失败）走的是 `_watch_loop`（第 57 行），根本不进 `_native_watch_loop`。→ 见新增任务 T22 | 保留错误理由与缺失的 poll 路径 |
| A13 | Phase 3 | C58 的体积闸门在 `_run_job` 再校验一次。**⚠ 第二轮修正机制描述 + 降级** | 机械 | P2 | `_recover_zombie_jobs` 实际只把 `parsing → queued`；真正绕过闸门是下次 `submit` 启动 worker 时按 `state` 取首个 queued（`worker.py:300`，不看本次 targets）；「排队后文件变大」是真 TOCTOU。修法廉价，但不是 CRITICAL | 只在扫描路径设闸 |
| A14 | Phase 3 | 自动模式的 pending 判据走**新函数** `_auto_pending()`，不改动 `scan_pending()` | 机械 | P5 | `scan_pending()` 的返回是 `kb_ingest(action='pending')` 的用户可见契约，有测试锁定 | 直接改 `scan_pending` |
| A15 | Phase 3 | C53 的回环测试除断言「非 form-urlencoded」外，再加 `req.has_header("Content-type")` 为真。**⚠ 第二轮澄清「自相矛盾」的质疑** | 机械 | P2 | 质疑方指出：既然空值与缺省在 OSS 语义上等价，为何还要锁头存在？答：语义等价 ≠ 回归安全。断言要锁的是「我们**显式**发了这个头」，防止未来重构退回 `urllib` 的自动注入——即防的是回归，不是当前行为 | 保持宽松断言 |
| A19 | Phase 3（第二轮） | C55 必须在 `inputSchema.properties` 里**增加 `vault_path`**，否则别名对模型不可见、对校验型客户端不可用 | 机械 | P1 | `kb_init_solo` 的 schema（`server.py:192-195`）只有 `path` 属性且 `required: ["path"]`；模型读的是 schema，`vault_path` 不在 properties 里它就永远不会传。且 `kb_remove` 已有同样的 schema/handler 分歧（schema 只 `path`，handler 兜四别名），说明这是既存口径不一致。**原计划「required 保持不动（契约不变）」与 C55 的目标（消除参数名不一致）自相矛盾** → 见新增任务 T23 |
| A20 | Phase 3（第二轮） | D3 的缓存根 env 覆盖必须**短路在 `resolve_default_cache_dir()` 的目录改名逻辑之前** | 机械 | P1 | `config.py:72-84` 的 `resolve_default_cache_dir()` 有 `os.rename` 副作用（旧目录独占存在时把宿主 `~/.vault_mcp_cache` 搬成新名）。若 env 覆盖落在它之后、或它先被调用，跑测试会**搬走宿主真实数据目录**——比写孤儿文件严重得多 → 见新增任务 T24 |
| A21 | Phase 3（第二轮） | C57 的 `limit` 必须设上限（或复用 `max_top_k` 夹取），并写清与 `budget_bytes` 的关系 | 机械 | P1 | `kb_search` 的 `limit` 在 schema 里只有 `minimum: 1`、**无 maximum**（`server.py:257`），而 `budget_bytes` 是 `[500, 100000]`（`:259`）。C57 若照抄无上限 `limit`，大库一次可拉全量 → 撞 v0.8.0 建立的 payload 预算不变量 → 见新增任务 T25 | 照抄 kb_search 的无上限 limit |
| A16 | Phase 3 | 新增回归测试锁定 `on_job_finished` 回调被真实调用（spy 计数） | 机械 | P1 | 回归测试铁律：既有行为已坏且新计划依赖它 | 只修不加测试 |
| A17 | Phase 3 | 自动路径的生效状态必须在 `doctor`/`STATUS.md` 可见 | 机械 | P1 | 否则用户无法判断 auto_watch 是「已关」还是「坏了」 | 只靠 hint |
| A18 | Phase 1 | 云端链路验收闸门（真机 smoke / 发版清单）**升为本计划扩张候选，交最终门** | 品味 | P1 | 本次事故的真教训是「付费云路径没有验收闸门」，而 7 张卡没有一张朝这个方向移动 | 记入 TODOS 不讨论 |

**自动裁决合计 20 项**（机械 19 / 品味 1 = A18），**用户裁定 4 项**（D1-D4），**用户挑战 1 项已由用户裁定关闭**（D1 的拆版建议被否决，本报告不再复议），**遗留待定 3 项**（E1 云端验收闸门 / E2 agent 约定式摄取 / X3 是否新建 `TODOS.md`，均在最终门裁决）。

---

## Phase 1 — CEO 评审（SELECTIVE EXPANSION，全 11 节）

### Step 0A — 前提挑战

| # | 前提 | 状态 | 裁定 |
|---|---|---|---|
| P1 | 一次发 0.8.1，7 条全落 | **用户已确认**（D1=C）；原为两句部挑战项 | 保留。风险敞口记入风险表 |
| P2 | `auto_watch` 默认关闭即可控风险 | **不成立**：默认关 ≠ 风险已控——它是「一旦打开就无上限」 | 由 D2 修复（新闸门 safe-by-default） |
| P3 | `max_file_size_mb` 默认 0 = 不限制 | **被推翻**（D2=C 改判默认 20，全路径统一） | 改写；release notes 必须声明行为变更 |
| P4 | `auto_watch=true` + `enabled=false` 不抛错，仅忽略并点明 | 成立（`doctor.py:449-457` 有同款「静默忽略但必须点出」范式；`config.py` 无跨键校验先例） | 保留 |
| P5 | #4 多命中 fail-closed | 成立（`server.py:453-459`、`1084-1087` 两条歧义报错先例） | 保留 |
| P6 | 提交编号从 `C53` 顺延 | 成立（`Changelog_developer.md:361` 末位为 C52；`C5[3-9]|C6[0-9]` 零命中） | 保留 |
| P7 | C54 三子项（a 必做 / b 补 app.toml / c 可选） | **范围被 D3 修订** | 改为 a + 产品级 env 覆盖 + 修正后的全量清单 |

**新增前提（计划未声明、但已被裁定影响）**：
- P8「本版含 3 处用户可见行为变更」（默认尺寸闸门 20MB、`kb_read(chunk_id)` 多库行为、新增公开环境变量名）→ 三份用户侧文档必须逐条写。
- P9「C58 的真机验证为零」——计划自己承认全部测试 mock `MineruClient`。这与本次 403 事故的成因同构（付费云路径无真机验收）。

### Step 0B — 已有代码杠杆表

| 子问题 | 已有代码 | 计划是否复用 |
|---|---|---|
| MinerU 预签名 403 | `_put_upload` 单点，v4(`mineru.py:159`) 与 Agent(`:215`) 共用 | **复用**（一处修全局） |
| 测试配置隔离 | `config.py:346-362` 的 `MORTIS_RAG_CONFIG`/`VAULT_MCP_CONFIG` 优先链已存在 | 复用 |
| 测试缓存落点 | `resolve_default_cache_dir()`（`config.py:72-84`）已是**运行时求值**的扩展点，「严禁模块顶层副作用」的注释说明它就是为这类需求留的 | **计划漏用** → D3 补上 |
| `path`/`vault_path` 别名 | `kb_remove`(`709-716`)、`_indexer_for`(`565-572`)、`_kb_describe`(`744-750`)、`kb_ingest`(`778-784`)、`kb_read`(`907-913`) 全是四别名 | 复用 ✔ |
| chunk_id 跨库寻址 | F5b 短名寻址已实现「全库遍历 + 唯一命中展开 / 多义列最多 5 个候选」（`server.py:1084-1087`） | **计划未点名复用** → A3 |
| 前缀过滤语义 | `kb_search.path_prefix`（`SearchFilter.matches()`，casefold 归一化） | 复用（计划已声明对齐） |
| 分页 | `SearchFilter.page_slice()` / `kb_search` 的 `next_offset` | 部分复用 → A10 |
| 解析完成通知 | `on_job_finished` 已接线（`server.py:766-771`） | **链路已死**（见 A4） |
| 「agent 主动摄取」 | `server.py:645-649`、`687-698` 的 hint 已在引导 agent 调 `kb_ingest(action='pending'/'submit')`；`SKILL.md:57` 已写 | **计划未列为 C58 的替代方案** → 见 0C-bis 与最终门 |

### Step 0C — 梦状态映射

```
   当前状态 (v0.8.0 @ 82989c8)              本计划 (v0.8.1)                          12 个月理想态
   ───────────────────────────────          ─────────────────────────────           ─────────────────────────────
   PDF 摄取旗舰功能 100% 传不上去    ──▶     403 修好，用户能传文件了         ──▶     付费云链路有验收闸门：
   （OSS 403，发版 3 天无人察觉）             （但闸门仍交给一次自愿的人工确认）        真机 smoke 进 CI / 发版清单自动卡
   测试往真实 home 写盘，累积       ──▶     缓存根可被 env 整体重定向         ──▶     任何嵌入方零污染；
   18153 个孤儿文件                           测试不再碰真实目录                       新写的用例天然不踩坑
   kb_read(chunk_id) 只能单库       ──▶     跨库自动寻址 + fail-closed       ──▶     「检索→精读」闭环零摩擦
   kb_list_files 大库一次吐数万字符 ──▶     分页 + 子树过滤                   ──▶     万级文件库输出可控
   参数名在 3 个工具间不一致        ──▶     与 15 个工具口径统一             ──▶     契约面零歧义
   （无）                          ──▶     auto_watch（默认关）              ──▶     agent 主动摄取成为默认路径
```

**梦状态落差（Dream state delta）**：本计划把**存量缺陷清干净了**，但没有任何一张卡朝「付费云路径可验收」这个 12 个月理想态的关键位移动。本次事故的真正教训（旗舰功能 100% 坏 3 天没人发现）在计划里只对应一句「需人工确认一次」——这是本计划最大的战略缺口。已升级为扩张候选 A18 交最终门。

### Step 0C-bis — 实施方案对照

```
方案 A：原计划（7 条一轮，测试侧补丁）
  Effort: M   Risk: Med-High
  Pros: 一轮版本仪式；CHANGELOG 按 Added/Fixed 混排符合既有实践
  Cons: C58 真机零验证；C54b 清单漏 10 个 stdio 用例 + 全部进程内用例；
        C56 未复用 F5b 已有范式；on_job_finished 死链未被发现
  Reuses: kb_remove 别名模式、MORTIS_RAG_* env 链

方案 B：原计划 + 三处根因修正（D2/D3/D4 + A1-A18）      ← 采纳（= 用户裁定 A + 修正）
  Effort: M   Risk: Low-Med
  Pros: 尊重用户「一次发」的裁定；D3 把缓存污染从「测试侧补丁」升级为「产品级可重定向」；
        D2 让新增成本闸门 safe-by-default；A9 砍掉 C56 的最大性能陷阱
  Cons: 单版承载 7 条 + 1 处产品级配置面变更 + 2 处契约/行为变更，release notes 必须写清三处变化
  Reuses: 同上 + resolve_default_cache_dir()

方案 C：拆版（0.8.1 = 止血 / 0.9.0 = 风险项）
  Effort: M   Risk: Low
  Pros: 止血最快、回滚点最清晰
  Cons: 多一轮版本收口仪式
  → 用户已明确否决（D1=C）。记录为已裁定，本报告不再重提。
```

**RECOMMENDATION：方案 B。** 映射到工程偏好：*「Right-sized diff：选择能清晰表达改动的最小 diff，但不把必要的重写硬压成最小补丁」* —— D3 与 A9 都不是最小补丁，但它们是根因修法；而 D2 是安全默认值的正确取舍。

### Step 0D — SELECTIVE EXPANSION 分析

**复杂度检查（HOLD 分析先行）**：本计划触及 12 个以上源码/配置文件、12+ 测试文件、5 份文档、2 份 CHANGELOG → **远超 8 文件阈值，标为 smell**。但范围已由用户裁定（D1=C），不缩。作为缓解：要求 C58 与 C56 各自独立 commit 且顺序置于 C53/C54/C55/C57 之后，使「低风险 5 张」具备独立可 revert 的提交边界。

**最小集检查**：可延后而不阻塞核心目标的项 → 无（D1 已裁定全做）。

**扩张扫描（候选，未加入范围）**：

| ID | 候选 | 依据 | Effort | Risk |
|---|---|---|---|---|
| E1 | 云端摄取路径验收闸门（真机 smoke 脚本 / 发版清单条目，可手动触发） | 本次事故教训；P9 | S | Low |
| E2 | agent 约定式摄取：注册库时返回「待解析清单 + 建议动作」，让 agent 按纪律主动调 `kb_ingest` | `server.py:645-649`、`687-698`、`SKILL.md:57` 已有基础设施，零线程、零意外扣费、不违硬设计 | S | Low |
| E3 | `kb_ingest(action='pending')` 返回 `next_offset`/`total`，与 C57 的分页口径统一 | 同一次改动里顺手对齐，避免两套口径 | S | Low |

（E2/E3 记为候选，是否采纳见最终门。）

### Step 0E — 时间审讯（人类工时；CC 压缩约 10-20x）

```
HOUR 1  (地基):      C53 的「空 Content-Type」到底是「不发这个头」还是「发一个空值头」？
                     答（已核验）：http.client 会发出 `Content-type: `（空值），不是不发。
                     OSS V1 StringToSign 下空值与缺省等价，故修法可用；但测试必须按此断言（A15）。
                     另需知道：urllib 无法真正「不发头」（传 None 会 ValueError）。

HOUR 2-3 (核心逻辑): C54 的 fixture 依赖顺序 —— function 级 fixture 必须**显式依赖** session 级 fixture，
                     否则 env 指向尚未创建的文件，`resolve_config_path` 会静默回落宿主 config
                     （`config.py:346-355`）——正是要修的那个 bug 会以「测试看起来是绿的」的方式复现。
                     另：pop("MORTIS_RAG_REGISTRY") 会让 conftest.py:15 的守卫失效，
                     使 `pytest_sessionfinish` 可能去写真实的 `~/.mortis_rag_mcp/status.json`。

HOUR 4-5 (集成):     C58 的 hook 挂在 `_fs_scheduler_loop`（watch.py:71-93）这条线程上。
                     若 hook 内同步跑全库扫描，文本 sync 会被饿死。必须早退 + 不可重入 + 可合并。
                     `_ingest_manager_for` 还会经 `_recover_zombie_jobs` 拉起跨进程文件锁并 mkdir
                     `<vault>/.mortis-parsed/` —— 这是计划未记账的副作用。

HOUR 6+ (打磨/测试): C56 的两条新分支（「未加载库探测」「有库未探测则不得谎报文件已修改」）目前零测试：
                     现有 test_kb_read_chunkid.py:263-270 全部先走 `_indexer_for`，从未覆盖新路径。
                     C58 只列 3 个测试文件不够（见 Phase 3 测试图）。
```

### Step 0F — 模式确认

**SELECTIVE EXPANSION**（autoplan 固定 override）。已按 HOLD 分析打底 + 扩张扫描（E1/E2/E3 交最终门）。

### Step 0.5 — 双声部

**双声部状态：`[subagent-only]`。** Codex CLI 未安装于本机（`Get-Command codex` 无输出）→ 按降级矩阵，CEO 阶段为「Claude 子代理单声部」。以下为独立子代理（未接触本报告任何先行结论）的原始产出：

#### CLAUDE SUBAGENT（CEO — 战略独立性）

**1. 是不是正确的问题 —— 否。** v0.8.0 把付费 PDF 摄取做成旗舰功能，却 100% 传不上去（`ingest/mineru.py:79-81`），发版 3 天无人察觉。10 倍杠杆的重框定不是「落 7 条」，而是「给云路径建立验收闸门」（真机 smoke 或发版清单）；计划只写「需人工确认后写进 changelog」，把发版闸门交给一次自愿动作。

**2. 前提：三条都是「裁定」而非证据。**
- 不拆补丁版：计划称「无成文规则」，但实际节奏是 0.7.0→0.7.3 九天三连补丁（`CHANGELOG_user.md:37-68`）。所谓「成本可数」是维护者记账成本，代价是用户等待。
- 「auto_watch 默认关」≠ 风险已控：它与 `ingest/worker.py:3-4` 硬设计正面冲突，且全部测试 mock `MineruClient` → 新自动路径零真机验证，正是 403 事故的同构复刻。
- `max_file_size_mb=0`：邻域类比关掉的是「机制」，此处关掉的是唯一成本闸门，而闸门服务对象恰是新增的自动花钱路径。

**3. 六个月后悔场景。** C58：watcher + 付费 API + 同步盘回灌（iCloud/Dropbox 一夜恢复大批文件）＝无人值守上传与扣费；用户分不清「已关」与「坏了」。C54：全局 session 夹具让「无配置可用」这一真实场景在整套测试中永久消失，未来测试因错误原因变绿。真根因是缓存根无 env 可覆盖（`config.py:436-437` 一带）——产品级修一处，测试一套不必动。

**4. 被草率排除的替代。** P0 不该排除：计划的验证策略靠 CI，却放任已知 flaky，发版闸门本身不可信。真正漏项是 **agent 约定式摄取**：`server.py:647,696` 已在提示 agent 调 `kb_ingest`，`SKILL.md:57` 已写——让 agent 主动 ingest，零线程、零意外扣费、不违硬设计。计划从未把它列为候选。

**5. 市场/竞争。** 全被动：7 条全来自两个用户，无外部信号。差异化「本地 + 零依赖」正被 MinerU 付费云依赖稀释；计划没有任何一张卡回答「为什么还要装这个 MCP」。且无成功判据，半年后无法复盘这版是赚是亏。

#### CODEX SAYS（CEO — strategy challenge）

`[codex-unavailable: binary not found]` — 未执行，未消耗 token。降级矩阵标注：CEO 阶段 = `[subagent-only]`。

#### CEO 双声部共识表

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════════════════════
  Dimension                            Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────────────────────
  1. Premises valid?                    NO      N/A    DISAGREE（计划前提被推翻 3 条：
                                                        P2/P3/P9，P3 已由用户改判）
  2. Right problem to solve?            NO      N/A    DISAGREE（缺口 = 云路径验收闸门，
                                                       已升为扩张候选 A18/E1）
  3. Scope calibration correct?          NO      N/A    用户裁定为准（D1=C）；子声部
                                                       认为 C58+C56 应独立发版 → 关闭
  4. Alternatives sufficiently explored? NO      N/A    CONFIRMED（agent 约定式摄取
                                                       从未入候选 → E2）
  5. Competitive/market risks covered?   NO      N/A    DISAGREE（零外部信号、无成功
                                                       判据 → 记入风险）
  6. 6-month trajectory sound?           NO      N/A    CONFIRMED（C58 无人值守扣费 /
                                                       C54 全局夹具永久遮蔽真实场景）
═══════════════════════════════════════════════════════════════════════════════
CONFIRMED = 双方一致。DISAGREE = 有声部缺席（Codex 未安装）故不计为已确认。
缺席声部 = N/A（不计入 CONFIRMED）。单声部 critical finding 照常上报。
```

### Section 1 — 架构评审

**新增依赖图（before → after）**

```
BEFORE (v0.8.0)                                    AFTER (v0.8.1, 本计划)
──────────────────────────────                     ──────────────────────────────────────────
server.py                                          server.py
  _indexer_for ─▶ MarkdownIndexer(vault,cfg)          _indexer_for ─▶ MarkdownIndexer(vault,cfg)
                    │                                     │   ├─ indexer._ingest_hook = f   ◀── 新
                    └─ start_watching()                   │   └─ start_watching()
                     │                                    │
  _ingest_manager_for ─▶ IngestManager                _ingest_manager_for ─▶ IngestManager
       ▲  on_job_finished=_on_job_finished                 ▲  on_job_finished=_on_job_finished(←已死链)
       │       （1 参签名 / 2 参实参 → TypeError 被吞）     │
       └──────────────────────────────────────────────────┘
                                                     _indexer/watch.py  ◀── 新引用方向
                                                       _fs_event_matters: 文本/文档 两级分类
                                                       文档事件 ─▶ owner._ingest_hook()
                                                                    │
                                                                    └─▶ server._ingest_manager_for
                                                                         .submit(None)
                                                                          │
                                                                          ├─ scan_pending 全库扫描
                                                                          ├─ _recover_zombie_jobs
                                                                          └─ mkdir <vault>/.mortis-parsed/
```

**耦合判决**：`_indexer/` 的成文规约是「仅自底向上依赖，严禁运行时反向导入 Facade」（`Quick-start_developer.md:168`）。`_ingest_hook` 是**回调**而非 import，字面上不违规；但它让 `indexer` 第一次持有「指向 server 的可调用对象」，等于把 server 侧的生命周期（`_ingest_managers` 双检锁、跨进程 `.ingest.lock`、产物目录 mkdir）拖进 watcher 线程。**判决：接受（显式、可注入、默认 None 优于硬耦合），但必须配 A2 的两条硬约束。**

**扩展性（10x / 100x 首个崩点）**：
- 10x = 库数 × 文件数。首个崩点是 **C56**：每次 `kb_read(chunk_id=…)` 构造 N 个 indexer → 读 N 份 chunks+vectors 缓存（A9 修）。第二个是 **C58**：每个文件事件一次全库扫描 + 首轮对每个 PDF 做 sha256（A2 修）。
- 100x：`kb_list_files` 的 `list_files()` 仍是先构造全量列表再切片（`indexer.py:961-962`）→ 内存峰值随文件数线性，切片只省序列化不省扫描。记为已知边界（本版不修）。

**单点故障**：无新增。`_ingest_hook` 若抛异常会打死 watcher 线程（计划已要求 hook 内 try/except 全吞，保持）。

**安全架构**：新增两个输入面。
- `kb_list_files.path_prefix` → 必须与 `kb_search.path_prefix` 同款（库内相对 posix 前缀，不得逃出库）。
- `kb_read(chunk_id)` 的跨库探测把读取范围从「单库」扩到「所有注册库」→ 必须逐库仍过注册表白名单与 `_safe_path` 沙箱（否则等于绕过 §十 的信任边界）。**定为高危待确认项 S3-1。**

**生产失败场景**：MinerU 429 → 自动模式反复重提（计划已识别）；hook 打死 watcher（计划已 try/except）；`_recover_zombie_jobs` 在用户不知情时恢复旧 job 并上传（计划未覆盖 → A13）。

**回滚姿态**：C53 revert 一行 / C54 revert fixture + env（新 env 名可保留为空操作）/ C55 revert 取参 / C56 保留旧分支开关 / C57 移除参数即恢复 / C58 `auto_watch=false` 立即止血 + revert。**全部为 git revert 级，无需数据迁移。**

*本节制图：上文依赖图（新增组件与既有组件关系）。*

### Section 2 — 错误与救援映射表

```
  METHOD/CODEPATH                     | WHAT CAN GO WRONG                    | EXCEPTION CLASS
  ────────────────────────────────────|──────────────────────────────────────|──────────────────
  mineru._put_upload (C53)            | OSS 校验头不匹配                     | HTTPError 403
                                      | 网络中断 (v4/Agent 共用)             | URLError/socket
  indexer.sync (既有)                 | 单文件读失败                         | OSError
  indexer._embed_missing (既有)       | 429 / 5xx / 超时                     | ProviderError
  ── 新增 ──                          |                                      |
  _kb_read 跨库探测 (C56)             | 某库缓存损坏/不可读                  | OSError / zlib.error
                                      | 库目录已删（注册表未清）             | FileNotFoundError
                                      | 构造 indexer 期间被并发删除          | RuntimeError
  _kb_list_files 分页 (C57)           | path_prefix 非法（逃出库）           | ValueError
                                      | offset 越界                          | （应返回空 + total）
  watch._ingest_hook (C58)            | hook 抛异常 → watcher 线程死         | Exception
                                      | submit 扫描期间库被删                | OSError
  ingest.scan_pending 闸门 (C58)      | 文件 stat 失败                       | OSError
                                      | 状态文件损坏                         | JSONDecodeError
  ────────────────────────────────────|──────────────────────────────────────|──────────────────

  EXCEPTION CLASS        | RESCUED?                | RESCUE ACTION                    | USER SEES
  ───────────────────────|─────────────────────────|──────────────────────────────────|──────────────────
  HTTPError 403 (C53)    | Y（修好后 200）          | 不再发生                          | 正常上传
  URLError (C53)         | Y（providers 重试循环）  | 指数退避重试                      | 成功或可读错误
  OSError (读文件)        | Y                       | 记 failed_files，继续             | kb_stats.failed_files
  ProviderError 429      | Y                       | 尊重 Retry-After 退避             | 静默（透明）
  OSError / zlib.error   | **计划未指定 ← GAP**     | 应跳过该库并记入「未探测」         | 「N 个库未探测」
  （C56 探测失败）        |                         |                                  |
  FileNotFoundError      | **计划未指定 ← GAP**     | 同上：跳过 + 计入未探测            | 同上
  ValueError (prefix)    | Y（应为 fail-closed）    | 报错并给修法                      | 可读错误
  offset 越界            | **计划未指定 ← GAP**     | 返回空 files + total 原值          | 空列表 + total
  Exception (hook)       | Y（计划已要求全吞）      | 吞掉 + 不打断 watcher             | 静默（隐性失败风险）
  JSONDecodeError        | Y（既有 state 容错）     | 重建空状态                        | 重新解析
  ───────────────────────|─────────────────────────|──────────────────────────────────|──────────────────
```

**规则复核**：
- 计划要求 hook 内 `try/except` 全吞 → 按本节的规则，「吞掉并继续」几乎从不可接受。但此处是**派生数据路径 + 线程保命**约束，与仓库既有的「派生数据失败一律吞掉降级」（`PROJECT_GUIDE.md:739`）一致，**接受**；代价是必须补 A17 的可观测性（否则是静默失败）。
- **catch-all 检查**：`except Exception` 在本仓库已有 3 处既成事实（hook、`_run_sync_quietly`、`on_job_finished`）。本计划新增的 hook 再添 1 处 → 总数上升。已在 A17 用「必须可见」对冲。

### Section 3 — 数据流与交互边界

**新数据流（C56 跨库 chunk_id 寻址）**

```
  INPUT                VALIDATION            TRANSFORM                 PERSIST          OUTPUT
  chunk_id ──▶ ①非空校验 ──▶ ②遍历 registry.load() ──▶ ③逐库探测 ──▶ （只读，无落盘）──▶ 结果
     │              │                    │                      │
     ▼              ▼                    ▼                      ▼
  [nil?]      [与 start_line/      [注册表条目目录         [库缓存损坏?
   报错          end_line/heading     不存在 → 跳过          → 跳过 + 计入
  [empty?]      互斥 → 报错]        [solo 库是否参与?        未探测]
   报错                             ← 计划未指定 ← GAP]    [并发 sync 中?
  [超长?]                                                  → 乐观快照容忍]
   未定义 ← GAP

  命中数判定：
    唯一命中 ──▶ 用该库展开 + 追加 vault/vault_path
    多命中   ──▶ fail-closed，列候选（对齐 server.py:453-459）
    零命中 + 全部探过 ──▶ 沿用「chunk_id not found: …（文件可能已修改…）」
    零命中 + 有库未探测 ──▶ 必须如实说明，不得谎报「文件已修改」  ← 新文案分支
```

**交互边界表**

```
  INTERACTION                     | EDGE CASE                          | HANDLED? | HOW?
  ────────────────────────────────|────────────────────────────────────|──────────|──────────────
  kb_read(chunk_id) 跨库寻址 C56   | 同一文件复制进两库 → 撞 id          | Y（计划） | fail-closed
                                  | 某库未加载/未索引                    | Y（计划） | 如实说明
                                  | 某库目录已删（注册表未清）           | **N ← GAP** | 需按 OSError 跳过
                                  | solo 库是否参与探测                  | **N ← GAP** | 见下方问题 3A
                                  | 探测期间另一进程在重建该库缓存        | **N ← GAP** | 乐观快照
  kb_list_files 分页 C57           | offset 越界                          | **N ← GAP** | 返回空 + total
                                  | path_prefix 逃出库（`..`/绝对路径）  | **N ← GAP** | fail-closed
                                  | 过滤后 0 条                         | Y（隐含） | 空 files
                                  | 未传参数 → 与现状完全一致            | Y（计划） | 已声明
  auto_watch C58                  | 缓冲溢出 events=None                | **N ← GAP** | 是否也触发 ingest
                                  | 用户手动删库/改 ignore 规则          | **N ← GAP** | 需复用 ignore 过滤
                                  | hook 触发时上一次扫描未结束（重入）   | **N ← GAP** | A2 要求可合并
                                  | 同步盘一夜回灌 500 个 PDF            | **N ← GAP** | 需单轮上限/节流
  Windows 升级锁 C59              | 用户不知道是哪个进程占用             | Y（部分） | 计划只写「先退出客户端」
```

### Section 4 — 代码质量评审

| 项 | 判定 | 说明 |
|---|---|---|
| DRY | **1 处违规** | C56 新造跨库探测口径，未复用 F5b 短名寻址的「唯一/多义」范式（`server.py:1084-1087`）→ A3 |
| DRY | **1 处违规** | C57 的 `path_prefix` 若无共用实现，将成为第三套前缀语义（`kb_search` / `fts.py:128-130` 的 `.mortis-parsed/` 穿透 / 新增）→ A11 |
| 命名 | 可接受 | `_ingest_hook` 与既有 `_fs_*`/`_watch_*` 前缀不冲突；建议在字段旁写一行「null object 模式，默认 None」 |
| 组织 | 一致 | 新配置键落在 `IngestConfig`（既有 11 键），符合「加配置键四步法」 |
| 过度工程 | 无 | 未引入抽象层 |
| 欠工程 | **1 处** | C57 的 `total` 语义未定义（是「已索引文件数」还是「过滤后总数」）→ A10 |
| 圈复杂度 | **1 处超阈** | `_kb_read` 重构后分支数 = 2(入参) × 4(命中判定) × 2(有无未探测库) ≈ 8+ 分支，超 5 分支阈值 → 拆出纯函数 `_locate_chunk_across_vaults()` |
| 依赖铁律 | 遵守 | 无新增第三方依赖（`dependencies = []` 未动） |

### Section 5 — 测试评审（CEO 视角的新增清单）

```
  NEW CODEPATHS / FLOWS                                  计划是否给了测试？
  ────────────────────────────────────────────────────── ───────────────────
  C53  _put_upload 显式空 Content-Type                    Y（1 条回环）→ 但需 A15 加固
  C54a session 级 app.toml + function 级 env pin          N ← GAP（计划只列验证命令）
  C54a' 缓存根 env 覆盖（D3 新增）                         N ← GAP（D3 后需补）
  C54b 12 个 stdio 用例 + 进程内用例的 app.toml            N ← GAP（无「不再写真实 home」断言）
  C54c 注册表隔离                                        Y（可选，由用户单独评估）
  C55  kb_init/kb_init_solo 的 vault_path 别名            Y（2 条）
  C56  唯一命中 / 多命中 / 零命中全探过 / 零命中有未探测库   N ← GAP（4 条中计划只提 2 条）
  C57  默认全量一致 / 切片 / 前缀 / total / truncated      Y（部分；缺 total 与越界）
  C58  配置默认值与校验 / 闸门与 skipped 报告 / 显式超限报错 | Y（部分）
       / 自动模式跳过 failed / hook 接线与关闭时零调用       |
  C58' forward: 自动模式不重试 failed 的退避策略           部分（「最小退避」未定义可测行为）
  C58'' 闸门在 _run_job 的兜底（A13）                       N ← GAP
  回归：on_job_finished 回调被调用（A16）                    N ← GAP ← CRITICAL
  ────────────────────────────────────────────────────── ───────────────────
```

### Section 6 — 性能评审

| 路径 | 代价 | 判定 |
|---|---|---|
| C56 `kb_read(chunk_id)` 探测 | 每次构造 N 个 `MarkdownIndexer` → 全量加载 chunks(+vectors) 缓存 + 打开/可能重建 FTS sqlite。20 库 × 20MB 缓存 ≈ 400MB 读放 | **High → A9 必修** |
| C58 hook 全库扫描 | `submit(None)` → `scan_pending()` 递归全库 + 首轮每 PDF sha256。万级文件库：秒级到十秒级，且落在防抖调度线程上 | **High → A2 必修** |
| C57 切片 | `list_files()` 读内存 `_chunks`，切片零额外 IO | 通过 |
| C57 全量构造 | 仍先构造全量列表再切片（内存 O(文件数)） | 已知边界，本版不修，记 TODO |
| C54 fixture | session 级单文件（计划刻意避免每用例 mktemp） | 通过（设计正确） |

### Section 7 — 可观测性与可调试性

| 项 | 判定 |
|---|---|
| 结构化日志 | 无新增日志需求（本地 stdio 工具，日志走 stderr）；`[diag]` 模块已存在可复用 |
| 指标 | C58 的 `skipped_too_large` 计数与 `reason="too_large"`（计划已有）✔ |
| 告警 | 不适用（无服务端） |
| 可调试性 | **GAP 1**：自动模式「本次是否真的自动触发过扫描」无任何痕迹 → 用户分不清「已关」与「坏了」→ A17 |
| 可调试性 | **GAP 2**：C56 的「探测了几个库、跳过几个库」不可观测 → 零命中报错无从排障 |
| Runbook | `doctor`/`STATUS.md` 是既有载体，应挂 A17 的 auto_watch 状态 |

### Section 8 — 部署与放量

| 项 | 判定 |
|---|---|
| 迁移安全 | 无 DB 迁移；新增配置键全部带默认值 |
| 功能开关 | C58 的 `auto_watch = false` 天然即 feature flag ✔ |
| 放量顺序 | C53 → C54/C55/C57/P0 → P1 → C56 → C58 → C59 → C60。**C53 必须最先合入**（用户 D1 裁定一次发版后，C53 的尽早合入是唯一的时延对冲） |
| 回滚方案 | 逐卡 git revert 级，见 Section 1 |
| 放量风险窗 | 无（本地 stdio，用户各自升级） |
| 环境对齐 | 无 staging 概念；CI 五矩阵即放量前验证 |
| Post-deploy 验证 | 应在 CHANGELOG 条目里显式记「真机 OSS 200 已验证 / 待实机验证」，而不是留给口头承诺（计划已提及，加固为必须出现在 CHANGELOG 正文） |
| Smoke test | 计划无。E1 候选即为补此项 |

### Section 9 — 长期轨迹

**技术债**：
- 文档债（必须本版清）：`ingest/worker.py:1-4` 与 `ingest/__init__.py:3-4` 的「不做 watcher 自动摄取」硬设计必须改写（A7）。否则本仓库最忌讳的「注释与实现对不上」当场成立。
- 文档债：`PROJECT_GUIDE` §四 ingest 专节 / §七 `[ingest]` 小节 = **新增**而非修改（A6）。§八 `PROJECT_GUIDE.md:626` 的环境变量优先级清单是三对，新增缓存根后变四对。
- 测试债：C54 后若 `test_diaglog` 的 external 慢分支永久失去覆盖，需补一条显式 external 用例。

**路径依赖**：`auto_watch` 一旦被用户真机用起来，关闭时需要清理 pending 状态（`.ingest_state.json` 里的 queued/parsing 条目）→ 回滚成本从「改一行」升到「改一行 + 状态清理」。

**可逆性评分**：C53 = 5/5 · C54 = 4/5（env 是公开面，改名有成本）· C55 = 5/5 · C56 = 3/5（契约变更）· C57 = 4/5 · C58 = 3/5 · C59 = 5/5 · C60 = 5/5。

**1 年后的新人能看懂吗**：C58 的控制流是隐式的（watch 事件 → hook → server → manager ×4 跳）。仓库规约要求复杂设计配 ASCII 图（`PROJECT_GUIDE.md:867`）→ 必须在 `watch.py` 与 `server.py` 两侧各留一张。

**平台潜力**：E1（云端验收闸门）是平台级；C58 不是。

### Section 10 — 设计/UX

**SKIPPED — 无 UI 范围**（Phase 0 范围检测：视图/渲染类词仅 1 类命中且全为误报）。开发者体验另见 Phase 3.5。

### 失败模式登记表（Failure Modes Registry）

```
CODEPATH                      | FAILURE MODE                    | RESCUED? | TEST? | USER SEES?            | LOGGED?
──────────────────────────────|─────────────────────────────────|──────────|───────|───────────────────────|────────
C53 mineru._put_upload        | OSS 403（当前 100% 复现）        | N → 本卡修 | Y     | HTTPError 403 trace   | Y(stderr)
C53 mineru._put_upload        | 真实云 200 未被任何测试覆盖       | N        | **N** | 上传失败或静默降级      | 部分
                              |                                 |          |       | ← **CRITICAL GAP**（P9）|
C54 conftest fixture          | function 级未依赖 session 级，    | N        | **N** | 测试仍读真实宿主 config  | N
                              | env 指向未创建文件→静默回落       |          |       | 且显示为绿              |
                              |                                 |          |       | ← **CRITICAL GAP**      |
C54 conftest                  | pop(REGISTRY) 使 sessionfinish    | N        | **N** | 测试跑完写真 home 的     | N
                              | 守卫失效 → 写真实 status.json     |          |       | ~/.mortis_rag_mcp/      |
                              |                                 |          |       | ← **CRITICAL GAP**      |
C54b app.toml（12 个 stdio +   | 缓存根持续被写（18153 孤儿）      | N        | **N** | 无（静默磁盘增长）       | N
   进程内用例）                |                                 |          |       | ← **CRITICAL GAP**      |
C55 _kb_init/_kb_init_solo    | 调用方传 vault_path → 参数丢失    | N → 本卡修 | Y     | 可读 ValueError          | Y
C56 _kb_read 跨库探测          | 未加载库被漏探 → 谎报零命中       | N → 本卡修 | **N** | 「文件可能已修改」（假） | N
                              |                                 |          |       | ← **CRITICAL GAP**      |
C56 _kb_read 跨库探测          | 探测期 OSError/缓存损坏 → 中断    | **N**    | **N** | 裸异常或误报            | N
                              |                                 |          |       | ← **CRITICAL GAP**      |
C56 _kb_read 跨库探测          | 连接 vault 缓存体积 × 库数（400MB）| N      | **N** | kb_read 变慢数十秒       | N
                              |                                 |          |       | ← **CRITICAL GAP**（A9）|
C57 _kb_list_files            | path_prefix 逃出库                | **N**    | **N** | 可能列到库外条目         | N
                              |                                 |          |       | ← **CRITICAL GAP**      |
C57 _kb_list_files            | offset 越界 → 语义未定义           | **N**    | **N** | 结果不确定              | N
                              |                                 |          |       | ← **CRITICAL GAP**      |
C58 _ingest_hook              | hook 抛异常 → watcher 线程死       | Y（全吞） | **N** | 静默（自动摄取失效）      | N
                              |                                 |          |       | ← **CRITICAL GAP**（A17）|
C58 hook 挂载                  | 全库扫描阻塞防抖线程 → 文本 sync 饿死 | N     | **N** | 笔记改动长时间不被索引   | N
                              |                                 |          |       | ← **CRITICAL GAP**（A2）|
C58 自动路径                    | 未经 ignore 过滤，上传被排除文件    | **N**    | **N** | 无（隐私边界被越过）      | N
                              |                                 |          |       | ← **CRITICAL GAP**（A8）|
C58 自动路径                    | 同步盘回灌 500 PDF → 无人值守扣费  | **N**    | **N** | 账单                        | N
                              |                                 |          |       | ← **CRITICAL GAP**      |
C58 _run_job 闸门              | _recover_zombie_jobs 恢复的旧 job  | **N**    | **N** | 静默上传超限文件          | N
                              | 绕过扫描期闸门                    |          |       | ← **CRITICAL GAP**（A13）|
C58 状态机                    | _save_state 超 500 条剪掉旧 failed | N      | **N** | 莫名重试 → 额度浪费        | N
                              | →「最新 job 状态」判据失真         |          |       | ← **CRITICAL GAP**      |
C58→C56 共用链                | on_job_finished 参数不匹配        | Y（吞掉） | **N** | 静默（解析产物延迟入索引） | N
                              |                                 |          |       | ← **CRITICAL GAP**（A4）|
C59 文档                      | 用户仍不知哪个进程占锁             | —        | —     | 卡在 WinError 5 反复重试  | —
──────────────────────────────|─────────────────────────────────|──────────|───────|───────────────────────|────────
合计 19 行，CRITICAL GAP（RESCUED=N 且 TEST=N 且 USER SEES=静默或错误）共 14 处。
```

### CEO 扩张决策（SELECTIVE EXPANSION）

| # | 提案 | Effort | 决策 | 理由 |
|---|---|---|---|---|
| E1 | 云端摄取路径验收闸门 | S | **交最终门**（品味） | 本次事故教训；计划无对应卡 |
| E2 | agent 约定式摄取（复用既有 hint 基础设施） | S | **交最终门**（品味） | 独立声部指出计划从未列为候选；零线程零意外扣费 |
| E3 | `kb_ingest(pending)` 补 `total`/`next_offset`，与 C57 口径统一 | S | **接受（并入 C57）** | 同一次改动顺手对齐，避免两套分页口径（DRY，P4） |

### CEO 必产出清单

- **NOT in scope**：拆版（D1 已裁定，不复议）· P2 的仓库处置（实测不在仓库内）· `kb_rebuild` 的 Windows 回收站已知红（`Quick-start_developer.md:201-203` 明确要求独立任务）· `list_files()` 的全量构造内存优化（Section 6 已知边界）· 非 Windows 平台的 `path_prefix` casefold（既有边界）· MCP 认证（明确的非目标）。
- **What already exists**：见 Step 0B 表（9 行）。其中 3 行计划未复用（`resolve_default_cache_dir()`、F5b 范式、agent 摄取 hint）。
- **Dream state delta**：见 Step 0C。本计划清理存量缺陷，但零位移于「付费云路径可验收」。
- **Error & Rescue Registry**：见 Section 2（12 行，3 处 GAP）。
- **Failure Modes Registry**：见上（19 行，14 处 CRITICAL GAP）。
- **Completion Summary**：见下。

```
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SELECTIVE EXPANSION (autoplan override)      |
  | System Audit         | server.py 实为 1313 行（文档写 1126 已过期）；  |
  |                      | 无 CLAUDE.md/TODOS.md；无设计文档；工作区干净   |
  | Step 0               | 前提 7 条 → 3 条被推翻/修订；扩张候选 3 条       |
  | Section 1  (Arch)    | 4 issues found（hook 反向持有/探测代价/         |
  |                      | 安全边界/回滚姿态齐备）                        |
  | Section 2  (Errors)  | 12 条路径映射，3 GAPS                          |
  | Section 3  (Security)| 4 issues found, 2 High severity               |
  | Section 4  (Data/UX) | 17 edge cases mapped, 11 unhandled            |
  | Section 5  (Quality) | 5 issues found（2 DRY / 1 欠工程 / 1 复杂度）    |
  | Section 6  (Tests)   | Diagram produced, 7 gaps（2 CRITICAL 回归）     |
  | Section 7  (Perf)    | 2 issues found（均为 High，已有 A9/A2 修法）    |
  | Section 8  (Observ)  | 2 gaps found（auto_watch 状态 / 探测可见性）     |
  | Section 9  (Deploy)  | 3 risks flagged（3 处行为变更需 release notes） |
  | Section 10 (Future)  | Reversibility: 3/5 最低（C56/C58），debt 3 项  |
  | Section 11 (Design)  | SKIPPED (no UI scope)                        |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (6 items)                            |
  | What already exists  | written (9 items, 3 unreused)                |
  | Dream state delta    | written                                      |
  | Error/rescue registry| 12 methods, 3 GAPS                           |
  | Failure modes        | 19 total, 14 CRITICAL GAPS                   |
  | TODOS.md updates     | 3 items proposed（仓库无 TODOS.md → 见最终门） |
  | Scope proposals      | 3 proposed, 1 accepted (E3), 2 → gate        |
  | CEO plan             | written（本节即持久化）                        |
  | Outside voice        | subagent-only（codex 未安装）                 |
  | Lake Score           | 4/4 推荐均选了完整选项（A1/A9/A2/A13）          |
  | Diagrams produced    | 依赖图 / 数据流图 / 边界表 / 失败模式登记表      |
  | Stale diagrams found | 3（PROJECT_GUIDE:432"1064行"、             |
  |                      | Quick-start:34"1126行"、Changelog:320 同）    |
  | Unresolved decisions | 0（D1-D4 已由用户裁定）                        |
  +====================================================================+
```

**Phase 1 完成。** Codex：未运行（未安装）。Claude 子代理：5 维战略质疑（4 项与主审一致）。共识：`[subagent-only]`，**2/6 已确认**（第 4 维替代方案、第 6 维 6 个月轨迹）、**4/6 分歧**（第 1/2/3/5 维）。**⚠ 第二轮修正：原写「1/6 已确认、4/6 分歧、1/6 不计」与本节共识表自身矛盾（表内两处 CONFIRMED）**。通过至 Phase 2。

> 注：`tasks-*.jsonl` 与 `main-reviews.jsonl` 里写入的 `consensus_confirmed:1` 沿用了当时的错误计数，未回改（日志为追加式，回改会造成双份记录）。以本节为准。

> **Phase 2 跳过** —— 无 UI 范围。`Phase 2 complete. skipped, no UI scope.`

---

## Phase 3 — Eng 评审（全 4 节）

### Step 0 — 范围挑战（读实际代码）

已读源码：`ingest/mineru.py`、`ingest/worker.py`、`server.py`（`_indexer_for`/`_kb_ingest`/`_kb_list_files`/`_kb_read`/`_ingest_manager_for`）、`indexer.py`（`_init_cache_paths`/`_cache_root`/`_list_files`）、`_indexer/watch.py`、`_indexer/chunking.py`、`model`、`config.py`、`tests/conftest.py`、`fts.py`、`doctor.py`、`skills/mortis-rag-mcp/SKILL.md`、`docs/PROJECT_GUIDE.md`、`.gitignore`、`config/app.toml.example`、`docs/Changelog_developer.md`。

**核心发现（本版最大的一条，计划完全未提）**：

```python
# mortis_rag_mcp/ingest/worker.py:393-397
        if self.on_job_finished is not None:
            try:
                self.on_job_finished(job["source"], out_md)   # ← 传 2 个位置参数
            except Exception:
                pass                                          # ← 静默吞掉

# mortis_rag_mcp/server.py:766
                    def _on_job_finished(out_path: str) -> None:  # ← 只收 1 个参数
                        indexer = self._indexers.get(key)
                        if indexer is not None:
                            threading.Thread(target=indexer.sync, ...).start()
```

`_on_job_finished()` 每次调用都抛 `TypeError: takes 1 positional argument but 2 were given`，被 `except Exception: pass` 吞掉。**该回调从未生效过一次。** 影响：PDF 解析完成后不会触发索引同步（实际靠每个 `kb_*` 调用自带的 `try_sync_with_guard` 与 30s 兜底节拍补救，所以现象被掩盖）；`_kb_ingest` 的 hint（`server.py:794-795`）声称「done 的文档已写入 .mortis-parsed/ 并可被 kb_search 检索」实际不成立。C58 的设计（「解析完立即可检索」）正建立在这条链上 → A4 + A16。

**范围挑战结论**：不缩（D1 已裁定），但要求按 Section 8 的顺序落地，使低风险 5 张具备独立可 revert 的提交边界。

### Section 1 — 架构评审

依赖图见 Phase 1 Section 1（同一张图，此处不重复）。

**耦合新增**：
1. `indexer._ingest_hook` → `server._ingest_manager_for`：**indexer 首次持有指向 server 的可调用对象**。判决：接受（回调优于硬耦合），配 A2 两条硬约束。
2. `watch.py` 的事件分类 → 两级：文本事件走既有 sync 防抖路径（**不变**）；可摄取文档事件走独立的 ingest 请求标志。判决：接受。
3. `IngestManager` 从「仅 `kb_ingest` 显式触发」变成「watcher 可触发」→ 引入 server 侧生命周期依赖（`_ingest_managers` 双检锁、`.ingest.lock` 跨进程锁、`<vault>/.mortis-parsed/` mkdir）。判决：接受但必须记账（A7 文档改写）。

**注意——计划的一条理由与事实不符（A12）**：计划写「可摄取文档（INGEST_EXTS）事件 → 单独的 ingest 请求标志，与 sync 解耦，避免 PDF 事件白跑一次全量 sha256 对账」。实测 `_indexer/watch.py:120-134`：

```python
    return any(lower.endswith(ext) for ext in _INDEXABLE_TEXT_EXTS)   # = {".md", ".txt"}
```

PDF 事件**今天什么也不触发**（返回 False → 不唤醒防抖 sync）。不存在「白跑一次全量对账」。改动本身无害且必需，但理由是错的——意味着计划作者对事件路径的模型有偏差，需重查 `events=None`（内核缓冲溢出）分支与 ignore/cache 子目录过滤是否也要带 ingest 标志。

### Section 2 — 代码质量评审

| 项 | 判定 | 证据与修法 |
|---|---|---|
| DRY | 违规 | C56 未复用 F5b 的「唯一/多义」范式（`server.py:1084-1087`）→ A3 |
| DRY | 违规 | C57 的 `path_prefix` 需与 `kb_search` 共用实现 → A11 |
| DRY | 违规 | `scan_pending()` 的用户可见契约被 C58 复用 → A14 改用新函数 `_auto_pending()`，避免改既有返回语义（`test_ingest_server.py:184-186`、`test_ingest_worker.py:82-103` 已锁定） |
| 命名 | 通过 | 新配置键 `auto_watch` / `max_file_size_mb` 落在 `IngestConfig`（既有 11 键），命名风格一致 |
| 复杂度过高 | 违规 | `_kb_read` 重构后 8+ 分支 → 拆纯函数 `_locate_chunk_across_vaults()` |
| 欠工程 | 违规 | C57 的 `total` 语义未定 → A10 |
| 过度工程 | 无 | — |
| 依赖铁律 | 通过 | 无新增第三方依赖 |

### Section 3 — 测试评审（**不跳过、不压缩**）

**框架检测**：`pyproject.toml` 声明 pytest（RUNTIME:python）；无 `pytest.ini`，`tests/` 目录存在；无 `CLAUDE.md` → 以 `Quick-start_developer.md:192-203` 为权威约定（靶向跑，全量交 CI）。

**Step 1-4：覆盖图（代码路径 × 用户流）**

```
CODE PATHS                                                      USER FLOWS
[+] ingest/mineru.py                                            [+] 用户传 PDF 到 MinerU
  ├── _put_upload() [C53]                                         ├── [GAP] 真机 OSS 200（P9，CRITICAL）
  │   ├── [★★★ TESTED] v4 通道解析/重试/错误码（test_ingest_mineru.py）  └── [★★ TESTED] 403 文案可读
  │   ├── [GAP] 显式空 Content-Type 被发出                          [+] 用户配置 [ingest]
  │   └── [GAP] 200 不抛错 + has_header 断言（A15）                    ├── [★★ TESTED] enabled/默认值（部分）
[+] server.py                                                     │   ├── [GAP] auto_watch 默认 false
  ├── _indexer_for() → _ingest_hook 挂载 [C58]                     │   └── [GAP] max_file_size_mb=20 默认与校验
  │   ├── [GAP] auto_watch=false 时零调用                          ├── [GAP] 跨键矛盾（auto_watch+enabled=false）
  │   ├── [GAP] hook 抛异常不死 watcher                             [+] 用户在库上加 PDF（C58）
  │   └── [GAP] hook 不在防抖线程内同步全库扫描（A2）                 ├── [GAP] 自动触发一次扫描
  ├── _kb_read() [C56]                                             ├── [GAP] 同步盘回灌 500 个 PDF
  │   ├── [★★★ TESTED] 单库 chunk_id 原地展开（test_kb_read_chunkid.py:72-78）  └── [GAP] 被 ignore 的文件不上传（A8）
  │   ├── [★★★ TESTED] 多库+无 vault_path 报错（:278-280 → 需改写） [+] 用户读 chunk（C56）
  │   ├── [GAP] 唯一命中自动跨库展开 + 追加 vault/vault_path         ├── [GAP] 唯一命中 → 自动展开
  │   ├── [GAP] 多命中 fail-closed 列候选                          ├── [GAP] 多命中 → 可读错误列候选
  │   ├── [GAP] 未加载库被探到（现有 :263-270 全先走 _indexer_for）  └── [GAP] 零命中有未探测库 → 如实说明
  │   └── [GAP] 有库未探测时不谎报「文件已修改」
  ├── _kb_list_files() [C57]
  │   ├── [★★ TESTED] files[0]["source"]（test_mcp_stdio.py）
  │   ├── [GAP] 默认全量行为与现状一致
  │   ├── [GAP] limit/offset 切片 + total + truncated + next_offset（A10）
  │   ├── [GAP] path_prefix 过滤 + 逃逸 fail-closed（A11）
  │   └── [GAP] offset 越界
  └── _on_job_finished() [A4]
      ├── [GAP] ← **CRITICAL 回归** 回调被真实调用（spy 计数，A16）
      └── [GAP] 2 参→1 参签名对齐后同步被触发
[+] ingest/worker.py [C58]
  ├── scan_pending()/submit(None)
  │   ├── [★★ TESTED] 幂等跳过（test_ingest_worker.py:82-103）
  │   ├── [GAP] 闸门在 channel_for 之前（否则超 200MB 走 pymupdf 静默降级）
  │   ├── [GAP] reason="too_large" + skipped_too_large 计数
  │   └── [GAP] 不塞进 kb_stats.skipped_unsupported（test_ingest_server.py:194-218 已锁定排除 INGEST_EXTS）
  ├── 显式 sources 超限
  │   ├── [GAP] 报错文案含修法
  │   └── [GAP] **无 key 时保留既有 pymupdf 兜底语义**（A1；否则 15MB PDF 从「粗糙结果」变硬失败）
  ├── _run_job() 兜底闸门（A13）
  │   ├── [GAP] _recover_zombie_jobs 恢复的旧 job 也过闸门
  │   └── [GAP] 排队后文件变大也过闸门
  ├── _auto_pending()（A14，新函数）
  │   ├── [GAP] 自动模式跳过 done/failed/queued/parsing
  │   ├── [GAP] 仅 sha256 变化才重入队
  │   ├── [GAP] failed 的最小退避（429 场景：sha 不变 → 必须靠退避而非 sha）
  │   └── [GAP] _save_state 超 500 剪旧 failed → 判据按 submitted_at 取 max 而非「某条」
  └── _ingest_hook 接线
      ├── [GAP] monkeypatch IngestManager.submit 计数
      └── [GAP] auto_watch=false 时零调用
[+] _indexer/watch.py [C58]
  ├── _fs_event_matters()
  │   ├── [★★ TESTED] 缓存子目录自激防护（test_watch_integration/test_fsnotify）
  │   ├── [GAP] 文档事件（INGEST_EXTS）走独立 ingest 标志
  │   └── [GAP] events=None（缓冲溢出）路径是否带 ingest 标志（A12 遗留问题）
[+] conftest.py / tests [C54]
  ├── [GAP] ← **CRITICAL** function 级 fixture 显式依赖 session 级（否则静默回落宿主 config）
  ├── [GAP] ← **CRITICAL** 缓存根 env 覆盖生效（D3）
  ├── [GAP] ← **CRITICAL** 12 个 stdio 用例 + 进程内用例不再写真实 home
  ├── [GAP] pop(REGISTRY) 不破坏 pytest_sessionfinish 守卫（否则写真 home status.json）
  └── [GAP] test_diaglog 的 external 慢分支补一条显式 external 用例（避免永久失去覆盖）

COVERAGE: 18/52 路径已覆盖（35%）  |  Code paths: 14/40 (35%)  |  User flows: 4/12 (33%)
QUALITY: ★★★:6 ★★:5 ★:3  |  GAPS: 34（2 标 CRITICAL 回归，1 标 [→E2E] 真机）
[→E2E] = 需要真机/集成测试（P9 云端 200）  |  本项目无 LLM eval 套件（Embedding 用 static/mock）
```

**Step 5：需补进计划的测试（按卡）**

| 卡 | 新增测试文件/用例 | 类型 | 断言要点 | 优先级 |
|---|---|---|---|---|
| C53 | `tests/test_ingest_mineru.py` 追加 | 集成（本机回环，端口 0） | 收到的 `Content-Type != application/x-www-form-urlencoded`；**且 `req.has_header("Content-type")` 为真**（A15）；200 不抛错 | P1 |
| C54 | `tests/conftest.py` + 新增 `tests/test_isolation_guard.py` | 单元 | ① session app.toml 存在且 `resolve_config_path` 指向它；② function 级 fixture 依赖 session 级（否则该断言会失败）；③ 缓存根落在 session tmp；④ `pytest_sessionfinish` 守卫未被破坏 | P1 |
| C54b | 逐文件（12 stdio + 进程内） | 集成 | 每个 app.toml 含可用 `[cache] dir`（或断言走 env 覆盖） | P1 |
| C55 | `tests/test_scoped_search.py` 追加 | 单元 | `vault_path=` 调 `kb_init_solo` 成功；旧 `path` 回归 | P2 |
| C56 | `tests/test_kb_read_chunkid.py` 改写+追加 | 单元 + stdio | ① 唯一命中跨库展开并含 `vault`/`vault_path`；② 撞 id → fail-closed 列候选；③ **未加载库也被探到**；④ 有库未探测时文案**不含**「文件可能已修改」；⑤ 探测期 OSError → 跳过该库并计入未探测 | P1 |
| C57 | `tests/test_mcp_stdio.py` 追加 | 集成 | ① 不传参行为与现状一致；② limit/offset 切片 + `total` = 过滤后切片前条目数 + `truncated` + `next_offset`；③ `path_prefix` 过滤；④ `path_prefix` 逃逸（`../`、绝对路径）fail-closed；⑤ offset 越界 → 空 + total 原值 | P1 |
| C58 | `tests/test_ingest_worker.py` + `test_ingest_server.py` + `test_watch_integration.py` + `test_fsnotify.py` + `test_config`（追加） | 单元 + 集成 | 见下「C58 专项」 | P1 |
| C58 专项 | 配置默认值与校验（含 0 与负值）；闸门位置（`channel_for` 之前）；`reason="too_large"` + `skipped_too_large`；**显式超限报错文案含修法**；**无 key 时保留 pymupdf 兜底（A1）**；`_run_job` 兜底闸门（A13）；`_auto_pending` 的 done/failed/queued/parsing 跳过 + sha 变化重入队 + failed 退避；`_save_state` 剪枝后判据仍取最新（按 `submitted_at` max）；hook 接线 monkeypatch 计数；**`auto_watch=false` 时零调用**；hook 抛异常不死 watcher；**文档事件触发 ingest 标志**；`events=None` 路径行为；**自动路径复用 ignore/exclude 过滤（A8）** | 单元 + 集成 | 全流程 mock `MineruClient` | P1 |
| A4 回归 | `tests/test_ingest_worker.py` 追加 | 单元（spy） | **`on_job_finished` 被真实调用**（当前 100% 失败，属 REGRESSION RULE 强制项）；签名对齐后触发一次 sync | **P1 CRITICAL** |
| P0 | `tests/test_adversarial_v070.py::test_gate_9_retryable_mineru_error` | 集成 | 状态断言改为带超时的轮询等待 | P1 |

**回归铁律（REGRESSION RULE）**：`on_job_finished` 参数不匹配属「既有行为已坏 + 新计划依赖它」→ 回归测试为强制项，**无 AskUserQuestion、不可跳过**（已落 A4/A16）。

**Flakiness 风险**：
- P0 的竞态（`force=True` 重试后立即 `mgr.status()`）→ 带超时轮询是正解。
- C58 的自动模式测试若依赖真实计时（防抖 0.5s / 兜底 30s）→ 必须注入可控节拍或 monkeypatch 时钟。
- C56 的跨库探测若依赖 `_indexers` 字典的加载顺序 → 测试必须显式构造「已加载/未加载」两种前置态。

**测试金字塔**：轻度倒置风险——本计划新增的单元测试与集成/stdio 测试数量接近（计划偏向 stdio 集成）。可接受（该项目 stdio 集成是主要回归网），但 C58 的分支组合应用单元测试覆盖，不要都塞进 stdio。

**Load/stress**：C56 的 N 库探测与 C58 的全库扫描都属「频繁调用 + 处理显著数据量」→ 计划未列任何性能断言。建议 C56 至少补一条「N=10 库时 kb_read(chunk_id) 耗时 < X」的冒烟断言（可用 mock 的空缓存构造）。

**LLM/提示变更**：本计划改动 `skills/mortis-rag-mcp/SKILL.md`（agent 使用纪律 = 提示面）→ 按框架规则属 `[→EVAL]`。仓库无 eval 套件，替代验证为：`skan` 手工对照 + `Quick-start_developer.md:233` 的 `scripts/eval_search.py --golden tests/eval/golden_queries.json`（记录 Hit@K 基线）。**C56 改变了 agent 的阅读纪律 → 必须跑一次 eval 确认检索侧零回退。**

### Test Plan Artifact

已写入 `~/.gstack/projects/moton16-Mortis-RAG-MCP/14166-main-eng-review-test-plan-20260929.md`（见同目录产物）。

### Section 4 — 性能评审

| 路径 | 最坏情况 | 结论 |
|---|---|---|
| C56 探测 | 20 库 × 全量加载 chunks(+vectors) + 打开/可能重建 FTS sqlite ≈ 数百 MB 读放，重复于**每次** chunk_id 读取 | **High**，A9 必修 |
| C58 hook | 每个文档事件一次全库递归 + 首轮每 PDF sha256；落在防抖调度线程 | **High**，A2 必修 |
| C58 首轮 | `_recover_zombie_jobs` 拉起 `.ingest.lock` 并 mkdir 产物目录（副作用，非性能） | 需记账（A7） |
| C57 切片 | 读内存 `_chunks`，零额外 IO | 通过 |
| C57 全量列表 | 内存 O(文件数)，切片不省扫描 | 已知边界，记 TODO |
| C54 fixture | session 级单文件（刻意避免每用例 mktemp） | 通过 |

### Eng 必产出清单

- **NOT in scope**：`list_files()` 全量构造优化 · 三级以上前缀语义统一（`fts.py:128-130` 的 `.mortis-parsed/` 穿透）· `_save_state` 500 条剪枝策略本身的重构（本版只在判据侧绕开）· Windows 回收站 `SAFE_DELETE_FAIL_CLOSED` · 多平台 watcher。
- **What already exists**：见 Phase 1 Step 0B。
- **Diagrams**：依赖图（Phase 1 S1）· C56 数据流四径图（Phase 1 S3）· 覆盖图（本节 Step 4）· 失败模式登记表（Phase 1）。
- **Failure modes**：19 行，14 CRITICAL GAP（见登记表）。
- **Worktree 并行化策略**：

| Step | 触及模块 | 依赖 |
|---|---|---|
| C53 修 Content-Type | `ingest/` | — |
| C54 测试隔离 + 缓存根 env | `tests/`、`config.py` | — |
| C55 参数别名 | `server.py` | — |
| C56 kb_read 跨库 | `server.py`、`_indexer/` | — |
| C57 kb_list_files 分页 | `server.py`、`indexer.py` | — |
| C58 auto_watch | `config.py`、`_indexer/watch.py`、`server.py`、`ingest/` | 依赖 C54（测试基建）与 A4 修链 |
| A4 回调修链 | `ingest/worker.py`、`server.py` | — |
| P0 flaky | `tests/` | — |
| P1 文档清理 | `docs/` | — |
| C60 收口 | `pyproject.toml`、`README`、`CHANGELOG` | 全部 |

**并行通道**：`Lane A: C53 → C58(ingest 部分)` / `Lane B: C54 + P0（tests/ 独占）` / `Lane C: C55 → C56 → C57（server.py 顺序，共享文件）` / `Lane D: A4 → C58(server/hook 部分)` / `Lane E: P1 文档清理（docs/，必须先于 C58 的文档改动）` / 最后 `C60`。
**冲突标记**：Lane C 与 Lane D 都改 `server.py`；Lane B 与 Lane D 都改 `tests/`；**Lane E 必须先于 C58 的 §四/§七 新增**。建议 A+B 并行（互不重叠），C 与 D 顺序执行，E 最先，C60 最后。

### Eng 完成小结

```
  +====================================================================+
  |            ENG PLAN REVIEW — COMPLETION SUMMARY                     |
  +====================================================================+
  | Scope challenge      | 不缩（用户 D1 裁定）；新增 1 条 CRITICAL 发现     |
  |                      | （on_job_finished 死链）                        |
  | S1 Architecture      | 3 处新增耦合，全部判决为接受 + 约束              |
  | S2 Code quality      | 7 项检查，4 项违规（3 DRY + 1 复杂度 + 1 欠工程） |
  | S3 Tests             | 覆盖图 52 路径 / 18 覆盖（35%）；GAPS 34        |
  | S4 Performance       | 2 High（C56 探测、C58 hook），均已有修法         |
  | Failure modes        | 19 行，14 CRITICAL GAP                         |
  | Test plan artifact   | written                                        |
  | Worktree strategy    | written（5 通道 + 3 冲突标记）                   |
  | Unresolved decisions | 0                                              |
  +====================================================================+
```

**Phase 3 完成。** Codex：未运行（未安装）。Claude 子代理：5 维（架构/边界/测试/安全/隐藏复杂度），提出 12 条 finding，其中 5 条 High。共识：`[subagent-only]`。通过至 Phase 3.5。

#### ENG 双声部共识表

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════════════════════
  Dimension                            Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────────────────────
  1. Architecture sound?                NO      N/A    DISAGREE（C56「轻量构造」不轻、
                                                       C58 hook 线程归属；均已修法）
  2. Test coverage sufficient?          NO      N/A    DISAGREE（34 GAP，2 CRITICAL 回归）
  3. Performance risks addressed?       NO      N/A    DISAGREE（2 High 未在计划中）
  4. Security threats covered?          NO      N/A    DISAGREE（跨库探测沙箱 + 自动摄取
                                                       隐私边界）
  5. Error paths handled?               NO      N/A    DISAGREE（14 CRITICAL GAP）
  6. Deployment risk manageable?        YES     N/A    CONFIRMED（默认关 + git revert 级）
═══════════════════════════════════════════════════════════════════════════════
```

---

## Phase 3.5 — DX 评审（DX POLISH 模式，全 8 轮）

### Step 0A — 开发者画像卡

```
DEVELOPER PERSONA CARD
======================
角色          : 本地优先的 AI agent 重度用户 / 二次开发者
环境          : Windows 10/11，单人维护，Obsidian 风格 Markdown 笔记库 1k~10k 文件
已有工具      : Claude Code / Codex / WorkBuddy 等 MCP 客户端（stdio 连接）
技术水平      : 会用 venv + pip，会手写 TOML；不读源码，读 README/QUICKSTART/SKILL.md
主要诉求      : ① 笔记不出本机 ② 装完就能用 ③ 大库检索可控 ④ 出问题能自己查明白
痛点来源      : 本版 7 条中有 2 条（#6 Windows 写锁、#2 测试宿主隔离）正是这类用户/贡献者踩的
第二个用户画像 : 贡献者/评估者（README → clone → pytest），看 CI 是否可信
```

### Step 0B — 开发者共情叙事

> 我在笔记本上装了 Obsidian，攒了三年笔记。看到这个 MCP，第一反应是「终于有个不把笔记传出去的东西」。照 README 建 venv、`pip install -e .`、在客户端配置里粘上 exe 路径，重启，`kb_init` —— 搜索出来了，感觉不错。
>
> 然后我有一堆教材 PDF。文档说要开 `[ingest] enabled = true`、要配 MinerU key。我照做了。点 `kb_ingest(action='submit')`。**一个文件都传不上去。** 我去看 `kb_stats`，`failed_files` 有一堆 403。我不知道 403 是我的 key 问题、额度问题，还是这个工具本身坏了——错误信息没告诉我。
>
> 后来我看到 issue 里说这是工具自己的 bug。我有点生气：这个功能在发布说明里是「旗舰」。三天了。
>
> 另外我是个愿意提 PR 的人。我 clone 下来跑 `pytest tests/ -q`，跑完之后发现我的 `~/.mortis_rag_mcp_cache` 里多了几百个文件，`~/.mortis_rag_mcp` 里多了个 `status.json`。**我只是想跑个测试。** 我不知道哪些文件是工具故意留的、哪些是它忘了收拾的，所以我一个都不敢删。
>
> 我想升级到新版修 403。`pip install -e .` 报 `[WinError 5]`。文档里没写这是因为我开着的 MCP 客户端占着 exe。我搜了半天才明白。

### Step 0C — 竞争基准

| 维度 | 同类参考做法 | 本项目当前 | 本计划后 |
|---|---|---|---|
| 安装 | uv/pipx 一行装 | `venv + pip install -e .`（4 步） | 不变 |
| 首次可用 | 装完即跑出结果 | 需配 MCP 客户端 + `kb_init` + 等首建索引 | 不变 |
| 付费云依赖的失败可见性 | 明确的鉴权/额度/网络三分文案 | `HTTPError 403` 直出 | 不变（C53 只修成功路径） |
| 测试环境洁净 | 默认全隔离 | 写真实 home（18153 孤儿文件） | **修复**（D3） |
| 升级安心度 | CHANGELOG + 升级须知 | 有 CHANGELOG，无锁占用说明 | **补**（C59） |
| 契约变更公告 | 迁移指南 + 弃用警告 | 无 | **需补**（C56 为契约变更） |

### Step 0D — 魔法时刻设计

**候选**：`kb_init` 扫到 PDF 时，直接返回「检测到 N 个 PDF/Office 文档，其中 M 个可立即解析；运行 `kb_ingest(action='submit')` 开始」——用户零配置就知道下一步。

**交付载体**：**已存在**。`server.py:645-649`、`687-698` 的 `hint` 已经在做这件事（「可调用 kb_ingest(action='pending') 查看待解析列表」）。→ **不需要新建，只需在 C55/C58 改动 hint 时不要破坏它**。这也是扩张候选 E2 的现成底座。

**判定**：`Magical Moment: 已设计（复用既有 hint），交付载体 = kb_init/kb_init_solo 的返回 hint`。

### Step 0E — 模式选择

**DX POLISH**（autoplan 固定 override）。

### Step 0F — 开发者旅程地图（9 阶段）

| # | 阶段 | 本计划触及 | 摩擦点 | 是否解决 |
|---|---|---|---|---|
| 1 | 发现 | 否 | README 无「和别的 RAG 方案比，为什么选这个」 | 否 |
| 2 | 评估 | 否 | 无成功判据 / 无 benchmark | 否 |
| 3 | 安装 | 否 | 4 步 + 可选依赖说明 | 否 |
| 4 | Hello World | 否 | TTHW 约 8-12 分钟 | 否 |
| 5 | 集成 | **是** | C59：`pip install -e .` WinError 5 无解释 | **部分**（只写「先退出客户端」，无「怎么确认占用」） |
| 6 | 调试 | **是** | C56 的跨库零命中报错必须区分「真没找到」vs「有库未探测」；C58 的自动模式状态不可见 | **部分**（C56 计划已提文案分支；C58 未提） |
| 7 | 升级 | **是** | 本版含 3 处行为变更（默认 20MB / kb_read 多库 / 新增 env） | **否 ← GAP**（计划未列 release notes 条目） |
| 8 | 扩展 | 否 | 无插件/无 SDK | 否 |
| 9 | 迁移 | 否 | 无 | — |

### Step 0G — 首次接触者困惑报告（角色扮演）

| # | 困惑 | 本计划是否消除 |
|---|---|---|
| 1 | 「403 到底是我的问题还是工具的问题？」 | **否**——C53 只修成功路径，错误文案未改 |
| 2 | 「跑完测试之后这些文件是谁的？能删吗？」 | **是**（D3+C54b） |
| 3 | 「`pip install -e .` 报 WinError 5 我该关哪个进程？」 | **部分**——C59 只给方向不给确认方法 |
| 4 | 「`kb_read(chunk_id=...)` 以前报错让我传 vault_path，现在又能自动找到了，我原来的脚本还行吗？」 | **否 ← GAP**——契约变更无迁移说明 |
| 5 | 「为什么这个 50MB 的 PDF 突然被跳过了？」 | **部分**——`reason="too_large"` 有了，但「默认值从无到 20」这件事只在 release notes 里（计划未列） |
| 6 | 「我打开了 auto_watch，怎么知道它在工作？」 | **否 ← GAP**（A17） |

### 8 轮评审（Pass 1-8）

**Pass 1 — 上手体验（零摩擦）：4/10**
10 分长这样：`pipx install mortis-rag-mcp` 一行 + 客户端配置片段可直接复制 + 首条检索结果 < 2 分钟。
当前实测路径：clone → `python -m venv .venv` → `pip install -e .` → 手写 MCP 客户端配置 → 重启客户端 → `kb_init` → **等首建索引（大库分钟级）** → 才有第一条结果。TTHW ≈ **8-12 分钟**（Competitive 区间下沿 / Needs Work）。
计划触及：C59 只解决升级路径的锁问题，不触碰首次上手。
**结论：本计划不改善 TTHW。记 DX 债（最终门候选）。**

**Pass 2 — API/CLI/SDK 设计：6/10 → 8/10**
- 改善 1（C55）：`kb_init`/`kb_init_solo` 补齐四别名，与另外 5 个工具口径统一。命名可猜性从「3 个例外」变「一致」。
- 改善 2（C57）：`limit`/`offset`/`path_prefix` 与 `kb_search` 同形 → 猜得出来。
- 风险 1（C57）：若 `total` 语义不明（A10），猜不出「还有没有下一页」。
- 风险 2（E3 已接受）：`kb_ingest(action='pending')` 应与 C57 同形补 `total`/`next_offset`，否则同一个包里两套分页口径。
- **改进后 8/10**（扣分项：`_kb_ingest` 的 `hint` 文案目前是错的，见 Pass 3）。

**Pass 3 — 错误信息与调试：5/10 → 7/10**
仓库规约要求「人类可读的 ValueError」；本计划的错误面变化：
- 好：C58 的 `reason="too_large"`（带 size/limit）+ `skipped_too_large` 计数 + hint；显式超限「报错文案给出修法」。
- 差 1 ← **GAP**：`_kb_ingest` 的 hint（`server.py:794-795`）声称「done 的文档…可被 kb_search 检索」，**实际因 `on_job_finished` 死链不成立**。这在修 `on_job_finished`（A4）后变成真的，但在此之前它是错误承诺 → 修链与文案必须同批。
- 差 2 ← **GAP**：C56 的新文案分支（「有库未探测」）计划提了，但没定义具体措辞。必须有：问题（哪个库没探）+ 原因（未加载/未索引）+ 修法（先对该库调一次 `kb_stats` 或传 `vault_path`）。
- 差 3 ← **GAP**：C53 的 403 在**修好之前**无法自诊断。建议（可选）：403 时 hint 提示「若为预签名上传失败，升级到 0.8.1」——属可选，不列为 P1。
- **改进后 7/10**。

**Pass 4 — 文档与学习：5/10 → 7/10**
- 计划已列：`app.toml.example`、`PROJECT_GUIDE` §四/§七、`QUICKSTART_user.md`、`SKILL.md`。
- 更正（A6）：PROJECT_GUIDE §四**无 ingest 专节**、§七**无 `[ingest]` 小节** → 是**新增**，不是修改。工作量被低估。
- 补充（A6'）：§八 的 `PROJECT_GUIDE.md:626` 环境变量优先级清单是三对（API_KEY/CONFIG/REGISTRY），新增缓存根后变四对 → 必须同步。
- 补充：`Quick-start_developer.md:34`（`server.py` 1126 行）、`:192-203`（测试章节）与 `docs/PROJECT_GUIDE.md:432`（1064 行）都是过期数字（实际 1313 行）→ 顺手订正。
- SKILL.md 需改的具体位置（实测）：`SKILL.md:30`（判定表 #7 的 kb_read 纪律）、`:39`（反模式）、`:47`（工具清单）；建议同步 `:25`、`:28`。
- **改进后 7/10**（扣分：无搜索引擎、无 copy-paste 完整的 MCP 客户端配置示例）。

**Pass 5 — 升级与迁移：4/10 → 7/10**
- C59 给了 Windows 写锁说明 ✔
- ← **GAP**：本版含 **3 处行为变更**，计划未把它们组织成升级须知：
  1. `max_file_size_mb` 默认 20 → 20MB 以上文件在 `submit(None)` 里被跳过（D2=C 的必然后果）
  2. `kb_read(chunk_id=...)` 多库行为从「报错要求显式 vault_path」变「唯一命中自动展开」
  3. 新增公开环境变量 `MORTIS_RAG_CACHE_DIR`
  → `CHANGELOG_user.md` 的 `## [0.8.1]` 段必须逐条写「升级须知」，`QUICKSTART_user.md` 升级章节同步。`tests/test_version_sync.py:18-20` 只强制「有条目」，不强制「有须知」——需人工保证。
- C58 的 `auto_watch` 默认关 = 无升级冲击 ✔
- **改进后 7/10**。

**Pass 6 — 开发者环境与工具：6/10 → 9/10**
- D3 落地的缓存根 env 覆盖，是本计划**对 DX 贡献最大的一项**：测试与任何嵌入方都不再污染真实 home。
- C54 的 session 级 fixture 减少 tmp 碎片（对开发者机器的实际体感）。
- P0 修 flaky → CI 绿灯可信（对贡献者是高价值）。
- **改进后 9/10**（扣分：本地不跑全量的约定使贡献者无法自查全量回归，只能等 CI）。

**Pass 7 — 社区与生态：2/10**
7 条全来自 2 个用户；无外部信号；无「为什么选这个而不是别家」的回答。本计划不触及。
**结论：记为长期 DX 债，不列入本版。**

**Pass 8 — DX 度量与反馈闭环：2/10**
无 TTHW 度量、无成功判据、无 issue 模板、无「本次改动是否改善了什么」的复盘口径。
**结论：记为长期 DX 债。** E1（云端验收闸门）若采纳，可顺带落地最小闭环：每次发版记录一条真机 smoke 结果。

### DX 记分卡

```
+====================================================================+
|              DX PLAN REVIEW — SCORECARD                             |
+====================================================================+
| Dimension            | Score  | Prior  | Trend  |
|----------------------|--------|--------|--------|
| Getting Started      |  4/10  |  4/10  |  →     |
| API/CLI/SDK          |  8/10  |  6/10  |  ↑     |
| Error Messages       |  7/10  |  5/10  |  ↑     |
| Documentation        |  7/10  |  5/10  |  ↑     |
| Upgrade Path         |  7/10  |  4/10  |  ↑     |
| Dev Environment      |  9/10  |  6/10  |  ↑     |
| Community            |  2/10  |  2/10  |  →     |
| DX Measurement       |  2/10  |  2/10  |  →     |
+--------------------------------------------------------------------+
| TTHW                 | 8-12 min | 8-12 min |  →               |
| Competitive Rank     | Needs Work（1-5 min 是 Competitive 区间）    |
| Magical Moment       | designed via kb_init/kb_init_solo 返回 hint  |
| Product Type         | developer tool / MCP server + CLI + agent skill |
| Mode                 | POLISH                                       |
| Overall DX           |  5.8/10 |  4.3/10 |  ↑                     |
+====================================================================+
| DX PRINCIPLE COVERAGE                                               |
| Zero Friction      | gap（TTHW 未改善）                             |
| Learn by Doing     | partial（QUICKSTART 有，但无示例库/示例查询）      |
| Fight Uncertainty  | partial（C56 文案分支 / C58 状态可见性仍缺）       |
| Opinionated + Escape Hatches | covered（新增 env + 0/负值语义 + 显式 cap）|
| Code in Context    | partial（SKILL.md 需改 3-5 处具体行）             |
| Magical Moments    | covered（复用既有 hint 底座）                    |
+====================================================================+
```

**低于 6 分的项判为关键 DX 债**：Getting Started（4）、Community（2）、DX Measurement（2）。TTHW 8-12 分钟未超 10 分钟硬阈值但落在 Needs Work 区间。**均不阻塞本版**（本版是 bug 修复批次），已在最终门列为扩张候选。

### DX 实施清单

```
DX IMPLEMENTATION CHECKLIST
============================
[x] 每个错误信息含「问题 + 原因 + 修法」        ← C58 显式超限文案已含；C56 需补
[ ] 文档有可直接复制粘贴、且实际可跑的示例        ← MCP 客户端配置片段仍缺
[x] 每个参数都有合理默认值
[ ] API/CLI 命名无需文档即可猜到                 ← C55 后达标；C57 的 total 语义待钉（A10）
[ ] 升级路径有迁移指南                          ← 3 处行为变更需写进升级须知
[ ] 破坏性变更有弃用警告                        ← C56 无过渡期（见最终门）
[ ] 文档内可搜索                                ← 无搜索引擎
[ ] 有社区渠道且有人值守                        ← 无
[ ] 免费层无需信用卡                            ← 不适用（本地工具）；但 MinerU 是付费云
[x] 有 CHANGELOG 且持续维护
[x] 在 CI/CD 中无需特殊配置即可运行
[ ] 首次运行产出有意义的输出                    ← 大库首建索引期间无进度反馈
```
（`[x]` = 已满足或本计划已覆盖；`[ ]` = 未满足，其中多数属长期债）

### DX 必产出清单

- **Developer Persona Card** ✔ · **Developer Empathy Narrative** ✔ · **Competitive DX Benchmark** ✔ · **Magical Moment Specification** ✔（复用既有 hint）· **Developer Journey Map** ✔（9 阶段）· **First-Time Developer Confusion Report** ✔（6 条，4 条未消除）
- **NOT in scope**：TTHW 压缩（pipx/uv 一行装）· 文档搜索引擎 · 社区渠道 · DX 度量体系 · MCP 客户端配置片段生成器 · 首建索引进度反馈 · 无 SDK/多语言。理由：本版是 bug 修复批次，且这些都不是 issue #5 的范围。
- **What already exists**：`kb_init`/`kb_init_solo` 的 PDF 提示 hint（`server.py:645-649`、`687-698`）· `QUICKSTART_user.md` 的 5 分钟颗粒度部署指南 · `CHANGELOG_user.md` + `test_version_sync.py` 的版本一致性守卫 · `doctor`/`STATUS.md` 作为 agent 信任锚 · `SKILL.md` 的工具判定表与反模式清单 · `scripts/eval_search.py` 的 Hit@K harness。
- **TODOS.md updates**：仓库**无 `TODOS.md`**。候选 3 项（TTHW 压缩 / 云路径验收闸门 / 自动摄取状态可见性）→ 交最终门决定是否新建该文件。**注意：新建 `TODOS.md` 等于新增一个仓库根文件，需你显式同意。**

```
  +====================================================================+
  |            DX PLAN REVIEW — COMPLETION SUMMARY                      |
  +====================================================================+
  | Mode                 | POLISH                                      |
  | Persona              | 本地优先 AI agent 重度用户 / 二次开发者         |
  | Passes 1-8           | 4/8/7/7/7/9/2/2                             |
  | TTHW                 | 8-12 min → 目标 < 5 min（本版不改善）          |
  | Gaps                 | 6 项（升级须知 / C56 文案 / C58 状态可见性 /   |
  |                      | 403 自诊断 / SKILL.md 位置 / 文档工作量低估）   |
  | Magical moment       | 已存在，复用                                    |
  | Competitive rank     | Needs Work                                   |
  | Overall              | 5.8/10（提升 +1.5）                            |
  +====================================================================+
```

**Phase 3.5 完成。** DX 总分 5.8/10，TTHW 8-12 分钟（目标 < 5）。Codex：未运行。Claude 子代理：5 维 DX 独立评审（并入上文 Pass 结论）。共识：`[subagent-only]`，1/6 已确认（升级路径安全）。通过至 Phase 4。

---

## 交叉阶段主题（Cross-Phase Themes）

在两个及以上阶段的独立声部中**各自独立浮现**的问题（最高置信度信号）：

**主题 1：缓存根不可重定向是根因，不是症状** —— 出现在 Phase 1（CEO 子代理：「真根因是缓存根无 env 可覆盖，产品级修一处，测试一套不必动」）与 Phase 3（事实核验：「`_init_cache_paths()` 无条件 mkdir，与 `cache.enabled` 无关」；工程声部：「改全局夹具让『无配置可用』这一真实场景在整套测试中永久消失」）。**已由 D3 采纳。**

**主题 2：付费云路径没有验收闸门** —— 出现在 Phase 1（CEO 子代理：「10 倍杠杆的重框定是给云路径建立验收闸门」）与 Phase 3.5（Pass 5/Pass 8：无升级须知、无 DX 度量、无发版 smoke）。两个阶段独立指向同一处：**7 张卡没有一张让「下次 403 类事故」更早被发现**。→ 扩张候选 E1。

**主题 3：隐式控制流 + 全吞异常 = 静默失败** —— 出现在 Phase 3 工程声部（`on_job_finished` 参数不匹配被 `except Exception: pass` 吞掉，链死而无人知）与 Phase 1 Section 7（C58 的 hook 状态不可见）。同一代码库里两个独立位置、同一种失败形状。

**主题 4（第二轮修正后）：计划作者的行号引用精确度极高，两处「看似失真」经复核都不是计划的问题** —— 原判据被推翻：①「1126 行」来自 `docs/Quick-start_developer.md:34` 与 `docs/Changelog_developer.md:320`，**PLAN.md 全文没有声称任何行数** → 是既存文档过期，不是计划失真；②计划 C54b 写的是「逐一排查**所有** stdio 用例的 app.toml」，指令本身完整，被点名的两个文件来自 changelog 举例 → 「清单漏了 10 个」是稻草人。**真正成立的一条**：`PROJECT_GUIDE` §四/§七 需**新增**而非修改 ingest 内容（Phase 3.5 Pass 4 低估了工作量）。教训记在第二阶段审核里：**事实核验声部容易把「文档过期」误记为「计划失真」，也容易把「举例」误读为「穷举」**。

---

## 实施任务（跨阶段聚合）

来源标记：`CEO` = Phase 1 · `ENG` = Phase 3 · `DX` = Phase 3.5。P1 阻塞发版；P2 应同分支落地；P3 为后续 TODO。

### 卡内改动（修正后的计划）

- [ ] **T1 (P1, human: ~2h / CC: ~15min) — `ingest/worker.py` + `server.py` — 修 `on_job_finished` 参数不匹配死链并按回归铁律加测试**（ENG，CRITICAL）
  - 依据：`worker.py:393-397` 传 2 个位置参数，`server.py:766` 的 `_on_job_finished(out_path)` 只收 1 个 → `TypeError` 被 `except Exception: pass` 吞掉，回调从未生效
  - 修法：统一签名（建议 `_on_job_finished(source: str, out_path: Path)` 或 worker 只传 `out_path`）；保留「同步失败不打死 worker 线程」的兜底，但把吞异常改为可观测（计数或 diag）
  - 文件：`mortis_rag_mcp/ingest/worker.py`、`mortis_rag_mcp/server.py`、`tests/test_ingest_worker.py`
  - 验证：`pytest tests/test_ingest_worker.py -q`（新增 spy 断言回调被调用 + 触发 sync）

- [ ] **T2 (P1, human: ~1d / CC: ~1h) — `config.py` + `tests/conftest.py` + 12 个 stdio 用例 — C54 三件套 + 缓存根 env 覆盖**（用户 D3；CEO+ENG 交叉主题 1）
  - C54a：session 级 fixture 生成真实存在的 app.toml（含 `[cache] dir`）；function 级 autouse fixture **显式依赖 session 级**（否则 env 指向未创建文件 → `resolve_config_path` 静默回落宿主配置，`config.py:346-355`）
  - C54a'：新增 `MORTIS_RAG_CACHE_DIR`（新名优先 / `VAULT_MCP_CACHE_DIR` 兼容），在 `resolve_default_cache_dir()` 或 `load_config()` 的 cache.dir 回落处生效；`config/app.toml.example` 补注释
  - C54b：按修正清单补 `[cache] dir`——12 个 stdio 用例（`test_wikilink_read.py`、`test_ingest_server.py:202`、`test_txt_indexing.py:51,119`、`test_subvaults.py:99,123`、`test_scoped_search.py:34`、`test_exempt.py:223`、`test_preview_mode.py:106`、`test_diaglog.py:333-345`、`test_mcp_stdio.py:28`、`test_kb_read_chunkid.py:89,260`、`test_budget_bytes.py`（8 处）、`test_anti_contention.py:133`）+ 进程内用例
  - C54b'：会话启动 `os.environ.pop("MORTIS_RAG_REGISTRY", None)` 会破坏 `conftest.py:15` 的守卫 → 必须改为「pop 后由 fixture 设置自己的临时注册表」，否则 `pytest_sessionfinish` 会写真 home 的 `status.json`
  - 文件：`mortis_rag_mcp/config.py`、`config/app.toml.example`、`tests/conftest.py`、12 个测试文件、新增 `tests/test_isolation_guard.py`
  - 验证：`pytest tests/test_diaglog.py tests/test_path_migration.py tests/test_doctor.py -q`，再单跑 `test_improvements.py`、`test_adversarial_v070.py`、`test_exempt.py`、`test_mcp_stdio.py`、`test_isolation_guard.py`

- [ ] **T3 (P1, human: ~4h / CC: ~30min) — `server.py` — C56 重构：只读探测 + 复用 F5b 范式 + 拆纯函数**（ENG；A3/A9）
  - 禁止 `MarkdownIndexer(path, config)` 全量构造（`__init__` 会全量加载 chunks/vectors 缓存 + 打开/重建 FTS sqlite，`indexer.py:249-282`）→ 新增只读文本层入口，或进程级带失效的 chunk_id 索引
  - 复用 `server.py:1084-1087` 的「唯一命中→展开 / 多命中→列候选」口径
  - 拆出纯函数 `_locate_chunk_across_vaults()`（当前 8+ 分支超阈值）
  - 逐库仍过注册表白名单与 `_safe_path` 沙箱（跨库 = 读取范围扩大，不得绕过信任边界）
  - 零命中文案分支：全部探过 → 沿用现有；有库未探测 → 如实说明（不得谎报「文件可能已修改」）
  - 探测期 OSError / 缓存损坏 → 跳过该库并计入「未探测」；明确 solo 库是否参与探测
  - 文件：`mortis_rag_mcp/server.py`、`mortis_rag_mcp/_indexer/`（只读入口）、`tests/test_kb_read_chunkid.py`、`skills/mortis-rag-mcp/SKILL.md`（`:30`/`:39`/`:47`）
  - 验证：`pytest tests/test_kb_read_chunkid.py tests/test_mcp_stdio.py -q` + `python scripts/eval_search.py --golden tests/eval/golden_queries.json`（记 Hit@K 基线）

- [ ] **T4 (P1, human: ~4h / CC: ~30min) — `server.py` + `indexer.py` — C57 分页：钉死 `total` 语义 + 前缀复用 + 越界**（ENG；A10/A11/E3）
  - `total` = **过滤后、切片前的条目数**；补 `next_offset`（与 `kb_search` 同形）；`truncated`
  - `path_prefix` 与 `kb_search.path_prefix` **共用同一校验与匹配实现**；逃逸（`../`、绝对路径）fail-closed
  - `offset` 越界 → 返回空 `files` + `total` 原值
  - 顺手（E3）：`kb_ingest(action='pending')` 补 `total`/`next_offset`，与 C57 口径统一
  - 文件：`mortis_rag_mcp/server.py`、`mortis_rag_mcp/indexer.py`、`tests/test_mcp_stdio.py`
  - 验证：`pytest tests/test_mcp_stdio.py tests/test_preview_mode.py -q`

- [ ] **T5 (P1, human: ~1.5d / CC: ~1.5h) — `config.py` + `_indexer/watch.py` + `server.py` + `ingest/worker.py` — C58 auto_watch（含 6 处修正）**（用户 D1/D2；A1/A2/A7/A8/A13/A14/A17）
  - **修正 1**（A2）：`_ingest_hook` 早退（`auto_watch` 关闭时零调用）+ 不在 watcher/防抖调度线程内同步跑全库扫描（丢给一次性 worker 线程或可合并的 dirty 标志）
  - **修正 2**（A1）：硬闸门只拦「用户自己设的 cap」；channel 强加的 10MB 上限**维持今天的 PyMuPDF 兜底语义**（否则无 key 用户的 15MB PDF 从「粗糙结果」变硬失败）
  - **修正 3**（D2=C）：`max_file_size_mb` 默认 **20**（非 0）；`0` 保留为显式不限制
  - **修正 4**（A13）：`_run_job` 再兜一次闸门（覆盖 `_recover_zombie_jobs` 恢复的旧 job 与「排队后文件变大」）
  - **修正 5**（A14）：自动模式判据走新函数 `_auto_pending()`，不动 `scan_pending()`（后者是 `kb_ingest(action='pending')` 的用户可见契约，`test_ingest_server.py:184-186` 已锁定）；「最新 job 状态」按 `submitted_at` 取 max（`_save_state` 超 500 会剪旧 failed）；failed 的退避策略必须独立于 sha（429 场景 sha 不变）
  - **修正 6**（A8）：自动路径必须复用 ignore/exclude 过滤（`.vaultignore`、`exclude_patterns`、产物目录）——隐私边界不得扩大
  - **修正 7**（A7）：改写 `ingest/worker.py:1-4` 与 `ingest/__init__.py:3-4` 的「不做 watcher 自动摄取」硬设计；`watch.py` 与 `server.py` 各留一张 ASCII 控制流图
  - **修正 8**（A17）：`doctor`/`STATUS.md` 报 `auto_watch` 生效状态；自动触发写可见痕迹
  - **修正 9**（A12）：更正计划中「避免 PDF 事件白跑一次全量 sha256 对账」的理由（`watch.py:134` 只对 `.md/.txt` 返 True，PDF 事件今天什么也不触发）；补 `events=None`（缓冲溢出）分支行为
  - 文件：`mortis_rag_mcp/config.py`、`config/app.toml.example`、`mortis_rag_mcp/_indexer/watch.py`、`mortis_rag_mcp/server.py`、`mortis_rag_mcp/ingest/worker.py`、`mortis_rag_mcp/ingest/__init__.py`、`tests/`（5 个文件）
  - 验证：`pytest tests/test_ingest_worker.py tests/test_ingest_server.py tests/test_watch_integration.py tests/test_fsnotify.py -q`

- [ ] **T6 (P1, human: ~30min / CC: ~5min) — `tests/test_ingest_mineru.py` — C53 测试加固**（ENG；A15）
  - 断言收到的 `Content-Type != application/x-www-form-urlencoded`，**且 `req.has_header("Content-type")` 为真**（锁死「显式空值」；http.client 会发出 `Content-type: `（空值），不是不发头——原测试对「空值或缺省」都放行 = 没锁定任何行为）
  - 文件：`tests/test_ingest_mineru.py`
  - 验证：`pytest tests/test_ingest_mineru.py -q`

- [ ] **T7 (P1, human: ~1h / CC: ~10min) — `docs/PROJECT_GUIDE.md` — 转义污染清理（前置独立 commit）**（用户 D4；CEO 主题）
  - 必须**排在 C58/C59 的文档改动之前**（否则同一文件里大段机械 diff 与语义编辑互相遮蔽）
  - 方式：`grep -n '\\\[\|\\_'` 全量定位后批量还原；实测 §3.2 标题、§4.5、§六/§七 多处表格与代码引用行
  - 顺手订正过期数字：`docs/PROJECT_GUIDE.md:432`「server.py 约 1064 行」→ 1313；`docs/Quick-start_developer.md:34`「1126 行」→ 1313
  - 文件：`docs/PROJECT_GUIDE.md`、`docs/Quick-start_developer.md`
  - 验证：渲染预览抽查 + `grep -c '\\\[\|\\_'` 归零

- [ ] **T8 (P1, human: ~1d / CC: ~1h) — 四份文档 — C58/C59/C56 的文档同步（含新增小节 + 升级须知）**（DX Pass 4/Pass 5；A6）
  - **新增**（不是修改）：`PROJECT_GUIDE.md` §四 ingest 模块专节（当前 §四 子节为 4.1 config → 4.10 doctor，无 ingest）、§七 `[ingest]` 小节（当前为 embedding/reranker/index/vector/cache）
  - **补**：`PROJECT_GUIDE.md:626` 的环境变量优先级清单由三对扩为四对（+ 缓存根）
  - **升级须知（3 处行为变更，逐条写）**：① `max_file_size_mb` 默认 20 → 20MB 以上文件在 `submit(None)` 里被跳过；② `kb_read(chunk_id=...)` 多库行为从「报错要求显式 vault_path」变「唯一命中自动展开」；③ 新增公开环境变量 `MORTIS_RAG_CACHE_DIR`
  - `CHANGELOG_user.md` 的 `## [0.8.1]` 段：`tests/test_version_sync.py:18-20` 只强制「有条目」，不强制「有须知」→ 人工保证
  - `QUICKSTART_user.md`：升级章节 + `auto_watch` 用户向说明（大白话，不写函数名/内部名）
  - `skills/mortis-rag-mcp/SKILL.md`：C56 纪律（`:30`/`:39`/`:47`，建议 `:25`/`:28`）+ PDF 摄取章节
  - 文件：`docs/PROJECT_GUIDE.md`、`docs/Quick-start_developer.md`、`QUICKSTART_user.md`、`CHANGELOG_user.md`、`skills/mortis-rag-mcp/SKILL.md`
  - 验证：`pytest tests/test_version_sync.py -q`

- [ ] **T9 (P1, human: ~2h / CC: ~15min) — `tests/test_adversarial_v070.py` — P0 竞态修复**（用户 D4）
  - `test_gate_9_retryable_mineru_error`：`force=True` 重试后立即 `mgr.status()`，worker 尚未把 job 从 `failed` 翻回 `queued/parsing` → 改为带超时的轮询等待（参考 `test_stdio_wikilink_read` 的修法）
  - 文件：`tests/test_adversarial_v070.py`
  - 验证：本地单文件多轮 + CI 连续观察

- [ ] **T10 (P2, human: ~1h / CC: ~10min) — `server.py` — C55 参数别名**（原计划，无修正）
  - `_kb_init`(`612-613`)、`_kb_init_solo`(`652`/取参 `661-663`) 改为 `path or vault_path or vault or vault_name`；`inputSchema.required` 不动
  - 文件：`mortis_rag_mcp/server.py`、`tests/test_scoped_search.py`
  - 验证：`pytest tests/test_scoped_search.py tests/test_solo_vault.py -q`

- [ ] **T11 (P2, human: ~1h / CC: ~10min) — `QUICKSTART_user.md` + `docs/Quick-start_developer.md` — C59 Windows 写锁说明（加强版）**（DX Pass 1/Pass 5）
  - 除「先退出 MCP 客户端或结束进程」外，补**怎么确认是哪个进程占用**（如任务管理器按名称查 `mortis-rag-mcp.exe` / `vault-mcp.exe`）+ 备选路径（先 `pip uninstall` 再装 / 重启终端）
  - 文件：`QUICKSTART_user.md`、`docs/Quick-start_developer.md`
  - 验证：无代码改动，人工核对

- [ ] **T12 (P2, human: ~2h / CC: ~15min) — `pyproject.toml` + `server.py` + `README`×2 + `CHANGELOG`×2 — C60 版本收口**
  - `0.8.0 → 0.8.1`、`SERVER_INFO`、README 徽章、`SKILL.md` 头部版本（当前 `:4` 为 5.2.0 / `:7` 为 0.8.0）
  - `CHANGELOG_user.md` 新增 `## [0.8.1]`（含 T8 的升级须知）；`PROJECT_GUIDE.md` §十五 版本详录（`837` 行起，倒序追加在顶部）
  - `docs/Changelog_developer.md` 逐卡记账（从 `C53` 起；末位现为 `C52`）
  - **把「真机 OSS 200 已验证 / 待实机验证」写进 CHANGELOG 正文**，而非留给口头承诺
  - 文件：`pyproject.toml`、`mortis_rag_mcp/server.py`、`README.md`、`README_EN.md`、`CHANGELOG_user.md`、`docs/PROJECT_GUIDE.md`、`docs/Changelog_developer.md`、`skills/mortis-rag-mcp/SKILL.md`
  - 验证：`pytest tests/test_version_sync.py -q`；全量回归交 CI

### 扩张候选（交最终门决定）

- [ ] **X1 (P1 候选, human: ~4h / CC: ~30min) — 云端摄取路径验收闸门**（E1；CEO 主题 2 + DX Pass 5/8）
  - 交付物：一个可手动触发的真机 smoke（真实 `_put_upload` 对 OSS 预签名 URL 的 200 验证），或发版清单里一条不可跳过的检查项
  - 理由：本次 403 事故（旗舰功能 100% 坏、3 天无人察觉）的**唯一**结构化成因是「付费云路径零真机验收」；7 张卡没有一张改变这一点
  - 风险：需要维护者持 MinerU key；不引入第三方依赖（脚本用 stdlib）

- [ ] **X2 (P1 候选, human: ~3h / CC: ~20min) — agent 约定式摄取**（E2；CEO 子代理独立提出 + DX Pass 3）
  - 内容：注册库时返回「待解析清单 + 建议动作」，并在 `SKILL.md` 写死纪律（`kb_init` 后若见 PDF 提示，先问用户再 `kb_ingest`）
  - 理由：`server.py:645-649`/`687-698` 的 hint 与 `SKILL.md:57` 已存在基础设施；**零线程、零意外扣费、不违 `worker.py:1-4` 硬设计**——是 C58 的可行替代或补充
  - 风险：依赖 agent 遵守纪律，无强制力

- [ ] **X3 (P3 候选, human: ~2h / CC: ~15min) — 新建 `TODOS.md`**（ENG + DX）
  - 现状：仓库**无 `TODOS.md`**（也无 `CLAUDE.md`），所有延后项只能靠 changelog 承接
  - 内容：TTHW 压缩、`list_files()` 全量构造优化、`_save_state` 剪枝重构、多平台 watcher、DX 度量体系、社区与外部信号
  - 注意：**这是新增仓库根文件，需你显式同意**

### 不需要行动的检查项（已核验通过）

| 项 | 结论 |
|---|---|
| C53 修法正确性 | **正确**。`has_header('Content-type')` 是精确键匹配（`urllib/request.py`），传空串确实抑制 urllib 的 `application/x-www-form-urlencoded` 注入；`http.client` 会发出空值头，OSS V1 StringToSign 下空值与缺省等价 |
| 零第三方依赖铁律 | 遵守（`dependencies = []` 未动） |
| 版本一致性守卫 | 存在（`tests/test_version_sync.py:14-20`） |
| 编号无冲突 | `C53` 起未被占用（`C52` 为末位；`FIX-x` 并行线不冲突） |
| `docs/*` gitignore | 正确（`.gitignore:19` + 3 条白名单） |
| P2 的「副本文件」 | **伪命题**：`git ls-files docs/` 只有 3 个文件，副本不在仓库内 → 只需删本地文件（破坏性操作，需你单独确认） |
| C58 的 per-channel 硬闸门先例 | 存在（`mineru.py:25-26` 200MB/10MB，错误码 `-60005`/`-30001`） |
| 「0 = 关闭」邻域语义 | 存在（`batch_size<=0`、`max_age_days=0`、`watch_fallback_interval<=0`） |
| C58 的 `channel_for` 闸门位置论证 | **准确**（`worker.py:352-365` 的 `except MineruError` → pymupdf 降级链路） |
| `_ingest_hook` 是否破坏 Facade 冻结测试 | **不破坏**（冻结的 7 项在 `mortis_rag_mcp/__init__.py:6`，`test_facade_freeze.py` 只做 hasattr/集合断言） |

### 已过期的文档引用（顺手订正，不影响发版）

`server.py` 实际 **1313 行**；`docs/PROJECT_GUIDE.md:432` 写「约 1064 行」、`docs/Quick-start_developer.md:34` 写「1126 行」、`docs/Changelog_developer.md:320`（C49 记录「1359→1126」）均过期。计划引用的其他 40+ 处行号全部准确（详见 Phase 3 事实核验清单）。

> **⚠ 归因修正**：这三个数字是**文档过期**，不是「计划的失真」——`PLAN.md` 全文没有声称任何行数。原报告的「3 处规模性失真」表述已撤回，见下方第二轮审核。

---

# 第二轮：独立对抗审核（用户要求）

> 2026-09-29 · 用户裁定「起一个子代理独立审核方案」。审核方式：全新上下文的子代理，**把上述评审报告的全部断言当作需要证伪的主张**（而非事实），逐条回源码核验，按 5 维（完备性/一致性/清晰度/范围/可行性）评分。
> 主审随后亲自复核了它提出的 4 条最关键证伪——**4 条全部成立**。

## 审核裁决

```
审查对象：GSTACK REVIEW REPORT（第一轮）
可信度：中偏低（5/10）
理由：源码行号核验扎实、C53/C56/PDF 事件的硬事实基本对，但因果推断与严重度分级系统性夸大——
      把既有缺陷、未量化的假设、设计权衡一律升格为 CRITICAL GAP，至少 6 条断言可被源码直接证伪。
首轮质量分：5/10
```

## 被证伪的断言（主审已逐条回源码复核，全部确认审核方正确）

| # | 原报告断言 | 实际事实 | 影响 |
|---|---|---|---|
| F1 | 「`_init_cache_paths()` **无条件** mkdir 缓存根，与 `cache.enabled` 无关」 | `indexer.py:195` = `if self.config.cache.enabled and self.config.cache.dir:`，198-203 另有 `except OSError` 降级 | A5 判据重写；`[cache] enabled = false` 的文件（如 `test_search_filters.py:182`）本来就安全；「18153 个孤儿文件」是**文件**数，原报告把「建空目录」与「写文件」混为一谈 |
| F2 | 「C58 的设计正建立在这条链上」（指 `on_job_finished` 死链） | C58 的 hook 直连 `_ingest_manager_for().submit(None)`，与之无关；「解析完可检索」由各 `kb_*` 的 `try_sync_with_guard`（`server.py:965/972/1132` 等）保证 | A4 因果判断撤回；缺陷仍真实（既有 bug + `server.py:794-795` 的 hint 对用户说谎），但**降为既有缺陷 P2**，不借 C58 搭车 |
| F3 | 「`path_prefix` 逃出库 = High severity 新攻击面」 | `models.py:143-166` 是 `chunk.source.startswith(prefix)`，`source` 恒为库内相对 posix 路径 → 字符串比较**不可能**路径穿越，`../`/绝对路径只零命中 = 天生 fail-closed | A11 保留 DRY 价值，**撤回安全严重度**；Step 3 的「2 High severity」夸大，实际 0 |
| F4 | 「计划的 3 处规模性失真」中的两处 | ①「1126 行」来自文档而非计划（计划未声称行数）；② 计划 C54b 写的是「逐一排查**所有** stdio 用例」，两个文件名是 changelog 举例 → 非穷举声明 | 交叉主题 4 重写；12 个文件的清单保留为**可执行清单**（省去逐个排查），不再是「计划漏项」 |
| F5 | C58 的事件分类只影响「理由错但改动无害」 | 方向对，**但漏了 poll 路径**：`watch.py:46-58` 显示 poll（或非 Windows / native 启动失败）走 `_watch_loop`（第 57 行），不进 `_native_watch_loop` → 计划写的「poll 情况由 `_native_watch_loop` 30s 兜底补 ingest 扫描」**不成立，auto_watch 在 poll 下永不触发** | 见新增 T22（P1） |
| F6 | 「显式超限报错砍掉 pymupdf 兜底」是未申报的行为变更 | 链路属实，但这是**偏好取舍**，且与用户 D2「全路径统一 20」存在张力 | A1 保留（区分「用户自设 cap」与「channel 强加 cap」），但明确标注它与 D2 的张力需你知情 |

## 审核方挖出、原报告完全漏掉的 4 条（已复核成立）

| # | 新发现 | 证据 | 严重度 |
|---|---|---|---|
| **N1** | **poll 模式下 auto_watch 永不触发** | `watch.py:46-58`：poll / 非 Windows / native 启动失败 → `owner._watch_loop`（57 行），`_native_watch_loop`（51 行）不参与。计划 C58 明确把 poll 交给该循环的兜底节拍 | **P1（真 bug，计划自相矛盾）** |
| **N2** | **C55 的别名对模型与校验型客户端都不可见** | `server.py:192-195`：`kb_init_solo` 的 schema 只有 `path` 属性且 `required: ["path"]`。模型读的是 `inputSchema`，`vault_path` 不在 properties 里它就永远不会传；且计划要求「required 保持不动」与 C55 目标（消除参数名不一致）自相矛盾 | **P1（目标落空）** |
| **N3** | **D3 的 env 覆盖若落在目录改名逻辑之后，跑测试会搬走宿主真实数据目录** | `config.py:72-84` 的 `resolve_default_cache_dir()` 有 `os.rename` 副作用（旧 `~/.vault_mcp_cache` 独占存在时原子搬迁）。env 覆盖必须**短路在它之前**，否则比写孤儿文件严重得多 | **P1（数据搬迁风险）** |
| **N4** | **C57 的 `limit` 无上限会撞 payload 预算不变量** | `server.py:257`：`limit` 只有 `minimum: 1`、无 `maximum`；`budget_bytes` 是 `[500, 100000]`（`:259`）。C57 若照抄，大库一次可拉全量 | **P1（v0.8.0 不变量回退）** |

## 原报告的内部不一致（审核方指出，全部成立，已在本节上方逐条修正）

1. Section 2 自评「过度工程：无」，却同时要求新增「只读文本层入口 + 纯函数拆分」→ A9 已改为两行开关方案。
2. Failure Modes Registry 把「C58 的事件分类理由」标为 CRITICAL GAP，正文却说「改动无害」→ 理由错误本身不是 gap，**漏掉 poll 路径**才是（已拆分为 N1）。
3. Section 3 说 C54 的 session 级单文件「设计正确」，Registry 却给同一设计两条 CRITICAL GAP → 区分「设计方向正确」与「执行细节有坑」。
4. 「1/6 已确认」与共识表内两处 CONFIRMED 不符 → 已改为 2/6。
5. **既有缺陷与本计划新引入风险混在同一张表、同一套评级里** → 本次拆表（见下）。
6. A15 被指「自相矛盾」（既然空值与缺省等价，为何还锁头存在）→ 已澄清：锁的是**回归安全**，不是当前行为。

## 拆表：既有缺陷 vs 本计划新引入风险

> 这张区分决定了「该不该在本版修」的结论完全不同。原报告把两者混在一起并统一标 P1，是它最主要的方法论错误。

**A 类 — 既有缺陷（本计划之前就已存在）**

| 项 | 证据 | 原评级 | 修正后 | 是否本版修 |
|---|---|---|---|---|
| `on_job_finished` 参数不匹配，回调从未生效 | `worker.py:395` / `server.py:766` | P1 CRITICAL | **P2** | 是（用户 D4 已定「搭车修 P0」的同一精神；且它让 `server.py:794-795` 的 hint 说谎） |
| 测试往真实缓存根写文件 | `indexer.py:195` + `cache.enabled` 默认 true | P1 CRITICAL | **P2** | 是（用户 D3 已裁定） |
| `test_gate_9_retryable_mineru_error` flaky | `REPORT.md` P0 | P1 CRITICAL | **P2** | 是（用户 D4 已裁定搭车） |
| `_save_state` 超 500 条剪掉旧 failed | `worker.py:136-145` | P1 CRITICAL | **P3** | 否（记入延后项） |
| `list_files()` 先构造全量列表再切片 | `indexer.py:961-962` | 已知边界 | **P3** | 否 |
| `_fts_ensure_populated` 在构造期可能写盘 | `indexer.py:216` | 计入 C56 代价 | **P3** | 否（但 A9 的开关会顺带绕开） |
| 文档过期行号（1064/1126） | 三处文档 | 「计划失真」 | **P3** | 是（顺手，零风险） |

**B 类 — 本计划新引入的风险（真正该进 P1 的）**

| 项 | 来源 | 评级 |
|---|---|---|
| poll 模式下 auto_watch 永不触发（N1） | C58 | **P1** |
| C55 别名对模型不可见（N2） | C55 | **P1** |
| env 覆盖若位置不对会搬走宿主数据目录（N3） | D3 | **P1** |
| C57 `limit` 无上限撞预算不变量（N4） | C57 | **P1** |
| 自动路径若不复用 ignore 过滤 = 隐私边界扩大 | C58 | **P1** |
| hook 在防抖线程内同步全库扫描 → 饿死文本 sync | C58 | **P1** |
| 无 key 时 channel 强加 10MB 上限变硬失败（与 D2 有张力） | C58 | **P1** |
| `_run_job` 闸门可被恢复的 queued job 与 TOCTOU 绕过 | C58 | **P2** |
| C56 探测的全量构造代价（量级未实测） | C56 | **P2** |
| `auto_watch` 状态不可观测 | C58 | **P2** |
| 三处行为变更未写升级须知 | D2/C56/D3 | **P1** |
| PROJECT_GUIDE §四/§七 需新增（非修改）ingest 内容 | C58 | **P1** |

**结论**：A 类 7 项里只有 3 项建议本版修（且都因你的裁定 D3/D4 已在内）；**P1 的真正来源是 B 类的 12 项**，而原报告把注意力分给了 A 类的既有债。

## 第二轮新增任务

- [ ] **T22 (P1, human: ~3h / CC: ~20min) — `_indexer/watch.py` — poll 模式的 auto_watch 节拍（N1）**
  - 问题：`watch.py:46-58` 的 poll 分支走 `_watch_loop`，计划指定的 `_native_watch_loop` 兜底节拍不存在 → poll 下 auto_watch 永不触发
  - 修法：在 `_watch_loop` 里同样挂 ingest 节拍（复用 `watch_fallback_interval`），**或**在 poll 模式下明确不支持 auto_watch 并在 `doctor`/hint 里点明（fail-closed 的表态优于静默失效）
  - 文件：`mortis_rag_mcp/_indexer/watch.py`、`mortis_rag_mcp/config.py`、`mortis_rag_mcp/doctor.py`、`tests/test_watch_integration.py`
  - 验证：`pytest tests/test_watch_integration.py tests/test_fsnotify.py -q`

- [ ] **T23 (P1, human: ~1h / CC: ~10min) — `server.py` — C55 的 schema 也要补 `vault_path`（N2）**
  - 问题：`server.py:192-195` 的 `kb_init_solo` schema 只有 `path` 属性 + `required: ["path"]` → 模型看不到 `vault_path`，校验型客户端也会拒；原计划「required 保持不动」使 C55 目标落空
  - 修法：`kb_init`/`kb_init_solo` 的 schema 增加可选 `vault_path` 属性（additive，向后兼容），描述里写明两种写法等价；`required` 放宽为 `[]` 或保留 `path` 但在描述中指明 `vault_path` 亦可。**顺带评估 `kb_remove` 的同类 schema/handler 分歧**（`server.py:179-182`）是否一并对齐
  - 文件：`mortis_rag_mcp/server.py`、`tests/test_scoped_search.py`、`skills/mortis-rag-mcp/SKILL.md`
  - 验证：`pytest tests/test_scoped_search.py tests/test_solo_vault.py -q` + `tools/list` 的 schema 体积复测（v0.8.0 有 ≤10% 门槛）

- [ ] **T24 (P1, human: ~1h / CC: ~10min) — `config.py` — env 覆盖必须短路在目录改名之前（N3）**
  - 问题：`config.py:72-84` 的 `resolve_default_cache_dir()` 有 `os.rename` 副作用；env 覆盖若在其后应用，跑测试会把宿主 `~/.vault_mcp_cache` 搬成新名
  - 修法：env 存在时**直接返回，不进入改名分支**（在函数最前短路）；补一条测试断言「env 存在时不会调用 `os.rename`」
  - 文件：`mortis_rag_mcp/config.py`、`tests/test_isolation_guard.py`
  - 验证：`pytest tests/test_isolation_guard.py tests/test_path_migration.py -q`

- [ ] **T25 (P1, human: ~1h / CC: ~10min) — `server.py` — C57 的 `limit` 加上限（N4）**
  - 问题：`kb_search` 的 `limit` 只有 `minimum: 1` 无 `maximum`；C57 若照抄，大库一次拉全量 → 撞 v0.8.0 的 payload 预算不变量
  - 修法：`kb_list_files.limit` 走与 `top_k` 同款夹取（`max_top_k`）或显式设 `maximum`；并写清 `limit` 与 `budget_bytes` 的关系
  - 文件：`mortis_rag_mcp/server.py`、`tests/test_mcp_stdio.py`
  - 验证：`pytest tests/test_mcp_stdio.py tests/test_preview_mode.py -q`

## 第二轮修正后的完成小结

```
  +====================================================================+
  |     SECOND-ROUND ADVERSARIAL REVIEW — CORRECTED SUMMARY            |
  +====================================================================+
  | 首轮质量分（对抗审核方给） | 5/10（可信度中偏低）                     |
  | 被证伪的断言              | 6 条（F1-F6），主审已逐条回源码复核        |
  | 审核方新挖出的问题         | 4 条（N1-N4），全部成立且均为 P1           |
  | 内部不一致                | 6 处，已在本报告内逐条修正                 |
  | 评级重构                  | 既有缺陷 7 项降级为 P2/P3；P1 来源改为     |
  |                          | 「本计划新引入风险」12 项                   |
  | 修正后自动裁决数           | 23 项（原 20 + A19/A20/A21）              |
  | 修正后任务数              | 25 项（原 21 + T22/T23/T24/T25）          |
  | 修正后 P1 数              | 12（B 类）+ 3（A 类中建议本版修者）= 15     |
  +====================================================================+
```

**这一轮的元教训（值得写进后续评审的方法论）**：单声部评审（Codex 缺席）时，主审的严重度分级会系统性漂移——把「既有缺陷」当「本计划引入的风险」、把「无法穿越的字符串比较」当「安全漏洞」、把「未实测的估计」当「High 性能问题」。**用户主动要求「起一个子代理独立审核方案」是本次的关键动作**：如果没有它，上述 6 条错误断言会原样进入施工图。

---

# 第三轮：对抗审核的对抗审核（用户要求）

> 2026-09-29 · 用户裁定「再打一轮对抗审核，专门去证伪第二轮（含 N1-N4），并审 E1/E2 本身值不值得做」。
> 方式：第三个全新上下文的子代理，明确要求「不要因为第二轮推翻了第一轮就认为第二轮更可信」。主审随后复核了它最关键的两条硬事实——**两条均成立**。

## 三轮可信度裁决

| 轮次 | 可信度 | 评价 |
|---|---|---|
| 第一轮（主审 + 三子代理） | **中** | F1/F3/F4/F6 的原始判断确有夸大与归因错误；但范围清单（NOT-in-scope、延后项）与对 C58 风险面的覆盖**更完整** |
| 第二轮（对抗审） | **中偏低** | F1-F5 的证伪正确；但「N1-N4 全部成立且均 P1」被本轮**证伪 3 条**（N2 严重度错、N3 定性错、N4 全错），降级也有过度处 |
| 综合 | — | **第二轮方向更接近真相**（拆「既有缺陷 vs 新风险」的方法论正确、F 系列基本可信），**但其 N 系列的评级理由不可信**；**第一轮在「本版该做什么」上更可靠** |

## N1-N4 终裁（第三轮证伪第二轮）

| # | 终裁 | 证据与理由 | 终评级 |
|---|---|---|---|
| **N1** | **成立，且比第二轮说的更严重** | `watch.py:46-59` 成立。**关键补充**：`fsnotify.py:218-242` 非 win32 恒 False + `config.py:210` 默认 `auto` ⇒ **Linux/macOS 是 100% poll，属常态而非极端回退**；Windows 上才是异常路径。另：`_fs_scheduler_loop`（`:71-93`）在 poll 下也会启动，但 `_fs_requested` 只由原生事件设置（`:112-117`）→ poll 下永假 | **P1 维持**（范围扩大到「非 Windows 平台 auto_watch 完全不可用」） |
| **N2** | **部分成立，降级** | schema 事实成立（`server.py:170-174`/`192-195`），且**无任何测试用别名**（正则扫 tests 零命中）、`PROJECT_GUIDE.md:496-500` 也只写 `path`、`kb_remove` 的四别名（`:709-716`）同样零测试 → 证据支持「既存口径不一致、从没人用过」。但「做了等于没做」不成立：MCP 客户端通常不拒收 properties 之外的参数（未禁 `additionalProperties`），handler 别名对「模型受其余 15 个工具的 `vault_path` 习惯影响而误传」**是有效的**——那正是 issue #3 的痛点。且 `PROJECT_GUIDE.md:1099` 有 ≤10% schema 体积门禁 → 不改 schema 属保守取舍 | **P2**（降级）；真正该补的是 **description 与报错文案**（`server.py:615`/`663`）——这一点**三轮都没提** |
| **N3** | **部分成立，定性错误** | `config.py:77` 的 rename 条件苛刻（new 不存在且 old 存在）；**关键**：`config.py:437` 显示 `resolve_default_cache_dir()` **今天已在每次 `load_config()` 生效** → 这不是 D3 新引入的风险，而是**既有行为**；且 rename 是 v0.7.1 设计内的原子迁移（`CHANGELOG_user.md:70,86-87`），**不是数据销毁**。`tests/conftest.py:26-28` 那条注释针对的是 **registry 目录**，不是 cache 目录 | **P3**（降级）；短路的做法仍值得做（严格更优），但不阻塞发版 |
| **N4** | **不成立** | ① `budget_bytes` 是 `kb_search` 的专属可选参数（`server.py:259`；`fanout.py:43-44` 仅非 None 时生效），`kb_list_files` **没有**这条通道（`server.py:238-241`/`963-966`）；② `kb_search.limit` **实际已被夹取**——`server.py:92-107` 的 `_search_filter` 有 `limit = min(limit, max_limit)`（**主审已亲自复核，确认属实**）；③ `list_files()` 返回摘要 `{source,title,chunks}`（`indexer.py:962`），与返回全文 chunk 的 kb_search 差几个数量级；④ C57 默认全量 = 现状（`PLAN.md` C57 卡原文） | **P3**（降级），且**理由错误**：「撞不变量」是把现状说成回归 |

## F1-F6 复核（第三轮的判断）

F1 成立（`indexer.py:195`/`198-203`）· F2 成立**但「hint 说谎」夸大**——`server.py:794-795` 的产物确实落盘，各 `kb_*` 自带 `try_sync_with_guard`（`:965/972/1132`），所以是**时延问题而非说谎**（措辞已在 A 类表修正）· F3 成立（`models.py:148-166`）· F4 基本成立 · F5 = N1，成立 · F6 部分成立（「未申报行为变更」改判为偏好取舍成立，但与 D2 的张力真实）。

## 过度修正检查

- 大部分降级合理：`_save_state`（C58 用 `_auto_pending` 绕开）、`list_files` 全量构造（已在 NOT-in-scope）、`_fts_ensure_populated`（A9 顺带绕开）——维持 P3。
- **降过头的一项：`on_job_finished`**。第二轮降 P2 并「留作 TODO」是过头的：修法 1 行、与用户 D4「P0 搭车」的精神一致，且它的缺席使「解析完立即可检索」退化为「下次调用才同步」。→ **改为：进本版（P2，不升 P1）**。
- 用户问题 17 的答案：4 项未决里「文档过期行号」已在 T7 覆盖；其余 3 项维持不进本版正确。

## E1 / E2 终裁（第三轮独立评估）

**E1 —— 降级做（不进 CI）**
- `ci.yml` 只有**单 job**（19-67）、**零 secret 注入**、无双跑 → 真机 smoke 需要付费 MinerU key，**不适合进 CI**（每次运行烧额度，且无 secret 通道）。
- 「清单条目」**有载体**，不是自我安慰：`PROJECT_GUIDE.md:764-769` 的发布 4 步 + `docs/Changelog_developer.md` 的「验证：」记账先例（C13/C14 都有）。
- 落地形态：**C53 与 C60 卡各加一个发版必填字段「实机验证 / 或显式声明未验证」**；可选 `scripts/smoke_mineru.py`；`ci.yml:10` 已声明 `workflow_dispatch`，需要时可挂一个**手动触发**的真机 job。
- 观察点：**不加 CI 自动 job**——无 secret 通道且烧额度。

**E2 —— 降级并入 C58（不单列卡）**
- hint **已存在且被测试锁定**：`server.py:638-649`/`687-698`；`tests/test_ingest_server.py:65,83,99` 断言 `ingestible_docs`。`SKILL.md` 关于摄取只有 `:49`、`:57` 两句。C58 的文档项**已含** SKILL.md PDF 章节。
- E2 的独特价值只剩「`auto_watch` 默认关时的默认路径」；**增量 ≈ hint 带上 pending 清单 + 一句纪律（S）**。
- 判定依据：**纪律不可测、机制可测** → 作为 C58 子项，不单列卡。
- → 原 X2/E2 任务从扩张候选**降为 C58 内的一条子项**。

## 两轮共同盲点（第三轮挖出，主审已复核 B3）

| # | 盲点 | 证据 | 严重度 |
|---|---|---|---|
| **B1** | **同名参数、相反缺省**：`kb_list_files.limit` 缺省=全量，而 `kb_search.limit` 缺省=top_k | `server.py:257` vs C57 卡原文「不传参数时行为与现在完全一致」 | **P1**（模型必然误判） |
| **B2** | **`truncated` 一词两个语义**：C57 新增的 `truncated` 是「被分页截断」，而 v0.8.0 的 `apply_budget.truncated` 是「字节预算截断」 | `fanout.py:50-58` | **P1**（消费方会误读） |
| **B3** | **C60 会漏掉日志版本号**：`diaglog.py:195` 的 `version: str = "0.8.0"` **与** `:226` 的 `str(version or "0.8.0")` 两处硬编码 | **主审已亲自复核，两处均确认存在**；另有 `tests/test_diaglog.py:120` | **P1**（版本收口不完整） |
| **B4** | **C56 的 A9 只解决向量部分**：每个未加载库的 **chunks 缓存**仍会全量加载 | `indexer.py:249-282` 的 `_init_cache_paths` 无条件调 `_load_chunks_cache()`；`load_vectors=False` 开关覆盖不到 | **P2** |

第三轮自陈的两项不确定（如实保留）：
1. 主流 MCP 客户端对 `inputSchema` 之外参数的实际校验行为——**仓库内无证据可证**，其判断基于「JSON Schema 默认放行」。
2. C56 的临时 `MarkdownIndexer` 是否会泄漏 `FtsIndex` 的 sqlite 连接/句柄——未读完 `_init_cache_paths` 与 FtsIndex 的完整生命周期，`load_vectors=False` 只覆盖向量部分（与 B4 同源）。

## 第三轮修正后的任务调整

| 任务 | 原评级 | 终评级 | 变化 |
|---|---|---|---|
| T22（poll 模式 auto_watch） | P1 | **P1** | 保留，**范围扩大到「非 Windows 平台 auto_watch 完全不可用」** |
| T23（C55 schema） | P1 | **P2** | 改写：**不改 schema**，改为补 `description` 与 `server.py:615`/`663` 的报错文案（提及 `vault_path` 亦可）；依据 `PROJECT_GUIDE.md:1099` 的 schema 体积门禁 |
| T24（缓存根 env 短路位置） | P1 | **P3** | 降级：`resolve_default_cache_dir()` 今天已在每次 `load_config` 生效，rename 是 v0.7.1 设计内迁移而非数据销毁 |
| T25（C57 limit 上限） | P1 | **P3** | 降级且理由重写：`limit` 实际已被 `_search_filter` 夹取（`server.py:98`）；禁 `kb_search.limit` 无 max 属口径问题而非风险 |
| **T26（新）** | — | **P1** | **`diaglog.py:195` + `:226` 的硬编码版本号纳入 C60 收口清单**（含 `tests/test_diaglog.py:120`） |
| **T27（新）** | — | **P1** | **C57 的 `limit` 缺省语义与 `truncated` 语义去重**：要么让 `kb_list_files.limit` 缺省对齐 `kb_search`（取 top_k 风格默认），要么在 schema 描述里显式写明「缺省返回全量」；同时把 `truncated` 换成不与字节截断撞名的键（如 `page_truncated`），或在描述里明确两者区别 |
| **T28（新）** | — | **P2** | **C56 的探测代价需覆盖 chunks 部分**：`load_vectors=False` 之外，评估「只读 chunks 层」或进程级 chunk_id 索引（A9 的 B4 补充） |

**第三轮元教训（与方法论有关，值得留下）**：**「对抗审核」本身也会过度修正**。第二轮的 4 条新发现里有 3 条的严重度或定性是错的——它把「既有行为」当「新风险」（N3）、把「已被夹取」当「无上限」（N4）、把「保守取舍」当「目标落空」（N2）。三轮下来最稳的结论是：**F 系列（证伪第一轮）基本可信，N 系列（新发现）需要逐条回源码复核严重度**。→ 若还有第四轮，应该审「第二轮的 N 系列评级」而不是再审事实。

---

# 最终状态：APPROVED + 施工图

> 2026-09-29 · 状态：**APPROVED**（用户裁定 A：先写回卡片正文，再批）
> 本文件现由三部分组成：① 原始计划（已按三轮评审改写卡片正文）② 三轮评审报告（审计链）③ 本节（施工图收口）
> 改动范围：11 张卡（C53-C64，其中 C61-C64 为本轮新增）+ 4 份文档 + 2 份 CHANGELOG + `.gitignore` 白名单

## 最终优先级拆表

原报告最大的方法论错误是把「既有缺陷」与「本计划新引入的风险」混在一张表、统一标 P1。拆开如下。

### B 类 — 本计划新引入的风险（**真正的 P1，阻塞发版**）

| # | 项 | 卡 | 证据 |
|---|---|---|---|
| 1 | poll / 非 Windows 下 auto_watch 完全不可用 | C61（C58） | `watch.py:46-59` + `fsnotify.py:218-242` + `config.py:210` |
| 2 | hook 在防抖线程内同步全库扫描 → 饿死文本 sync | C58 | `watch.py:71-93` |
| 3 | 自动路径不复用 ignore 过滤 = 隐私边界扩大 | C58 | `worker.py:48` 有现成清单 |
| 4 | 无 key 时 channel 强加 10MB 上限变硬失败（与 D2 有张力） | C58（A1） | `mineru.py:118-122` + `worker.py:352-365` |
| 5 | `_run_job` 闸门可被恢复的 queued job 与 TOCTOU 绕过 | C58（A13） | `worker.py:300` |
| 6 | `kb_list_files.limit` 与 `kb_search.limit` 同名相反缺省 | C62（C57） | `server.py:257` vs C57 承诺 |
| 7 | `truncated` 一词两个语义（分页 vs 字节预算） | C62（C57） | `fanout.py:50-58` |
| 8 | `diaglog.py` 两处硬编码版本号，收口会漏 | C63（C60） | `diaglog.py:195`/`:226`（**主审已复核**）+ `test_diaglog.py:120` |
| 9 | 三处行为变更未写升级须知 | C60（T8） | `test_version_sync.py:18-20` 只强制「有条目」 |
| 10 | PROJECT_GUIDE §四/§七 需**新增** ingest 内容（非修改） | C58（A6） | §四 子节 4.1→4.10 无 ingest；§七 子节无 ingest |
| 11 | 真机 OSS 200 无任何验收手段 | C53/C60（E1） | `ci.yml` 单 job、零 secret |
| 12 | C55 别名不可发现（模型读 schema） | C55（N2） | `server.py:192-195` |

### A 类 — 既有缺陷（本版之前就存在；3 项因 D3/D4 已在内）

| # | 项 | 终评级 | 是否本版修 | 说明 |
|---|---|---|---|---|
| 1 | `on_job_finished` 参数不匹配（回调从未生效） | **P2** | **是** | 1 行修法；与 D4「P0 搭车」同精神；修后 `server.py:794-795` 的 hint 才为真 |
| 2 | 测试往真实缓存根写文件 | **P2** | **是**（D3） | 真实机制见 C54b |
| 3 | `test_gate_9_retryable_mineru_error` flaky | **P2** | **是**（D4） | 带超时轮询 |
| 4 | `_save_state` 超 500 条剪掉旧 failed | P3 | 否 | C58 用 `_auto_pending()` 按 `submitted_at` 绕开 |
| 5 | `list_files()` 全量构造后才切片 | P3 | 否 | 已在 NOT-in-scope |
| 6 | `_fts_ensure_populated` 构造期可能写盘 | P3 | 否 | C56 的开关顺带绕开 |
| 7 | 文档过期行号（1064/1126） | P3 | **是**（随手） | `server.py` 实际 1313 行 |
| 8 | C54 的 env 覆盖若位置不对会触发目录改名 | **P3** | 是 | `resolve_default_cache_dir()` 今天已在每次 `load_config` 生效；rename 是 v0.7.1 设计内迁移，非数据销毁 |
| 9 | `kb_list_files.limit` 无 schema 上限 | **P3** | 否 | handler 侧已有夹取口径；加上限反而改默认行为 |

## 开工顺序（冲突已标注）

```
Lane E（最先，独占 docs/）      P1 转义清理 + 过期行号订正 + 仓库地图/白名单修齐（X3）
                                 ↓ 必须完成后再动 PROJECT_GUIDE
Lane A（可并行）                C53 → C63（diagnlog 版本）
Lane B（可并行，独占 tests/）    C54（a/a'/b/c）+ P0 flaky + 新增 tests/test_isolation_guard.py
Lane C（server.py 顺序）        C55 → C56 → C57(+C62) → C64
Lane D（依赖 A/B 与 A4 修复）    A4 修回调链 → C58（含 C61）→ C59
最后                            T8 文档同步（依赖 E 与 C58）→ C60 收口
```

**进度**（2026-09-29）：**Lane E 已完成** —— 分支 `feat/v0.8.1`，三笔提交：`e5eb33d`（FIX-9 转义清理）、
`96e205f`（FIX-10 行号+仓库地图/白名单+新建 Execution-plan）、`eaceabf`（记账）。下一步 Lane A（C53 → C63）。

**冲突提示**：Lane C 与 Lane D 都改 `server.py` → 顺序执行；Lane B 与 Lane D 都改 `tests/` → 顺序执行；
**Lane E 必须先于 C58 的 §四/§七 新增**（否则机械 diff 与语义编辑互相遮蔽）。

## 25 项任务聚合（跨三轮评审）

**P1（15 项，阻塞发版）**
- 云端验收发版必填字段（C53/C60，E1 降级形态）· agent 约定式摄取并入 C58（E2 降级）
- poll 路径 ingest 节拍（C61）· C58 的体积闸门四条修正（A1/A8/A13/A14）
- `kb_list_files` 缺省语义 + `truncated` 改名（C62）· diaglog 版本去硬编码（C63）
- C54 三件套 + 缓存根 env 覆盖 + 全量清单（C54a/a'/b）· 修 `on_job_finished` 死链 + 回归测试（A4）
- 三处行为变更的升级须知（T8）· PROJECT_GUIDE 新增 ingest 内容（A6）· C56 探测改两行开关 + 错误路径 + 可观测性
- C57 的 `total`/`next_offset`/`prefix` 复用 · C55 的 description + 报错文案 · P1 转义清理（前置 commit）· P0 flaky

**P2（6 项）** · **P3（4 项）** —— 明细见 `~/.gstack/projects/moton16-Mortis-RAG-MCP/tasks-*.jsonl`

## 产物清单

| 产物 | 位置 |
|---|---|
| 计划 + 三轮评审 + 施工图 | 本文件（`docs/v0.8.1/PLAN.md`，约 2200 行） |
| 还原点（原始计划逐字副本） | `~/.gstack/projects/moton16-Mortis-RAG-MCP/main-autoplan-restore-20260929-080941.md` |
| 测试计划（供 `/qa` 消费） | 同目录 `14166-main-eng-review-test-plan-20260929.md` |
| 任务聚合（JSONL） | 同目录 `tasks-ceo-review/eng-review/devex-review-*.jsonl` |
| 评审日志（供 `/ship` 仪表盘） | 同目录 `main-reviews.jsonl`（6 条） |

**下一步建议**：`/ship` 创建 PR；或直接按「开工顺序」从 Lane E 开始动手。
