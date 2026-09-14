"""pytest 全局钩子：跑完测试把成绩记入 STATUS.md。失败静默。"""
from __future__ import annotations


import os
from pathlib import Path


def pytest_sessionfinish(session, exitstatus):
    try:
        if os.getenv("MORTIS_RAG_NO_STATUS_HOOK") == "1":
            return
        if os.getenv("CI") or os.getenv("GITHUB_ACTIONS"):
            return
        if os.getenv("VAULT_MCP_REGISTRY") or os.getenv("MORTIS_RAG_REGISTRY"):
            return

        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = getattr(reporter, "stats", {}) if reporter else {}
        passed = len(stats.get("passed", []))
        failed = len(stats.get("failed", [])) + len(stats.get("error", []))
        skipped = len(stats.get("skipped", []))
        total_collected = getattr(session, "testscollected", passed + failed + skipped)
        from mortis_rag_mcp import doctor
        # 仅在宿主环境已有 status.json 时才更新观测测试成绩，严禁凭空在干净宿主目录造文件。
        # 注意这里刻意不走 doctor._status_json_path()：它内部调 registry.user_config_dir()，
        # 而后者把「旧目录 ~/.vault_mcp 原子改名」当作存在性检查的副作用执行——单跑一次
        # pytest 就会把开发者真实的数据目录搬走。这里直接拼新名路径，零副作用。
        if not (Path.home() / ".mortis_rag_mcp" / "status.json").is_file():
            return
        doctor.record_test_run(passed=passed, failed=failed, skipped=skipped, total_collected=total_collected)
    except Exception:
        pass
