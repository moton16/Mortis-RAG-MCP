"""pytest 全局钩子与宿主隔离（v0.8.1 C54）。

隔离三件事，缺一不可：
1. **配置来源**钉在会话临时目录里的一真实文件上（杜绝读宿主 ~/.mortis_rag_mcp/config.toml）。
2. **缓存根**用 env 覆盖到临时区，且该短路发生在 resolve_default_cache_dir() 的改名逻辑之前
   （否则单跑一次测试就会触发宿主 ~/.vault_mcp_cache → ~/.mortis_rag_mcp_cache 的原子搬迁）。
3. **宿主状态写入**断开：测试不写 STATUS.md/status.json，并移除会压过用例自设旧名的宿主注册表变量。
"""
from __future__ import annotations


import os
import re
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_host(tmp_path_factory):
    """把「宿主」换成一个一次性会话环境，返回该环境目录。

    为什么 pin 的必须是**真实存在的文件**：resolve_config_path 对「env 指定但文件不存在」
    的路径会继续回落 ~/.mortis_rag_mcp/config.toml（宿主配置）——假 pin 会静默失效，还会把
    宿主的 external embedding 端点与真实 key 引进测试。
    为什么用 session 级单文件而非每用例 mktemp：避免加剧 pytest 临时目录碎片。
    为什么直接写 os.environ（而不是 function 级 monkeypatch）：pytest 只按依赖图与作用域
    排序，autouse 的 function 级 fixture 不会自动继承 session 级 fixture 的产物；写在这一层
    没有排序陷阱，且对子进程（stdio 用例以 {**os.environ} 传 env）同样生效。
    """
    iso = tmp_path_factory.mktemp("host_iso")
    cfg_path = iso / "app.toml"
    cfg_path.write_text(
        "# 测试会话级配置：只提供 [cache] 落点，其余键一律走内置默认（embedding=static）。\n"
        "[cache]\n"
        f'dir = "{(iso / "session_cache").as_posix()}"\n'
        "enabled = true\n",
        encoding="utf-8",
    )

    saved: dict[str, str | None] = {}

    def _set(name: str, value: str | None) -> None:
        saved[name] = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    _set("MORTIS_RAG_CONFIG", str(cfg_path))
    _set("VAULT_MCP_CONFIG", None)
    # 会话兜底缓存根：显式配了 [cache] dir 的用例不受影响；没配的走这里（再被下面的
    # per-test fixture 收窄到用例自己的目录）。
    _set("MORTIS_RAG_CACHE_DIR", str(iso / "cache_root"))
    _set("VAULT_MCP_CACHE_DIR", None)
    # 宿主导出的新名注册表会压过用例自设的旧名（registry 新名优先），历史上造成过
    # KeyError: 'description' 假失败 → 会话内移除，让各用例自己的隔离口径生效。
    _set("MORTIS_RAG_REGISTRY", None)
    # review §3.6/C54 补漏：legacy 宿主可能还在导出旧名变量，registry_path() 对旧名
    # 同样生效——只清新名会让迁移测试在模拟 legacy 宿主下走 dummy 注册表路径。
    # 两变量一并暂存/清除/恢复。
    _set("VAULT_MCP_REGISTRY", None)
    # 会话级封顶：测试不得把成绩写进宿主 ~/.mortis_rag_mcp/STATUS.md。**刻意不还原**——
    # pytest_sessionfinish 在 fixture teardown 之后才跑，还原会让守卫重新依赖「宿主恰好
    # 有 status.json」这条错误的封顶。
    os.environ["MORTIS_RAG_NO_STATUS_HOOK"] = "1"

    try:
        yield iso
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


@pytest.fixture(autouse=True)
def isolated_cache_dir(request, monkeypatch, isolated_host):
    """每个用例一个独立缓存根（env 覆盖），显式依赖 session 级 fixture。

    必须显式声明 isolated_host：pytest 只按依赖图排序，autouse 的 function 级 fixture 不会
    自动继承 session 级 fixture 的环境准备。目录放在会话隔离区（不在用例 tmp_path 内），
    避免与该用例自己构造的 `tmp_path/"cache"` 相互干扰。
    """
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", request.node.nodeid)[:80] or "node"
    cache_dir = isolated_host / "cache_root" / safe
    monkeypatch.setenv("MORTIS_RAG_CACHE_DIR", str(cache_dir))
    return cache_dir


def pytest_sessionfinish(session, exitstatus):
    try:
        if os.getenv("MORTIS_RAG_NO_STATUS_HOOK") == "1":
            return
        if getattr(session.config.option, "collectonly", False):
            # review §3.7/C54：--collect-only 不执行用例，0/0/0 的成绩没有观测价值，
            # 还会在模拟宿主里凭空写 status.json/STATUS.md/status.lock。
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
