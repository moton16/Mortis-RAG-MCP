# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[Semantic Versioning](https://semver.org/lang/zh-CN/)。提交信息为 Conventional Commits。

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
