# CLAUDE.md

项目核心规范与 AI Agent 协作法则详见 [AGENTS.md](AGENTS.md)。

## 核心纪律（所有开发者与 Agent 必须遵守）

1. **文档分工绝对铁律**：
   - `CHANGELOG_user.md`：**终端用户更新日志**。只在发 release 时改；必须用大白话讲新增/移除的功能与体验优化，**严禁出现任何技术术语、函数名、底层锁或代码细节**！
   - `docs/Changelog_developer.md`：**每次 commit 即记账的技术流水**。必须标明用户名、日期、Agent、底模，记录技术细节。
   - `docs/PROJECT_GUIDE.md`：系统完整技术说明书与第十五节版本变更详录。
   - `docs/Quick-start_developer.md`：开发者 30 秒上手指南。
2. **零第三方运行时依赖**：`pyproject.toml` 中 `dependencies = []`。
3. **门禁与测试**：提交前必须保证 `python -m pytest` 全绿，动检索必须保证 `python scripts/eval_search.py --golden tests/eval/golden_queries.json` Hit@5 维持基线。
