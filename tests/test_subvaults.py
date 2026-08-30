from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from vault_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from vault_mcp.indexer import MarkdownIndexer


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "vault_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "VAULT_MCP_REGISTRY": str(config.parent / "vaults.toml")},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def _cfg(tmp_path: Path, **cache_kwargs) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


def test_vault_placement_cache_lives_inside_vault(tmp_path):
    vault = tmp_path / "子库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")

    cfg = _cfg(tmp_path, placement="vault", subdir=".mcp_cache")
    indexer = MarkdownIndexer(vault, cfg)
    indexer.sync()

    cache_dir = vault / ".mcp_cache"
    assert cache_dir.exists()
    cache_files = list(cache_dir.rglob("*.bin"))
    assert len(cache_files) == 2  # chunks 层 + vectors 层
    assert indexer.stats()["cache_enabled"] is True


def test_vault_placement_ignores_cache_subdir_when_scanning(tmp_path):
    vault = tmp_path / "子库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    (vault / ".mcp_cache").mkdir()
    # A stray markdown inside the cache dir must not be indexed.
    (vault / ".mcp_cache" / "should_not_index.md").write_text("# Nope\nhidden", encoding="utf-8")

    cfg = _cfg(tmp_path, placement="vault", subdir=".mcp_cache")
    indexer = MarkdownIndexer(vault, cfg)
    chunks = indexer.sync()

    sources = [chunk.source for chunk in chunks]
    assert "a.md" in sources
    assert not any("should_not_index" in source for source in sources)


def test_rebuild_recreates_index_from_scratch(tmp_path):
    vault = tmp_path / "库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nold", encoding="utf-8")

    cfg = _cfg(tmp_path)
    indexer = MarkdownIndexer(vault, cfg)
    indexer.sync()
    cache_files_before = list((tmp_path / "cache").rglob("*.bin"))
    assert cache_files_before

    (vault / "a.md").write_text("# A\nnew content", encoding="utf-8")
    indexer.rebuild()

    assert any("new content" in chunk.content for chunk in indexer.all_chunks())
    assert indexer.stats()["files"] == 1


def test_stdio_kb_vaults_lists_registered_vaults(tmp_path):
    # 0.3.0：kb_vaults 列出注册表条目（而非根库子文件夹）。
    vault_one = tmp_path / "安华帝国"
    vault_two = tmp_path / "工作"
    vault_one.mkdir()
    vault_two.mkdir()
    (vault_one / "设定.md").write_text("# 设定\n内容", encoding="utf-8")
    (vault_two / "周报.md").write_text("# 周报\n内容", encoding="utf-8")
    # 未注册的文件夹不应出现
    (tmp_path / "未注册").mkdir()

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_one)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_two)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_vaults", "arguments": {}}},
    ])
    data = json.loads(responses[3]["result"]["content"][0]["text"])
    names = {item["name"]: item for item in data["vaults"]}
    assert "安华帝国" in names
    assert "工作" in names
    assert "未注册" not in names
    assert names["安华帝国"]["files"] == 1
    assert names["安华帝国"]["exists"] is True


def test_stdio_kb_rebuild_returns_stats(tmp_path):
    vault = tmp_path / "库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{vault.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_rebuild", "arguments": {}}},
    ])
    stats = json.loads(responses[1]["result"]["content"][0]["text"])
    assert stats["files"] == 1
    assert stats["chunks"] >= 1
