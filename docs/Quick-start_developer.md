# Quick-start：开发者上手指南

> 面向第一次接触本仓库的开发者（人类或 agent）。**只读这一篇就够上手**：
> 项目概况 → 架构 → 代码逻辑 → 各模块职责 → 开发约定 → 常见任务食谱。
> 需要溯源某次具体改动时才去翻 `Changelog_developer.md`；要写新功能先看
> 是否已有排期（`PROJECT_GUIDE.md` §15 与 issue / PR 记录）。

---

## 1. 30 秒版：这是什么

Mortis'RAG MCP 是一个**本地 Markdown 知识库 RAG 服务器**，通过 MCP 协议（stdio +
换行分隔 JSON-RPC，协议版本 `2025-06-18`）向 AI agent 提供 16 个 `kb_*` 工具：
注册任意文件夹为知识库 → 后台增量索引（切块 + embedding + FTS）→ 三路混合检索
（FTS5 BM25 + 向量余弦 + bigram 词法，RRF 融合）→ rerank → 返回结构化 chunks。

**核心设计红线**（改动前先默念三遍）：

1. **零第三方运行时依赖**：`pyproject.toml` 里 `dependencies = []`。HTTP 用 `urllib`，
   TOML 用 `tomllib`（3.10 有内置 fallback 解析器），向量存储自己写二进制编解码。
   可选 accel/vec 支持加速与磁盘向量；docs/media 支持格式解析与预览，非核心必需。
2. **不绑定任何路径**：知识库关系存用户级注册表 `~/.mortis_rag_mcp/vaults.toml`（兼容旧名 `~/.vault_mcp/vaults.toml`），仓库零个人配置。
3. **按实际能力降级**：FTS/reranker 等失败可降级，但配置无效、版本不符及媒体不可用应返回可见错误；不承诺所有路径永不报错。
4. **注释解释"为什么"**：代码里大量注释记录的是"曾经踩过的坑"，删注释等于拆地雷标识。

### 1.1 0.9 候选开发入口（2026-10-08）

以下地图的旧行数/测试计数仅是历史概览，不能用作施工定位。当前统一状态见
`docs/v0.9.0/beta2/UNIFIED_EXECUTION_PLAN_2026-10-08.md`（被忽略，交接需另行复制）。

- 配置→文本 templates/profile→indexer sync→单库/fanout query；媒体由独立 profile/provider 提供，native ID 含媒体 fingerprint，旧 pending 向量不覆盖新计算结果。
- `DocumentStore` 维护 generation/revision/control/job/checkpoint/blob/occurrence；
  import 最终 busy/CAS 与发布同用现有 OS mutation lock；固定 revision read/export pin 在返回前再校验。
- `retry` 排队后唤醒 worker；cancelled 释放媒体保护后重试清段 checkpoint，failed 保有效段，unknown 不自动重新提交。
- virtual 文档源 SHA/render SHA 也写到媒体 proxy/native；occurrences 逐页遍历固定 revision。只有成功生成 native 的 occurrence 停用 proxy 文本向量，不支持的 MIME/模态保留 proxy。
- **已剔除**：ffmpeg 音频解码 / Whisper 转录链路在 v0.9.0（E17）物理清除——`ingest/audio.py`、`ingest/transcription.py`、`AudioConfig`/`[audio]` 段、worker 音频分卷与转录 subjobs 调度及专项测试一并移除；音频只保留原生 embedding transport 与 occurrence 展示出口。
- **仍未完成**：真实媒体 transport 端到端、独立图片摄取、真实检索质量与宿主 Media 验收；不以配置声明或 PNG 冒音频 fixture 证明生产功能。
- `scripts/eval_quality.py` 读取已有成对排名，计算 Recall/NDCG@10/固定 seed bootstrap；
  示例只有两问，缺 ≥100 问/七类/语料 SHA/引用定位与跨库检查时不是最终 PASS。
  `scripts/eval_resources.py` 测合成 SQLite/客户端规模、延迟与 Python 分配峰值，不称 RSS。
- CI 核心仍零运行时依赖；新增 Linux/Windows docs/media/vec **必需正向** lane，
  安装/导入失败应红。本地 vec 缺包时 skip 是缺证据，不是磁盘后端通过。

验证时在仓库 `.runtime/任务ID/GUID` 创建父目录，独立 TEMP/TMP/TMPDIR、pytest basetemp/cache；
中途仅相关单文件，全量在整合末或 CI。不要写默认 `%TEMP%`，不要共用 basetemp。
本地完成检查后只 `git add -- 明确路径` 并及时 commit，保留 hooks，不自动远端操作。

## 2. 仓库地图

```
Mortis-RAG-MCP/
├── mortis_rag_mcp/          # 包本体（~10850 行，v0.8.0 模块化拆分架构）
│   ├── __main__.py          # 入口：python -m mortis_rag_mcp --serve-mcp-stdio
│   ├── config.py            # 配置加载（526 行）
│   ├── registry.py          # 用户级知识库注册表（384 行）
│   ├── server.py            # MCP 协议层 + 16 个公开工具路由表与轻量入口
│   ├── _server/             # 服务端路由与跨库编排私有包
│   │   ├── search_dispatch.py # 单库/Scoped/全局检索路由与入参规范化
│   │   └── fanout.py          # 跨库候选聚合、权重计算、去重、rerank、全局/分组分页
│   ├── indexer.py           # MarkdownIndexer Facade、向后兼容 re-export 与生命周期（1253 行）
│   ├── _indexer/            # 索引器核心实现私有包
│   │   ├── models.py        # Chunk、SearchFilter 数据模型与纯去重逻辑
│   │   ├── cache_codec.py   # _CacheCodec、_VectorsCodec 二进制编解码持久化
│   │   ├── chunking.py      # 切块算法、正则与表格保护
│   │   ├── scanning.py      # 目录遍历、IgnoreMatcher 规则匹配与 Fast-Stat 对账判据
│   │   ├── sync_engine.py   # run_sync 增量同步引擎与并发 embedding 处理
│   │   ├── search.py        # SearchEngine 单库只读检索（FTS5/向量/三路 RRF/rerank）
│   │   ├── snapshot.py      # 快照打包导出、校验导入恢复与 Zip Slip 安全防护
│   │   ├── exemptions.py    # 豁免规则维护与八项状态级联清理
│   │   └── watch.py         # 文件系统 watcher 监听与防抖生命周期调度
│   ├── ingest/              # PDF/Office 异步摄取与表格处理（worker, mineru, tables）
│   ├── providers.py         # embedding / reranker HTTP 封装（230 行）
│   ├── fts.py               # FTS5 SQLite 封装（152 行）
│   ├── vector.py            # 向量后端：memory / sqlite_vec（367 行）
│   └── fsnotify.py          # Windows ReadDirectoryChangesW 原生监听（557 行）
├── config/app.toml.example  # 配置模板（app.toml 本体被 gitignore）
├── skills/mortis-rag-mcp/   # 配套 agent skill（教 AI 怎么用这套工具）
├── tests/                   # pytest，49 个测试文件 / 416 个用例
├── docs/
│   ├── Quick-start_developer.md       # 本文件
│   ├── Changelog_developer.md         # 每次 commit 的技术变更流水
│   ├── PROJECT_GUIDE.md               # 全系统架构指南（代码级现状全貌）
│   └── Docs_Folder-descriptions.md    # docs/ 目录保留口径说明（本目录的元文档）
├── QUICKSTART_user.md       # 用户向：初次部署指南
├── CHANGELOG_user.md        # 用户向：release 版本变更（无技术细节）
└── README.md (中文主页) / README_EN.md (English)
```

## 3. 架构分层

```
AI agent (WorkBuddy/Codex/...)
   │  stdio, newline-delimited JSON-RPC
   ▼
┌─────────────────────────────────────────────┐
│ server.py  VaultMcpServer                    │  协议层：initialize / tools/list /
│  - _tool_definitions()  工具 schema          │  tools/call 分发、参数归一化、
│  - call_tool()          16 个工具入口        │  跨库 fan-out 合并、库级权重
│  - _fanout_search()     跨库检索             │
└──────┬───────────────────┬──────────────────┘
       │                   │
       ▼                   ▼
┌──────────────┐   ┌──────────────────────────────┐
│ registry.py  │   │ indexer.py  MarkdownIndexer   │  每库一个实例
│ VaultRegistry│   │  - sync()     增量索引         │
│ vaults.toml  │   │  - search()   三路混合检索     │
│ (跨进程锁)   │   │  - read()/stats()/exempt...    │
└──────────────┘   └──┬───┬───┬───┬──────────────┘
                      │   │   │   │
        ┌─────────────┘   │   │   └──────────────┐
        ▼                 ▼   ▼                  ▼
   ┌─────────┐     ┌──────────┐ ┌─────────┐  ┌──────────┐
   │fts.py   │     │vector.py │ │磁盘缓存  │  │fsnotify  │
   │FtsIndex │     │Memory/   │ │_Cache-  │  │Windows-  │
   │(BM25)   │     │SqliteVec │ │Codec    │  │Directory │
   └─────────┘     └──────────┘ │_Vectors-│  │Watcher   │
                                 │Codec    │  └──────────┘
        ┌────────────────────────└─────────┘
        ▼
   providers.py：embedding / reranker 外部 API（urllib + 重试退避）
```

**进程模型**：单进程 stdio 服务，由 MCP 客户端拉起。索引建库、watcher、防抖调度
全部跑在后台 daemon 线程里，主线程只读 stdin 分发请求。

## 4. 核心数据流

### 4.1 索引管线（`MarkdownIndexer.sync()` → `_sync_locked()`）

```
扫描 .md（rglob + IgnoreMatcher 排除 .obsidian/.git/.trash/node_modules）
  → Fast-Stat 对账：先比 (st_mtime_ns, st_size)，未变文件直接跳过（零磁盘读取）
  → 变了才读内容算 sha256，再比签名，仍同 → 跳过
  → _chunk_file()：
      frontmatter 解析（tags/properties，rag:false 豁免）
      → 代码块围栏跟踪（``` 与 ~~~ 内的 # 不当标题）
      → 按标题层级切块 + chunk_overlap 重叠（_overlap_tail）
      → （可选 inject_image_captions）图片 alt/图注注入
  → 磁盘缓存比对（_CacheCodec，meta 含 chunk_size/overlap/table_guard 等代际键，
    参数变了自动失效重建文本层；向量按 chunk.id + content_hash 复用，零重嵌）
  → _embed_missing()：只补缺向量的 chunk（content_hash 相同的全库复用向量）
      external 模式：batch_size 切批 + 重试/指数退避（429 尊重 Retry-After）
  → FTS 落盘（fts/vault_<key>.fts.sqlite，trigram 分词）
  → failed_files 落盘（vault_<key>.failed.json，原子写）
```

**索引健康看 `failed_files`**（kb_stats），不是看 files/chunks 数。
**修少量失败文件不要 rebuild**：每次调任何 `kb_*` 工具都会触发增量 sync，
反复调 `kb_stats` 就能一轮轮补齐。`kb_rebuild` = 全量重嵌，只在换模型/维度时用。

### 4.2 检索管线（`MarkdownIndexer.search()`）

```
query → _query_tokens（中文 bigram 分词）
  → 三路召回，每路 top-40：
      ① FTS5 BM25（fts.py，path_prefix 下推 SQL 减候选）
      ② 向量余弦（vector.py；numpy 批量或标量回退）
      ③ bigram 词法（_query_tokens + 单词边界 \b 提权，兜短词/缩写）
  → RRF 融合（k=60）→ SearchFilter 后过滤（path_prefix/tags/mtime，rerank 前）
  → dedupe_by_content_hash（相同正文只留第一）
  → rerank top-60（providers.py，bge-reranker；挂了静默跳过）
  → top_k 切片（offset/limit 分页）
```

**跨库 fan-out（`server._fanout_search`）**：不传 `vault_path` 时触发——
查询只 embed 一次 → 逐库 search → 合并按 `score × 库 weight` 全局排序 →
跨库再去重 → 整体 rerank 一次 → 权重乘回。solo 库永远跳过并列进 `excluded_solo`。

### 4.3 一次 tools/call 的旅程

```
stdin 一行 JSON → handle() → method=="tools/call"
  → call_tool(name, arguments)      # arguments 畸形时归一化为 {}
  → 各 _kb_* handler：
      路径类参数 → _resolve_vault_path()（规范化绝对路径，防相对路径歧义）
      → _indexer_for() 取/建该库的 MarkdownIndexer（含缓存目录初始化）
      → indexer.sync() 先增量对账，再执行实际操作
  → _text_content() 包成 MCP content 返回
```

## 5. 各模块职责与改动注意

| 模块 | 职责 | 关键入口 | 改动时的坑 |
|---|---|---|---|
| `config.py` | TOML 加载、`${ENV_VAR}` 插值、配置链 `--app-config` > `MORTIS_RAG_CONFIG` > `VAULT_MCP_CONFIG` > `~/.mortis_rag_mcp/config.toml` > `~/.vault_mcp/config.toml` > 默认 | `load_config()` | 新配置项必须给默认值 + example 文件同步加注释；`AppConfig.vault_path` 会被 `__post_init__` 特殊处理 |
| `registry.py` | vaults.toml 读写（**原子写 tmp+replace**，跨进程排他锁 Windows `msvcrt.locking`/POSIX `fcntl.flock`） | `VaultRegistry.add/remove/set_weight/set_solo` | 字符串字段序列化必须 `json.dumps`（曾有 LLM 传入的引号毁掉整个注册表的 bug）；新字段要在 `load()` 里给老文件回退值 |
| `doctor.py` | 环境自检与 Agent 信任锚（`STATUS.md` / `status.json`）生成器 | `doctor.run()` | 零第三方依赖，探活复用 `providers.py`；核心项门禁防假 VALID；Windows 冲突 4 次退避原子写 |
| `server.py` | 协议层 + 15 个公开工具路由表与轻量入口 | `call_tool()` / `main()` | 15 个工具显式映射表路由；所有入参统一走 `_normalize_call_arguments()` 归一化；检索分发委托至 `_server/` |
| `_server/` | 服务端跨库编排与检索分发私有包 | `dispatch_search()` / `fanout_search()` | 活实例契约（直读 `_indexers`，禁快照化）；跨库 embedding 全局仅计算一次；库级权重在 rerank 完成后乘回 |
| `indexer.py` | MarkdownIndexer Facade 稳定入口、声明周期入口与向后兼容 re-export | `sync()` / `search()` | 保留向后兼容薄委托与历史测试打桩点；`__init__.__all__` 严格维持 7 项；锁获取时序固定为 `_sync_lock -> _cache_lock` |
| `_indexer/` | 索引与检索引擎核心实现私有包 | 各子模块独立导出 | 仅自底向上依赖，严禁运行时反向导入 Facade；数据模型 `Chunk` 字段顺序与 `VMCPC/VMCPV` 二进制协议严格锁定；常驻 Chunk 禁止原地修改 score |
| `providers.py` | embedding/reranker HTTP（重试、退避、batch 切分、static 兜底） | `create_*_provider()` | 429 必须尊重 `Retry-After`；其余 4xx 不重试 |
| `fts.py` | FTS5 trigram 索引 | `FtsIndex.search()` | trigram 对 <3 字符天然跳过（短词由 indexer 的 bigram 词法路兜底）；`source` 列是 UNINDEXED，`path_prefix` 下推只减候选 |
| `vector.py` | 向量后端 Protocol + memory + sqlite_vec（磁盘） | `create_vector_backend()` | sqlite_vec 操作有 `_serialized` 装饰器串行化；首次切换自动从旧缓存迁移；批量余弦（numpy 可选加速，缺 numpy 自动回退标量）在 `_indexer/search.py::semantic_rank` |
| `fsnotify.py` | Windows 原生目录监听（ctypes + ReadDirectoryChangesW） | `WindowsDirectoryWatcher` | 纯 ctypes 手写 OVERLAPPED 结构，改结构体定义前先看 `_declare_prototypes`；事件经防抖调度线程（条件变量，不是 Timer 风暴） |

## 6. 缓存与状态文件布局

```
~/.mortis_rag_mcp/                         # 用户级数据目录（旧名 ~/.vault_mcp 独占时原子迁移）
├── vaults.toml            # 注册表（原子写 + 跨进程锁）
├── config.toml            # 用户级配置（可选）
├── STATUS.md              # Agent 信任锚（doctor 自动生成，禁止手改）
├── status.json            # 机器可读全量状态报告
└── (缓存默认在) ~/.mortis_rag_mcp_cache/<profile>/
    ├── vault_<key>.chunks.bin      # 文本层缓存（_CacheCodec，zlib 压缩）
    ├── vault_<key>.vectors.bin     # 向量缓存（float32 + zlib）
    ├── vault_<key>.failed.json     # 失败文件名单（原子写）
    ├── fts/vault_<key>.fts.sqlite  # FTS5 索引
    └── vec/vault_<key>.vec.sqlite  # sqlite_vec 后端（可选）
```

`[cache] placement = "vault"` 时缓存改放各库 `.mcp_cache/` 子目录（分发场景连缓存一起带走）。

## 7. 测试

```powershell
# 设置 UTF-8 编码环境后运行靶向测试
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest tests/test_compact_search.py tests/test_read_heading.py -q
```

- 56 个测试文件（单机推荐按模块靶向运行；CI 全量矩阵覆盖 Ubuntu 3.10–3.13 与 Windows 3.12）：切块/缓存/多库/紧凑投影/预算/物理读取/章节定位/自动摄取/防抖监听/宿主隔离/快照等。
- **约定**：不碰真实网络（embedding 用 `static` 模式或注入 FakeProvider）；临时库一律 `tmp_path`；测试注册表与配置经 `MORTIS_RAG_CONFIG` / `MORTIS_RAG_REGISTRY` 严格隔离，绝不污染宿主真实环境。
- 已知 Windows 平台坑：`kb_rebuild` 删 FTS 缓存走系统回收站，trash 失败会
  `SAFE_DELETE_FAIL_CLOSED`（`test_subvaults.py::test_stdio_kb_rebuild_returns_stats`
  在部分 Windows 环境因此红）——修它是件独立任务，别顺手带在别的 commit 里。
- **Windows 升级文件写锁**：Windows 环境下 MCP 客户端拉起的 console 入口 exe（`mortis-rag-mcp.exe` 或 `vault-mcp.exe`）运行期间会被系统锁定。此时若执行 editable 重装（`pip install -e .`）会报 `[WinError 5] 拒绝访问`。仅 `git pull` 更新了磁盘源码，运行中未重启的 Python 进程不会自动重新加载模块。排查与安全升级步骤见 §9 食谱。

## 8. 开发约定

1. **commit 即记账**：每次 commit 后把技术变更追加到 `docs/Changelog_developer.md`
   （格式见该文件开头：GitHub 用户名、日期、agent、模型）。
2. **文档分工**（不许串味）：
   - `CHANGELOG_user.md`：**面向终端用户**。只在发 release 时改；只讲功能增减与体验改善，**严禁出现任何技术术语或底层代码细节**（函数名、变量名、私有类、底层锁、内部脚本名出现在这里 = 事故）。
   - `QUICKSTART_user.md`：用户初次部署指南，保持"5 分钟跑通"的颗粒度。
   - `docs/Changelog_developer.md`：commit 级技术流水，必须记录操作者与技术细节。
   - `docs/PROJECT_GUIDE.md`：全系统架构指南，代码级架构变动写在第十五节。
   - `docs/Quick-start_developer.md`：本文件，架构/约定变了就同步。
3. **Breaking 变更**：工具更名/删工具 = 大版本，README + CHANGELOG_user 顶部必须写
   升级须知，skill 同步改（skill 教 AI 用工具，名字对不上 AI 就会调幽灵工具）。
4. **skill 同步**：`skills/mortis-rag-mcp/SKILL.md` 是发给 AI 看的"使用纪律"，改工具
   行为时必须同步改它；写纪律用可判定的 if-then，不写散文。
5. **fail-closed**：涉及删除/覆盖的操作（缓存清理、快照导入替换）出错时宁可不删，
   也别留下半个状态的文件。

## 9. 常见任务食谱

### 加一个新工具
1. `server.py._tool_definitions()` 加 schema（描述写"何时用"，不只写"是什么"）
2. `call_tool()` 加分发 → 写 `_kb_xxx(self, arguments)` handler
3. 需要路径 → 过 `_resolve_vault_path()`；需要索引器 → `_indexer_for()`
4. `tests/test_mcp_stdio.py` 加协议层用例 + 对应功能测试文件
5. skill 的工具清单加一行；CHANGELOG_user 在下次 release 补一句

### 改检索行为（切块/打分/过滤）
1. **先跑 eval**：`python scripts/eval_search.py --golden tests/eval/golden_queries.json`
   记录基线 Hit@K
2. 改代码；若影响切块结果 → `_cache_meta()` 加代际键（让旧缓存自动重建）
3. 再跑 eval 对比；无提升就 revert
4. `tests/test_hybrid.py` / `test_search_filters.py` 补用例

### 加一个配置项
1. `config.py` 对应 `*Config` dataclass 加字段（带默认值）
2. `load_config()` 解析（字符串走 `_env()` 插值，数字走 `_numeric()`）
3. `config/app.toml.example` 加带注释的样例
4. 若影响切块/embedding → `_cache_meta()` 代际键

### 升级已有部署（Windows 进程占用排查）
1. **停止客户端连接**：先在对应 MCP 客户端（如 Claude Desktop / Codex / WorkBuddy）中停用或关闭连接器。注意：仅关闭终端或 IDE 窗口不保证后台托管的 Python 子进程完全退出。
2. **只读排查残留进程**：运行以下只读 PowerShell 命令，精确定位占用进程的 PID、名称与命令行，避免按进程名全杀其他 Python 任务：
   ```powershell
   Get-CimInstance Win32_Process |
       Where-Object { $_.Name -in @('mortis-rag-mcp.exe', 'vault-mcp.exe') -or $_.CommandLine -like '*mortis_rag_mcp*' } |
       Select-Object ProcessId, Name, ExecutablePath, CommandLine
   ```
3. **安全终止进程（仅在客户端无法正常关闭时）**：确认 PID 确实归属本项目后，才针对性执行 `Stop-Process -Id <确认过的PID>`；若客户端配置了自动拉起，必须先关连接器，不要循环 kill。
4. **使用本虚拟环境 Python 执行重装**：
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -e .
   ```
   （切勿尝试“先 pip uninstall”，文件被锁时卸载同样会失败；系统重启仅作为句柄死锁无法解除时的最后排障手段）。
5. **推荐连接器配置方式**：客户端推荐直接使用该 venv `python.exe` 配合参数 `-m mortis_rag_mcp --serve-mcp-stdio`，减少 console exe 被锁冲突。运行中模块不会热更新，每次更新后仍须重启服务生效。
6. **验证升级**：重新启用连接器，检查 initialize 回显版本号与 `kb_list` 响应。

## 10. 上手 checklist

- [ ] `pip install -e .` + `pytest tests/ -q` 全绿（Windows 上 rebuild 那个已知红除外）
- [ ] 读完本文件 §3-§5，能不看代码讲清索引/检索两条管线
- [ ] 跑过一次 eval（哪怕只有占位查询）
- [ ] 知道四条文档分工和 commit 记账规则
- [ ] 动手前确认：`docs/PROJECT_GUIDE.md` §15 或 issue / PR 记录里是否已有排期方案——有就照着做，别另起炉灶
