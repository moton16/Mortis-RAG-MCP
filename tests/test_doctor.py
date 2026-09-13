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
