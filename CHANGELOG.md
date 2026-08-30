# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[Semantic Versioning](https://semver.org/lang/zh-CN/)。提交信息为 Conventional Commits。

## [0.5.0] - 2026-08-30

> **升级兼容性：默认配置从 0.4.1 升级 = 0 次重新 embedding。** 各项改动只 bump 文本层
> 缓存代际号（秒级重建，向量按 chunk.id 全部命中）；唯一会触发全量重嵌的操作是手动
> 开启 `inject_image_captions`（它改变 chunk content → id，属已知的 opt-in 代价）。

### Added

- **embedding 韧性（C1）**：外部 embedding 请求支持重试 + 指数退避（`[embedding]
  max_retries = 3`、`retry_backoff = 1.0`，429 优先遵循服务端 `Retry-After`，其余
  4xx 不重试），并按 `batch_size = 32` 把长文件的切片切成多个请求——此前整文件塞
  单请求 + 零重试，一次限流即整批索引失败。
- **failed_files 持久化（C2）**：失败名单落盘为 `vault_<key>.failed.json`（原子写），
  进程重启后 `kb_stats` 仍能报告上一轮的失败原因；补嵌成功 / 文件修复 / purge /
  rebuild 时自动清理。
- **检索过滤与分页（C3）**：`kb_search` 新增 `path_prefix`（目录前缀，FTS 层 SQL 下推
  + 统一后过滤）、`tags`（frontmatter 标签，命中任一即可）、`mtime_after` /
  `mtime_before`（epoch 秒或 ISO 8601 字符串，闭区间）与 `offset` / `limit` 分页；
  chunk metadata 新增 `mtime`（内容最后一次变化的时间）。过滤在 rerank 之前执行，
  不浪费配额；跨库 fan-out 时过滤逐库生效、分页在全局合并后一次完成。
- **内容去重（C4）**：chunk metadata 新增 `content_hash`；embedding 阶段逐字重复的
  段落只请求一次 API，跨文件/跨库的相同内容直接复用已算出的向量；检索结果按
  `dedupe = true`（默认）保序去重，重复备份不再占掉 top_k。
- **库级权重与分组 fan-out（C5）**：注册表 v2 新增 `weight` 字段与 `kb_set_weight`
  工具（0 < w <= 100，老 toml 容错为 1.0）；跨库检索分数乘以库权重后再全局排序；
  `kb_search` 新增 `group_by_vault`，按库分组返回（组序按各组最高分降序）。
- **图片 alt/图注注入（C6，opt-in 默认关）**：`[index] inject_image_captions = true`
  后，在每张图片（标准 Markdown `![alt](path)` 与 Obsidian `![[path|图注]]`）所在行
  后插入 `[图片: alt 图注 (文件名)]`，让图片语义可检索；代码块内不注入，豁免内容
  不会被注入救活。开启会全量重嵌（见上）。
- **Windows 原生目录监听（C7）**：新模块 `fsnotify.py` 用 ctypes 直调
  `ReadDirectoryChangesW`（overlapped I/O，`CancelIo` 干净退出），零第三方依赖；
  `[index] watch_method = "auto"`（默认）时事件驱动替换 0.25s 全量轮询（空闲 0 CPU，
  毫秒级响应），防抖 5s 封顶防编辑器保存风暴饿死同步，watcher 线程死亡自动降级回
  轮询，30s 低频全量对账兜底（`watch_fallback_interval`，0 关闭）；`"poll"` 完整保留
  旧行为。
- **索引快照迁移（C8）**：`kb_export` 把 chunks + 向量 + FTS 三层缓存打包成 zip
  （含 manifest：格式版本 / cache key / 模型维度 meta / 统计），`kb_import` 在另一台
  机器按本机 cache key 落地并重写 meta——**导入后下一次 sync 0 次 embedding 调用**。
  zip 成员白名单校验（拒绝路径穿越名）、model/dimension 不一致拒绝（`force = true`
  仅导文本层并本地重嵌）。

### Changed

- RRF 每路候选宽度与 rerank 负载上限从硬编码常量改为配置（C0）：`[index]
  rrf_per_route = 40`、`rerank_cap = 60`，默认值与旧行为一致。
- 无变化的 sync 不再重写缓存文件（`[cache] placement = "vault"` 时避免缓存写入
  反复触发原生监听的自激循环，也省 IO）。
- `VaultMcpServer` 新增 `shutdown()` 并注册 atexit，嵌入式调用（测试/脚本）退出时
  释放原生监听的目录句柄。

### Fixed

- 轮询 watcher 的启动竞态：签名基线原先取在首次 sync 之后，「sync 完成到取基线
  之间」落盘的文件会被永久漏掉；基线改为先取、由 sync 补齐窗口期改动。
- 向量补齐成功后，旧的 failed_files 条目在全部三条分支都会被清除（此前「文件未
  变更」分支的重试成功不会清名单，持久化文件会永久撒谎）。

## [0.4.1] - 2026-08-30

### Fixed

- **安装兜底指引补全**：此前 `Cannot import 'setuptools.build_meta'` 的文档兜底只写了
  `--no-build-isolation`，对"setuptools 缺失/损坏"（Codex/Trae 等捆绑 Python 常见）的场景
  无效，用户被迫先修 pip 才能装包。现在三份文档（README 中英 / QUICKSTART）统一改为：
  `pip install --upgrade pip setuptools wheel` 修环境 → 重试 → 极端情况才降级
  `--no-build-isolation`；并明确"零依赖"指运行时依赖（`dependencies=[]`），
  setuptools 仅是 PEP 517 构建后端。

### Changed

- 配套 Skill 同步至 0.4.0 语义（v3.0.0）：三路 RRF 混合检索、2 字中文 bigram 兜底、
  `use_hybrid` / `[vector] backend` 配置文档、stats 新字段、重复副本豁免技巧。

## [0.4.0] - 2026-08-30

### Added

- **混合检索（三路 RRF 融合）**：`kb_search` 默认融合三条检索路——FTS5 BM25（原生 SQLite
  trigram 分词器，中文可用）、向量余弦、bigram 词法层；`[index] use_hybrid = false`
  可完整还原旧行为。RRF k=60、每路 top-40，融合后走既有 rerank top-60 → top_k。
  - 2 字中文查询（如「银狼」）trigram 无法命中（<3 字符），由 bigram 词法路天然兜底。
  - FTS 索引按库落盘（`fts/vault_<key>.fts.sqlite`），增量同步按 source 增删改；
    热缓存升级时自动按行数比对回填（`_fts_ensure_populated`）。
- **磁盘向量后端（sqlite-vec，可选）**：`[vector] backend = "sqlite_vec"` 把向量存进
  每库独立的 `vectors/vault_<key>.vec.sqlite`（vec0 + vid 映射，二进制序列化、批量
  upsert），`Chunk.embedding` 不再驻留内存——13,417 切片 × 1024 维实测常驻内存
  **52.4MB → 0MB**。首次切换自动从既有 `.bin` 缓存一次性迁移、零重嵌；缺失向量惰性
  重嵌自愈；热重启复用磁盘库。安装：`pip install "mortis-rag-mcp[vec]"`；
  未安装/加载失败自动回退 memory 后端。
- **向量抽象层**（`vault_mcp/vector.py`）：`VectorBackend` Protocol +
  `MemoryVectorBackend`（默认，行为零变化）+ `SqliteVecBackend`（可选）。
- `stats()` 新增 `use_hybrid` / `fts_enabled` / `vector_backend` 字段（纯增量，兼容旧客户端）。

### Fixed

- 热缓存升级后 FTS 索引永不构建（warm `.bin` 缓存下无"变更文件"，sync 不写 FTS）——
  按 FTS 行数 ≠ 切片数自动回填。
- sqlite-vec 后端误与 FTS 共用同一 sqlite 文件 —— 向量库改为独立路径。

### Performance

- 真实库实测（13,417 切片 / 685 文件，零 API 调用）：FTS BM25 查询平均 0.136ms；
  离线 RRF 全流程 3.6ms；磁盘后端迁移 15s（一次性）；检索延迟增量可忽略。

## [0.3.0] - 2026-08-30

### Added

- **用户级 Vault 注册表**（`~/.vault_mcp/vaults.toml`）：任意文件夹经 `kb_init` 注册为
  知识库，跨重启/换设备可用；`kb_unregister` 注销（可选 purge_cache）；
  `kb_vaults` 列出注册库（含存活状态）。
- **跨库 fan-out 检索**：多库时 `kb_search` 不传 `vault_path` 自动跨全部注册库检索，
  query 只 embed 一次、合并候选统一 rerank，结果带 `vault`/`vault_name`/`searched`/`errors`。
- **配置链**：`--app-config` > `VAULT_MCP_CONFIG` > `~/.vault_mcp/config.toml` >
  内置默认；API key 统一回退 `VAULT_MCP_API_KEY`；legacy `[vault].path` 首启自动迁移。
- 移除根库包含检查：`vault_path` 接受任意已注册绝对路径（注册表白名单取代旧 LFI 约束）。

### Changed

- 项目更名 **Mortis'RAG MCP**（包名保持 `vault_mcp`；控制台脚本 `mortis-rag-mcp`，
  旧 `vault-mcp` 兼容保留）；仓库公开于 `moton16/Mortis-RAG-MCP`。
- 全仓脱敏：源码/文档零个人路径；个人配置由 `.gitignore` 排除，分发仅含模板。
- 配套 Skill（`skills/vault-mcp/`）与快速开始（`docs/QUICKSTART.md`）入库。

## [0.1.0] / [0.2.0] - 2026-08-04 ~ 2026-08-08

### Added

- 纯标准库 MCP 服务骨架（stdio JSON-RPC，`initialize` / `tools/list` / `tools/call` / `ping`）。
- Markdown 切块（标题分段 + frontmatter tags）、静态哈希 embedding 兜底。
- 磁盘缓存（chunk 签名 + float32 向量二进制格式），增量同步按 sha256 签名跳过未变文件。
- 并发 embedding、rerank 精排（bge-reranker-v2-m3 免费）、子库支持、`kb_exempt` 豁免管理。
- 中文 bigram 词法分词（解决整串精确匹配退化）、numpy 批量余弦加速。
