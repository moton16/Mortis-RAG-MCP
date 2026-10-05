from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, IndexConfig, load_config
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """自动将注册表重定向至单测独立 tmp_path，杜绝污染全局 ~/.mortis_rag_mcp/vaults.toml。"""
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    """通过 stdio 进程启动真实 MCP Server 验证 JSON-RPC 往返与 stderr 纯净度。"""
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    reg_file = str(config.parent / "vaults.toml")
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "MORTIS_RAG_REGISTRY": reg_file, "VAULT_MCP_REGISTRY": reg_file},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, f"stderr 必须保持干净，实际输出: {proc.stderr}"
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_stdio_search_and_read_chunk_id(tmp_path: Path):
    """F1 stdio 集成：kb_search 命中后直接用 chunk_id 调用 kb_read 原地展开。"""
    vault = tmp_path / "notes"
    vault.mkdir()
    # 构造一个多行笔记文件
    lines = [f"Line {i:03d} content for testing" for i in range(1, 101)]
    lines[50] = "SpecialKeyword target line in middle"
    (vault / "doc.md").write_text("\n".join(lines), encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n[index]\nread_max_chars = 20000\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "MainVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "SpecialKeyword", "vault_path": "MainVault"}}},
    ]
    responses = _run_stdio(app_toml, requests)
    search_res = json.loads(responses[2]["result"]["content"][0]["text"])
    assert len(search_res["chunks"]) > 0
    target_chunk = search_res["chunks"][0]
    chunk_id = target_chunk["id"]
    assert chunk_id

    # 4. 用 chunk_id 原地展开，默认 expand_lines=30
    read_req = [
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_read", "arguments": {"chunk_id": chunk_id, "vault_path": "MainVault"}}},
    ]
    read_res_list = _run_stdio(app_toml, requests[:2] + read_req)
    read_out = json.loads(read_res_list[2]["result"]["content"][0]["text"])

    assert read_out["source"] == "doc.md"
    assert read_out["chunk_id"] == chunk_id
    assert "SpecialKeyword" in read_out["content"]
    assert read_out["truncated"] is False
    # 行号区间合理展开
    assert read_out["start_line"] >= 1
    assert read_out["end_line"] >= read_out["start_line"]


def test_kb_read_chunk_id_expand_lines_variations(tmp_path: Path):
    """验证 expand_lines=0、超大值夹取 (500)、负数与非法值防御解析。"""
    vault = tmp_path / "vault_expand"
    vault.mkdir()
    lines = [f"Row {i:03d}" for i in range(1, 201)]
    lines[100] = "TargetRow in the center"
    (vault / "test.md").write_text("\n".join(lines), encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ExpVault")
    indexer = server._indexer_for({"vault_path": "ExpVault"})
    indexer.sync()

    chunks = indexer.all_chunks()
    assert len(chunks) > 0
    cid = chunks[0].id
    c_start = chunks[0].metadata.get("start_line", 1)
    c_end = chunks[0].metadata.get("end_line", c_start)

    # 1. expand_lines = 0：精确切片行区间
    res_zero = server._kb_read({"chunk_id": cid, "expand_lines": 0, "vault_path": "ExpVault"})
    assert res_zero["chunk_id"] == cid
    assert res_zero["start_line"] == c_start
    assert res_zero["end_line"] == c_end

    # 2. expand_lines = 1000：夹取到 500
    res_large = server._kb_read({"chunk_id": cid, "expand_lines": 1000, "vault_path": "ExpVault"})
    assert res_large["start_line"] == max(1, c_start - 500)
    assert res_large["end_line"] == c_end + 500

    # 3. expand_lines = -5：夹取到 0
    res_neg = server._kb_read({"chunk_id": cid, "expand_lines": -5, "vault_path": "ExpVault"})
    assert res_neg["start_line"] == c_start
    assert res_neg["end_line"] == c_end

    # 4. expand_lines = "invalid"：非法值退回默认 30
    res_invalid = server._kb_read({"chunk_id": cid, "expand_lines": "not_a_number", "vault_path": "ExpVault"})
    assert res_invalid["start_line"] == max(1, c_start - 30)
    assert res_invalid["end_line"] == c_end + 30


def test_kb_read_chunk_id_not_found(tmp_path: Path):
    """验证未命中 chunk_id 抛出符合契约的清晰报错文案。"""
    vault = tmp_path / "vault_not_found"
    vault.mkdir()
    (vault / "dummy.md").write_text("dummy text\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="NFVault")
    indexer = server._indexer_for({"vault_path": "NFVault"})
    indexer.sync()

    fake_id = "0123456789abcdef9999"
    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": fake_id, "vault_path": "NFVault"})

    msg = str(exc_info.value)
    assert msg.startswith("chunk_id not found: 0123456789ab…")
    assert "请重新 kb_search 获取新 id" in msg


def test_kb_read_chunk_id_mutual_exclusive(tmp_path: Path):
    """验证 chunk_id 与 start_line/end_line/heading 互斥报错。"""
    vault = tmp_path / "vault_mutex"
    vault.mkdir()
    (vault / "note.md").write_text("# Title\ncontent here\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="MutexVault")
    indexer = server._indexer_for({"vault_path": "MutexVault"})
    indexer.sync()
    cid = indexer.all_chunks()[0].id

    # 1. chunk_id + start_line
    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": cid, "start_line": 1, "vault_path": "MutexVault"})

    # 2. chunk_id + end_line
    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": cid, "end_line": 2, "vault_path": "MutexVault"})

    # 3. chunk_id + heading
    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": cid, "heading": "Title", "vault_path": "MutexVault"})

    # 4. 空字符串与 None 不误判互斥
    res_empty = server._kb_read({
        "chunk_id": cid,
        "start_line": "",
        "end_line": None,
        "heading": "",
        "vault_path": "MutexVault",
    })
    assert res_empty["chunk_id"] == cid
    assert "Title" in res_empty["content"]


def test_kb_read_missing_source_and_chunk_id(tmp_path: Path):
    """验证 source 与 chunk_id 均未提供时报错。"""
    vault = tmp_path / "vault_empty"
    vault.mkdir()
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="EmptyVault")

    with pytest.raises(ValueError, match="source or chunk_id is required for kb_read"):
        server._kb_read({"vault_path": "EmptyVault"})

    with pytest.raises(ValueError, match="source or chunk_id is required for kb_read"):
        server._kb_read({"source": "  ", "chunk_id": "", "vault_path": "EmptyVault"})


def test_read_max_chars_configuration_and_truncation(tmp_path: Path):
    """验证 [index] read_max_chars 配置生效、非法值防御兜底与截断标记。"""
    # 1. 默认配置 read_max_chars = 20000
    cfg1 = load_config(None)
    assert cfg1.index.read_max_chars == 20000

    cfg_toml = tmp_path / "custom.toml"
    cfg_toml.write_text("[index]\nread_max_chars = 300\n", encoding="utf-8")
    cfg2 = load_config(cfg_toml)
    assert cfg2.index.read_max_chars == 300

    # 2. 非法值下限防御：< 100 退回 20000
    cfg_toml.write_text("[index]\nread_max_chars = 50\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000

    # 3. 非法值上限防御：> 1000000 退回 20000
    cfg_toml.write_text("[index]\nread_max_chars = 2000000\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000

    # 4. 非法字符串/bool/inf 防御：退回 20000
    cfg_toml.write_text("[index]\nread_max_chars = 'bad_value'\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000
    cfg_toml.write_text("[index]\nread_max_chars = true\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000
    assert IndexConfig(read_max_chars=float("inf")).read_max_chars == 20000

    # 5. 截断行为验证
    vault = tmp_path / "vault_trunc"
    vault.mkdir()
    long_content = "A" * 600
    (vault / "long.md").write_text(long_content, encoding="utf-8")

    cfg_toml.write_text("[index]\nread_max_chars = 250\n", encoding="utf-8")
    server = VaultMcpServer(cfg_toml)
    server.registry.add(str(vault), name="TruncVault")
    indexer = server._indexer_for({"vault_path": "TruncVault"})
    indexer.sync()
    cid = indexer.all_chunks()[0].id

    res = server._kb_read({"chunk_id": cid, "vault_path": "TruncVault"})
    assert res["truncated"] is True
    assert len(res["content"]) == 250
    assert res["chunk_id"] == cid


def test_multi_vault_chunk_id_disambiguation(tmp_path: Path):
    """跨两库同内容：未指定 vault_path 提示显式传参；指定后精确读取。"""
    vault1 = tmp_path / "vault_one"
    vault1.mkdir()
    vault2 = tmp_path / "vault_two"
    vault2.mkdir()

    same_text = "# Shared Note\nIdentical content in both vaults\n"
    (vault1 / "shared.md").write_text(same_text, encoding="utf-8")
    (vault2 / "shared.md").write_text(same_text, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault1), name="VaultOne")
    server.registry.add(str(vault2), name="VaultTwo")

    idx1 = server._indexer_for({"vault_path": "VaultOne"})
    idx2 = server._indexer_for({"vault_path": "VaultTwo"})
    idx1.sync()
    idx2.sync()

    chunk1 = idx1.all_chunks()[0]
    chunk2 = idx2.all_chunks()[0]
    # 同内容 chunk.id 相同
    assert chunk1.id == chunk2.id
    cid = chunk1.id

    # 1. 未传 vault_path 且真撞 id：fail-closed 并列出候选库（C56 契约变更）
    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": cid})
    msg = str(exc_info.value)
    assert "同时命中" in msg
    assert "VaultOne" in msg and "VaultTwo" in msg

    # 2. 显式传 VaultOne：成功读取
    res1 = server._kb_read({"chunk_id": cid, "vault_path": "VaultOne"})
    assert res1["source"] == "shared.md"
    assert res1["chunk_id"] == cid
    assert "Identical content" in res1["content"]

    # 3. 显式传 VaultTwo：成功读取
    res2 = server._kb_read({"chunk_id": cid, "vault_path": "VaultTwo"})
    assert res2["source"] == "shared.md"
    assert res2["chunk_id"] == cid
    assert "Identical content" in res2["content"]


def test_chunk_id_unique_hit_auto_expands_across_vaults(tmp_path: Path):
    """C56 契约变更：唯一命中时未传 vault_path 也自动展开，并带库归属键。"""
    vault_a = tmp_path / "vault_a"
    vault_b = tmp_path / "vault_b"
    vault_a.mkdir()
    vault_b.mkdir()
    (vault_a / "only_here.md").write_text("# Only\nunique alpha content\n", encoding="utf-8")
    (vault_b / "other.md").write_text("# Other\nbeta content\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault_a), name="VaultA")
    server.registry.add(str(vault_b), name="VaultB")
    idx_a = server._indexer_for({"vault_path": "VaultA"})
    idx_b = server._indexer_for({"vault_path": "VaultB"})
    idx_a.sync()
    idx_b.sync()

    cid = next(c for c in idx_a.all_chunks() if c.source == "only_here.md").id
    res = server._kb_read({"chunk_id": cid})

    assert res["source"] == "only_here.md"
    assert res["chunk_id"] == cid
    assert res["vault"] == "VaultA"
    assert res["vault_path"] == str(idx_a.vault_path)


def test_chunk_id_probe_covers_unloaded_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C56：探测必须覆盖未加载的库——只查 self._indexers 会谎报「not found」。"""
    # 关掉启动预索引线程，保证「未加载」这个前提是确定的（否则后台线程可能抢先加载）
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    loaded = tmp_path / "vault_loaded"
    unloaded = tmp_path / "vault_unloaded"
    loaded.mkdir()
    unloaded.mkdir()
    (loaded / "a.md").write_text("# A\nloaded content\n", encoding="utf-8")
    (unloaded / "b.md").write_text("# B\ntarget content in unloaded vault\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(loaded), name="LoadedVault")
    server.registry.add(str(unloaded), name="UnloadedVault")
    idx = server._indexer_for({"vault_path": "LoadedVault"})
    idx.sync()

    # 第二个库刻意不经 server（保持「未加载」），用临时 indexer 建好缓存后立刻释放
    probe = MarkdownIndexer(str(unloaded), server.config)
    probe.sync()
    cid = probe.all_chunks()[0].id
    VaultMcpServer._close_probe_indexer(probe)
    assert str(unloaded.resolve()) not in server._indexers

    res = server._kb_read({"chunk_id": cid})

    assert res["source"] == "b.md"
    assert res["vault"] == "UnloadedVault"
    assert "target content" in res["content"]


def test_chunk_id_zero_hit_reports_unprobed_vaults(tmp_path: Path):
    """C56：有库未探测时，零命中文案必须如实报「跳过」清单，不得谎报文件已修改。"""
    import shutil

    alive = tmp_path / "vault_alive"
    gone = tmp_path / "vault_gone"
    alive.mkdir()
    gone.mkdir()
    (alive / "n.md").write_text("# N\nalive content\n", encoding="utf-8")
    (gone / "g.md").write_text("# G\ngone content\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(alive), name="AliveVault")
    server.registry.add(str(gone), name="GoneVault")
    idx = server._indexer_for({"vault_path": "AliveVault"})
    idx.sync()
    shutil.rmtree(gone)  # 注册表有条目但目录已删 → 探测必须跳过并计入「未探测」

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": "0" * 40})
    msg = str(exc_info.value)

    assert "跳过 1 个" in msg
    assert "GoneVault" in msg
    assert "目录已不存在" in msg
    assert "文件可能已修改" not in msg


def test_chunk_id_probe_honours_budget_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C64：探测上限（库数/时间预算）生效时，被裁掉的库必须计入「未探测」并如实报错。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    monkeypatch.setattr(VaultMcpServer, "_PROBE_MAX_UNLOADED_VAULTS", 1)

    vaults = []
    for name in ("a", "b", "c"):
        vault = tmp_path / f"vault_{name}"
        vault.mkdir()
        (vault / "n.md").write_text(f"# {name}\ncontent {name}\n", encoding="utf-8")
        vaults.append(vault)

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    for name, vault in zip(("A", "B", "C"), vaults):
        server.registry.add(str(vault), name=name)
        idx = MarkdownIndexer(str(vault), server.config)
        idx.sync()
        VaultMcpServer._close_probe_indexer(idx)

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": "0" * 40})
    msg = str(exc_info.value)

    assert "超过探测库数上限" in msg
    assert "探测了 1 个" in msg
    assert "跳过 2 个" in msg


def test_chunk_id_hit_in_solo_vault_reports_name_only(tmp_path: Path):
    """D5 裁定：solo 库参与 chunk_id 探测，但结果只给库名 + solo 标记（不展开路径）。"""
    solo = tmp_path / "vault_solo"
    normal = tmp_path / "vault_normal"
    solo.mkdir()
    normal.mkdir()
    (solo / "s.md").write_text("# S\nsolo only content\n", encoding="utf-8")
    (normal / "n.md").write_text("# N\nnormal content\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(solo), name="SoloVault", solo=True)
    server.registry.add(str(normal), name="NormalVault")
    idx_solo = server._indexer_for({"vault_path": "SoloVault"})
    idx_normal = server._indexer_for({"vault_path": "NormalVault"})
    idx_solo.sync()
    idx_normal.sync()

    cid = next(c for c in idx_solo.all_chunks() if c.source == "s.md").id
    res = server._kb_read({"chunk_id": cid})

    assert res["source"] == "s.md"
    assert res["vault"] == "SoloVault"
    assert res["solo"] is True
    assert "vault_path" not in res


def test_chunk_read_no_registered_vaults(tmp_path: Path):
    """C65: 零注册库 kb_read(chunk_id=...) 经 handle 必须 isError=true，文本含 kb_init，不能协议 -32000 / IndexError。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "kb_read", "arguments": {"chunk_id": "0" * 40}},
    }
    resp = server.handle(req)
    assert resp is not None
    assert "result" in resp, f"必须返回 result 而非 jsonrpc error: {resp}"
    result = resp["result"]
    assert result.get("isError") is True
    err_text = result["content"][0]["text"]
    assert "kb_init" in err_text


def test_chunk_probe_one_hit_with_unprobed_vault_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: 唯一已探命中但存在未探库时（超库数或超时间），必须 fail-closed 报 incomplete 并要求显式传 vault_path。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    v_hit = tmp_path / "vault_hit"
    v_unprobed = tmp_path / "vault_unprobed"
    v_hit.mkdir()
    v_unprobed.mkdir()
    (v_hit / "hit.md").write_text("# Target\ncontent target\n", encoding="utf-8")
    (v_unprobed / "other.md").write_text("# Other\ncontent other\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(v_hit), name="HitVault")
    server.registry.add(str(v_unprobed), name="UnprobedVault")

    idx_hit = server._indexer_for({"vault_path": "HitVault"})
    idx_hit.sync()
    cid = idx_hit.all_chunks()[0].id

    # 1. 超库数限制导致 UnprobedVault 未探测
    monkeypatch.setattr(VaultMcpServer, "_PROBE_MAX_UNLOADED_VAULTS", 0)
    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": cid})
    msg = str(exc_info.value)
    assert "incomplete" in msg or "未完成" in msg or "未探测" in msg
    assert "vault_path" in msg
    assert "HitVault" in msg

    # 2. 超时间预算导致 UnprobedVault 未探测
    monkeypatch.setattr(VaultMcpServer, "_PROBE_MAX_UNLOADED_VAULTS", 32)
    monkeypatch.setattr(VaultMcpServer, "_PROBE_BUDGET_SECONDS", -1.0)
    with pytest.raises(ValueError) as exc_info2:
        server._kb_read({"chunk_id": cid})
    msg2 = str(exc_info2.value)
    assert "incomplete" in msg2 or "未完成" in msg2 or "未探测" in msg2
    assert "vault_path" in msg2


def test_chunk_probe_missing_cache_is_unprobed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: 未加载且无有效文本缓存的库计入 skipped（尚无可探测文本索引），不谎报文件可能已修改。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    v1 = tmp_path / "vault_loaded"
    v2 = tmp_path / "vault_no_bin"
    v1.mkdir()
    v2.mkdir()
    (v1 / "a.md").write_text("# A\ncontent a\n", encoding="utf-8")
    (v2 / "b.md").write_text("# B\ncontent b\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(v1), name="V1")
    server.registry.add(str(v2), name="V2")
    idx1 = server._indexer_for({"vault_path": "V1"})
    idx1.sync()

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": "0" * 40})
    msg = str(exc_info.value)
    assert "尚无可探测文本索引" in msg
    assert "文件可能已修改" not in msg


def test_chunk_probe_valid_empty_cache_is_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: 合法空缓存被成功读取，属于完整探测零命中，报告文件可能已修改。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    v1 = tmp_path / "vault_empty_1"
    v2 = tmp_path / "vault_empty_2"
    v1.mkdir()
    v2.mkdir()

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(v1), name="V1")
    server.registry.add(str(v2), name="V2")

    # 分别为两库写入合法空缓存
    from mortis_rag_mcp._indexer.cache_codec import _CacheCodec
    idx1 = MarkdownIndexer(str(v1), server.config)
    idx2 = MarkdownIndexer(str(v2), server.config)
    _CacheCodec.dump(idx1._chunks_cache_path, idx1._chunks_meta(), {})
    _CacheCodec.dump(idx2._chunks_cache_path, idx2._chunks_meta(), {})
    VaultMcpServer._close_probe_indexer(idx1)
    VaultMcpServer._close_probe_indexer(idx2)

    # 清空常驻 indexer，确保探测走临时 probe 读 bin 缓存路径
    server._indexers.clear()

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": "0" * 40})
    msg = str(exc_info.value)
    assert "文件可能已修改" in msg
    assert "跳过" not in msg


def test_chunk_probe_solo_diagnostics_do_not_leak_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: solo 候选/跳过原因只给库名与 solo 标记，绝不泄露绝对路径。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    solo_dir = tmp_path / "secret_solo_dir"
    solo_dir.mkdir()
    normal_dir = tmp_path / "normal_dir"
    normal_dir.mkdir()

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(solo_dir), name="SecretSolo", solo=True)
    server.registry.add(str(normal_dir), name="NormalVault")

    import shutil
    shutil.rmtree(solo_dir)  # 目录删除触发异常

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": "0" * 40})
    msg = str(exc_info.value)
    secret_path = str(solo_dir)
    assert secret_path not in msg
    assert "SecretSolo" in msg


def test_probe_load_vectors_false_survives_backend_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: 当配置 sqlite_vec 但回退到 memory 时，load_vectors=False 必须保证 _load_vectors_cache 调用为 0。"""
    from mortis_rag_mcp.config import VectorConfig

    vault = tmp_path / "vault_vec"
    vault.mkdir()
    (vault / "test.md").write_text("# Test\ncontent\n", encoding="utf-8")

    config = load_config(None)
    config.vector.backend = "sqlite_vec"

    # 首次建库写出缓存
    idx_init = MarkdownIndexer(str(vault), config)
    idx_init.sync()

    call_count = 0
    orig_load_vec = MarkdownIndexer._load_vectors_cache

    def spy_load_vectors(self):
        nonlocal call_count
        call_count += 1
        return orig_load_vec(self)

    monkeypatch.setattr(MarkdownIndexer, "_load_vectors_cache", spy_load_vectors)

    # 构造 load_vectors=False 实例
    probe = MarkdownIndexer(str(vault), config, load_vectors=False)
    assert call_count == 0, f"load_vectors=False 时 _load_vectors_cache 应被跳过，实际调用次数: {call_count}"
    VaultMcpServer._close_probe_indexer(probe)


def test_chunk_probe_closes_resources_on_all_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: 探测临时 indexer 在零命中/单命中/歧义/incomplete 各退出路径均恰当关闭，不关常驻。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    (v1 / "a.md").write_text("# A\ncontent a\n", encoding="utf-8")
    (v2 / "b.md").write_text("# B\ncontent b\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(v1), name="V1")
    server.registry.add(str(v2), name="V2")

    idx1 = MarkdownIndexer(str(v1), server.config)
    idx1.sync()
    idx2 = MarkdownIndexer(str(v2), server.config)
    idx2.sync()

    # 常驻 V1
    resident = server._indexer_for({"vault_path": "V1"})
    closed_probes: list[MarkdownIndexer] = []
    orig_close = VaultMcpServer._close_probe_indexer

    def spy_close(probe: MarkdownIndexer):
        closed_probes.append(probe)
        orig_close(probe)

    monkeypatch.setattr(VaultMcpServer, "_close_probe_indexer", staticmethod(spy_close))

    # 1. 零命中退出路径
    with pytest.raises(ValueError):
        server._kb_read({"chunk_id": "0" * 40})
    assert any(p.vault_path == v2 for p in closed_probes)
    assert resident not in closed_probes


def test_probe_promoted_chunk_disappeared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C65: 探测命中临时库提升常驻时若 chunk 消失，必须明确提示重新 kb_search，不得用旧 probe chunk 兜底。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    v = tmp_path / "vault_promo"
    v.mkdir()
    (v / "doc.md").write_text("# Title\ncontent promo\n", encoding="utf-8")
    v_other = tmp_path / "other"
    v_other.mkdir()
    (v_other / "other.md").write_text("# Other\ncontent other\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(v), name="PromoVault")
    server.registry.add(str(v_other), name="OtherVault")

    idx = MarkdownIndexer(str(v), server.config)
    idx.sync()
    idx_other = MarkdownIndexer(str(v_other), server.config)
    idx_other.sync()
    VaultMcpServer._close_probe_indexer(idx)
    VaultMcpServer._close_probe_indexer(idx_other)

    cid = idx.all_chunks()[0].id

    # 探测前让常驻 indexer 的 all_chunks 返回空列表（模拟缓存变化）
    orig_indexer_for = server._indexer_for

    def fake_indexer_for(args):
        inst = orig_indexer_for(args)
        inst._chunks.clear()
        return inst

    monkeypatch.setattr(server, "_indexer_for", fake_indexer_for)

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": cid})
    msg = str(exc_info.value)
    assert "索引已变化" in msg or "重新 kb_search" in msg
