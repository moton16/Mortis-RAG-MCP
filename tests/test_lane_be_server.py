from types import SimpleNamespace

import pytest

from mortis_rag_mcp.server import VaultMcpServer, _tool_definitions


LEGACY_TOOLS = {
    "kb_init", "kb_init_solo", "kb_remove", "kb_describe", "kb_list",
    "kb_ingest", "kb_set_weight", "kb_rebuild", "kb_export", "kb_import",
    "kb_search", "kb_list_files", "kb_read", "kb_stats", "kb_exempt",
}


def bare_server():
    server = object.__new__(VaultMcpServer)
    server.config = SimpleNamespace(diag=None)
    return server


def test_media_schema_keeps_legacy_tools():
    definitions = {tool["name"]: tool for tool in _tool_definitions()}
    assert set(definitions) == LEGACY_TOOLS | {"kb_read_media"}
    assert set(VaultMcpServer._TOOL_ROUTE_TABLE) == set(definitions)
    schema = definitions["kb_read_media"]["inputSchema"]
    assert set(schema["required"]) == {"vault_path", "source", "revision_id", "occurrence_id"}
    assert schema["properties"]["representation"]["default"] == "metadata"
    assert definitions["kb_import"]["inputSchema"]["properties"]["trust_parsed_documents"]["default"] is False


def test_media_content_is_not_double_wrapped():
    server = bare_server()
    result = {"content": [{"type": "text", "text": "metadata"},
                          {"type": "image", "data": "YWJj", "mimeType": "image/png"}]}
    server._kb_read_media = lambda arguments, **kwargs: result
    assert server.call_tool("kb_read_media", {}) is result
    server._kb_list = lambda arguments: {"vaults": []}
    assert server.call_tool("kb_list", {})["content"][0]["type"] == "text"


def test_media_content_diagnostic_path_is_not_double_wrapped(monkeypatch):
    from mortis_rag_mcp import diaglog
    server = bare_server()
    server.config.diag = SimpleNamespace(enabled=True)
    result = {"content": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}]}
    server._kb_read_media = lambda arguments, **kwargs: result
    monkeypatch.setattr(diaglog, "instrument_call", lambda **kwargs: kwargs["handler"](kwargs["arguments"]))
    monkeypatch.setattr(diaglog, "record", lambda **kwargs: None)
    assert server.call_tool("kb_read_media", {}) is result


@pytest.mark.parametrize("arguments", [
    {"source": "a.pdf", "allow_stale": "false"},
    {"source": "a.pdf", "allow_stale": 1},
    {"source": "a.pdf", "media_refs_offset": True},
    {"source": "a.pdf", "media_refs_offset": -1},
])
def test_read_new_arguments_are_strict(arguments):
    with pytest.raises(ValueError):
        bare_server()._kb_read(arguments)


@pytest.mark.parametrize("arguments", [
    {"snapshot": "backup.zip", "trust_parsed_documents": "true"},
    {"snapshot": "backup.zip", "replace": True},
    {"snapshot": "backup.zip", "replace": True, "confirm_replace": "true"},
])
def test_import_requires_explicit_boolean_confirmation(arguments):
    with pytest.raises(ValueError):
        bare_server()._kb_import(arguments)
