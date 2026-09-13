---
name: mortis-rag-mcp
description: "调用 Mortis'RAG MCP 检索本地知识库。触发词：搜知识库、查笔记、kb_search、vault 检索、RAG 搜索、mortis rag。"
version: 5.0.0
---

# mortis-rag-mcp 检索路由（0.7.0）

连接即读 server 的 `instructions`（路由纪律已内嵌）。本文件只补判定表与反模式。

## 检索路由判定表（按序匹配，命中即执行）

| # | 条件（判定信号） | 动作 |
|---|---|---|
| 1 | 用户提到库名/课程名/文件夹名/主题目录（如"数电""教材"） | 先 `kb_list` 匹配 `name`/`description` → 命中则 `kb_search` 必须带 `vault_path`；指向子目录再加 `path_prefix='目录/'` |
| 2 | 问题是明确概念/术语/人名/缩写 | 直接 `kb_search(query, top_k=5)`；多库时优先按 #1 定向 |
| 3 | 问题模糊、探索性（"我最近学过什么""哪都可能有"） | 不传 `vault_path` fan-out，用结果里的 `vault` 字段判断来源，第二轮按 #1 定向收窄 |
| 4 | `kb_search` 返回带 `hint` | 读它。它在告诉你上次检索太宽，下次定向 |
| 5 | 命中后要看原文 | `kb_read(source, vault_path, start_line, end_line)`，禁一次拉全篇 |

## 反模式（禁止）

- 禁止在条件 #1 命中时省略 `vault_path`"先看看"——定向永远先于试探。
- 禁止对 solo 库（私密/试验）做无 `vault_path` 的全局搜并声称"没搜到"。
- 禁止用 `kb_rebuild` 修少量失败文件；正确姿势是反复 `kb_stats`/`kb_search` 触发增量 sync。
  `kb_rebuild` = 全量重嵌（几百~几千次 API 调用，免费档必爆限流），仅在换模型/维度时用。

## 工具一句话清单（细节以工具自带 schema 为准，此处不抄）

kb_search（检索）/ kb_read（读原文）/ kb_list（列库+description）/ kb_describe（设库描述）/
kb_init / kb_init_solo（私密库）/ kb_remove / kb_list_files / kb_stats（健康看 failed_files）/
kb_set_weight / kb_exempt（豁免私密）/ kb_rebuild（高危，见上）/ kb_export / kb_import / kb_ingest（PDF 摄取，默认关闭）

## 管理机制

混合检索 = FTS5 BM25 + 向量余弦 + bigram 词法三路 RRF + rerank。2 字中文与短英文缩写有兜底，正常搜即可。
配置链：`--app-config` > `VAULT_MCP_CONFIG` > `~/.vault_mcp/config.toml` > 内置默认。
PDF 摄取层默认关闭：`kb_ingest` 报 disabled 时，引导用户在 config/app.toml 设 `[ingest] enabled=true` 重启后用。
