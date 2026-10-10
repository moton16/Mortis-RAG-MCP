# 检索与精读

需要过滤、分页、消歧或处理陈旧索引时读本页。媒体与管理操作分别见入口链接，参数以实际 schema 为准。

## 定向与候选

- `vault_path` 单库、`vault_paths` 指定多库、都省略为全局；不要同时传两种选库参数。solo 只排除于无目标全局检索，显式定向可以读。
- `path_prefix` 是库内相对 POSIX 路径前缀，不是库名或绝对路径。用用户给出的真实目录，未知所属库才查 `kb_list`。
- `tags` 匹配任一标签（OR）；`mtime_after/mtime_before` 接受 epoch 秒或 ISO 8601 字符串，过滤的是修改时间，不是事件发生时间。
- `exact_terms` 不区分大小写、全部须匹配（AND），最多 8 项、每项 100 字符。适用于查询词被语义泛化的专名；不必把整段问题拆成硬包含词。
- frontmatter `aliases` 支持简称召回；原生 `.md/.markdown/.txt` 都可搜。只在需要已知文件清单时用 `kb_list_files`，不是查询前必做。
- 默认去重会合并相同正文；需要比较副本/跨库重复才用 `dedupe=false`。初筛关闭 rerank 是选择，不代表此后精排也应关闭。

```json
{"name":"kb_search","arguments":{"query":"DMA 中断说明","vault_paths":["数电","微机"],"exact_terms":["DMA"],"tags":["复习"],"preview":true,"top_k":5,"budget_bytes":6000}}
```

## 投影与回读

| 投影 | 定位字段 | 回读 |
|---|---|---|
| `compact=true`（隐含 preview） | `source/heading/lines/snippet`；无 ID | 所属库 + source + lines 的两个物理行号 |
| `preview=true` 或 `mode="preview"` | `id/source/start_line/end_line` 与摘要 | 命中 `id` → chunk_id，或 source + 行区间 |
| 默认 full | ID、正文、metadata | 只对缺失上下文再读，不重复拉全篇 |

单库明确查询可沿用原库参数；全局/分组结果从命中或 group 的 `vault` 取归属。
`kb_read(chunk_id=...)` 不同时传 heading/start_line/end_line；跨库 ID 碰撞、未完成首扫或版本失效时按错误定向重搜，不转成无目标猜测。

```json
{"name":"kb_read","arguments":{"chunk_id":"从preview或full命中id复制","vault_path":"微机","expand_lines":20}}
```

物理源可用双链短名（含 `[[名称]]`）寻址，但同名文件/同名章节仍须消歧。
`heading` 读取该节及子节，直到下一个同级/更高级标题；遇到同名标题，用返回的候选起始行定位（同时给行范围时行范围优先）。
行号越界从错误 `actual: N` / `total_lines` 修正，不猜分卷偏移。

```json
{"name":"kb_read","arguments":{"source":"[[计算机网络]]","vault_path":"课程笔记","heading":"传输层"}}
```

## 三类游标各走各的路

1. **搜索列表**：`offset/limit`；返回 `next_offset` 时沿它续页。`top_k` 不是永远固定的页长，也不推算截断后的实际返回条数。
2. **分组搜索**：`group_by_vault=true`；原样把 `group_next_offsets` 作为下一次 `group_offsets`。保持原来的 scoped/global 目标、查询、过滤和排序；不切为单库继续旧游标。
3. **正文**：`truncated=true` 时把 `next_start_line` 传为 `start_line`，`next_start_char` 传为 `start_char`。字符偏移为该行内 0-based Unicode 偏移，长单行也能续读；不要用 `end_line + 1`。

```json
{"name":"kb_search","arguments":{"query":"总线","vault_paths":["数电","微机"],"group_by_vault":true,"group_offsets":{"数电":2,"微机":0},"compact":true,"budget_bytes":6000}}
```

```json
{"name":"kb_read","arguments":{"source":"小说/卷一.txt","vault_path":"小说库","start_line":697,"start_char":1200,"end_line":720}}
```

示例中的游标/ID/行号需替换为实际响应值；媒体引用分页是另一套 `media_refs_offset`，见媒体参考。

## 预算、后台状态与证据

- 搜索 `budget_bytes` 是 UTF-8 输出预算（500–100000），不是字符数/token 数。`returned=0, truncated=true` 或 `budget_exceeded` 先改投影、范围、预算；使用 `minimum_budget_bytes` 提示，不以相同参数自旋。
- 后台首次构建的空结果与真实未命中不同；`status="indexing"` 看 `progress/retry_after`，不要无间隔探测。已有索引可在刷新中返回，检查 `indexing_error/index_state/next_action`，不把局部结果当作全库已完备。
- `kb_read` 是原文直读，不调用 embedding、不主动同步；已知 source 可直接读，不必先等待搜索索引 ready。
- 源已变更使 chunk_id 失效时重新搜索；物理文本可按 source/heading 读当前正文。virtual 的旧存档只有任务明确需要时才 `allow_stale=true`，并标明存档而非当前源。
- `kb_stats` 可查看 `failed_files/skipped_unsupported/pending_paid_requests` 等实际状态，但不是每次查询的前置探活，也不通过紧密反复调用它修复失败。
- 回答引用库 + source + 行号；virtual 补 revision，原生媒体补 occurrence。区分原文、摘要/图注、推断与尚未读取的内容。
