import hashlib
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, CacheConfig, ChunkingConfig, EmbeddingConfig, load_config
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.server import _tool_definitions

ROOT = Path(__file__).resolve().parents[1]


def test_docs_describe_real_cli_and_unverified_native():
    for name in ("README.md", "QUICKSTART_user.md", "skills/mortis-rag-mcp/SKILL.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "--abandon-request" in text
        assert "index_state" in text
        assert "未验" in text or "尚未" in text
    skill = (ROOT / "skills/mortis-rag-mcp/SKILL.md").read_text(encoding="utf-8")
    assert "运行（或请用户运行）" not in skill


def test_actual_sixteen_tools_and_example_templates():
    tools = {t["name"]: t for t in _tool_definitions()}
    assert len(tools) == 16
    assert {"kb_read_media", "kb_ingest", "kb_import"} <= tools.keys()
    assert "retry" in tools["kb_ingest"]["inputSchema"]["properties"]["action"]["enum"]
    example = load_config(ROOT / "config/app.toml.example")
    assert example.embedding.query_template == "{text}"
    assert example.embedding.document_template == "{text}"


def test_synthetic_legacy_cache_upgrade_and_snapshot_recovery(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "note.md"
    source.write_text("# Legacy\n\nalpha beta content", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = CacheConfig(dir=str(tmp_path / "cache"), enabled=True)
    old = MarkdownIndexer(vault, AppConfig(cache=cache,
        embedding=EmbeddingConfig(mode="static", dimension=8),
        chunking=ChunkingConfig(mode="legacy_chars", mode_explicit=True)))
    old.sync()
    original = [(c.id, c.content) for c in old.all_chunks()]
    backup = tmp_path / "backup.zip"
    old.export_snapshot(backup)
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    old.close_document_store()
    upgraded = MarkdownIndexer(vault, AppConfig(cache=cache,
        embedding=EmbeddingConfig(mode="static", dimension=8)))
    try:
        upgraded.sync()
        assert upgraded._chunking_config.mode == "legacy_chars"
        assert [(c.id, c.content) for c in upgraded.all_chunks()] == original
        upgraded.import_snapshot(backup)
        upgraded.sync()
        assert [(c.id, c.content) for c in upgraded.all_chunks()] == original
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest
    finally:
        upgraded.close_document_store()
