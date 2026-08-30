---
name: vault-mcp
description: "调用 Mortis'RAG MCP（vault_mcp）检索知识库（语义搜索/读原文/查索引/注册库）。触发词：搜知识库、查笔记、kb_search、vault 检索、RAG 搜索。"
version: 3.0.0
---

# vault-mcp（知识库检索，0.4.0 混合检索 + 磁盘向量后端）

连接器：MCP `mortis-rag-mcp`（stdio），server 0.4.0。**9 个工具**。不绑定任何路径：任意文件夹经 `kb_init` 注册为知识库，注册表持久化在 `~/.vault_mcp/vaults.toml`（跨重启/换设备可用）。

## 工具速查

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `kb_init` | **注册任意文件夹为知识库**（后台建索引+启动监听） | `path`(必, 绝对路径), `name`(可选) |
| `kb_unregister` | 注销知识库 | `path`(必), `purge_cache`(默认false) |
| `kb_search` | **核心**：0.4.0 起三路 RRF 混合检索（FTS5 BM25 + 向量余弦 + bigram 词法）；**多库时不传 vault_path = 跨全部注册库 fan-out** | `query`(必), `top_k`(默认10), `use_rerank`(默认true), `vault_path`(可选) |
| `kb_read` | 读原文（不调 LLM）；**fan-out 后的 source 是库内相对路径，必须带 vault_path** | `source`(必), 或 `heading` / `start_line`+`end_line`, `vault_path` |
| `kb_list` | 列出已索引文件 | `vault_path`(可选) |
| `kb_stats` | 索引状态：文件/切片数、模型、缓存、**use_hybrid/fts_enabled/vector_backend** | `vault_path`(可选) |
| `kb_vaults` | 列出**已注册**知识库（含 exists/indexed/files/last_sync） | 无 |
| `kb_exempt` | RAG 豁免管理（排除私密/草稿/备份副本内容） | `action`(必): list/add_pattern/remove_pattern/exempt_file/unexempt_file/check |
| `kb_rebuild` | 删缓存强制全量重建 | 无（**高危**，见下方注意） |

## 调用模式

```json
{"name":"kb_search","arguments":{"query":"检索词","top_k":5}}
{"name":"kb_init","arguments":{"path":"D:\\某笔记库","name":"我的笔记"}}
{"name":"kb_search","arguments":{"query":"检索词","vault_path":"D:\\某笔记库"}}
```

## 路由决策

1. **vault_path 必须是已注册库的绝对路径**；未注册 → 先 `kb_vaults` 看列表，需要新库就 `kb_init`。
2. **不传 vault_path**：单注册库 → 直接搜它；多注册库 → 自动跨库 fan-out（结果带 `vault`/`vault_name` 字段，告诉用户命中了哪个库）。
3. **目标明确时优先带 vault_path 定向搜**（精度高），模糊/跨库直接不传让它 fan-out。

## 混合检索（0.4.0，默认开启）

- `[index] use_hybrid = true`：FTS5 BM25（原生 SQLite trigram，中文可用）+ 向量余弦 + bigram 词法三路 RRF 融合（k=60，每路 top-40）→ rerank top-60 → top_k。`false` 完整还原旧行为。
- **2 字中文查询（如「银狼」）trigram 无法命中**（<3 字符），由 bigram 词法路自动兜底——正常搜即可，无需特殊处理。
- FTS 索引依赖 `[cache] enabled = true`（索引文件在缓存目录 `fts/vault_<key>.fts.sqlite`）；缓存关闭/索引缺失/查询失败时自动降级，检索永不报错。
- 磁盘向量后端（可选）：`[vector] backend = "sqlite_vec"`（需 `pip install "mortis-rag-mcp[vec]"`），向量存磁盘不驻内存（13k 切片约省 55MB RAM），首次切换自动从旧缓存迁移、零重嵌；未安装/加载失败自动回退 memory。默认 `memory`（numpy 暴力扫描，零依赖）。

## 流程约定

1. 新环境/新文件夹 → `kb_init` 一次即可（自动后台索引+watcher 增量同步，无需 rebuild）
2. 检索用 `kb_search`，中文无需分词；命中后 `kb_read` 读原文（带 vault_path）
3. 索引缺失时 `kb_list` 查文件；少量失败靠反复 `kb_stats`/`kb_search` 触发增量 sync 补齐
4. **检索结果重复副本**（如 `*_Raw_Backup/`、`备份/` 目录）：用 `kb_exempt add_pattern` 加目录级规则（尾斜杠 = 目录规则，如 `教材_Raw_Backup/`），立即生效且可逆

## 注意

- **`kb_rebuild` 是删缓存的全量重建：会把每个文件的每个 chunk 全部重新 embedding（几百~几千次 API 调用）。外部 embedding（如硅基流动免费档）有 RPM 限流，rebuild 极易打爆限流 → failed_files 暴涨。**
- **修复少量失败文件的正确姿势：不 rebuild，靠增量 `sync()`**——每次调用 `kb_*` 工具都会自动触发 `sync()`（只重嵌签名变化的文件 + 只补缺向量的 chunk，幂等安全）。反复调 `kb_stats` 即可一轮轮补，失败有退避重试。**只有全库要重建（如换 embedding 模型/维度）才用 rebuild。**
- 真要 rebuild：先确认配置里 `embedding_max_workers` 并发 ≤ 2，前后留足限流窗口（至少几分钟），只跑一次，跑完 `kb_stats` 验证 failed_files 为空。失败文件可稍后靠 sync 补。
- 索引健康检查看 `kb_stats` 的 `failed_files`，不是 files/chunks 数。
- 配置链：`--app-config` > `VAULT_MCP_CONFIG` 环境变量 > `~/.vault_mcp/config.toml` > 内置默认；API key 统一回退 `VAULT_MCP_API_KEY`
- 私密/草稿内容用 `kb_exempt` 排除：单文件用 `exempt_file`（frontmatter rag: false），长期规则用 `add_pattern` 写 `.vaultignore`；`check` 查豁免原因
- 源码：仓库根目录（项目名 Mortis'RAG MCP，Python 包名 `vault_mcp`；纯标准库核心 + 可选 vec 依赖；测试 `python -m pytest tests/`）
