"""Validate shipped skill examples against the current public MCP schemas."""

import json
import os
import re
from pathlib import Path

import pytest

from mortis_rag_mcp.server import _tool_definitions


SKILL = Path(
    os.environ.get(
        "MORTIS_SKILL_DIR",
        str(Path(__file__).resolve().parents[1] / "skills" / "mortis-rag-mcp"),
    )
)
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in _tool_definitions()}


def validate(value, schema, location="arguments"):
    """Strictly check examples, including unknown fields silently ignored at runtime."""
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        checks = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }
        assert any(checks[t] for t in types), f"{location}: expected {types}"
    if "enum" in schema:
        assert value in schema["enum"], f"{location}: invalid enum {value!r}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, compare in (
            ("minimum", lambda a, b: a >= b),
            ("maximum", lambda a, b: a <= b),
        ):
            if key in schema:
                assert compare(value, schema[key]), f"{location}: outside {key}"
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        assert not missing, f"{location}: missing {sorted(missing)}"
        properties = schema.get("properties", {})
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties"))
            assert isinstance(child, dict), f"{location}: unknown field {key}"
            validate(item, child, f"{location}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{location}[{i}]")


def example_call(example):
    if "name" in example and "arguments" in example:
        assert set(example) == {"name", "arguments"}
        return example["name"], example["arguments"]
    # Also validate pre-0.9 skill examples that show only tool arguments.
    if "query" in example:
        return "kb_search", example
    if "occurrence_id" in example:
        return "kb_read_media", example
    if "action" in example:
        return "kb_ingest", example
    return "kb_read", example


def test_skill_frontmatter_supported_fields():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    header = re.match(r"^---\n(.*?)\n---", text, re.S)
    assert header, "missing YAML frontmatter"
    keys = set(re.findall(r"^([a-z][a-z-]*):", header[1], re.M))
    assert {"name", "description"} <= keys
    assert keys <= {"name", "description", "license", "allowed-tools", "metadata"}


def test_skill_reference_links_resolve():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for link in re.findall(r"\]\((references/[^)#]+)(?:#[^)]*)?\)", text):
        path = (SKILL / link).resolve()
        assert path.is_relative_to(SKILL.resolve()), link
        assert path.is_file(), link
        assert path.read_text(encoding="utf-8").strip(), link


def test_skill_examples_use_public_schema():
    examples = 0
    files = [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"```json\s*\n(.*?)\n```", text, re.S):
            name, arguments = example_call(json.loads(block))
            assert name in SCHEMAS, f"{path}: unknown tool {name}"
            validate(arguments, SCHEMAS[name], f"{path.name}:{name}")
            examples += 1
    assert examples, "no executable examples"


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("kb_read_media", {"inline": True}),
        (
            "kb_read_media",
            {
                "vault_path": "notes",
                "source": "diagram.png",
                "revision_id": "returned-revision",
                "occurrence_id": "returned-occurrence",
                "inline": True,
            },
        ),
        ("kb_read", {"source": "doc.pdf", "revision_id": "invented-parameter"}),
        ("kb_search", {"query": "diagram", "top_k": True}),
        (
            "kb_read_media",
            {
                "vault_path": "notes",
                "source": "diagram.png",
                "revision_id": "returned-revision",
                "occurrence_id": "returned-occurrence",
                "representation": "image",
            },
        ),
        ("kb_read", {"source": "note.md", "start_line": 0}),
    ],
)
def test_example_checker_rejects_invalid_calls(name, arguments):
    with pytest.raises(AssertionError):
        validate(arguments, SCHEMAS[name])
