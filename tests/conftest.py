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
    # Low-priority legacy override keeps default registry in this test's sandbox,
    # while tests of new/old override precedence can still set either variable.
    registry = cache_dir / "vaults.toml"
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(registry))
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


# --------------------------------------------------------------------- stdio 助手
# C66「优先使用已就绪索引、后台刷新」之后，kb_search 不再在前台阻塞等待首建：
# 冷启动返回 status="indexing"，首建/刷新在飞时带 indexing_in_progress 并返回
# 当前可见的部分结果，真实客户端按 retry_after 稍候重试。
# 批式 stdio 会话（一次性喂完 stdin）表达不了「稍候重试」，且快速连发 N 条请求
# 也等不到后台线程推进（实测 50 条连发仍在首建中）。这里提供交互式轮询会话：
# 逐条发请求、逐条读应答、带真实间隔，直到判据成立或超时。

_STDIO_POLL_INTERVAL = 0.15
_STDIO_POLL_TIMEOUT = 60.0


def run_stdio_polling(
    config_path,
    prefix_requests: list[dict],
    poll_request: dict,
    is_settled,
    *,
    followup_requests: list[dict] | None = None,
    interval: float = _STDIO_POLL_INTERVAL,
    timeout: float = _STDIO_POLL_TIMEOUT,
    env_overrides: dict | None = None,
) -> dict:
    """在一条 stdio 会话里轮询 poll_request，直到 is_settled(data) 或超时。

    返回 {"prefix": [原始响应...], "observed": [轮询响应体...], "followups": [原始响应...]}
    —— prefix / followup 保留完整 JSON-RPC 包络（调用方常需检查 result.isError），
    轮询部分只给已解析的响应体，最后一个即判据成立的那次。
    followup_requests 在判据成立后于**同一条会话**内按序发出（索引此时已就绪，
    结果确定），用于承载后续断言。退出时等待子进程收尾并断言 stderr 为空。
    """
    import json as _json
    import queue as _queue
    import subprocess as _sp
    import sys as _sys
    import threading as _threading
    import time as _time

    proc = _sp.Popen(
        [_sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config_path)],
        stdin=_sp.PIPE,
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, **(env_overrides or {})},
    )
    lines: "_queue.Queue[str | None]" = _queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    _threading.Thread(target=_reader, daemon=True, name="stdio-reader").start()

    def _next_raw(wait: float) -> str:
        try:
            item = lines.get(timeout=wait)
        except _queue.Empty:
            raise AssertionError(f"stdio 会话在 {wait:.1f}s 内无应答")
        if item is None:
            raise AssertionError("stdio 会话提前结束")
        return item

    def _send(payload: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(_json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    observed: list[dict] = []
    prefix_responses: list[dict] = []
    followup_responses: list[dict] = []
    try:
        for req in prefix_requests:
            _send(req)
        for _ in prefix_requests:
            prefix_responses.append(_json.loads(_next_raw(timeout)))
        next_id = max(int(r.get("id", 0)) for r in prefix_requests) + 1
        deadline = _time.monotonic() + timeout
        settled = False
        while _time.monotonic() < deadline:
            _send({**poll_request, "id": next_id})
            resp = _json.loads(_next_raw(interval + 5.0))
            data = _json.loads(resp["result"]["content"][0]["text"])
            # 保底：即便判据一直不成立，也把已观测结果带给调用方自行裁量
            observed.append(data)
            next_id += 1
            if is_settled(data):
                settled = True
                break
            _time.sleep(interval)
        if settled and followup_requests:
            for req in followup_requests:
                _send({**req, "id": next_id})
                followup_responses.append(_json.loads(_next_raw(interval + 10.0)))
                next_id += 1
        if proc.stdin is not None:
            proc.stdin.close()
    finally:
        try:
            proc.wait(timeout=15)
        except _sp.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    assert not stderr, stderr
    return {"prefix": prefix_responses, "observed": observed, "followups": followup_responses}


@pytest.fixture
def stdio_polling():
    """交互式 stdio 轮询会话助手（见 run_stdio_polling）。"""
    return run_stdio_polling
