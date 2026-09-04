# Mortis'RAG MCP（原名 vault-mcp / Obsidian RAG MCP）项目文档

> 面向 Obsidian Markdown 知识库的本地 RAG（检索增强生成）MCP 服务。
> 提供结构化切片检索、原文读取、增量索引、向量语义召回与重排序能力，供 AI Agent（WorkBuddy / Codex / Trae 等）通过 MCP 协议调用。

- 仓库：`Mortis-RAG-MCP`（Python 包名保持 `vault_mcp`，控制台脚本新增 `mortis-rag-mcp`，旧 `vault-mcp` 兼容保留）
- 当前版本：`mortis-rag-mcp 0.6.0`（solo 独立库 + 工具更名 + 混合检索 + 检索过滤/去重 + 库级权重 + 原生监听 + 索引快照）
- 知识库：**不绑定任何路径** —— 任意文件夹通过 `kb_init` 注册为知识库，注册表持久化于 `~/.vault_mcp/vaults.toml`（跨重启/跨设备可用）

> **新用户看这里**：下载后如何初始化（装包 → 配 key → 接入 MCP 客户端 → `kb_init` 建库 → 安装配套 Skill），见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

---

## 一、技术栈清单

### 1.1 语言与运行环境

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | `>= 3.10`（实际运行 `3.13.12`） | `pyproject.toml` 声明，MCP 服务运行于 managed 3.13.12 |
| 构建后端 | `setuptools >= 68` | `pyproject.toml` build-system |
| MCP 协议 | `2025-06-18` | `server.py` 中 `initialize` 声明的协议版本 |
| 项目包名 | `vault-mcp` | 控制台入口：`vault-mcp --serve-mcp-stdio` |

### 1.2 运行时依赖

**核心原则：零第三方运行时依赖**（`pyproject.toml` 中 `dependencies = []`），全部使用 Python 标准库：

| 标准库模块 | 用途 |
|---|---|
| `json` / `struct` / `zlib` / `array` | 二进制缓存编解码（`_CacheCodec`，float32 向量 + zlib 压缩） |
| `hashlib` | 文件内容签名（sha256）、chunk 标识（sha1）、缓存 key（sha256） |
| `re` | 标题解析、词法分词、frontmatter 提取 |
| `threading` / `concurrent.futures` | 后台索引线程、并发 embedding（默认 6 线程） |
| `urllib.request` | 外部 embedding / reranker HTTP 调用 |
| `pathlib` / `os` | 路径处理、文件扫描 |
| `tomllib`（3.11+） | TOML 配置解析（3.10 有内置 fallback 解析器） |

**可选加速依赖**（本会话新增，仅 `_semantic_rank` 批量余弦用）：

| 依赖 | 版本 | 作用 |
|---|---|---|
| `numpy` | `2.5.1` | 向量矩阵批量余弦相似度，替代逐条 Python 循环，全库检索提速约一个数量级；缺失时自动回退标量 `_cosine` |

> 安装：`python -m pip install numpy`（可选；不装也能跑，只是检索慢些）。

### 1.3 外部服务（推理 API）

| 服务 | 模型 | 用途 | 计费 |
|---|---|---|---|
| 硅基流动 SiliconFlow | `Qwen/Qwen3-Embedding-8B` | 文本向量化（embedding） | 0.28 元/百万 token |
| 硅基流动 SiliconFlow | `BAAI/bge-reranker-v2-m3` | 检索结果重排序（rerank） | **免费** |

API Key 通过环境变量注入（`EMBEDDING_API_KEY` / `RERANKER_API_KEY`），配置中支持 `${ENV_VAR}` 插值，请求头 `Authorization: Bearer ...`。

### 1.4 测试

- 框架：`pytest`（无版本锁定，随环境最新）
- 用例：`tests/` 下 6 个文件（`test_indexer` / `test_providers` / `test_mcp_stdio` / `test_cache` / `test_multivault` / `test_subvaults`）
- 覆盖：切块、静态 embedding、reranker 回退、增删改/重命名生命周期、Windows 与 Unicode 路径、缓存复用、多库与子库。

---

## 二、项目沿用与继承说明

### 2.1 项目来源

本项目为**自研的"纯标准库" MCP 服务器**，早期历史无 git 仓库可追溯（项目目录未初始化 git，以下"开发过程记录"以文件时间戳与当前会话实测为准）。它不是对某一开源项目的 fork，而是围绕三个既有生态标准从零组装：

| 沿用对象 | 类型 | 衔接方式 |
|---|---|---|
| **MCP（Model Context Protocol）** | 协议标准 | 以 stdio + newline-delimited JSON-RPC 实现 `initialize` / `tools/list` / `tools/call` / `ping`，符合 2025-06-18 协议版本 |
| **SiliconFlow OpenAI 兼容 API** | 外部服务接口 | `POST /v1/embeddings`（`{"model","input","dimensions"}`）与 `POST /v1/rerank`（`{"model","query","documents"}`） |
| **Qwen3-Embedding 系列 / BGE-reranker-v2-m3** | 开源模型 | 通过硅基流动托管 API 调用，模型名直接写死在 `config/app.toml` |
| **Obsidian vault 目录约定** | 数据格式 | 识别 `---frontmatter---` 与 `tags:`、`# 标题` 层级、`.obsidian` 隐藏目录忽略 |

### 2.2 模块继承结构（自研包 `vault_mcp/`）

| 模块 | 职责 | 对外接口 |
|---|---|---|
| `config.py` | TOML 配置加载、环境变量插值、参数校验 | `load_config(path)` → `AppConfig` |
| `providers.py` | 外部 HTTP 封装（embedding / reranker）、静态哈希 embedding 兜底 | `create_embedding_provider` / `create_reranker_provider` |
| `indexer.py` | 文件扫描、切块、增量同步、磁盘缓存、检索（词法+语义+rerank） | `MarkdownIndexer` 类 |
| `server.py` | MCP 协议层：工具定义、请求分发、stdio 服务 | `serve_stdio(config_path)` |

### 2.3 与 WorkBuddy 的衔接方式

MCP 服务注册在 `~/.workbuddy/mcp.json`（stdio 模式）：

```json
"vault-mcp": {
  "type": "stdio",
  "command": "python",
  "args": ["-m", "vault_mcp", "--serve-mcp-stdio", "--app-config", "<项目目录>/config/app.toml"],
  "env": {
    "PYTHONPATH": "<项目目录>",
    "EMBEDDING_API_KEY": "...",
    "RERANKER_API_KEY": "..."
  }
}
```

也可在 Codex / Trae 中注册为 `mcp_servers.vault_mcp`（详见英文版 README）。

### 2.4 本会话（2026-08-08）新增的优化项

在原有结构上完成了"管线修复 + 性能优化"一轮改造，改动均在原模块内扩展，未破坏既有接口：

1. `indexer.py` `search()`：词法过滤从"AND 硬淘汰"改为**软信号**（命中加分 0.2），语义改为**全库召回**；rerank 候选截断 top-60。
2. `indexer.py` 新增 `_query_tokens()`：中文 **bigram 分词**（原 `[\w]+` 会把整句中文字符串吞成一个 token，导致中文检索退化为整串精确匹配）。
3. `indexer.py` 新增 `_semantic_rank()`：numpy 批量余弦（缺 numpy 时回退标量 `_cosine`）。
4. `indexer.py` `_make_chunks()`：**修复了 chunk_overlap 配置存在但从未生效的 bug**，新增 `_overlap_tail()` 实现重叠切块。
5. `indexer.py` `_cache_meta()`：缓存元数据加入 `chunk_overlap`，参数变更时缓存正确失效重建。
6. `server.py`：`kb_search` 的 `use_rerank` 默认值从 `False` 改为 `True`，免费 reranker 默认启用。
7. `config/app.toml`：向量维度 `4096 → 1024`（Qwen3-8B 原生 MRL 裁剪），`chunk_overlap 0 → 150`。

---

## 三、调用方法

### 3.1 安装依赖

```powershell
cd <项目目录>

# 可选：创建虚拟环境（推荐）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装包（editable 模式）
python -m pip install --upgrade pip
python -m pip install -e .

# 若报 Cannot import 'setuptools.build_meta'：这是环境缺/坏 setuptools（Codex/Trae
# 等捆绑 Python 常见），不是本包问题。先修构建环境再重试：
# python -m pip install --upgrade pip setuptools wheel
# 仍失败（离线/构建隔离异常）再退而求其次：
# python -m pip install -e . --no-build-isolation
# 注："零依赖"指运行时依赖（dependencies=[]）；从源码安装需要 setuptools 作为
# PEP 517 构建后端，健康环境下 pip 会自动提供。

# 可选：numpy 加速语义检索
python -m pip install numpy

# 首次使用：复制配置模板
Copy-Item .\config\app.toml.example .\config\app.toml
```

### 3.2 配置（`config/app.toml`）

0.3.0 起配置解析链：`--app-config` 参数 > `VAULT_MCP_CONFIG` 环境变量 > `~/.vault_mcp/config.toml` > 内置默认值。**配置里不再有 `[vault].path`**（旧配置中的该项仍会被识别并自动迁移进注册表）；知识库完全由 `kb_init` 注册管理。API key 为空时自动回退读取环境变量 `VAULT_MCP_API_KEY`。

```toml
[embedding]
mode = "external"                              # external=真实模型 / static=本地哈希兜底
endpoint = "https://api.siliconflow.cn/v1/embeddings"
model = "BAAI/bge-m3"
dimension = 1024
send_dimensions = false                        # bge-m3 不接受 dimensions 字段
api_key = "${VAULT_MCP_API_KEY}"               # 环境变量插值，或留空回退 VAULT_MCP_API_KEY
timeout = 60
max_retries = 3                                # 0.5.0：首败后的额外重试次数（指数退避）
batch_size = 32                                # 0.5.0：单请求最大文本数（<=0 = 旧的整文件单请求）
retry_backoff = 1.0                            # 0.5.0：退避基数秒；429 时服务端 Retry-After 优先

[reranker]
enabled = true
endpoint = "https://api.siliconflow.cn/v1/rerank"
model = "BAAI/bge-reranker-v2-m3"              # 硅基流动免费
api_key = "${VAULT_MCP_API_KEY}"
timeout = 60

[index]
chunk_size = 1200        # 按字符数切块
chunk_overlap = 150      # 相邻块重叠字符数（防止剧情长文切块断裂）
debounce_seconds = 0.5   # 文件变更防抖
rrf_per_route = 40       # 0.5.0：RRF 每路候选宽度（原硬编码）
rerank_cap = 60          # 0.5.0：单次 rerank API 的 chunk 上限（原硬编码）
watch_method = "auto"    # 0.5.0：auto=可用就用原生事件 / native=优先原生 / poll=0.4.x 轮询
watch_fallback_interval = 30.0  # 0.5.0：原生监听期间的全量对账周期秒（0=关闭）
inject_image_captions = false   # 0.5.0：图片 alt/图注注入；开启=可检索但会全量重嵌，慎开

[cache]
enabled = true
dir = "~/.vault_mcp_cache"
embedding_max_workers = 2
placement = "home"       # home=共享缓存目录 / vault=缓存放各库 .mcp_cache/（分发推荐）
subdir = ".mcp_cache"
```

### 3.3 启动 MCP 服务

```powershell
# 方式一：安装后的控制台脚本
vault-mcp --serve-mcp-stdio --app-config .\config\app.toml

# 方式二：python -m 直接运行
python -m vault_mcp --serve-mcp-stdio --app-config .\config\app.toml
```

服务为 **stdio 模式**：stdout 只输出 JSON-RPC 响应，通过 stdin 接收 newline-delimited JSON 请求。正常情况由 Agent（WorkBuddy/Codex）拉起，无需手动运行。

### 3.4 功能模块 / API 调用方式

共 13 个工具，均为 `tools/call` 的 JSON-RPC 请求：

> **0.6.0 工具更名（Breaking）**：`kb_unregister` → `kb_remove`、`kb_vaults` → `kb_list`、
> 原 `kb_list`（列文件）→ `kb_list_files`，旧名不再保留。

#### `kb_init` — 注册知识库（0.3.0 新增，首次使用必调）

```json
{"name": "kb_init", "arguments": {"path": "D:\\我的笔记", "name": "我的笔记"}}
```

- 校验目录存在 → 写入用户级注册表 `~/.vault_mcp/vaults.toml` → 后台建立索引 → 启动文件监听（增量同步）。
- `name` 可选，默认取文件夹名。重复注册同一目录（Windows 大小写不敏感）会报 `already registered`。
- 注册表跨重启保留；换设备/分发给他人时，对方只需对新文件夹跑一次 `kb_init` + 配好自己的 `VAULT_MCP_API_KEY`。

#### `kb_remove` — 从注册表移除知识库（0.6.0 更名，原 `kb_unregister`）

```json
{"name": "kb_remove", "arguments": {"path": "D:\\我的笔记", "purge_cache": true}}
```

- 停止文件监听并从注册表移除（不动文件夹本身）。`purge_cache=true` 时同时删除该库的磁盘索引缓存。
- 移除后重新 `kb_init` 即可恢复（磁盘缓存默认保留，秒级、无需重新 embedding）——这也是取消 solo 独立库的正道。

#### `kb_init_solo` — 注册/转为独立库（0.6.0 新增）

```json
{"name": "kb_init_solo", "arguments": {"path": "D:\\私密笔记", "name": "私密库"}}
```

- 独立库（solo）**不参与全局检索**：`kb_search` 不传 `vault_path` 时跳过它（并在结果的 `excluded_solo` 字段点名）；只有显式传 `vault_path` 才会被搜索。索引、文件监听、快照迁移等能力与普通库完全一致。
- 三种输入：未注册文件夹 → 注册为独立库；已注册普通库 → 原地转为独立库（秒级，不动索引与缓存）；已是独立库 → 幂等确认。
- 唯一注册库是独立库、或全部库都是独立库时，不传 `vault_path` 的检索会明确报错并提示显式传 `vault_path`，不会悄悄搜索。
- 取消独立库：`kb_remove` 后重新 `kb_init`（磁盘缓存保留，0 次重新 embedding）。

#### `kb_search` — 语义+词法混合检索（核心）

```json
{
  "name": "kb_search",
  "arguments": {
    "query": "卡芙卡和银狼的关系",
    "top_k": 10,
    "use_rerank": true,
    "vault_path": "C:\\Users\\you\\1\\Obsidian Vault\\安华帝国"
  }
}
```

- `query`：必填检索词。
- `top_k`：返回条数，默认 10。
- `use_rerank`：默认 `true`，调用免费 bge-reranker-v2-m3 对 top-60 精排。
- `vault_path`：可选，**已注册**知识库的绝对路径；**缺省时跨全部非 solo 注册库 fan-out 检索**（每库取候选 → 合并 → query 只 embed 一次 → 统一 rerank 一次），结果每项带 `vault`（注册路径）与 `vault_name` 字段，另有 `searched`/`errors` 汇总与 `excluded_solo`（本次被跳过的独立库）。solo 库必须显式传 `vault_path` 才会被搜索。
- **0.5.0 过滤与分页**（过滤在 rerank 之前执行，不浪费配额；fan-out 时过滤逐库生效、分页在全局合并后一次完成）：
  - `path_prefix`：只保留 `source` 以该前缀开头的 chunk（如 `"教材/"`）；
  - `tags`：frontmatter 标签过滤，命中任一即可（大小写不敏感，自动去 `#`）；
  - `mtime_after` / `mtime_before`：按文件内容最后修改时间过滤（epoch 秒或 ISO 8601 字符串，闭区间）；
  - `offset` / `limit`：分页参数，`limit` 缺省回落到 `top_k`；
  - `group_by_vault`：仅 fan-out 生效，按库分组返回 `groups`（组序按各组最高分降序，每组各取一页）；
  - `dedupe`：默认 `true`，正文完全相同的 chunk 只保留最前一条（重复备份不再占 top_k）。
- 检索链路：bigram 词法软打分 → 全库 embedding 余弦召回（numpy 加速）→ 词法加分融合 →（可选）rerank 精排 → 返回 top_k。

返回（`chunks[]` 内每项，fan-out 时多出 `vault`/`vault_name`）：
```json
{
  "id": "sha1...", "content": "切片原文", "score": 0.93,
  "source": "星穹铁道.md", "title": "卡芙卡与银狼",
  "metadata": {"heading": "卡芙卡与银狼", "start_line": 1, "end_line": 5, "chunk_index": 0, "tags": [], "mtime": 1788000000.0, "content_hash": "0123456789abcdef"}
}
```

#### `kb_read` — 读取原文（不调 LLM）

```json
{"name": "kb_read", "arguments": {"source": "星穹铁道.md", "heading": "卡芙卡与银狼"}}
{"name": "kb_read", "arguments": {"source": "星穹铁道.md", "start_line": 1, "end_line": 10}}
```

支持按 `source`、`heading` 或 1-based 行区间读取。

#### `kb_list_files` — 列出已索引文件（0.6.0 更名，原 `kb_list`）

```json
{"name": "kb_list_files", "arguments": {}}
```
返回 `[{source, title, chunks}]`。

#### `kb_stats` — 索引状态

```json
{"name": "kb_stats", "arguments": {}}
```
返回文件数、切片数、失败文件、最后同步时间、embedding 模型/维度、reranker/cache 是否启用。

#### `kb_list` — 列出已注册知识库（0.6.0 更名，原 `kb_vaults`）

```json
{"name": "kb_list", "arguments": {}}
```
返回每个注册条目的 `{name, path, registered_at, weight, solo, exists, indexed, files, last_sync}`。

#### `kb_rebuild` — 删除缓存并强制全量重建

```json
{"name": "kb_rebuild", "arguments": {"vault_path": "C:\\Users\\you\\1\\Obsidian Vault"}}
```
首次建库、模型/维度/切块参数变更、缓存损坏时使用。

#### `kb_set_weight` — 设置库级检索权重（0.5.0 新增）

```json
{"name": "kb_set_weight", "arguments": {"vault_path": "D:\\笔记\\工作库", "weight": 2.5}}
```

- 跨库 fan-out 检索时，该库所有 chunk 的分数先乘以 `weight` 再参与全局排序，用于表达「这个库更重要」。
- 取值 0 < weight <= 100，默认 1.0（不放大）；权重持久化在注册表（v2），老 toml 自动容错为 1.0。
- 配合 `kb_search` 的 `group_by_vault = true` 可以「权重定组序、组内看相关度」。

#### `kb_export` / `kb_import` — 索引快照迁移（0.5.0 新增）

```json
{"name": "kb_export", "arguments": {"out_path": "D:\\backup\\work-snapshot.zip", "vault_path": "D:\\笔记\\工作库"}}
{"name": "kb_import", "arguments": {"snapshot": "D:\\backup\\work-snapshot.zip", "vault_path": "D:\\笔记\\工作库"}}
```

- 换机/换目录不再需要全量重新 embedding：`kb_export` 把 chunks + 向量 + FTS 三层缓存连同 manifest（格式版本/cache key/模型维度/统计）打包成 zip；在新机器上先 `kb_init` 注册目标目录，再 `kb_import` 恢复。
- **核心承诺：导入后的下一次同步 0 次 embedding API 调用**（文本层、向量、FTS 原样落地；本机 cache key 由导入逻辑自动重写）。
- 安全：zip 成员按白名单精确校验（路径穿越名直接拒绝）；快照的向量模型/维度与本机配置不一致时拒绝导入，`force = true` 可强制——此时只导入文本层，向量由本地重新计算。
- 前提：`[cache] enabled = true` 且导出前完成过至少一次索引。

### 3.5 运行测试

```powershell
python -m pytest -q
python -m pytest -q tests/test_providers.py
python -m pytest -q tests/test_indexer.py
python -m pytest -q tests/test_mcp_stdio.py
```

> 注意：测试会向 `~/.vault_mcp_cache/` 写入临时 vault 缓存（pytest 临时目录哈希命名）。已在"缓存改进方案"中给出隔离建议。

---

## 四、开发过程记录

> 早期时间点来自文件时间戳（项目无 git 历史）；2026-08-08 为当前会话实际开发记录。

### 阶段 0：项目搭建（2026-08-04）

- 建立 `vault_mcp/` 四模块结构（config / providers / indexer / server），确立"纯标准库、零运行时依赖"路线。
- 完成 `config/app.toml` 与配置解析器（支持 `${ENV_VAR}` 插值、3.10 TOML fallback）。
- 落地基础能力：Markdown 切块（标题分段 + frontmatter tags）、静态哈希 embedding、stdio MCP 服务骨架。
- 配套基础测试：`test_indexer.py`、`test_providers.py`、`test_mcp_stdio.py`。

### 阶段 1：缓存、多库与子库（2026-08-07）

- 实现**磁盘缓存**：chunk 签名 + float32 向量 + zlib 压缩的二进制格式（`VMCPC` v1），默认存 `~/.vault_mcp_cache/vault_<sha256(路径前16位)>.bin`。
- 增量同步：文件内容 sha256 签名比对，未变更文件跳过 embedding；启动后磁盘缓存命中可秒级复用。
- 并发 embedding（`embedding_max_workers=6`）：348 文件全量重建从 ~9 分钟降到 ~2.5 分钟。
- 多 vault 支持（`vault_path` 参数）、子库支持（`kb_vaults`）、强制重建（`kb_rebuild`）、缓存 placement（home/vault）。
- 补充测试：`test_cache.py`、`test_multivault.py`、`test_subvaults.py`。

### 阶段 4：混合检索 —— FTS5 BM25 + RRF 三路融合（2026-08-30，本会话）

**目标**：把自研的「bigram 词法软信号 + 余弦加法融合」升级为业界收敛的标准答案（basic-memory / obsidian-mcp-server / RAGLite 同款），零新增依赖。

- **FTS5 索引**（新模块 `fts.py`）：每库一个 `{cache_root}/{namespace}/fts/vault_{key}.fts.sqlite`，trigram 分词器（原生 SQLite 支持，中文可用），BM25 排序；journal_mode=MEMORY（派生索引，可重建，不追求持久性）；`_sync_locked` 增量钩子按 source 增删改，与 sha256 签名比对联动。
- **三路 RRF**（`_hybrid_rank`）：路 A = FTS5 BM25（仅保留 ≥3 字符 token 的查询词，`"AND"` 连接）；路 B = 向量余弦原始分快照；路 C = 保留原 bigram 词法层（**天然补上 trigram 对 2 字中文如"银狼"的 0 命中缺口**）。RRF k=60、每路 top-40，融合后走既有 rerank top-60 → top_k。
- **开关**：`[index] use_hybrid`（默认 true）；false 时逐字走旧路径。FTS 索引依赖 `[cache] enabled = true`，缓存关闭时自动降级旧行为。
- **向量抽象层**（新模块 `vector.py`）：`VectorBackend` Protocol + `MemoryVectorBackend`（默认，包装现有 numpy 暴力扫描，存储格式不变）+ **`SqliteVecBackend` 磁盘后端（已完整实现）**——`[vector] backend="sqlite_vec"`（安装 `pip install "mortis-rag-mcp[vec]"`）后向量存每库独立的 `vectors/vault_<key>.vec.sqlite`（vec0 + vid 映射、二进制序列化、批量 upsert），`Chunk.embedding` 不驻留内存；真实库实测常驻向量内存 **52.4MB → 0MB**；首次切换自动从 `.bin` 一次性迁移、零重嵌，缺失向量惰性重嵌自愈，热重启复用磁盘库（1s）；sqlite-vec 未安装/加载失败自动回退 memory。
- **stats() 新增**：`use_hybrid` / `fts_enabled` / `vector_backend`（纯增量，兼容旧客户端）。
- **测试**：58 个用例全绿（新增 `test_hybrid.py` 8 项 + `test_vector_backend.py` 5 项：三路 RRF 合并、2 字 CJK bigram 兜底、开关还原旧行为、FTS 缺失/失败降级、增量同步更新 FTS、rebuild/purge 清理、stats 新键；磁盘后端：RAM 释放、.bin 迁移零重嵌、增量同步、purge 删文件、热重启复用）。实测确认：13k 条 1024 维向量 KNN 14.8ms、FTS 中文 BM25 命中正确、trigram <3 字查询 0 命中（由 bigram 路兜底）。
- **更新日志**：版本变更记录见 `CHANGELOG.md`。

### 阶段 3：通用化改造 —— 用户级 Vault 注册表（2026-08-30，本会话）

**目标**：解除对单一硬编码目录的绑定，使 vault-mcp 成为可分发、可换设备的通用工具。

- **用户级注册表**（新模块 `registry.py`）：任意文件夹经 `kb_init` 注册为知识库，持久化于 `~/.vault_mcp/vaults.toml`（原子写、normcase+realpath 归一键去重、损坏容错、`VAULT_MCP_REGISTRY` 环境变量可重定向）。
- **新工具**：`kb_init`（注册+后台首索引+watcher）、`kb_unregister`（停 watcher+移除+可选 purge_cache）；`kb_vaults` 改为列出注册表条目（含 exists/indexed/files/last_sync）。
- **移除根库包含检查**：`vault_path` 接受任意已注册绝对路径；未注册报错并提示 `kb_init`（注册表白名单取代旧 LFI 根库约束）。
- **缺省 fan-out 检索**：多库环境下 `kb_search` 不传 `vault_path` 时跨全部注册库检索，query 只 embed 一次，合并候选统一 rerank 一次（`rerank_chunks` 提取为模块级函数），结果带 `vault`/`vault_name`/`searched`/`errors`。
- **watcher-per-vault**：注册库各有一个监听线程（幂等启停），启动时后台串行预索引全部注册库（避免 N 线程打爆 embedding API）。
- **配置链**：`--app-config` > `VAULT_MCP_CONFIG` > `~/.vault_mcp/config.toml` > 内置默认；删除硬编码个人路径；API key 统一回退 `VAULT_MCP_API_KEY`；legacy `[vault].path` 首启自动迁移进注册表（本机零手动步骤）。
- **indexer.py 仅 3 处小改**：`rerank_chunks()` 模块级化、`purge_cache()`、`search(query_vector=)` 复用参数；watcher/缓存/豁免机制天然 per-vault，未动。
- **测试**：45 个用例全绿；新增 `test_registry.py`（注册表单元）与 `test_registry_server.py`（stdio 集成：fan-out、重复注册、注销+purge、死库容错、legacy 迁移），stdio 测试统一经 `VAULT_MCP_REGISTRY` 隔离注册表。

### 阶段 2：管线修复与性能优化（2026-08-08，本会话）

**问题 1：MCP 服务超时**
- 现象：`kb_stats` / `kb_search` 请求超时（MCP error -32001）。
- 排查：确认服务进程未运行、缓存未加载时首次请求会触发全量同步。
- 处置：本次未改服务生命周期，定位为"首启同步耗时 + 全库线性扫描"双重因素，通过优化 2/3 缓解。

**问题 2：中文检索退化为整串精确匹配**
- 根因：`_WORD_RE = [\w]+` 对中文贪婪吞整串，`search()` 的 AND 词法过滤要求 query 每个 token 都出现在文档中，语义 embedding 只在词法幸存者上排序 → 换说法、部分匹配全查不到。
- 方案：`_query_tokens()` 中文 bigram 分词 + 词法改为软信号 + 全库语义召回。

**问题 3：免费 reranker 从未生效**
- 根因：`use_rerank` 默认 `False`，调用方不显式传 `true` 时 rerank 永不触发。
- 方案：默认值改 `True`。

**问题 4：chunk_overlap 配置形同虚设**
- 根因：`app.toml` 里解析了 `chunk_overlap`，但 `_make_chunks()` 切块代码完全没用它（配置死代码）。
- 方案：实现 `_overlap_tail()`，切块时保留上一块尾部重叠；`_cache_meta()` 加入 `chunk_overlap` 使参数变更正确失效缓存。

**问题 5：4096 维全库线性扫描**
- 根因：无向量索引，逐 chunk Python 循环算余弦，6157 chunks × 4096 维较慢。
- 方案：维度裁剪到 1024（Qwen3-8B MRL 原生支持，精度损失极小）+ numpy 批量余弦（缺失回退）。

**问题 6（验证环节）：测试脚本误触发真实 API**
- 现象：离线测试用假 key 调外部 embedding → 401，chunk 全部丢弃。
- 处置：测试改用 static 模式 / 注入 FakeProvider 验证切块与语义排序逻辑；另做一次真实 API 端到端验证（建库 → 检索 → rerank，7 项全 PASS）。

### 阶段 2 验证结果

- 离线 16 项全 PASS：配置加载、bigram 分词（含中英混合）、overlap 切块（含 0 对照组）、语义排序 top1 正确、词法不硬淘汰。
- 真实 API 端到端 7 项全 PASS：真实建库 6 chunks、向量确认 1024 维、语义召回命中、rerank 真实调用成功。

---

## 五、缓存持久化改进方案

### 5.1 现状与问题

用户反馈：**每次重启或切换 Agent 后都必须重新对全库进行分析**，大量重复劳动。

**实测真相**（解析 `~/.vault_mcp_cache/` 全部缓存文件后）：

1. 缓存**确实落盘**了：主库 `vault_5fa216106ab440a8.bin` 62MB / 442 文件、安华帝国 17MB / 99 文件、DateALive 12MB / 126 文件、翁法罗斯 9MB / 46 文件，均为 4096 维向量。
2. 目录里另有 **39 个 543~573 字节的"幽灵缓存"**，全部指向 `<pytest 临时目录>` —— 是 **pytest 测试产生的临时 vault 缓存**，污染了共享缓存目录，且部分文件无法解析（损坏/截断）。

### 5.2 缓存失效根因分析

| # | 根因 | 机理 | 后果 |
|---|---|---|---|
| R1 | **缓存 key 直接由 vault 绝对路径哈希生成** | `hashlib.sha256(os.fspath(vault_path.resolve()))[:16]` | 路径大小写、符号链接、挂载点、相对/绝对写法任一不同 → key 不同 → 找不到缓存 → 全量重建 |
| R2 | **缓存 meta 全字段强校验** | `_cache_meta()` 含 vault + embedding_mode + embedding_model + dimension + chunk_size + chunk_overlap，任一不同 → 整份缓存作废 | 换模型 / 改维度 / 改切块参数 → **连文本 chunk 一起丢**，全量重分析 |
| R3 | **chunk 文本与向量捆绑存储** | 单一 `.bin` 同时存签名+chunk+向量 | embedding 配置变化时，本可复用的文本切块也一并作废 |
| R4 | **缓存目录无命名空间隔离** | placement=home 时所有 vault、所有测试临时库共享一个目录 | 测试产生的临时缓存与真实缓存混放，污染且难清理 |
| R5 | **缓存失效无分层/无降级** | meta 不匹配即整体丢弃，无"部分复用"路径 | 维度变了连 chunk 文本都重算，浪费 |
| R6 | **跨 Agent 共享依赖隐式约定** | 共享依赖同一 `cache.dir` + 完全相同的 embedding 配置 | 不同 Agent 若配置略有差异（维度、模型名）即互相作废缓存 |

### 5.3 改进方案：分层持久化缓存（chunk 层 + 向量层分离）✅ 已实现

> **状态：本方案已于 2026-08-08 落地实现**，见下方"落地实现"小节。方案设计保留如下供理解设计意图。

#### 方案总览

将现有"单文件全量缓存"拆为**两个独立缓存文件**，各自独立失效、独立复用：

```
~/.vault_mcp_cache/
├── chunks/                      # 文本切块层（与模型无关）
│   └── vault_<CANON_KEY>.chunks.bin
└── vectors/                     # 向量层（与模型绑定）
    └── vault_<CANON_KEY>.<MODEL_HASH>.<DIM>.vec.bin
```

#### 存储位置与命名

| 项 | 设计 |
|---|---|
| 根目录 | `cache.dir`（默认 `~/.vault_mcp_cache`），跨会话/Agent 共享 |
| 规范化 key | `CANON_KEY = sha256(normcase(realpath(vault_path)))[:16]` —— 统一大小写（Windows）、解析符号链接、去掉尾部分隔符，消除 R1 |
| 可选手动覆盖 | 新增配置 `cache.id`：用户可显式指定稳定标识（如 `"main-vault"`），彻底脱离路径依赖 |
| chunks 层 | `vault_<KEY>.chunks.bin`，meta = {canon_key, chunk_size, chunk_overlap} |
| vectors 层 | `vault_<KEY>.<model_hash>.<dim>.vec.bin`，meta = {model, dimension, mode}，`model_hash = sha256(model)[:8]` |

#### 更新策略（增量，避免重复劳动）

1. **chunks 层**：文件内容 sha256 签名不变 → 不重新切块、不写盘；新增/变更文件 → 只对变更文件切块并追加；删除 → 移除对应条目。参数 `chunk_size`/`chunk_overlap` 变更 → 仅 chunks 层失效重建。
2. **vectors 层**：chunk 文本不变且 embedding 配置不变 → 直接加载向量；模型/维度变更 → **只重算向量**，chunk 文本从 chunks 层复用（消除 R3/R5）。
3. 并发 embedding 沿用 `embedding_max_workers`。
4. 写入原子化：沿用现有 `tmp 文件 + replace` 模式，防崩溃损坏。

#### 失效处理

| 触发场景 | 处理 |
|---|---|
| 文件内容变化 | chunks 层增量更新 → 变更文件重算向量 |
| `chunk_size` / `chunk_overlap` 变更 | 仅 chunks 层重建 → 全量重向量化 |
| embedding 模型 / 维度变更 | 仅 vectors 层重建（chunk 文本复用） |
| vault 路径变化 | 规范化 key 后命中不同缓存文件，旧文件保留（可配 `cache.max_age_days` 自动清理） |
| 缓存文件损坏 | 解析失败自动视为无缓存，重新构建（现有 try/except 已覆盖） |
| 显式全量 | `kb_rebuild` 删除该 vault 两层缓存后重建 |

#### 测试污染隔离（消除 R4）

- 测试代码强制使用独立缓存目录（如 `tmp_path / "cache"`，现有测试已部分这样做）；
- 或在 pytest 配置中设置环境变量 `VAULT_MCP_CACHE_DIR=<tmp>`，使测试缓存永不写入用户真实缓存目录；
- 一次性清理历史幽灵缓存：删除 `~/.vault_mcp_cache/` 下指向 pytest 临时目录的条目（文件内 meta.vault 含 `pytest-of-` 即删）。

#### 跨 Agent 共享约定（消除 R6）

- **共享前提**：所有 Agent 使用**相同的 `cache.dir` + 相同的 embedding 模型/维度配置**。把 `app.toml` 视为共享配置（如放固定路径 + 环境变量注入 key），不要每个 Agent 各写一份。
- **建议**：引入 `cache.namespace` 配置（默认 `"default"`），企业/多项目场景可隔离；个人场景统一保持 `"default"` 即可全共享。

### 5.4 落地实现（2026-08-08）

**改动文件**：`config.py` / `indexer.py` / `config/app.toml`（备份于 `E:\Softwares\WorkCache\2026-08-08-17-37-58\bak\`）

1. **`config.py`**：`CacheConfig` 新增 `namespace`（默认 `"default"`）、`id`（可选显式缓存 ID）、`max_age_days`（可选自动清理）三个字段及校验。
2. **`indexer.py`**：
   - 缓存拆两层：`chunks/`（文本层，meta=key+chunk_size+chunk_overlap）+ `vectors/`（向量层，meta=key+model+dimension），各自独立失效。
   - `_cache_key()`：优先 `cache.id`（显式，免疫路径差异）；否则 `normcase(realpath(vault))` 规范化哈希（Windows 大小写/符号链接/尾分隔符不再导致失配）。
   - `_VectorsCodec`：向量层二进制编解码（chunk id → float32 embedding，zlib）。
   - `_sync_locked`：文本层总是更新（embedding 失败也保留文本，词法可搜）；`_embed_missing()` 只对**缺向量的 chunk** 重新 embedding——换模型/维度时文本层复用、只重算向量。
   - `_save_chunks_cache` 保存时剥离 embedding（`_strip_embedding`），确保向量只存向量层。
   - `rebuild()` 删两层缓存；`stats()` 增加 `cache_key` / `cache_namespace` 字段。
3. **`config/app.toml`**：新增 `[cache]` 段（`namespace` / `id` / `max_age_days` 注释示例）。

**验证**（全 PASS）：
- 分层缓存专项 16 项：首次建库落盘、重启零调用、**换维度只重算向量（文本复用）**、路径写法差异命中同一缓存、`cache.id` 派生 key、增量只处理变更文件。
- 项目原测试 26 项全通过（3 个测试断言随新行为/新结构更新：embedding 失败保留文本、增量旧内容断言、缓存路径断言）。
- 真实 API 端到端：首次建库 0.5s、重启复用 **0.00s**、换维度 0.3s（仅重算向量）、检索正常。

**效果**：换模型/维度不再全量重分析（文本层复用）；路径写法差异不再失配；跨 Agent 共享同一 `namespace` 即命中同一缓存；测试缓存与真实缓存隔离。

### 5.5 落地成本评估

| 改动 | 涉及 | 工作量 |
|---|---|---|
| 缓存 key 规范化 + `cache.id` 支持 | `indexer.py` `__init__` / `_cache_root` / `_cache_meta` | 小 |
| 拆分为 chunks/vectors 两层 | `_CacheCodec` 扩展 + 新增 vectors 编解码 + `sync()` 流程拆分 | 中 |
| meta 分层失效逻辑 | `_load_cache` / `_save_cache` 改造 | 中 |
| 测试隔离 + 幽灵缓存清理脚本 | `tests/conftest.py` + 一次性脚本 | 小 |
| `cache.namespace` / `cache.max_age_days` | `config.py` + `indexer.py` | 小 |

预计收益：**换模型/维度不再触发全量重分析（chunk 文本复用）；路径写法差异不再导致缓存失配；测试不再污染真实缓存**。这正是"缓存无法持久保存"问题的完整解法。

---

## 附：变更记录

| 日期 | 内容 |
|---|---|
| 2026-08-04 | 项目搭建：四模块结构、静态 embedding、stdio MCP 骨架、基础测试 |
| 2026-08-07 | 磁盘缓存、增量同步、并发 embedding、多库/子库、kb_rebuild |
| 2026-08-08 | 管线修复：词法软信号+bigram 分词、rerank 默认开启、numpy 语义加速、维度 1024、chunk_overlap 修复、缓存 meta 补全；备份于 `E:\Softwares\WorkCache\2026-08-08-17-37-58\bak\` |
| 2026-08-08 | 分层持久化缓存落地：chunks/vectors 两层独立失效、缓存 key 规范化（normcase+realpath / cache.id）、换模型只重算向量、namespace/max_age_days 配置、测试隔离；备份于 `bak\*.cachefix.*` |
| 2026-08-30 | 0.5.0（分支 `feat/embedding-resilience-and-search-upgrades`）：embedding 重试/退避/切批、failed_files 持久化、检索过滤+分页+内容去重、库级权重+分组 fan-out、图片 alt/图注注入（opt-in）、Windows 原生目录监听（fsnotify）、kb_export/kb_import 快照迁移、RRF/rerank 参数可配置；默认配置升级 0 次重嵌 |
