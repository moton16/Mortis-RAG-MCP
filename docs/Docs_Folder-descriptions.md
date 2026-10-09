# 说明
/docs 文件夹中只需要保留"Changelog_developer.md"、"PROJECT_GUIDE.md"、"Quick-start_developer.md"、"Docs_Folder-descriptions.md"，其他细化文档请放入对应版本的文件夹中，并显式ignore。
也即：作为开发者，你只需要阅读主文件夹中的这些文档即可了解项目全貌。而不必要阅读其他文档，若无需求，请不要这么做。
例外：`docs/v0.8.1/`（版本主计划 PLAN.md、实机报告、执行 Worklog）已按明确裁定作为当前施工版本的跟踪归档随版本入库（见 `.gitignore` 版本目录特例），不推广为全部历史或未来版本目录均自动获例外。

2026-10-08 实际规则：`docs/v0.9.0/` 仍命中 `docs/*`，规划/报告/任务卡不会跟随代码 commit。
执行窗口需沿用当前本机目录或显式复制 `docs/v0.9.0/beta2/`，按其中
`PLANNING_DELIVERY_MANIFEST.json` 核 SHA。不要未经批准改忽略或 force-add。
总体任务状态只在 `UNIFIED_EXECUTION_PLAN_2026-10-08.md`；四份 tracked 开发文档维护实际架构和技术流水，不建立第二份总计划。
离线评测使用说明在 `tests/eval/README.md`，运行证据/候选包位于被忽略的 `.runtime/`，均不应混进发布包。
