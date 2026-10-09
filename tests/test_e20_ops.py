"""E20 operational regressions; no host/network/real-vault access."""
import errno
from pathlib import Path

import pytest

from mortis_rag_mcp import config, doctor, registry
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def test_explicit_missing_config_is_not_healthy_default(tmp_path):
    missing = tmp_path / "typo.toml"
    section, cfg = doctor.check_config(str(missing))
    assert section["ok"] is False
    assert cfg is None
    # Doctor deliberately bounds free-text diagnostics; a long basetemp may
    # truncate the tail. The typed resolver still retains the exact full path.
    assert "Explicit configuration file not found:" in section["detail"]
    with pytest.raises(FileNotFoundError, match="typo.toml") as error:
        config.resolve_config_path(missing)
    assert str(missing) in str(error.value)


def test_missing_env_config_does_not_read_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MORTIS_RAG_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        config.resolve_config_path()


def test_automatic_unconfigured_default_stays_legal(tmp_path, monkeypatch):
    monkeypatch.delenv("MORTIS_RAG_CONFIG", raising=False)
    monkeypatch.delenv("VAULT_MCP_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    section, cfg = doctor.check_config(None)
    assert section["ok"] is True
    assert cfg.embedding.mode == "static"
    assert config.resolve_config_path() is None


@pytest.mark.parametrize("quote", ['"', "'"])
def test_fallback_hash_inside_quoted_path_is_not_comment(tmp_path, monkeypatch, quote):
    target = (tmp_path / "cache # 2026").as_posix()
    path = tmp_path / "app.toml"
    path.write_text(f"[cache] # section comment\ndir = {quote}{target}{quote} # comment\n"
                    "enabled = true# compact comment\n", encoding="utf-8")
    monkeypatch.setattr(config, "tomllib", None)
    loaded = config.load_config(path)
    assert loaded.cache.dir == target
    assert loaded.cache.enabled is True


def test_registry_unreadable_is_not_empty_and_blocks_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "vaults.toml"
    reg = registry.VaultRegistry(path)
    reg.add(tmp_path / "existing", "existing")
    before = path.read_bytes()
    assert [e.name for e in reg.load()] == ["existing"]

    def denied(_path):
        raise PermissionError(errno.EACCES, "synthetic denied", str(path))

    monkeypatch.setattr(registry, "read_toml_file", denied)
    assert [e.name for e in reg.load()] == ["existing"]
    assert reg.read_status == "unknown"
    with pytest.raises(OSError, match="REGISTRY_READ_UNKNOWN"):
        reg.add(tmp_path / "new", "new")
    fresh = registry.VaultRegistry(path)
    with pytest.raises(OSError, match="REGISTRY_READ_UNKNOWN"):
        fresh.save([])
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(path))
    assert doctor.check_registry()["ok"] is False
    assert path.read_bytes() == before


def test_registry_empty_is_known_and_writeable(tmp_path):
    path = tmp_path / "empty.toml"
    path.write_text("version = 4\n", encoding="utf-8")
    reg = registry.VaultRegistry(path)
    assert reg.load() == []
    assert reg.read_status == "ok"
    reg.add(tmp_path / "a", "a")
    assert [entry.name for entry in reg.load()] == ["a"]


def test_cache_write_failure_keeps_memory_and_reports_persistence(tmp_path, monkeypatch):
    import mortis_rag_mcp.indexer as module
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Memory\npersistent failure keyword", encoding="utf-8")
    idx = MarkdownIndexer(vault, AppConfig(cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache"))))
    original = module._CacheCodec.dump

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(module._CacheCodec, "dump", full)
    idx.sync()
    assert idx.search("keyword")
    status = idx.persistence_status
    assert status["state"] == "failed"
    assert status["errors"]["chunks"]["errno"] == errno.ENOSPC
    assert status["errors"]["chunks"]["path"] == str(idx._chunks_cache_path)
    assert "persistence" in idx.index_state()["next_action"].lower()
    assert idx.failed_files == {}
    monkeypatch.setattr(module._CacheCodec, "dump", original)
    idx._save_cache()
    assert idx.persistence_status["state"] == "ready"
    assert idx.persistence_status["errors"] == {}
    reopened = MarkdownIndexer(vault, idx.config)
    assert reopened.all_chunks()


def test_failure_ledger_write_error_clears_after_empty_save(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    idx = MarkdownIndexer(vault, AppConfig(cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache"))))
    idx.failed_files = {"a.md": "synthetic parse failure"}
    original = Path.write_text

    def denied(path, *args, **kwargs):
        if path.name.endswith(".failed.json.tmp"):
            raise OSError(errno.ENOSPC, "synthetic ledger disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", denied)
    idx._save_cache()
    assert idx.persistence_status["errors"]["failed_files"]["errno"] == errno.ENOSPC
    idx.failed_files.clear()
    idx._save_cache()
    assert idx.persistence_status["state"] == "ready"
    assert idx.persistence_status["errors"] == {}


@pytest.mark.parametrize("endpoint,key,allowed", [
    ("http://127.0.0.1:8000/v1/embeddings", "", True),
    ("http://localhost:8000/v1/embeddings", "", True),
    ("https://paid.invalid/v1/embeddings", "", False),
    ("http://127.0.0.1:8000/v1/embeddings", "synthetic-key", False),
])
def test_explicit_doctor_cache_false_only_proven_local_free(tmp_path, monkeypatch, endpoint, key, allowed):
    from mortis_rag_mcp.providers import ExternalEmbeddingProvider
    calls = []

    def fake_embed(self, texts):
        calls.append(texts)
        return [[1.0, 0.0]]

    monkeypatch.setattr(ExternalEmbeddingProvider, "embed", fake_embed)
    cfg = AppConfig(embedding=EmbeddingConfig(mode="external", endpoint=endpoint, model="fixture",
        dimension=2, api_key=key), cache=CacheConfig(enabled=False, dir=str(tmp_path / "cache")))
    result = doctor.probe_embedding(cfg)
    assert result["ok"] is allowed
    assert calls == ([["ping"]] if allowed else [])
    assert not (tmp_path / "cache").exists()
