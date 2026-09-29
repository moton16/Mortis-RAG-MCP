# 执行方案（待执行功能与延后项）

> 本文件是**「还没做」的落点**：待执行功能的代码级方案、已识别但未排期的缺陷与加固项。
> 文档分工（见 `Quick-start_developer.md` §4）：`Changelog_developer.md` = 已完成的提交流水；
> `PROJECT_GUIDE.md` = 系统现状全貌；**本文件 = 未完成项**。
> 入库口径：`docs/*` 默认被 `.gitignore` 忽略，仅白名单放行 5 个主文档（含本文件）；
> 版本子目录 `docs/vX.Y.Z/`（版本计划、评审报告等草稿）**不进仓库**。

## 使用约定

1. 一条目 = 一个可独立落地的工作单元：**问题 → 证据（符号名优先，行号会腐烂）→ 触发条件 → 验收方式**。
2. 做完就地划掉：改为 `[x]` 并补一行「落地于 vX.Y.Z / 哪个 commit」；技术细节写进 `Changelog_developer.md`。
3. 新功能的**代码级方案**（改动点、测试清单、提交切分）直接写在本文件对应条目下，不另开文档。

## 一、未排期缺陷与加固（截至 v0.8.1 开工）

- [ ] **1. `_save_state` 剪枝会丢旧失败记录**
  - 问题：单库累计 job 超过 500 条时按时间剪枝，较早的 `failed` 记录被删除，「按 `submitted_at` 取最新状态」的判据在极端情况下失真。
  - 证据：`mortis_rag_mcp/ingest/worker.py::_save_state`。
  - 现状：v0.8.1 的自动摄取判据走独立函数按 `submitted_at` 绕开该缺陷，本体未修。
  - 验收：构造 >500 条历史 job 的单测，断言「最新状态」仍可解析。

- [ ] **2. `list_files()` 先构造全量列表再切片**
  - 问题：`kb_list_files` 的分页发生在全量构造之后，超大库（数万文件）单次调用仍要付全量内存与延迟。
  - 证据：`mortis_rag_mcp/indexer.py::list_files`（返回 `{source,title,chunks}` 摘要列表）。
  - 验收：为「只取前 N 条」加早停路径，并用大库夹具量测前后耗时。

- [ ] **3. FTS 在「只读探测」路径上可能写盘**
  - 问题：以只读探测为目的构造 `MarkdownIndexer(...)` 时，FTS sqlite 文件仍会被打开/回填。
  - 证据：`mortis_rag_mcp/indexer.py::_init_cache_paths` → `_fts_ensure_populated`。
  - 现状：v0.8.1 的跨库 `chunk_id` 探测加 `load_vectors=False` 只省掉向量层；文本层与 FTS 仍在。
  - 验收：只读探测不产生新的 sqlite 写入（断言文件尺寸/mtime 不变）。

- [ ] **4. 多平台 watcher**
  - 问题：`mortis_rag_mcp/fsnotify.py` 只有 win32（`ReadDirectoryChangesW`）实现，非 Windows 恒不可用并回落 `poll`。
  - 待办：Linux inotify / macOS FSEvents 后端；或至少在 `doctor` 里把「本机只能用 poll」显式说清。
  - 验收：平台条件跳过也要有实现与文档口径，且 poll 下的自动摄取节拍有测试锁定。

## 二、DX / 工程化候选（无承诺，按需认领）

- [ ] **5. 云端链路验收闸门**：MinerU 真机 smoke（`scripts/smoke_mineru.py` + `workflow_dispatch` 手动 job，**不进自动 CI**——单 job、零 secret、且烧付费额度）。v0.8.1 先把它降级为发版必填字段「实机验证：已通过（日期）/ 待实机验证（原因）」。
- [ ] **6. 首次上手时间（TTHW）压缩**：pipx / uv 一行安装；MCP 客户端配置片段生成器。
- [ ] **7. 首建索引进度反馈**：大库首次索引的可观测进度（当前只有 `kb_stats` 轮询口径）。
- [ ] **8. 周边建设**：文档搜索引擎、社区与外部信号渠道、DX 度量体系。

## 三、已上报但未顺手修的口径残留

- [ ] **9. `PROJECT_GUIDE.md` §2.3 关于 numpy 的注记已过期**：`accel` extra 已在 v0.8.0 落地，注记仍称「numpy 连 optional-dependencies 都没声明」。
- [ ] **10. `Quick-start_developer.md` §7 的 `SAFE_DELETE_FAIL_CLOSED` 描述与代码不符**：全库 `*.py` grep 零命中该字符串，`purge_cache` / `rebuild` 走 `Path.unlink()`；`tests/test_subvaults.py` 的 rebuild「已知环境红」真实成因待独立排查。

---

> 来源：v0.8.1 计划与三轮评审的 NOT-in-scope / P3 项，以及 v0.8.0 修复批次「偏差与上报」中明确「未顺手修」的条目。
