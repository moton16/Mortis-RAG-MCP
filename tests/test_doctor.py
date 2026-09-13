from pathlib import Path
from unittest.mock import patch, MagicMock
from mortis_rag_mcp import doctor


def test_doctor_check_python():
    res = doctor.check_python()
    assert res["ok"] is True


def test_doctor_check_config_static_mode_wording(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    mock_cfg = MagicMock()
    mock_cfg.embedding.mode = "static"
    mock_cfg.embedding.model = ""
    mock_cfg.embedding.endpoint = ""
    mock_cfg.embedding.api_key = ""
    with patch("mortis_rag_mcp.config.load_config", return_value=mock_cfg):
        res, _ = doctor.check_config(None)
        assert res["ok"] is True
        assert "免密(内置哈希)" in res["detail"]


def test_doctor_check_registry_with_missing_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    mock_reg = MagicMock()
    mock_entry1 = MagicMock(path=str(tmp_path / "exist"), name="v1")
    (tmp_path / "exist").mkdir()
    mock_entry2 = MagicMock(path=str(tmp_path / "missing"), name="v2")
    mock_reg.load.return_value = [mock_entry1, mock_entry2]

    with patch("mortis_rag_mcp.registry.VaultRegistry", return_value=mock_reg):
        res = doctor.check_registry()
        # ENG-5: 部分库离线不判死，仍为 True 并给出警告
        assert res["ok"] is True
        assert "1/2 个库可用" in res["detail"]


def test_doctor_record_test_run_does_not_forge_valid_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    # 当 status.json 为空时，仅运行测试绝不能把 overall 赋为 True
    doctor.record_test_run(passed=5, failed=0, skipped=0, total_collected=5)
    data = doctor._read_json()
    assert data["overall"] is False
    assert "tests" in data["sections"]


def test_probe_reranker_api_contract():
    mock_cfg = MagicMock()
    mock_cfg.reranker.enabled = True
    mock_provider = MagicMock()
    with patch("mortis_rag_mcp.providers.create_reranker_provider", return_value=mock_provider):
        res = doctor.probe_reranker(mock_cfg)
        assert res["ok"] is True
        mock_provider.rerank.assert_called_once_with("ping", ["doc"])


def test_doctor_run_quiet_and_write(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    exit_code = doctor.run(full=False, quiet=True)
    assert exit_code in (0, 1)
    status_md = tmp_path / "STATUS.md"
    status_json = tmp_path / "status.json"
    assert status_md.exists()
    assert status_json.exists()
    content = status_md.read_text(encoding="utf-8")
    assert "给 Agent 的硬约束" in content
    assert "python -m mortis_rag_mcp --doctor" in content


def test_doctor_render_md_sanitizes_pipe_and_newlines():
    data = {
        "overall": True,
        "sections": {
            "config": {
                "ok": True,
                "detail": "Line1\nLine2|with pipe\r\nLine3",
            }
        }
    }
    rendered = doctor.render_md(data)
    # The table row must NOT contain unescaped newline or bare pipe inside detail
    for line in rendered.splitlines():
        if line.startswith("| 配置 |"):
            assert "\n" not in line
            assert "Line1 Line2\\|with pipe Line3" in line


def test_doctor_check_config_reranker_missing_key():
    mock_cfg = MagicMock()
    mock_cfg.embedding.mode = "static"
    mock_cfg.embedding.model = ""
    mock_cfg.embedding.endpoint = ""
    mock_cfg.embedding.api_key = ""
    mock_cfg.reranker.enabled = True
    mock_cfg.reranker.endpoint = "https://remote.api/rerank"
    mock_cfg.reranker.api_key = ""
    with patch("mortis_rag_mcp.config.load_config", return_value=mock_cfg):
        res, _ = doctor.check_config(None)
        assert res["ok"] is False
        assert "reranker api_key 缺失" in res["detail"]


def test_doctor_check_config_local_endpoint_hosts():
    for host in ("http://0.0.0.0:11434", "http://[::1]:11434", "http://host.docker.internal:11434"):
        mock_cfg = MagicMock()
        mock_cfg.embedding.mode = "external"
        mock_cfg.embedding.model = "nomic-embed-text"
        mock_cfg.embedding.endpoint = host
        mock_cfg.embedding.api_key = ""
        mock_cfg.reranker.enabled = False
        with patch("mortis_rag_mcp.config.load_config", return_value=mock_cfg):
            res, _ = doctor.check_config(None)
            assert res["ok"] is True
            assert "免密/本地" in res["detail"]


def test_probe_embedding_dimension_mismatch():
    mock_cfg = MagicMock()
    mock_cfg.embedding.mode = "external"
    mock_cfg.embedding.dimension = 1024
    mock_provider = MagicMock()
    mock_provider.embed.return_value = [[0.1] * 1536]
    with patch("mortis_rag_mcp.providers.create_embedding_provider", return_value=mock_provider):
        res = doctor.probe_embedding(mock_cfg)
        assert res["ok"] is False
        assert "维度不匹配" in res["detail"]


def test_doctor_run_does_not_duplicate_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    # 模拟第一次有全量探测结果
    status_json = tmp_path / "status.json"
    status_json.write_text(
        '{"sections": {"embedding_api": {"ok": true, "detail": "dim=384, 10ms"}}}',
        encoding="utf-8"
    )
    # 连续运行两次 full=False
    doctor.run(full=False, quiet=True)
    doctor.run(full=False, quiet=True)
    data = doctor._read_json()
    detail = data["sections"]["embedding_api"]["detail"]
    assert detail.count("（沿用上次全量探测）") == 1


def test_doctor_record_test_run_preserves_generated_at(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    status_json = tmp_path / "status.json"
    status_json.write_text(
        '{"generated_at": "2026-09-01T00:00:00+08:00", "sections": {}}',
        encoding="utf-8"
    )
    doctor.record_test_run(passed=10, failed=0, skipped=0, total_collected=10)
    data = doctor._read_json()
    assert data["generated_at"] == "2026-09-01T00:00:00+08:00"


def test_doctor_is_local_endpoint_strict_hostname():
    # Valid locals
    assert doctor._is_local_endpoint("http://localhost:11434") is True
    assert doctor._is_local_endpoint("http://127.0.0.1:11434") is True
    assert doctor._is_local_endpoint("http://127.0.0.2:11434") is True
    assert doctor._is_local_endpoint("http://[::1]:11434") is True
    assert doctor._is_local_endpoint("http://host.docker.internal:11434") is True

    # Hostname substring attacks / remote hosts with localhost in path or query
    assert doctor._is_local_endpoint("https://api.openai.com/v1?tag=localhost") is False
    assert doctor._is_local_endpoint("https://localhost.attacker.com/v1") is False
    assert doctor._is_local_endpoint("https://proxy.internal/127.0.0.1/rerank") is False
    assert doctor._is_local_endpoint("") is False


def test_doctor_external_mode_requires_endpoint_and_model():
    mock_cfg = MagicMock()
    mock_cfg.embedding.mode = "external"
    mock_cfg.embedding.endpoint = ""
    mock_cfg.embedding.model = ""
    mock_cfg.embedding.api_key = "sk-valid-key"
    mock_cfg.reranker.enabled = False

    with patch("mortis_rag_mcp.config.load_config", return_value=mock_cfg):
        res, _ = doctor.check_config(None)
        assert res["ok"] is False
        assert "endpoint缺失" in res["detail"]
        assert "model缺失" in res["detail"]


def test_doctor_external_mode_unprobed_cannot_be_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    mock_cfg = MagicMock()
    mock_cfg.embedding.mode = "external"
    mock_cfg.embedding.endpoint = "https://api.remote.com"
    mock_cfg.embedding.model = "text-embed"
    mock_cfg.embedding.api_key = "sk-valid"
    mock_cfg.reranker.enabled = False

    with patch("mortis_rag_mcp.config.load_config", return_value=mock_cfg):
        # When full=False and no previous probe exists, external mode MUST NOT evaluate to True
        exit_code = doctor.run(full=False, quiet=True)
        assert exit_code == 1
        data = doctor._read_json()
        assert data["overall"] is False


def test_doctor_run_preserves_generated_at_on_incremental(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    status_json = tmp_path / "status.json"
    status_json.write_text(
        '{"generated_at": "2026-09-10T12:00:00+08:00", "sections": {"embedding_api": {"ok": true, "detail": "ok"}}}',
        encoding="utf-8"
    )
    doctor.run(full=False, quiet=True)
    data = doctor._read_json()
    assert data["generated_at"] == "2026-09-10T12:00:00+08:00"


def test_doctor_run_marks_expired_if_past_freshness_days(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_status_dir", lambda: tmp_path)
    status_json = tmp_path / "status.json"
    # 20 days ago
    status_json.write_text(
        '{"generated_at": "2026-08-01T12:00:00+08:00", "sections": {"embedding_api": {"ok": true, "detail": "ok"}}}',
        encoding="utf-8"
    )
    exit_code = doctor.run(full=False, quiet=True)
    assert exit_code == 1
    data = doctor._read_json()
    assert data["overall"] is False

