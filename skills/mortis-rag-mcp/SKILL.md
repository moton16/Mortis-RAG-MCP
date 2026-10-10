---
name: mortis-rag-mcp
description: "使用 Mortis'RAG MCP 搜索与读取本地知识库、查笔记和图片，或管理知识库、摄取 PDF/Office/图片、迁移索引。适用于 kb_* 工具调用，不代替网页搜索。"
metadata:
  version: "0.9.0"
---

# Mortis 知识库调用指南（v0.9.0）

以连接返回的 server instructions、实际工具 schema 和当前响应为准。普通查询直接调用工具；已有有效 `~/.mortis_rag_mcp/STATUS.md` 时不重复环境预检，缺失/过期也不自动探活。实际错误优先于旧状态；用户要求诊断时才运行一次 `python -m mortis_rag_mcp --doctor --vault "库的绝对路径"`（按需加 `--app-config`），它可能访问真实端点，失败后不循环重试。

## 选库与初筛

| 用户目标 | 调用选择 |
|---|---|
| 已知注册库 | `kb_search` 传 `vault_path`，可用库名或绝对路径；子目录另加库内 POSIX `path_prefix` |
| 多个指定库 | 传 `vault_paths` 数组；显式点名的 solo 库也参与 |
| 知道主题/文件夹但不知所属库 | 必要时 `kb_list` 看 name/description 后选库；主题名不自动等于注册库名 |
| 确需全局探索 | 省略库参数，自动排除 solo；依据结果归属收窄下一轮 |

候选较多时先 `compact=true, budget_bytes=3500`；需要摘要和切片 ID 时用 `preview=true`。初筛可选 `use_rerank=false`，精排按需要恢复，不固定关闭。专名/代码符号需要硬包含时用 `exact_terms`（AND）。标签、时间、分页和错误处理见 [检索与精读](references/retrieval.md)。

```json
{"name":"kb_search","arguments":{"query":"架构设计","vault_path":"技术笔记","path_prefix":"项目/","top_k":10,"compact":true,"budget_bytes":3500,"use_rerank":false}}
```

## 回读与续页

- compact 只有 `source/heading/lines/snippet`，没有 `id`。使用响应顶层或分组/命中归属中的库，以及 `source`、`lines[0:2]` 回读；单库结果也可沿用本次明确的库。
- preview/full 的命中 `id` 可传为 `kb_read(chunk_id=命中.id, vault_path=所属库, expand_lines=30)`；与行区间/heading 互斥。不跨库猜 ID，也不凭旧切片行号读取已改源文件。
- 已知章节用 `source + heading`；双链短名可作 `source`，歧义时按候选消歧。正文截断沿 `next_start_line + next_start_char`，而非 `end_line + 1`。
- 检索分页沿 `next_offset`；分组分页将 `group_next_offsets` 原样传回 `group_offsets`，保持路由/查询/过滤/排序不变。零条但截断时扩大预算、收窄范围或用 compact，不原参循环。
- `status="indexing"` 按 `progress/retry_after` 等待；索引/持久化异常沿 `index_state/next_action` 排查。少量失败用增量刷新，不为日常查询自动 `kb_rebuild`。

```json
{"name":"kb_read","arguments":{"source":"项目/架构.md","vault_path":"技术笔记","start_line":42,"end_line":70}}
```

## 图片、文档与管理：按任务加载

- **查看命中图片/图表**：阅读 [媒体与摄取](references/media-and-ingest.md)，含引用续页与版本核对。先 `kb_read` 取 `media_refs`，沿真实 source/revision/occurrence 调 `kb_read_media`；默认 metadata，图片内联用 `representation="inline"`，不是 `inline=true`。预算不足不等于图片不存在。
- **以文搜图/提交文件**：同一媒体参考说明摄取流程。v0.9.0 支持显式提交 PNG/JPEG/WebP（virtual），不自动扫描全库图片或做 OCR；原生跨模态向量与 caption/proxy 文本召回是不同能力。音频转录/转码已移除。
- **注册、豁免、迁移、配置或诊断**：阅读 [管理与恢复](references/maintenance.md)。导入成功不等于索引 ready；新库 token 估算切块与旧库字符兼容、模板/profile 复用条件都需保留。
- 文档/媒体上传、启用摄取、批量扫描、全量重建、导入替换和删除等变更应对应用户当前任务；查询不顺便改配置或启动这些操作。配置已启用且用户要求的正常能力不追加审批平台。失败/取消任务可显式 retry；结果 unknown 先核原任务/请求台账（或 CLI `--abandon-request` 放弃本机意图），不自动重新提交。部分外部宿主客户端富媒体展示尚未验收。

## 可复用任务提示

> 用 Mortis 搜索指定知识库中的目标；先定向、预算初筛，再按结果归属回读必要原文。正文或媒体只使用响应给出的真实定位与续页字段。回答注明库、source、行号或 revision，并区分未命中、索引未就绪、预算不足和调用失败；没有实际读到的内容不当作证据。
