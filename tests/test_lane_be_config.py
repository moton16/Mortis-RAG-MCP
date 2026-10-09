from dataclasses import asdict
from pathlib import Path

import pytest

from mortis_rag_mcp import config
from mortis_rag_mcp.config import AppConfig, ChunkingConfig, IngestConfig, MediaConfig


def load_text(tmp_path, text):
    path = tmp_path / "app.toml"
    path.write_text('[cache]\ndir = "isolated-cache"\n' + text, encoding="utf-8")
    return config.load_config(path)


def test_new_install_and_programmatic_compatibility(tmp_path):
    assert AppConfig().chunking.mode == "estimated_tokens"  # R2 unified new-library default.
    cfg = load_text(tmp_path, "")
    assert cfg.chunking.mode == "estimated_tokens"
    assert cfg.chunking.mode_explicit is False
    assert cfg.embedding.client_slicing is False
    assert cfg.media.inline_max_bytes == 8388608


@pytest.mark.parametrize("text", ['chunk_size = 1000\n', '[index]\nchunk_overlap = 10\n'])
def test_old_explicit_settings_stay_legacy(tmp_path, text):
    if text.startswith("chunk_size"):
        text = '[index]\n' + text
    cfg = load_text(tmp_path, text)
    assert cfg.chunking.mode == "legacy_chars"
    assert not cfg.chunking.mode_explicit
    assert cfg.chunking.legacy_explicit


@pytest.mark.parametrize("routing, expected", [("auto", "auto"), ("local", "local"), ("mineru", "mineru"), ("cloud", "mineru")])
def test_routing_contract(tmp_path, routing, expected):
    cfg = load_text(tmp_path, f'[ingest]\nrouting = "{routing}"')
    assert cfg.ingest.routing == expected


def test_flat_legacy_and_explicit_override(tmp_path):
    path = tmp_path / "flat.toml"
    path.write_text('chunk_size = 600\n[cache]\ndir = "isolated-cache"', encoding="utf-8")
    assert config.load_config(path).chunking.mode == "legacy_chars"
    cfg = load_text(tmp_path, '[index]\nchunk_size = 600\n[chunking]\nmode = "estimated_tokens"')
    assert cfg.chunking.mode == "estimated_tokens"
    assert cfg.chunking.mode_explicit


def test_legacy_alias_conflict(tmp_path):
    cfg = load_text(tmp_path, '[chunking]\nlegacy_chunking = true')
    assert cfg.chunking.mode == "legacy_chars"
    with pytest.raises(ValueError, match="conflicts"):
        load_text(tmp_path, '[chunking]\nmode = "estimated_tokens"\nlegacy_chunking = true')


@pytest.mark.parametrize("text", [
    '[ingest]\nenabled = "false"',
    '[ingest]\npymupdf_fallback = "false"', '[embedding]\nclient_slicing = "false"',
    '[media]\npreview_enabled = "false"', '[chunking]\nlegacy_chunking = "false"',
    '[chunking]\ntarget_tokens = true', '[chunking]\noverlap_tokens = 384',
    '[chunking]\nhard_limit_tokens = 100', '[chunking]\nestimator_profile = "unknown"',
    '[media]\nrefs_limit = 101', '[media]\ninline_max_bytes = 0',
    '[embedding]\nmedia_adapter = 123',
    '[embedding]\nmedia_dimension = 0',
])
def test_invalid_config_is_rejected(tmp_path, text):
    with pytest.raises(ValueError):
        load_text(tmp_path, text)


@pytest.mark.parametrize("factory, kwargs", [
    (ChunkingConfig, {"target_tokens": True}), (MediaConfig, {"refs_limit": False}),
    (IngestConfig, {"routing": "bogus"}),
])
def test_programmatic_validation(factory, kwargs):
    with pytest.raises(ValueError):
        factory(**kwargs)


def test_fallback_and_normal_parser_match(tmp_path, monkeypatch):
    text = '[chunking]\nmode = "estimated_tokens"\ntarget_tokens = 400\n[media]\nrefs_limit = 12'
    normal = load_text(tmp_path, text)
    monkeypatch.setattr(config, "tomllib", None)
    fallback = load_text(tmp_path, text)
    assert asdict(normal) == asdict(fallback)


def test_example_loads(monkeypatch):
    monkeypatch.setenv("MORTIS_RAG_API_KEY", "test-key")
    cfg = config.load_config(Path(__file__).parents[1] / "config" / "app.toml.example")
    assert cfg.chunking.mode_explicit
    assert cfg.media.preview_max_bytes == 524288


def test_doctor_new_fields_are_read_only(tmp_path, monkeypatch):
    from mortis_rag_mcp import doctor
    cfg = load_text(tmp_path, '[media]\nrefs_limit = 12')
    before = asdict(cfg)
    monkeypatch.setattr(config, "resolve_config_path", lambda _: None)
    monkeypatch.setattr(config, "load_config", lambda _: cfg)
    result, returned = doctor.check_config(None)
    assert result["ok"]
    assert "chunking=" in result["detail"]
    assert "media_provider=" in result["detail"]
    assert returned is cfg
    assert asdict(cfg) == before


def test_media_provider_config_keys_and_validation(tmp_path, monkeypatch):
    from mortis_rag_mcp import doctor
    text = (
        '[embedding]\n'
        'media_adapter = "gemini"\n'
        'media_endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"\n'
        'media_model = "gemini-embedding-2"\n'
        'media_dimension = 768\n'
        'media_api_key_env = "GEMINI_API_KEY"\n'
    )
    cfg = load_text(tmp_path, text)
    assert cfg.embedding.media_adapter == "gemini"
    assert cfg.embedding.media_endpoint == "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
    assert cfg.embedding.media_model == "gemini-embedding-2"
    assert cfg.embedding.media_dimension == 768
    assert cfg.embedding.media_api_key_env == "GEMINI_API_KEY"

    # Also verify media_provider alias works
    text_alias = (
        '[embedding]\n'
        'media_provider = "siliconflow_vl"\n'
        'media_endpoint = "https://api.siliconflow.cn/v1/embeddings"\n'
    )
    cfg_alias = load_text(tmp_path, text_alias)
    assert cfg_alias.embedding.media_adapter == "siliconflow_vl"

    # Verify doctor check_config shows media_provider details
    monkeypatch.setattr(config, "resolve_config_path", lambda _: None)
    monkeypatch.setattr(config, "load_config", lambda _: cfg)
    res, _ = doctor.check_config(None)
    assert "media_provider=" in res["detail"]
    assert "gemini" in res["detail"]
    assert "dim=768" in res["detail"]
    assert "GEMINI_API_KEY" in res["detail"]
