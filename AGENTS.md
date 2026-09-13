# AGENTS.md — AI Agent 与开发者协作规范

本文件是所有进入本仓库工作的 AI Agent（WorkBuddy / Codex / Claude Code / Trae / Antigravity / Cursor 等）以及人类维护者的**强制行为法则**。任何改动必须严格遵守以下规则。

---

## 一、核心设计红线

1. **零第三方运行时依赖**：`pyproject.toml` 中 `dependencies = []`。HTTP 只用标准库 `urllib`，TOML 只用标准库 `tomllib`。不得为了省事引入任何第三方运行时轮子（测试/开发依赖除外）。
2. **不绑定任何个人路径**：代码与配置模板中严禁硬编码绝对路径。知识库关系由用户级注册表 `~/.vault_mcp/vaults.toml` 管理。
3. **检索永不报错，层层兜底**：任何网络超时、解析异常、模型降级均需在内部安全捕获并降级，绝不向用户或客户端抛出未处理的崩溃异常。

---

## 二、文档体系分工铁律（绝对红线，严禁串味）

本仓库文档分工极其明确，各司其职，**绝对禁止将技术实现细节写入面向用户的文档**：

| 文档路径 | 目标读者 | 更新时机 | 写作规则与内容约束 |
|---|---|---|---|
| **`CHANGELOG_user.md`** | **终端用户** | **仅在发布 release 时修改** | **【红线】只许使用普通用户看得懂的大白话！**<br>1. 只写**新增/移除了什么功能**、**优化了哪些使用体验**。<br>2. **严禁**出现任何技术术语、底层逻辑、代码行号、函数名/变量名（如 `iter_table_blocks`、`scandir`）、数据结构、底层系统锁、正则、内部评测脚本等。<br>3. 违背此条将直接被拒收打回。 |
| **`docs/Changelog_developer.md`** | **开发者 / Agent** | **每次 commit 必须同步追加** | **技术流水记账**。<br>1. 必须在开头标明：`用户名,日期,Agent名,模型底模`。<br>2. 详细记录该 commit 的代码改动逻辑、修复的根因、受影响的模块及验证结果。 |
| **`docs/PROJECT_GUIDE.md`** | **开发者 / 架构师** | **架构或版本变更时更新** | **全项目架构说明书**。<br>包含全系统数据流、并发模型、安全边界与[第十五节：版本变更详录]。代码级架构变更在此详述。 |
| **`docs/Quick-start_developer.md`**| **新加入的开发者/Agent** | **开发约定变动时更新** | **30 秒上手指南**。<br>涵盖架构分层、核心流程、测试约定与常见任务食谱。 |
| **`QUICKSTART_user.md`** | **初次部署用户** | **部署步骤变动时更新** | 保持“5 分钟跑通”的用户安装指引，不讲内部实现。 |

---

## 三、Commit 与版本发布规范

1. **提交信息规范**：遵循 Conventional Commits（`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`）。
2. **Commit 即记账**：每产生一次 commit，必须立即在 `docs/Changelog_developer.md` 追加对应的流水记录。
3. **发布门禁**：
   - 全量测试通过：`python -m pytest`（当前 231 项必须全绿）。
   - 检索回归测试：`python scripts/eval_search.py --golden tests/eval/golden_queries.json`（Hit@5 必须维持基线）。
   - 版本号一致性：`pyproject.toml`、`server.py`、`README.zh-CN.md`、`docs/PROJECT_GUIDE.md` 版本号必须完全同步。
