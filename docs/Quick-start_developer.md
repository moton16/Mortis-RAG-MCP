# Quick-start：开发者上手指南

> 面向第一次接触本仓库的开发者（人类或 agent）。**只读这一篇就够上手**：
> 项目概况 → 架构 → 代码逻辑 → 各模块职责 → 开发约定 → 常见任务食谱。
> 需要溯源某次具体改动时才去翻 `Changelog_developer.md`；要写新功能先看
> `Execution-plan_developer.md`（如有对应方案）。

---

## 1. 30 秒版：这是什么

Mortis'RAG MCP 是一个**本地 Markdown 知识库 RAG 服务器**，通过 MCP 协议（stdio +
换行分隔 JSON-RPC，协议版本 `2025-06-18`）向 AI agent 提供 13 个 `kb_*` 工具：
注册任意文件夹为知识库 → 后台增量索引（切块 + embedding + FTS）→ 三路混合检索
（FTS5 BM25 + 向量余弦 + bigram 词法，RRF 融合）→ rerank → 返回结构化 chunks。

**核心设计红线**（改动前先默念三遍）：

1. **零第三方运行时依赖**：`pyproject.toml` 里 `dependencies = []`。HTTP 用 `urllib`，
   TOML 用 `tomllib`（3.10 有内置 fallback 解析器），向量存储自己写二进制编解码。
   唯一的可选加速依赖是 numpy（缺失时自动回退标量余弦）和 sqlite_vec（可选磁盘向量后端）。
2. **不绑定任何路径**：知识库关系存用户级注册表 `~/.vault_mcp/vaults.toml`，仓库零个人配置。
3. **检索永不报错**：FTS 缺失、向量后端加载失败、reranker 挂掉——全部自动降级，不抛给用户。
4. **注释解释"为什么"**：代码里大量注释记录的是"曾经踩过的坑"，删注释等于拆地雷标识。

## 2. 仓库地图

```
Mortis-RAG-MCP/
├── mortis_rag_mcp/          # 包本体（~5700 行）
│   ├── __main__.py          # 入口：python -m mortis_rag_mcp --serve-mcp-stdio
│   ├── config.py            # 配置加载（390 行）
│   ├── registry.py          # 用户级知识库注册表（304 行）
│   ├── server.py            # MCP 协议层 + 15 个工具 handler（1000+ 行）
│   ├── ingest/              # PDF/Office 异步摄取与表格处理（worker, mineru, tables）
│   ├── indexer.py           # 索引与检索核心（2787 行，全项目的心脏）
│   ├── providers.py         # embedding / reranker HTTP 封装（230 行）
│   ├── fts.py               # FTS5 SQLite 封装（149 行）
│   ├── vector.py            # 向量后端：memory / sqlite_vec（367 行）
│   └── fsnotify.py          # Windows ReadDirectoryChangesW 原生监听（557 行）
├── config/app.toml.example  # 配置模板（app.toml 本体被 gitignore）
├── skills/mortis-rag-mcp/   # 配套 agent skill（教 AI 怎么用这套工具）
├── tests/                   # pytest，22 个文件 185+ 用例
├── docs/
│   ├── Quick-start_developer.md    # 本文件
│   ├── Changelog_developer.md      # 每次 commit 的技术变更流水
│   └── Execution-plan_developer.md # 待执行功能的代码级方案
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
│  - call_tool()          13 个 handler        │  跨库 fan-out 合并、库级权重
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
| `config.py` | TOML 加载、`${ENV_VAR}` 插值、配置链 `--app-config` > `VAULT_MCP_CONFIG` > `~/.vault_mcp/config.toml` > 默认 | `load_config()` | 新配置项必须给默认值 + example 文件同步加注释；`AppConfig.vault_path` 会被 `__post_init__` 特殊处理 |
| `registry.py` | vaults.toml 读写（**原子写 tmp+replace**，跨进程排他锁 Windows `msvcrt.locking`/POSIX `fcntl.flock`） | `VaultRegistry.add/remove/set_weight/set_solo` | 字符串字段序列化必须 `json.dumps`（曾有 LLM 传入的引号毁掉整个注册表的 bug）；新字段要在 `load()` 里给老文件回退值 |
| `server.py` | 协议层 + 工具分发 + fan-out | `call_tool()` | 新工具 = schema + 分发 + handler 三处；fan-out 的分页只能在全局合并后做一次（逐库分页再合并顺序无意义） |
| `indexer.py` | 切块、缓存、增量、检索、豁免、快照、watcher | `sync()` / `search()` | ① 改切块逻辑必须同步 `_cache_meta()` 加代际键，否则旧缓存不失效；② 常驻 `Chunk` 对象不许原地改 `score`（用 `dataclasses.replace` 产副本，曾有并发脏读 bug）；③ `_safe_path()` 防路径逃逸，读文件必经它 |
| `providers.py` | embedding/reranker HTTP（重试、退避、batch 切分、static 兜底） | `create_*_provider()` | 429 必须尊重 `Retry-After`；其余 4xx 不重试 |
| `fts.py` | FTS5 trigram 索引 | `FtsIndex.search()` | trigram 对 <3 字符天然跳过（短词由 indexer 的 bigram 词法路兜底）；`source` 列是 UNINDEXED，`path_prefix` 下推只减候选 |
| `vector.py` | 向量后端 Protocol + memory（numpy）+ sqlite_vec（磁盘） | `create_vector_backend()` | sqlite_vec 操作有 `_serialized` 装饰器串行化；首次切换自动从旧缓存迁移 |
| `fsnotify.py` | Windows 原生目录监听（ctypes + ReadDirectoryChangesW） | `WindowsDirectoryWatcher` | 纯 ctypes 手写 OVERLAPPED 结构，改结构体定义前先看 `_declare_prototypes`；事件经防抖调度线程（条件变量，不是 Timer 风暴） |

## 6. 缓存与状态文件布局

```
~/.vault_mcp/
├── vaults.toml            # 注册表（原子写 + 跨进程锁）
├── config.toml            # 用户级配置（可选）
└── (缓存默认在) ~/.vault_mcp_cache/<profile>/
    ├── vault_<key>.chunks.bin      # 文本层缓存（_CacheCodec，zlib 压缩）
    ├── vault_<key>.vectors.bin     # 向量缓存（float32 + zlib）
    ├── vault_<key>.failed.json     # 失败文件名单（原子写）
    ├── fts/vault_<key>.fts.sqlite  # FTS5 索引
    └── vec/vault_<key>.vec.sqlite  # sqlite_vec 后端（可选）
```

`[cache] placement = "vault"` 时缓存改放各库 `.mcp_cache/` 子目录（分发场景连缓存一起带走）。

## 7. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q     # 全量（约 80s）
```

- 22 个测试文件：切块/缓存/多库/子库/豁免/去重/快照/solo/混合检索/过滤器/并发硬化/监听……
- **约定**：不碰真实网络（embedding 用 `static` 模式或 monkeypatch）；临时库一律 `tmp_path`；
  Windows 与 Unicode 路径已有专项用例，新功能涉及路径必须补。
- 已知 Windows 平台坑：`kb_rebuild` 删 FTS 缓存走系统回收站，trash 失败会
  `SAFE_DELETE_FAIL_CLOSED`（`test_subvaults.py::test_stdio_kb_rebuild_returns_stats`
  在部分 Windows 环境因此红）——修它是件独立任务，别顺手带在别的 commit 里。

## 8. 开发约定

1. **commit 即记账**：每次 commit 后把技术变更追加到 `docs/Changelog_developer.md`
   （格式见该文件开头：GitHub 用户名、日期、agent、模型）。
2. **文档分工**（不许串味）：
   - `CHANGELOG_user.md`：**面向终端用户**。只在发 release 时改；只讲功能增减与体验改善，**严禁出现任何技术术语或底层代码细节**（函数名、变量名、私有类、底层锁、内部脚本名出现在这里 = 事故）。
   - `QUICKSTART_user.md`：用户初次部署指南，保持"5 分钟跑通"的颗粒度。
   - `docs/Changelog_developer.md`：commit 级技术流水，必须记录操作者与技术细节。
   - `docs/PROJECT_GUIDE.md`：全系统架构指南，代码级架构变动写在第十五节。
   - `docs/Quick-start_developer.md`：本文件，架构/约定变了就同步。
   - `docs/Execution-plan_developer.md`：待执行功能方案，做完一个划掉一个。
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

## 10. 上手 checklist

- [ ] `pip install -e .` + `pytest tests/ -q` 全绿（Windows 上 rebuild 那个已知红除外）
- [ ] 读完本文件 §3-§5，能不看代码讲清索引/检索两条管线
- [ ] 跑过一次 eval（哪怕只有占位查询）
- [ ] 知道四条文档分工和 commit 记账规则
- [ ] 动手前确认：`docs/Execution-plan_developer.md` 里是否已有对应方案——有就照着做，别另起炉灶
