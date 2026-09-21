---
name: mortis-rag-mcp
description: "调用 Mortis'RAG MCP 检索本地知识库。触发词：搜知识库、查笔记、kb_search、vault 检索、RAG 搜索、mortis rag。"
version: 5.2.0
---

# mortis-rag-mcp 检索路由（0.7.2）

连接即读 server 的 `instructions`（路由纪律已内嵌）。本文件只补判定表与反模式。

## 信任锚（开工前必读，优先级最高）

本机环境状态由 `~/.mortis_rag_mcp/STATUS.md` 权威记录（`python -m mortis_rag_mcp --doctor` 自动生成）。

- STATUS.md 标注 ✅ 且生成时间在 7 天内：**禁止任何预检**——不查 venv、不点依赖、不验证 key、不跑 kb_stats/kb_list 探活。直接按下方判定表调用工具干活。
- STATUS.md 缺失/过期/标注 ❌：**不要逐项手查**。运行（或请用户运行）`python -m mortis_rag_mcp --doctor`，一条命令重新探测并重写 STATUS.md。
- **熔断保护**：若运行一次 `--doctor` 后状态依然为 ❌，**禁止反复重试**，直接停止预检并向用户汇报失败项。
- kb_* 工具实际报错时：报错 > STATUS.md。进入排障，第一步仍是 `--doctor`。

## 检索路由判定表（按序匹配，命中即执行）

| # | 条件（判定信号） | 动作 |
|---|---|---|
| 1 | 用户提到具体库名/文件夹名/主题（如"数电"、"DateALive"） | **直接传库名**（无需拼漫长绝对路径）：`kb_search(query, vault_path="库名")`；指向子树再加 `path_prefix='目录/'` |
| 2 | 用户需要联合检索多个特定库（如"数电与微机"、"主库+某特定独立库"） | **Scoped 多库检索**：传库名数组 `vault_paths=["数电", "微机"]`；被点名的 solo 库会合法参与联合召回 |
| 3 | 大范围初步探索、泛搜或需要较多候选（`top_k >= 5`） | **开启预览模式**：`kb_search(query, ..., preview=True)`，体积压缩 70%+，获取高光摘要与精确行号锚点 |
| 4 | 问题模糊、探索性（"我最近学过什么""哪都可能有"） | 不传 `vault_path` 全局盲搜（自动跳过 solo 库），根据结果 `vault`/`vault_name` 研判，下轮按 #1 定向 |
| 5 | `kb_search` 返回 `status: "indexing"` | 知识库后台首次构建中。向用户汇报进度（`progress`），依据 `retry_after` 稍候或转战其他子任务，勿紧密自旋 |
| 6 | 定位到高价值目标片段后需要精读原文上下文 | **二段式精读**：`kb_read(source, start_line, end_line, vault_path="库名")` 提取切题段落，严禁无范围全篇硬拉 |

## 反模式（禁止）

- 禁止在条件 #1 命中时省略 `vault_path`"先看看"——定向永远先于试探。
- 禁止费力拷贝 Windows 漫长物理路径传参——优先直接传人类可读库名（大小写不敏感自然解析）。
- 禁止一次性大范围拉取完整 content 倾倒进主上下文——初筛务必善用 `preview=True`，后续由 `kb_read` 精准精读。
- 禁止对 solo 库（独立私密库）做无目标的全局盲搜并声称"没搜到"——需在 `vault_path` 或 `vault_paths` 中显式点名。
- 禁止用 `kb_rebuild` 修少量失败文件；正确姿势是反复 `kb_stats`/`kb_search` 触发增量 sync。
  `kb_rebuild` = 全量重嵌（几百~几千次 API 调用，免费档必爆限流），仅在换模型/维度时用。

## 工具一句话清单（细节以工具自带 schema 为准）

kb_search（检索，支持库名/vault_paths/preview）/ kb_read（读原文）/ kb_list（列库+description）/ kb_describe（设库描述）/
kb_init / kb_init_solo（独立库）/ kb_remove / kb_list_files / kb_stats（看 skipped_unsupported/failed_files）/
kb_set_weight / kb_exempt（毫秒级豁免私密）/ kb_rebuild（高危，见上）/ kb_export / kb_import / kb_ingest（PDF 摄取，默认关闭）

## 资产格式与管理机制

- **格式支持**：Markdown（`.md`）与纯文本（`.txt` 小说/分卷资料）均为原生一等公民，享受同等分块、章节标题感知与检索待遇；未收录格式在 `kb_stats` 的 `skipped_unsupported` 中透明列出。
- **混合检索**：FTS5 BM25 + 向量余弦 + bigram 词法三路 RRF + rerank。2 字中文与短英文缩写有兜底。
- **配置链**：`--app-config` > `MORTIS_RAG_CONFIG` > `VAULT_MCP_CONFIG` > `~/.mortis_rag_mcp/config.toml` > `~/.vault_mcp/config.toml` > 内置默认。
- **PDF 摄取层**：默认关闭。`kb_ingest` 报 disabled 时，引导用户在 config/app.toml 设 `[ingest] enabled=true` 重启后用。
