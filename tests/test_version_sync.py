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


def test_diaglog_package_version_matches():
    from mortis_rag_mcp.diaglog import PACKAGE_VERSION
    assert PACKAGE_VERSION == mortis_rag_mcp.__version__


def test_readme_badges_match_current_version():
    root = Path(__file__).resolve().parents[1]
    expected_version = SERVER_INFO["version"]

    readme_zh = root.joinpath("README.md").read_text(encoding="utf-8")
    m_zh = re.search(r'\[!\[Version: ([^\]]+)\]', readme_zh)
    assert m_zh, "README.md 缺少 Version badge"
    assert m_zh.group(1) == expected_version, f"README.md badge 版本 ({m_zh.group(1)}) 与当前版本 ({expected_version}) 不一致"

    readme_en = root.joinpath("README_EN.md").read_text(encoding="utf-8")
    m_en = re.search(r'\[!\[Version: ([^\]]+)\]', readme_en)
    assert m_en, "README_EN.md 缺少 Version badge"
    assert m_en.group(1) == expected_version, f"README_EN.md badge 版本 ({m_en.group(1)}) 与当前版本 ({expected_version}) 不一致"


def test_skill_package_version_matches():
    root = Path(__file__).resolve().parents[1]
    expected_version = SERVER_INFO["version"]
    skill_text = root.joinpath("skills", "mortis-rag-mcp", "SKILL.md").read_text(encoding="utf-8")

    m_title = re.search(r'#\s*mortis-rag-mcp\s*检索路由[（\(]([^）\)]+)[）\)]', skill_text)
    assert m_title, "SKILL.md 标题缺少版本标注"
    assert m_title.group(1) == expected_version, f"SKILL.md 标题中的包版本 ({m_title.group(1)}) 与当前版本 ({expected_version}) 不一致"

    m_fm = re.search(r'^version:\s*([0-9\.]+)', skill_text, re.MULTILINE)
    assert m_fm, "SKILL.md frontmatter 缺少 version"
