"""pytest 全局钩子：跑完测试把成绩记入 STATUS.md。失败静默。"""
from __future__ import annotations


def pytest_sessionfinish(session, exitstatus):
    try:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = getattr(reporter, "stats", {}) if reporter else {}
        passed = len(stats.get("passed", []))
        failed = len(stats.get("failed", [])) + len(stats.get("error", []))
        skipped = len(stats.get("skipped", []))
        total_collected = getattr(session, "testscollected", passed + failed + skipped)
        from mortis_rag_mcp import doctor
        doctor.record_test_run(passed=passed, failed=failed, skipped=skipped, total_collected=total_collected)
    except Exception:
        pass
