import io
import urllib.request

import pytest

from mortis_rag_mcp.ingest import mineru


@pytest.mark.parametrize("payload", [b"[" * 65 + b"0" + b"]" * 65,
                                    b"[" + b"0," * 100_001 + b"0]"], ids=["depth", "nodes"])
def test_http_json_rejects_structure_before_load(monkeypatch, payload):
    monkeypatch.setattr(mineru.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(payload))
    monkeypatch.setattr(mineru.json, "loads", lambda *a, **kw: pytest.fail("loads before admission"))
    with pytest.raises(mineru.MineruError) as error:
        mineru._http_json(urllib.request.Request("https://example.com/poll"), 1)
    assert error.value.code_str == "RESOURCE_LIMIT"
    assert error.value.retryable is False


@pytest.mark.parametrize("exception", [MemoryError, RecursionError])
def test_http_json_converts_allocation_errors(monkeypatch, exception):
    monkeypatch.setattr(mineru.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b'{"ok":true}'))

    def fail(*args, **kwargs):
        raise exception()

    monkeypatch.setattr(mineru.json, "loads", fail)
    with pytest.raises(mineru.MineruError) as error:
        mineru._http_json(urllib.request.Request("https://example.com/poll"), 1)
    assert error.value.code_str == "RESOURCE_LIMIT"
    assert error.value.retryable is False
