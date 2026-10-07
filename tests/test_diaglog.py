from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, DiagConfig, EmbeddingConfig, load_config
from mortis_rag_mcp import diaglog
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.registry import VaultRegistry
from mortis_rag_mcp.server import SERVER_INFO, VaultMcpServer


@pytest.fixture
def test_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "my_vault"
    vault.mkdir()
    (vault / "note1.md").write_text("# Title\nThis is a test note for diagnostic logging.", encoding="utf-8")
    return vault


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_vault: Path) -> tuple[Path, Path]:
    reg_file = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_file))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_file))
    registry = VaultRegistry(reg_file)
    registry.add(str(test_vault), name="my_vault")
    return test_vault, reg_file


def test_diag_config_defaults():
    """断言 DiagConfig 的默认值符合规格（enabled 默认 False 关闭）。"""
    cfg = DiagConfig()
    assert cfg.enabled is False
    assert cfg.dir == "~/.mortis_rag_mcp"
    assert cfg.max_bytes == 1048576
    assert cfg.files == 2
    assert cfg.retention_days == 7


def test_diag_config_validation():
    """断言 DiagConfig 在非法参数下的防御性校验。"""
    with pytest.raises(ValueError, match="diag.max_bytes must be positive"):
        AppConfig(diag=DiagConfig(max_bytes=0))
    with pytest.raises(ValueError, match="diag.files must be >= 1"):
        AppConfig(diag=DiagConfig(files=0))
    with pytest.raises(ValueError, match="diag.retention_days must be >= 0"):
        AppConfig(diag=DiagConfig(retention_days=-1))
    with pytest.raises(ValueError, match="diag.dir must not be empty"):
        AppConfig(diag=DiagConfig(dir=""))


def test_load_config_diag_section(tmp_path: Path):
    """断言 load_config 能正确解析 [diag] 节。"""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        """
[diag]
enabled = true
dir = "/custom/diag/path"
max_bytes = 204800
files = 4
retention_days = 14
""",
        encoding="utf-8",
    )
    app_cfg = load_config(toml_file)
    assert app_cfg.diag.enabled is True
    assert app_cfg.diag.dir == "/custom/diag/path"
    assert app_cfg.diag.max_bytes == 204800
    assert app_cfg.diag.files == 4
    assert app_cfg.diag.retention_days == 14


def test_diag_disabled_by_default_zero_side_effects(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言默认关闭时零文件系统操作、零副作用。"""
    server = VaultMcpServer()
    # 显式使用临时路径作为 dir，确保如果误写能够被检出
    diag_dir = tmp_path / "should_not_exist_diag"
    server.config.diag.dir = str(diag_dir)
    assert server.config.diag.enabled is False

    res = server.call_tool("kb_list", {})
    assert "content" in res

    res2 = server.call_tool("kb_search", {"query": "test"})
    assert "content" in res2

    # 绝不能生成任何日志文件或目录
    assert not diag_dir.exists()


def test_diag_enabled_whitelist_fields_and_corr_id(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言开启诊断日志后输出格式严格符合 10 键白名单，且同一调用共享 corr_id。"""
    diag_dir = tmp_path / "diag_out"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    # 1. 调用 kb_list：单阶段输出（serialize）
    server.call_tool("kb_list", {})

    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    first_line = lines[0]

    # 严格断言白名单
    allowed_keys = set(diaglog.WHITELIST_KEYS)
    assert set(first_line.keys()).issubset(allowed_keys)
    assert first_line["tool"] == "kb_list"
    assert first_line["stage"] == "serialize"
    # 版本号取自包顶层单一真源，不再硬编码（C63）——发版只改 mortis_rag_mcp.__version__
    assert first_line["version"] == SERVER_INFO["version"]
    assert isinstance(first_line["ms"], (int, float))
    assert isinstance(first_line["response_bytes"], int)
    assert len(first_line["corr_id"]) == 16  # secrets.token_hex(8)

    # 清空以测 kb_search
    log_file.unlink()

    # 2. 调用 kb_search：多阶段覆盖（sync, retrieve, rerank, serialize）
    server._indexer_for({"vault_path": str(isolated_env[0])}).sync()
    search_res = server.call_tool("kb_search", {"query": "test", "preview": True})
    assert "content" in search_res

    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 至少应包含 4 个阶段
    stages = [entry["stage"] for entry in lines]
    assert "sync" in stages
    assert "retrieve" in stages
    assert "rerank" in stages
    assert "serialize" in stages

    # 所有阶段共享相同的 corr_id
    corr_ids = {entry["corr_id"] for entry in lines}
    assert len(corr_ids) == 1

    ser_entry = next(entry for entry in lines if entry["stage"] == "serialize")
    assert "result_count" in ser_entry
    assert ser_entry["result_count"] >= 1
    assert "response_bytes" in ser_entry
    assert ser_entry["response_bytes"] > 0
    if "truncated" in ser_entry:
        assert isinstance(ser_entry["truncated"], bool)

    # 3. 测试包含 truncated 字段的工具调用（如 kb_read）
    server.call_tool("kb_read", {"source": "note1.md", "vault_path": str(isolated_env[0])})
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    read_ser_entry = lines[-1]
    assert read_ser_entry["tool"] == "kb_read"
    assert read_ser_entry["stage"] == "serialize"
    assert "truncated" in read_ser_entry
    assert isinstance(read_ser_entry["truncated"], bool)

    # 所有条目的字段全都在白名单内
    for entry in lines:
        assert set(entry.keys()).issubset(allowed_keys)
        assert entry["stage"] in diaglog.ALLOWED_STAGES


def test_diag_privacy_guarantee(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言敏感信息（query、密钥、绝对路径）绝对不落盘。"""
    test_vault, _ = isolated_env
    diag_dir = tmp_path / "diag_privacy"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    secret_key = "sk-super-secret-key-9999"
    secret_query = "CLASSIFIED_RESEARCH_PLAN_TOP_SECRET"
    abs_path_leak = "C:\\Windows\\System32\\SensitiveFile.txt"

    # 1. 成功调用：传入包含敏感关键词的 query、模拟额外参数以及合法 vault_path
    server.call_tool(
        "kb_search",
        {
            "query": secret_query,
            "api_key": secret_key,
            "vault_path": str(test_vault),
            "custom_sensitive_path": abs_path_leak,
        },
    )

    # 2. 失败调用：传入未注册路径触发 fail 记录，验证异常信息中的路径也不泄露
    try:
        server.call_tool(
            "kb_search",
            {
                "query": secret_query,
                "api_key": secret_key,
                "vault_path": abs_path_leak,
            },
        )
    except ValueError:
        pass

    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    raw_content = log_file.read_text(encoding="utf-8")

    # 绝对禁止出现敏感内容
    assert secret_key not in raw_content
    assert secret_query not in raw_content
    assert abs_path_leak not in raw_content
    assert "System32" not in raw_content


def test_diag_error_handling_stage_fail(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言异常调用会记录 stage='fail' 与分类后的 error_code。"""
    diag_dir = tmp_path / "diag_fail"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    # 1. 触发 unknown tool 异常 (ValueError)
    with pytest.raises(ValueError, match="unknown tool: invalid_tool_name"):
        server.call_tool("invalid_tool_name", {})

    log_file = diag_dir / "diag.log"
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    fail_entry = lines[0]
    assert fail_entry["stage"] == "fail"
    assert fail_entry["error_code"] == "value_error"
    assert fail_entry["tool"] == "invalid_tool_name"
    assert isinstance(fail_entry["ms"], (int, float))

    # 2. 触发参数校验异常 (ValueError)
    with pytest.raises(ValueError):
        server.call_tool("kb_set_weight", {})  # 缺少 weight 必填参数

    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[1]["stage"] == "fail"
    assert lines[1]["error_code"] == "value_error"


def test_diag_rotation(tmp_path: Path):
    """断言超 max_bytes 日志轮转为 .1，保留 files 代。"""
    log_dir = tmp_path / "diag_rot"
    cfg = DiagConfig(enabled=True, dir=str(log_dir), max_bytes=200, files=2)

    # 写入第一条，文件约 100 字节
    diaglog.record("corr1", "kb_search", "sync", 5.0, config=cfg)
    log_file = log_dir / "diag.log"
    assert log_file.is_file()
    assert not (log_dir / "diag.log.1").exists()

    # 写入多条触发轮转
    for i in range(5):
        diaglog.record(f"corr_rot_{i}", "kb_search", "retrieve", 10.0, config=cfg)

    # 此时应轮转出 diag.log.1
    assert log_file.is_file()
    assert (log_dir / "diag.log.1").is_file()
    # files=2 时绝对不能出现 diag.log.2
    assert not (log_dir / "diag.log.2").exists()

    # 断言两文件均为有效 jsonl
    for p in (log_file, log_dir / "diag.log.1"):
        for line in p.read_text(encoding="utf-8").splitlines():
            data = json.loads(line)
            assert data["stage"] in diaglog.ALLOWED_STAGES


def test_diag_retention_cleanup(tmp_path: Path):
    """断言惰性清理超过 retention_days 的旧文件。"""
    log_dir = tmp_path / "diag_retention"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 构造 10 天前的旧日志
    old_file = log_dir / "diag.log.old_archive"
    old_file.write_text('{"stage": "sync"}\n', encoding="utf-8")
    past_mtime = time.time() - (10 * 86400)
    os.utime(old_file, (past_mtime, past_mtime))

    cfg = DiagConfig(enabled=True, dir=str(log_dir), retention_days=7)
    # 写入新日志触发清理
    diaglog.record("corr_ret", "kb_list", "serialize", 2.0, config=cfg)

    # 旧文件已被自动删除
    assert not old_file.exists()
    assert (log_dir / "diag.log").is_file()


def test_diag_write_failure_silent_and_safe(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言写入失败（只读路径或权限受限）时静默吞掉，绝不抛出异常。"""
    # 将 dir 指向一个已存在的普通文件（导致无法作为目录创建文件）
    dummy_file = tmp_path / "blocked_file"
    dummy_file.write_text("blocker", encoding="utf-8")

    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(dummy_file)

    # 绝不抛出任何异常，检索主流程正常返回
    res = server.call_tool("kb_list", {})
    assert "content" in res


def test_kb_stats_reports_accel(isolated_env: tuple[Path, Path]):
    """F7.2：断言 kb_stats 返回 accel 字段且结构为 {'numpy': bool, 'sqlite_vec': bool}。"""
    test_vault, _ = isolated_env
    indexer = MarkdownIndexer(test_vault, AppConfig(vault_path=str(test_vault), embedding=EmbeddingConfig(mode="static", dimension=4)))
    stats = indexer.stats()

    assert "accel" in stats
    accel = stats["accel"]
    assert isinstance(accel, dict)
    assert "numpy" in accel
    assert isinstance(accel["numpy"], bool)
    assert "sqlite_vec" in accel
    assert isinstance(accel["sqlite_vec"], bool)

    # 通过 server.call_tool("kb_stats") 验证端到端回显
    server = VaultMcpServer()
    server_res = server.call_tool("kb_stats", {})
    parsed_res = json.loads(server_res["content"][0]["text"])
    assert "accel" in parsed_res
    assert isinstance(parsed_res["accel"]["numpy"], bool)
    assert isinstance(parsed_res["accel"]["sqlite_vec"], bool)


def test_diag_stdio_stderr_clean(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言 stdio 服务在开启诊断日志后 stderr 依然绝对干净（符合集成测试约束）。"""
    test_vault, reg_file = isolated_env
    config_file = tmp_path / "app_stdio.toml"
    diag_dir = tmp_path / "stdio_diag"
    diag_dir_str = str(diag_dir).replace("\\", "/")
    config_file.write_text(
        f"""
[diag]
enabled = true
dir = "{diag_dir_str}"
""",
        encoding="utf-8",
    )

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "diagnostic test"}}},
    ]
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)

    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config_file)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "MORTIS_RAG_REGISTRY": str(reg_file), "VAULT_MCP_REGISTRY": str(reg_file)},
    )

    assert proc.returncode == 0, proc.stderr
    # 铁律：stderr 必须绝对为空
    assert not proc.stderr, proc.stderr

    # 验证日志文件已生成且记录了两个工具的调用
    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    tools_logged = {l["tool"] for l in lines}
    assert "kb_list" in tools_logged
    assert "kb_search" in tools_logged


def test_kb_search_stage_order_and_accurate_timing(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言 kb_search 的 4 个阶段按契约顺序严格输出 (sync -> retrieve -> rerank -> serialize)，
    且重排耗时被精确扣除，retrieve 阶段耗时不被 rerank 污染。
    """
    test_vault, _ = isolated_env
    diag_dir = tmp_path / "diag_order"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    indexer = server._indexer_for({"vault_path": str(test_vault)})
    indexer.sync()

    class SleepyReranker:
        def rerank_or_none(self, query: str, contents: list[str]) -> list[dict[str, Any]]:
            time.sleep(0.06)
            return [{"index": i, "relevance_score": 0.8} for i in range(len(contents))]

    indexer.reranker_provider = SleepyReranker()

    server.call_tool("kb_search", {"query": "test", "use_rerank": True, "vault_path": str(test_vault)})

    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 严格检验 4 阶段顺序：sync -> retrieve -> rerank -> serialize
    assert len(lines) == 4
    stages = [l["stage"] for l in lines]
    assert stages == ["sync", "retrieve", "rerank", "serialize"]

    sync_entry, retrieve_entry, rerank_entry, serialize_entry = lines
    assert rerank_entry["ms"] >= 50.0  # 真实反映 SleepyReranker 耗时
    assert retrieve_entry["ms"] < 30.0  # 确认 rerank 耗时已从 retrieve 中扣除，未发生双重计数


def test_fanout_multi_vault_stages_deduplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """断言在跨多库 fan-out 检索时，各库耗时正确合并，4 个阶段各出一行，不产生多行 sync/retrieve。"""
    reg_file = tmp_path / "vaults_fanout.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_file))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_file))
    registry = VaultRegistry(reg_file)

    v1 = tmp_path / "vault_1"
    v1.mkdir()
    (v1 / "a.md").write_text("# Note A\nAlpha content", encoding="utf-8")
    registry.add(str(v1), name="v1")

    v2 = tmp_path / "vault_2"
    v2.mkdir()
    (v2 / "b.md").write_text("# Note B\nBeta content", encoding="utf-8")
    registry.add(str(v2), name="v2")

    diag_dir = tmp_path / "diag_fanout"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    server.call_tool("kb_search", {"query": "content"})

    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 契约断言：4 个阶段各出一行
    assert len(lines) == 4
    stages = [l["stage"] for l in lines]
    assert stages == ["sync", "retrieve", "rerank", "serialize"]


def test_diag_concurrent_calls_isolation_and_no_wrapper_leak(isolated_env: tuple[Path, Path], tmp_path: Path):
    """断言高并发多线程调用下 ContextVar 完全隔离 corr_id，且代理包装不发生层叠泄漏。"""
    import concurrent.futures
    test_vault, _ = isolated_env
    diag_dir = tmp_path / "diag_concurrency"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    def worker(i: int) -> None:
        server.call_tool("kb_search", {"query": f"test {i}", "vault_path": str(test_vault)})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(worker, range(15)))

    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 15 次调用，每次产生 4 行日志 = 60 行
    assert len(lines) == 60
    by_corr: dict[str, list[dict[str, Any]]] = {}
    for entry in lines:
        by_corr.setdefault(entry["corr_id"], []).append(entry)

    assert len(by_corr) == 15
    for cid, entries in by_corr.items():
        assert len(entries) == 4
        stages = [e["stage"] for e in entries]
        assert stages == ["sync", "retrieve", "rerank", "serialize"]

    # 检查包装器未层叠
    count = 0
    fn = server._indexer_for
    while hasattr(fn, "__closure__") and fn.__closure__:
        count += 1
        found_next = False
        for cell in fn.__closure__:
            if callable(cell.cell_contents) and "indexer_for" in cell.cell_contents.__name__:
                fn = cell.cell_contents
                found_next = True
                break
        if not found_next:
            break
    assert count <= 1


def test_kb_search_partial_stages_on_failure(isolated_env: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """断言当 kb_search 执行在检索阶段抛出异常时，前置已完成阶段（sync）被保留输出，随后记录 fail。"""
    test_vault, _ = isolated_env
    diag_dir = tmp_path / "diag_partial_fail"
    server = VaultMcpServer()
    server.config.diag.enabled = True
    server.config.diag.dir = str(diag_dir)

    indexer = server._indexer_for({"vault_path": str(test_vault)})
    indexer.sync()

    def exploding_search(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Disk read failed midway")

    monkeypatch.setattr(indexer, "search", exploding_search)

    with pytest.raises(OSError, match="Disk read failed midway"):
        server.call_tool("kb_search", {"query": "test", "vault_path": str(test_vault)})

    log_file = diag_dir / "diag.log"
    assert log_file.is_file()
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 应先输出 sync 阶段，再输出 fail 阶段
    assert len(lines) == 2
    assert lines[0]["stage"] == "sync"
    assert lines[1]["stage"] == "fail"
    assert lines[1]["error_code"] == "os_error"
    assert lines[0]["corr_id"] == lines[1]["corr_id"]

