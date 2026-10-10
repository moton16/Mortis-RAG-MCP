---
name: mortis-rag-mcp
description: "调用 Mortis'RAG MCP 检索本地知识库。触发词：搜知识库、查笔记、kb_search、vault 检索、RAG 搜索、mortis rag。"
version: 0.9.0
---

# mortis-rag-mcp 检索路由（0.9.0）

连接即读 server 的 `instructions`（路由纪律已内嵌）。本文件只补判定表、检索纪律与反模式。

## 信任锚（开工前必读，优先级最高）

本机环境状态由 `~/.mortis_rag_mcp/STATUS.md` 权威记录（`python -m mortis_rag_mcp --doctor` 自动生成）。

- STATUS.md 标注 ✅ 且生成时间在 7 天内：**禁止任何预检**——不查 venv、不点依赖、不验证 key、不跑 kb_stats/kb_list 探活。直接按下方判定表调用工具干活。
- STATUS.md 缺失/过期/标注 ❌：不自动探活、不自动运行 doctor；按已有工具查询，实际报错再核本地配置/状态并报告原因。用户显式要求诊断时才运行 `python -m mortis_rag_mcp --doctor`，该命令可能请求真实端点。
- **熔断保护**：若运行一次 `--doctor` 后状态依然为 ❌，**禁止反复重试**，直接停止预检并向用户汇报失败项。
- kb_* 工具实际报错时：报错 > STATUS.md。依据错误与已有状态排障；不以历史凭证覆盖当前失败，也不默认重发 unknown 请求。

## 0.9 增量（0.9.0 已于 2026-10-09 发布）

- `kb_import` 的 `index_state=empty/rebuilding/unverified/ready` 与 import 成功分开；沿 `next_action` 核源/重解析，不自动信任包内正文。
- `kb_ingest(action="retry", job_id="实际ID", vault_path="库名")` 只对 failed/cancelled 重试，unknown 先查询原任务。取消后的重试重做失去 blob 保护的段，不伪称断点都可保留。
- `--list-requests --vault "绝对路径"` 只读记录；`--abandon-request REQUEST_ID --vault "绝对路径"` 仅放弃本机意图，不取消远端任务、不重发。现有配置启用的远端能力不增设审批系统。
- compact 仍按 source/行号回读；媒体沿 `kb_read` 的 `media_refs` 及固定 revision/offset 翻页，再用 `kb_read_media(source, revision_id, occurrence_id, vault_path)` 请求 metadata/inline。不要猜媒体地址或把 caption 当原生向量。
- 原生媒体 provider/index/read 内部路径已接通，且「输入文字直接命中库中图片」已在本机载体上验证；真实媒体端点的通用装配、单独一张图片直接入库、以及宿主客户端里的图片显示尚未验收。proxy 不替代 native。升级/回退见仓库 `QUICKSTART_user.md` §0.4。
- 音频转录 / 音频转码链路已在 0.9.0 物理移除（含 `[audio]` 段与 `audio_enabled`）；工具面与检索纪律不受影响。

## 检索路由判定表（按序匹配，命中即执行）

| # | 条件（判定信号） | 动作 |
|---|---|---|
| 1 | 用户提到具体库名/文件夹名/主题（如"数电"、"DateALive"） | **直接传库名**（无需拼漫长绝对路径）：`kb_search(query, vault_path="库名")`；指向子树再加 `path_prefix='目录/'` |
| 2 | 用户需要联合检索多个特定库（如"数电与微机"、"主库+某特定独立库"） | **Scoped 多库检索**：传库名数组 `vault_paths=["数电", "微机"]`；被点名的 solo 库会合法参与联合召回 |
| 3 | 大范围初步探索、泛搜或需要较多候选（`top_k >= 5`） | **开启紧凑初筛或预算**：优先 `compact=true, budget_bytes=3500, use_rerank=false`（精简结构化字段，避免转储）或 `preview=true`，按需获取切片与行号 |
| 4 | 专有名词、代码符号、人名代号易被语义泛化稀释或未进入 Top-K | **显式硬包含**：`kb_search(query, exact_terms=["专有名词", "代号"])`（AND 语义，全库扫描保底召回） |
| 5 | 问题模糊、探索性（"我最近学过什么""哪都可能有"） | 不传 `vault_path` 全局盲搜（自动跳过 solo 库），根据结果 `vault`/`vault_name` 研判，下轮按 #1 定向 |
| 6 | `kb_search` 返回 `status: "indexing"` | 知识库后台首次构建中。向用户汇报进度（`progress`），依据 `retry_after` 稍候或转战其他子任务，勿紧密自旋 |
| 7 | 搜索命中切片后需要精读上下文 | **切片原地展开（极力推荐）**：普通 preview/full 检索命中后，优先调用 `kb_read(chunk_id=c.id, expand_lines=30)` 原地展开，自动换算行号且防路径错误 |
| 8 | 读到笔记内的双链引用（如 `[[计算机网络]]`、`[[架构#模块]]`）想深泛 | **双链短名直读**：直接调用 `kb_read(source="计算机网络")` 或 `kb_read(source="[[计算机网络]]")` 自动消歧寻址 |
| 9 | 定位到特定章节或已知精确行号 | **区间精读或章节直读**：`kb_read(source, start_line, end_line, vault_path="库名")` 或带 `heading`，超 `read_max_chars` 自动安全截断 |

## 检索调用六项纪律（可判定 If-Then 规范）

1. **大候选初筛定向与预算**：已知目标库必须显式定向 `vault_path`；`top_k >= 5` 时优先开启 `compact=true, budget_bytes=3500, use_rerank=false` 进行轻量初筛，削减无效 payload；不作无依据的特定百分比节省假设。
2. **compact 结果回读规范**：`compact=true` 投影中**不包含 chunk_id**。后续必须使用结果中的顶层或 group 内 `vault` + `source` + 行区间字段调用 `kb_read` 进行回读——compact 投影的行区间在 `lines` 数组中（`start_line=c.lines[0]`、`end_line=c.lines[1]`），严禁对 compact 结果臆造或传递不存在的 chunk_id。对于普通 `preview=true` 或完整结果，仍保留 `chunk_id`，唯一命中时可直接读取。
3. **预算截断与续页纪律**：若 `budget_bytes` 导致返回 `returned=0` 且 `truncated=true`，不得使用完全相同的参数在原库无限重试；应改为启用 `compact=true`、增大 `budget_bytes` 或缩小目标库/目录范围。当遇到 `budget_exceeded` 时按提示的 `minimum_budget_bytes` 调整或缩减范围；多库分组检索触发截断时，必须将返回的 `group_next_offsets` 原样作为下一轮的 `group_offsets` 沿原路由续页，严禁中途切换为单库并沿用旧排序游标。
4. **原文读取越界与标题消歧**：`kb_read` 若发生行号越界，根据报错中返回的 `actual: N`（响应字段 `total_lines`）核对校正分卷行号；已知小节标题优先传 `heading` 读取完整章节；若提示 `存在歧义`（`N 处同名标题 (起始行: 42, 108)`），使用返回的候选物理行号带上 `start_line` 明确消歧；若读取因长度截断（`truncated=true`），后续读取必须使用 `next_start_line` 与 `next_start_char` 续读，不得简单沿用 `end_line + 1`。
5. **后台构建与陈旧状态应对**：`kb_search` 返回 `status: "indexing"` 或 `stale_chunks` 时，查看 `stats` 与 `retry_after` 建议；未经用户明确指令，**严禁自行调用 `kb_rebuild`**；若源文档已发生更新但后台未完成重新嵌入，可直接使用 `source` + `heading` 进行原文直读，无需等待或依赖陈旧 chunk 行号。
6. **文档自动摄取授权边界**：`[ingest] auto_watch` 与 `enabled` 默认关闭。未获用户明确许可前，严禁擅自修改配置文件启用自动监听或手动提交摄取任务；`kb_ingest` 的 `pending`/`status` 探查仅为状态查看，不构成云端上传授权；遇到相同哈希的失败任务，须由用户确认后主动重试。

## 规范调用样例（真实参数格式）

大候选紧凑初筛（注意：布尔值必须为 JSON 小写 `true`/`false`）：
```json
{"query":"目标实体","vault_path":"小说库","path_prefix":"vol08","top_k":30,"compact":true,"budget_bytes":3500,"use_rerank":false}
```

紧凑结果回读行区间：
```json
{"source":"vol08/part03.txt","vault_path":"小说库","start_line":697,"end_line":720}
```

物理章节标题直读：
```json
{"source":"vol08/part03.txt","vault_path":"小说库","heading":"Chapter 907: Section Title"}
```

用户主动授权的文档摄取配置参考（仅供用户自行配置开启，无 key 时保留本地 fallback，不承诺云端公共额度永不变化）：
```toml
[ingest]
enabled = true
auto_watch = true
max_file_size_mb = 20
api_key = "${MINERU_API_TOKEN}"
```

## 反模式（禁止）

- 禁止在条件 #1 命中时省略 `vault_path`"先看看"——定向永远先于试探。
- 禁止费力拷贝 Windows 漫长物理路径传参——优先直接传人类可读库名（大小写不敏感自然解析）。
- 禁止一次性大范围拉取完整 content 倾倒进主上下文——初筛务必善用 `compact=true` 或 `budget_bytes`，避免触发宿主转储。
- 禁止对 `compact=true` 的结果尝试调用 `kb_read(chunk_id=...)`——compact 不返回 chunk_id，必须使用 source + 行号或 heading 回读。
- 禁止在返回 `returned=0` 且 `truncated=true` 时使用同参数自旋重试。
- 禁止在读到双链短名时因不知道具体目录路径而放弃——直接将短名作为 `source` 传入 `kb_read`。
- 禁止对 solo 库（独立私密库）做无目标的全局盲搜并声称"没搜到"——需在 `vault_path` 或 `vault_paths` 中显式点名。
- 禁止擅自修改 `[ingest]` 配置或自动上传解析文档——必须获得用户明确授权。
- 禁止用 `kb_rebuild` 修少量失败文件；正确姿势是反复 `kb_stats`/`kb_search` 触发增量 sync。
  `kb_rebuild` = 全量重嵌（几百~几千次 API 调用，免费档必爆限流），仅在换模型/维度时用。

## 工具一句话清单（细节以工具自带 schema 为准）

kb_search（检索，支持库名/vault_paths/preview/compact/budget_bytes/exact_terms）/ kb_read（读原文，支持chunk_id/行范围/heading章节）/ kb_list（列库+description）/ kb_describe（设库描述）/
kb_init / kb_init_solo（独立库）/ kb_remove / kb_list_files / kb_stats（看 skipped_unsupported/failed_files）/
kb_set_weight / kb_exempt（毫秒级豁免私密）/ kb_rebuild（高危，见上）/ kb_export / kb_import / kb_ingest（文档摄取，默认关闭）/ kb_read_media（固定版本媒体读取）

## 资产格式与管理机制

- **格式支持**：Markdown（`.md`/`.markdown`）与纯文本（`.txt` 小说/分卷资料）均原生索引；PDF/DOCX/PPTX/XLSX 本地解析需可选 docs 依赖；未收录格式在 `kb_stats` 的 `skipped_unsupported` 中列出。
- **混合检索**：FTS5 BM25 + 向量余弦 + bigram 词法三路 RRF + rerank。2 字中文与短英文缩写有兜底。
- **别名感知**：原生识别笔记 frontmatter 中声明的 `aliases` 别名（支持缩写/简称/中文逗号），无需正文重复提及即可精准检索召回。
- **配置链**：`--app-config` > `MORTIS_RAG_CONFIG` > `VAULT_MCP_CONFIG` > `~/.mortis_rag_mcp/config.toml` > `~/.vault_mcp/config.toml` > 内置默认。
- **PDF 摄取层**：默认关闭。`kb_ingest` 报 disabled 时，引导用户在 config/app.toml 设 `[ingest] enabled=true` 重启后用。
