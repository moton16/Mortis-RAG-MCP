from mortis_rag_mcp.ingest.tables import (
    convert_small_tables,
    iter_table_blocks,
    split_large_table,
)


def test_iter_table_blocks_basic():
    # 1. No tables
    lines = ["# Title", "Just text", "More text"]
    assert iter_table_blocks(lines) == []

    # 2. Single line table
    lines = ["# Title", "<table><tr><td>cell</td></tr></table>", "Footer"]
    assert iter_table_blocks(lines) == [(1, 1)]

    # 3. Multiline table
    lines = [
        "Header text",
        "<table>",
        "  <tr>",
        "    <th>H1</th><th>H2</th>",
        "  </tr>",
        "  <tr>",
        "    <td>A</td><td>B</td>",
        "  </tr>",
        "</table>",
        "Footer text",
    ]
    assert iter_table_blocks(lines) == [(1, 8)]

    # 4. Multiple tables
    lines = [
        "<table>",
        "</table>",
        "Between",
        "<table>",
        "  <tr><td>X</td></tr>",
        "</table>",
    ]
    assert iter_table_blocks(lines) == [(0, 1), (3, 5)]


def test_iter_table_blocks_unclosed_protects_to_end():
    lines = [
        "Text before",
        "<table>",
        "  <tr><td>Unclosed</td></tr>",
        "More lines",
        "End of document",
    ]
    blocks = iter_table_blocks(lines)
    assert blocks == [(1, 4)]


def test_convert_small_tables_to_pipe():
    html_table = (
        "Some text before\n"
        "<table>\n"
        "  <tr><th>Name</th><th>Score</th></tr>\n"
        "  <tr><td>Alice</td><td>100</td></tr>\n"
        "  <tr><td>Bob</td><td>90</td></tr>\n"
        "</table>\n"
        "Some text after"
    )
    result = convert_small_tables(html_table)
    assert "<table>" not in result
    assert "| Name | Score |" in result
    assert "| --- | --- |" in result
    assert "| Alice | 100 |" in result
    assert "| Bob | 90 |" in result
    assert "Some text before" in result
    assert "Some text after" in result


def test_convert_small_tables_colspan_rowspan_preserved():
    # Colspan
    html_colspan = (
        "<table>\n"
        "  <tr><th colspan=\"2\">Merged Header</th></tr>\n"
        "  <tr><td>A</td><td>B</td></tr>\n"
        "</table>"
    )
    res_colspan = convert_small_tables(html_colspan)
    assert "<table" in res_colspan
    assert "colspan" in res_colspan

    # Rowspan
    html_rowspan = (
        "<table>\n"
        "  <tr><td rowspan='2'>Merged Cell</td><td>A</td></tr>\n"
        "  <tr><td>B</td></tr>\n"
        "</table>"
    )
    res_rowspan = convert_small_tables(html_rowspan)
    assert "<table" in res_rowspan
    assert "rowspan" in res_rowspan


def test_convert_small_tables_ragged_rows_preserved():
    # Row 1 has 2 cells, Row 2 has 3 cells (width mismatch)
    html_ragged = (
        "<table>\n"
        "  <tr><td>A</td><td>B</td></tr>\n"
        "  <tr><td>C</td><td>D</td><td>E</td></tr>\n"
        "</table>"
    )
    result = convert_small_tables(html_ragged)
    assert "<table" in result
    assert "<tr>" in result


def test_convert_small_tables_max_cells_limit():
    # Table with 6 cells, but max_cells set to 4
    html_table = (
        "<table>\n"
        "  <tr><td>1</td><td>2</td><td>3</td></tr>\n"
        "  <tr><td>4</td><td>5</td><td>6</td></tr>\n"
        "</table>"
    )
    result = convert_small_tables(html_table, max_cells=4)
    assert "<table" in result
    assert "<td>6</td>" in result

    # When max_cells >= 6, it converts
    result_converted = convert_small_tables(html_table, max_cells=6)
    assert "<table>" not in result_converted
    assert "| 1 | 2 | 3 |" in result_converted


def test_split_large_table():
    # Table of 12 lines
    lines = [
        "<table>",
        "  <tr><td>Row 1</td></tr>",
        "  <tr><td>Row 2</td></tr>",
        "  <tr><td>Row 3</td></tr>",
        "  <tr><td>Row 4</td></tr>",
        "  <tr><td>Row 5</td></tr>",
        "  <tr><td>Row 6</td></tr>",
        "  <tr><td>Row 7</td></tr>",
        "  <tr><td>Row 8</td></tr>",
        "  <tr><td>Row 9</td></tr>",
        "  <tr><td>Row 10</td></tr>",
        "</table>",
    ]

    # If max_lines is 5: splits into multiple parts
    parts = split_large_table(lines, max_lines=5)
    assert len(parts) >= 2
    for part in parts:
        assert part[0] == "<table>"
        assert part[-1] == "</table>"
        # Verify internal content has valid tr/td
        content = "\n".join(part)
        assert "<tr><td>" in content

    # If max_lines >= len(lines): no split
    parts_nosplit = split_large_table(lines, max_lines=50)
    assert len(parts_nosplit) == 1
    assert parts_nosplit[0] == lines
