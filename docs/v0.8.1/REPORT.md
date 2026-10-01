# v0.8.1 开工报告

> 2026-09-28 · 承接 v0.8.0 发版（PR #4 → merge `82989c8` → tag `v0.8.0` → [Release 已发布](https://github.com/moton16/Mortis-RAG-MCP/releases/tag/v0.8.0)），CI 五矩阵全绿。本报告记录发版过程中发现的遗留事项，作为 v0.8.1 的候选范围。

## 待办

### P0 — `test_gate_9_retryable_mineru_error` 偶发红灯（flaky）

- **现象**：main 合并后首跑 CI 失败（run 36440434091），`tests/test_adversarial_v070.py::test_gate_9_retryable_mineru_error` 断言 `queued + parsing >= 1` 失败（`{'failed': 2}`）；重跑即绿，代码树与绿着的 PR 分支完全相同，确认为偶发竞态。
- **根因**：`force=True` 重试后立即调 `mgr.status()`，worker 线程尚未把 job 从 `failed` 翻转回 `queued/parsing`（翻转与再次失败之间的窗口）。
- **修法建议**：把状态断言改为带超时的轮询等待（参考本分支 `test_stdio_wikilink_read` 竞态的修复思路），或注入可控的 worker 节拍。

### P1 — `docs/PROJECT_GUIDE.md` 转义污染清理（全量）

- **现象**：源文件里多处残留历史转义序列（如 `\[vector]`、`\_semantic_rank` 多余反斜杠，GitHub 渲染会显示成 `\[vector]`），§2.3 已在本版顺手清理，其余章节仍有残留（§3.2 标题、§4.5、§六/七多处表格与代码引用行）。
- **修法建议**：一次 grep `\\\[|\\_` 全量定位后批量还原为普通字符；纯文档 diff，无测试影响。

### P2（待定）— 开发日志副本文件

- `docs/Changelog_developer - 副本.md` 为历史副本，与正式版并存易混淆，建议确认后删除或移出仓库。

## 已闭环（无需再处理）

- ~~sqlite-vec 真实 vec0 分支未验证~~：v0.8.0 CI 全矩阵安装 `[vec]` extra 并通过，`_validate_vector_sqlite` 真实分支已被覆盖。
- ~~PROJECT_GUIDE §2.3 注记称 "numpy 未声明 extra"~~：已在 `27a2d82` 对齐现状（accel/vec 均为声明 extra），用户侧安装建议落 `QUICKSTART_user.md` §1。
- ~~kb_read 冷启动竞态~~：`_kb_read` 已补 `try_sync_with_guard` 守护，`test_stdio_wikilink_read` 已改为两段会话，CI 稳定复现路径已消除（`4345e9e`）。

## 验证方式

- P0：本地单文件 `pytest tests/test_adversarial_v070.py` + CI 连续多轮观察；不引入全量本地跑（遵守项目 pytest tmp_path 规则）。
- P1：渲染预览抽查 + grep 零残留。
