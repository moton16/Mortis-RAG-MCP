import re
from pathlib import Path

from mortis_rag_mcp.server import SERVER_INFO


def _pyproject_version() -> str:
    text = Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml 里找不到 version"
    return m.group(1)


def test_server_info_matches_pyproject():
    assert SERVER_INFO["version"] == _pyproject_version()


def test_user_changelog_has_current_version():
    text = Path(__file__).resolve().parents[1].joinpath("CHANGELOG_user.md").read_text(encoding="utf-8")
    assert f"## [{SERVER_INFO['version']}]" in text, "CHANGELOG_user.md 缺少当前版本条目"
