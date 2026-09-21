from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "VAULT_MCP_REGISTRY": str(config.parent / "vaults.toml")},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_scoped_search_by_name_and_array(tmp_path):
    vault_a = tmp_path / "vault_a"
    vault_b = tmp_path / "vault_b"
    vault_a.mkdir(parents=True)
    vault_b.mkdir(parents=True)
    (vault_a / "alpha.md").write_text("# Alpha\n数电逻辑门设计规范与TTL参数", encoding="utf-8")
    (vault_b / "beta.md").write_text("# Beta\n微机接口技术与8086总线规范", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_a), "name": "DigitalCircuit"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b), "name": "MicroComputer"}}},
        # 1. 按名称单库检索 (精确大小写)
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "规范", "vault_path": "DigitalCircuit"}}},
        # 2. 按名称单库检索 (不区分大小写)
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "规范", "vault_path": "digitalcircuit"}}},
        # 3. 数组定向组合检索 (Scoped Multi-Vault)
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "规范", "vault_paths": ["DigitalCircuit", "MicroComputer"]}}},
    ]
    responses = _run_stdio(config, requests)

    # 验证 4: DigitalCircuit 单库命中 alpha.md
    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    assert len(r4["chunks"]) > 0
    assert all(c["source"] == "alpha.md" for c in r4["chunks"])

    # 验证 5: 大小写不敏感同样命中 alpha.md
    r5 = json.loads(responses[4]["result"]["content"][0]["text"])
    assert len(r5["chunks"]) > 0
    assert all(c["source"] == "alpha.md" for c in r5["chunks"])

    # 验证 6: 定向多库联合召回 alpha.md 和 beta.md
    r6 = json.loads(responses[5]["result"]["content"][0]["text"])
    sources = {c["source"] for c in r6["chunks"]}
    assert "alpha.md" in sources
    assert "beta.md" in sources
    assert len(r6["searched"]) == 2
    # 定向检索时不应有收窄 hint
    assert "hint" not in r6


def test_scoped_search_solo_vault_inclusion(tmp_path):
    vault_pub = tmp_path / "pub"
    vault_solo = tmp_path / "solo"
    vault_pub.mkdir(parents=True)
    vault_solo.mkdir(parents=True)
    (vault_pub / "guide.md").write_text("# Guide\n公开知识库文档", encoding="utf-8")
    (vault_solo / "diary.md").write_text("# Diary\n绝密私密日记记录", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_pub), "name": "PublicNotes"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(vault_solo), "name": "SecretNotes"}}},
        # 1. 全局盲搜：未传 vault_path / vault_paths，跳过 solo
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "文档 日记"}}},
        # 2. 定向联合检索：显式指定包含 Solo 库
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "文档 日记", "vault_paths": ["PublicNotes", "SecretNotes"]}}},
        # 3. 定向单搜 Solo 库
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "日记", "vault_path": "SecretNotes"}}},
    ]
    responses = _run_stdio(config, requests)

    # 全局盲搜只搜到 PublicNotes，excluded_solo 记录 solo 库
    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    sources4 = {c["source"] for c in r4["chunks"]}
    assert "guide.md" in sources4
    assert "diary.md" not in sources4
    assert len(r4["excluded_solo"]) == 1

    # 定向包含 Solo 库：两者均可召回
    r5 = json.loads(responses[4]["result"]["content"][0]["text"])
    sources5 = {c["source"] for c in r5["chunks"]}
    assert "guide.md" in sources5
    assert "diary.md" in sources5
    assert len(r5["excluded_solo"]) == 0

    # 单搜 Solo 库
    r6 = json.loads(responses[5]["result"]["content"][0]["text"])
    sources6 = {c["source"] for c in r6["chunks"]}
    assert "diary.md" in sources6


def test_comma_safe_path_parsing(tmp_path):
    # 路径本身含有逗号
    vault_comma = tmp_path / "Math, Science"
    vault_comma.mkdir(parents=True)
    (vault_comma / "notes.md").write_text("# Math and Science\n高等数学与工程热力学", encoding="utf-8")

    vault_other = tmp_path / "other"
    vault_other.mkdir(parents=True)
    (vault_other / "other.md").write_text("# Other\n其他无关参考资料", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_comma), "name": "Math, Science"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_other), "name": "OtherNotes"}}},
        # F-05: 传入含有逗号的库名或路径，优先整体匹配，严禁盲目切散
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "热力学", "vault_path": "Math, Science"}}},
        # 推荐标准：通过 vault_paths 数组传递含逗号的库与其它库
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "资料", "vault_paths": ["Math, Science", "OtherNotes"]}}},
    ]
    responses = _run_stdio(config, requests)

    # 验证 4: Math, Science 整体匹配成功（单库单搜）
    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    assert len(r4["chunks"]) > 0
    assert r4["chunks"][0]["source"] == "notes.md"

    # 验证 5: vault_paths 数组联合检索成功
    r5 = json.loads(responses[4]["result"]["content"][0]["text"])
    assert len(r5["searched"]) == 2
    sources5 = {c["source"] for c in r5["chunks"]}
    assert "other.md" in sources5


def test_comma_fallback_split(tmp_path):
    # 测试常规不含逗号的库通过逗号拼接回退拆分
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    (v1 / "a.md").write_text("# A\nalpha info", encoding="utf-8")
    (v2 / "b.md").write_text("# B\nbeta info", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(v1), "name": "VOne"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(v2), "name": "VTwo"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "info", "vault_path": "VOne, VTwo"}}},
    ]
    responses = _run_stdio(config, requests)
    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    assert len(r4["searched"]) == 2


def test_invalid_vault_name_error(tmp_path):
    vault = tmp_path / "v1"
    vault.mkdir(parents=True)
    (vault / "a.md").write_text("# A\ncontent", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "MyVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "test", "vault_path": "NoSuchVault"}}},
    ]
    responses = _run_stdio(config, requests)
    # 应以 isError 返回且列出可用库名
    assert responses[2]["result"]["isError"] is True
    err_body = json.loads(responses[2]["result"]["content"][0]["text"])
    err = err_body["error"]
    assert "neither a registered vault name nor an absolute path" in err
    assert "'MyVault'" in err
