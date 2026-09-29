import re
from pathlib import Path

import mortis_rag_mcp
from mortis_rag_mcp.server import SERVER_INFO


def _pyproject_version() -> str:
    text = Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml 里找不到 version"
    return m.group(1)


def test_server_info_matches_pyproject():
    assert SERVER_INFO["version"] == _pyproject_version()


def test_package_version_matches_server_info():
    """C63：包顶层 __version__ 是单一真源，SERVER_INFO 与诊断日志都从它取。"""
    assert mortis_rag_mcp.__version__ == SERVER_INFO["version"]


def test_user_changelog_has_current_version():
    text = Path(__file__).resolve().parents[1].joinpath("CHANGELOG_user.md").read_text(encoding="utf-8")
    assert f"## [{SERVER_INFO['version']}]" in text, "CHANGELOG_user.md 缺少当前版本条目"
