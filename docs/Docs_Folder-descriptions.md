# 说明
/docs 文件夹中只需要保留"Changelog_developer.md"、"PROJECT_GUIDE.md"、"Quick-start_developer.md"、"Docs_Folder-descriptions.md"，其他细化文档请放入对应版本的文件夹中，并显式ignore。
也即：作为开发者，你只需要阅读主文件夹中的这些文档即可了解项目全貌。而不必要阅读其他文档，若无需求，请不要这么做。

2026-10-09 实际规则（E18 已用 `git check-ignore -v` 与 `git ls-files docs` 逐条核实，不凭印象）：
- 忽略规则是 `docs/*` 加四行白名单（`!docs/Changelog_developer.md`、`!docs/PROJECT_GUIDE.md`、`!docs/Quick-start_developer.md`、`!docs/Docs_Folder-descriptions.md`）。`.gitignore` 里**没有** `!docs/v0.8.1/` 之类的"版本目录特例"。
- 因此 `docs/v0.8.1/**` 与 `docs/v0.9.0/**` 全部命中 `docs/*` 被忽略，`git ls-files docs` 只返回上面四份文件；历史版本的计划、报告与 Worklog 都是**本机本地交付**，不会跟随代码 commit，需按要求显式复制到执行工作区。
- 执行窗口需沿用当前本机目录或显式复制 `docs/v0.9.0/beta2/`，按其中 `PLANNING_DELIVERY_MANIFEST.json` 核 SHA。不要未经批准改忽略或 force-add。
- 总体任务状态只在 `UNIFIED_EXECUTION_PLAN_2026-10-08.md`；四份 tracked 开发文档维护实际架构和技术流水，不建立第二份总计划。
- 文档分工：面向用户的能力与升级说明在根目录 `README.md` / `README_EN.md` / `QUICKSTART_user.md` / `CHANGELOG_user.md`（只讲功能增减与体验，不写技术细节）；技术流水与实现细节只在 `Changelog_developer.md` 与 `PROJECT_GUIDE.md`。
- 离线评测使用说明在 `tests/eval/README.md`，运行证据/候选包位于被忽略的 `.runtime/`，均不应混进发布包。
