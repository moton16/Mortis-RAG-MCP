from mortis_rag_mcp.config import load_config


def test_unset_config_env_warns_and_remains_loadable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MORTIS_FIXTURE_UNSET_KEY", raising=False)
    path = tmp_path / "app.toml"
    path.write_text('[embedding]\nmode="static"\napi_key="${MORTIS_FIXTURE_UNSET_KEY}"\n',
                    encoding="utf-8")
    assert load_config(path).embedding.mode == "static"
    assert "MORTIS_FIXTURE_UNSET_KEY" in capsys.readouterr().err
