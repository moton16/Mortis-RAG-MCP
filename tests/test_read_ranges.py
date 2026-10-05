from __future__ import annotations

import json
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, IndexConfig
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp._indexer.reading import ReadResult, read_file_result, ReadFileNotFoundError
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def test_read_out_of_bounds_diagnostics(tmp_path: Path):
    """C69a Req 1: 10行文件请求 start=11340/end=11450 必须报错且包含详细诊断提示，不得空白成功。"""
    vault = tmp_path / "bounds_vault"
    vault.mkdir()
    lines = [f"Line {i:02d} text" for i in range(1, 11)]
    (vault / "short.md").write_text("\n".join(lines), encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="BVault")

    # 1. 直接通过 _kb_read 抛出具名 ValueError
    with pytest.raises(ValueError) as exc_info:
        server._kb_read({
            "source": "short.md",
            "start_line": 11340,
            "end_line": 11450,
            "vault_path": "BVault",
        })
    err_msg = str(exc_info.value)
    assert "requested" in err_msg
    assert "11340" in err_msg
    assert "actual" in err_msg
    assert "10" in err_msg
    assert "short.md" in err_msg
    assert "核对分卷行号或用heading" in err_msg

    # 2. 验证协议级 handle 返回 isError=True
    handle_res = server.handle({
        "method": "tools/call",
        "id": 101,
        "params": {
            "name": "kb_read",
            "arguments": {
                "source": "short.md",
                "start_line": 11340,
                "end_line": 11450,
                "vault_path": "BVault",
            },
        },
    })
    assert handle_res is not None
    assert handle_res["result"]["isError"] is True
    err_text = handle_res["result"]["content"][0]["text"]
    assert "11340" in err_text
    assert "核对分卷行号或用heading" in err_text


def test_read_start1_and_eof_and_clamp(tmp_path: Path):
    """C69a Req 7 & 8: start=1, start=EOF, start=EOF+1, end省略/超EOF/倒置及只end。"""
    vault = tmp_path / "clamp_vault"
    vault.mkdir()
    lines = [f"Row {i:02d}" for i in range(1, 11)]
    (vault / "doc.md").write_text("\n".join(lines), encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="CVault")

    # 1. start=1, end=10 (全量显式)
    res = server._kb_read({"source": "doc.md", "start_line": 1, "end_line": 10, "vault_path": "CVault"})
    assert res["effective_start_line"] == 1
    assert res["effective_end_line"] == 10
    assert res["total_lines"] == 10
    assert res["start_line"] == 1
    assert res["end_line"] == 10
    assert res["content"] == "\n".join(lines)
    assert res["truncated"] is False

    # 2. start=10 (EOF 单行)
    res = server._kb_read({"source": "doc.md", "start_line": 10, "vault_path": "CVault"})
    assert res["effective_start_line"] == 10
    assert res["effective_end_line"] == 10
    assert res["content"] == "Row 10"

    # 3. start=11 (EOF + 1 越界)
    with pytest.raises(ValueError, match="requested: 11.*actual: 10"):
        server._kb_read({"source": "doc.md", "start_line": 11, "vault_path": "CVault"})

    # 4. end=999 (超EOF 正常钳制到 total_lines)
    res = server._kb_read({"source": "doc.md", "start_line": 5, "end_line": 999, "vault_path": "CVault"})
    assert res["start_line"] == 5
    assert res["end_line"] == 999
    assert res["effective_start_line"] == 5
    assert res["effective_end_line"] == 10
    assert res["total_lines"] == 10

    # 5. 只传 end_line=3 (start 缺省回退 1)
    res = server._kb_read({"source": "doc.md", "end_line": 3, "vault_path": "CVault"})
    assert res["start_line"] is None
    assert res["end_line"] == 3
    assert res["effective_start_line"] == 1
    assert res["effective_end_line"] == 3
    assert res["content"] == "Row 01\nRow 02\nRow 03"

    # 6. 范围倒置 (end < start)
    with pytest.raises(ValueError, match="end_line must be >= start_line"):
        server._kb_read({"source": "doc.md", "start_line": 5, "end_line": 4, "vault_path": "CVault"})


def test_read_empty_file_branches(tmp_path: Path):
    """C69a Req 7 & 8: 空文件两分支（无范围返回空内容且effective为null；带范围报错）。"""
    vault = tmp_path / "empty_vault"
    vault.mkdir()
    (vault / "empty.md").write_text("", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="EVault")

    # 1. 无显式范围：返回 content="", total_lines=0, effective两者为 None
    res = server._kb_read({"source": "empty.md", "vault_path": "EVault"})
    assert res["content"] == ""
    assert res["total_lines"] == 0
    assert res["effective_start_line"] is None
    assert res["effective_end_line"] is None
    assert res["start_line"] is None
    assert res["end_line"] is None
    assert res["content_end_line"] is None
    assert res["next_start_line"] is None
    assert res["next_start_char"] is None
    assert res["truncated"] is False

    # 2. 带 start_line=1 请求空文件：明确越界报错
    with pytest.raises(ValueError, match="超出空文件范围.*actual: 0"):
        server._kb_read({"source": "empty.md", "start_line": 1, "vault_path": "EVault"})

    # 3. 带 end_line=1 请求空文件：明确越界报错
    with pytest.raises(ValueError, match="超出空文件范围.*actual: 0"):
        server._kb_read({"source": "empty.md", "end_line": 1, "vault_path": "EVault"})


def test_read_bom_crlf_unicode(tmp_path: Path):
    """C69a Req 4: UTF-8-BOM、CRLF换行与 CJK/Emoji 字符读取准确性。"""
    vault = tmp_path / "encoding_vault"
    vault.mkdir()

    # 带 UTF-8 BOM 与 CRLF 换行的中文与 Emoji 笔记
    content_str = "# 深度标题 🚀\r\n第二行包含中文测试：雪豹\r\n第三行结束 🌟\r\n"
    raw_bom = "\ufeff".encode("utf-8") + content_str.encode("utf-8")
    (vault / "bom_note.md").write_bytes(raw_bom)

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="EncVault")

    res = server._kb_read({"source": "bom_note.md", "vault_path": "EncVault"})
    # BOM 不应被视作正文字符泄漏在行首
    assert res["content"].startswith("# 深度标题 🚀")
    assert "雪豹" in res["content"]
    assert res["total_lines"] == 3
    assert res["effective_start_line"] == 1
    assert res["effective_end_line"] == 3


def test_read_invalid_parameters(tmp_path: Path):
    """C69a Req 6 & 11 & 13: 严格校验布尔、小数、负数与非法组合参数。"""
    vault = tmp_path / "param_vault"
    vault.mkdir()
    (vault / "note.md").write_text("line 1\nline 2\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="PVault")

    # 1. start_line 显式布尔被拒绝（防 _parse_int(True)==1 假通过）
    with pytest.raises(ValueError, match="start_line must be an integer >= 1, got boolean True"):
        server._kb_read({"source": "note.md", "start_line": True, "vault_path": "PVault"})

    # 2. start_line 小数被拒绝
    with pytest.raises(ValueError, match="start_line must be an integer >= 1, got float 1.5"):
        server._kb_read({"source": "note.md", "start_line": 1.5, "vault_path": "PVault"})

    # 3. start_line 零与负数被拒绝
    with pytest.raises(ValueError, match="start_line must be >= 1"):
        server._kb_read({"source": "note.md", "start_line": 0, "vault_path": "PVault"})

    # 4. end_line 显式布尔被拒绝
    with pytest.raises(ValueError, match="end_line must be an integer >= 1, got boolean False"):
        server._kb_read({"source": "note.md", "end_line": False, "vault_path": "PVault"})

    # 5. start_char 显式布尔与负数被拒绝
    with pytest.raises(ValueError, match="start_char must be an integer >= 0, got boolean True"):
        server._kb_read({"source": "note.md", "start_line": 1, "start_char": True, "vault_path": "PVault"})
    with pytest.raises(ValueError, match="start_char must be >= 0"):
        server._kb_read({"source": "note.md", "start_line": 1, "start_char": -1, "vault_path": "PVault"})

    # 6. 未传 start_line 但 start_char 非零报错
    with pytest.raises(ValueError, match="start_char requires start_line"):
        server._kb_read({"source": "note.md", "start_char": 5, "vault_path": "PVault"})

    # 7. start_char 超过行长度报错
    with pytest.raises(ValueError, match="超出行长度范围"):
        server._kb_read({"source": "note.md", "start_line": 1, "start_char": 100, "vault_path": "PVault"})


def test_read_unindexed_direct_access(tmp_path: Path):
    """C69a Req 3: 未索引的磁盘文件可直接通过 kb_read 读取，不受 sync 阻塞或遗漏。"""
    vault = tmp_path / "direct_vault"
    vault.mkdir()
    (vault / "unindexed.txt").write_text("Freshly written text without indexing\nLine 2", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="DVault")

    # 不执行 indexer.sync()，直接读取
    res = server._kb_read({"source": "unindexed.txt", "vault_path": "DVault"})
    assert res["content"] == "Freshly written text without indexing\nLine 2"
    assert res["total_lines"] == 2


def test_read_sandbox_and_security(tmp_path: Path):
    """C69a Req 3: 严格校验沙箱越界与非文本扩展名。"""
    vault = tmp_path / "sec_vault"
    vault.mkdir()
    (vault / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="SVault")

    # 1. 越界路径
    with pytest.raises(ValueError, match="source must stay inside the vault"):
        server._kb_read({"source": "../outside.md", "vault_path": "SVault"})

    # 2. 非白名单格式
    with pytest.raises(ValueError, match="source must be a Markdown or plain-text file"):
        server._kb_read({"source": "photo.png", "vault_path": "SVault"})


def test_read_helper_delimiter_boundary_and_continuation(tmp_path: Path):
    """C69a Req 12: 纯 helper 验证分隔符前 (本行, len) 与分隔符后 (下一行, 0) 的精准游标映射与无缝拼接。"""
    p = tmp_path / "sample.txt"
    p.write_text("abc\ndef", encoding="utf-8")

    # 1. cap=3 -> 返回 "abc", next=(1, 3) (停在分隔符前)
    res1 = read_file_result(p, "sample.txt", start_line=1, end_line=2, max_chars=3)
    assert res1.content == "abc"
    assert res1.truncated is True
    assert res1.content_end_line == 1
    assert res1.next_start_line == 1
    assert res1.next_start_char == 3

    # 从 (1, 3) 续读 -> 返回 "\ndef"
    res1_cont = read_file_result(p, "sample.txt", start_line=res1.next_start_line, end_line=2, start_char=res1.next_start_char)
    assert res1_cont.content == "\ndef"
    assert res1.content + res1_cont.content == "abc\ndef"

    # 2. cap=4 -> 返回 "abc\n", next=(2, 0) (停在分隔符后)
    res2 = read_file_result(p, "sample.txt", start_line=1, end_line=2, max_chars=4)
    assert res2.content == "abc\n"
    assert res2.truncated is True
    assert res2.content_end_line == 1
    assert res2.next_start_line == 2
    assert res2.next_start_char == 0

    # 从 (2, 0) 续读 -> 返回 "def"
    res2_cont = read_file_result(p, "sample.txt", start_line=res2.next_start_line, end_line=2, start_char=res2.next_start_char)
    assert res2_cont.content == "def"
    assert res2.content + res2_cont.content == "abc\ndef"


def test_read_long_single_line_multi_page_restoration(tmp_path: Path):
    """C69a Req 10, 11, 12: 单行超 cap (300字, read_max_chars=100) 多页读取并完整无损拼接复原。"""
    vault = tmp_path / "longline_vault"
    vault.mkdir()
    # 构造一行 300 字符的长单行
    long_line = "A" * 100 + "B" * 100 + "C" * 100
    (vault / "single_long.md").write_text(long_line, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n[index]\nread_max_chars = 100\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(vault), name="LongVault")

    # Page 1
    p1 = server._kb_read({"source": "single_long.md", "start_line": 1, "end_line": 1, "vault_path": "LongVault"})
    assert p1["content"] == "A" * 100
    assert p1["truncated"] is True
    assert p1["content_end_line"] == 1
    assert p1["next_start_line"] == 1
    assert p1["next_start_char"] == 100

    # Page 2
    p2 = server._kb_read({
        "source": "single_long.md",
        "start_line": p1["next_start_line"],
        "end_line": 1,
        "start_char": p1["next_start_char"],
        "vault_path": "LongVault",
    })
    assert p2["content"] == "B" * 100
    assert p2["truncated"] is True
    assert p2["content_end_line"] == 1
    assert p2["next_start_line"] == 1
    assert p2["next_start_char"] == 200

    # Page 3
    p3 = server._kb_read({
        "source": "single_long.md",
        "start_line": p2["next_start_line"],
        "end_line": 1,
        "start_char": p2["next_start_char"],
        "vault_path": "LongVault",
    })
    assert p3["content"] == "C" * 100
    assert p3["truncated"] is False
    assert p3["content_end_line"] == 1
    assert p3["next_start_line"] is None
    assert p3["next_start_char"] is None

    # 完整复原
    restored = p1["content"] + p2["content"] + p3["content"]
    assert restored == long_line


def test_read_multi_line_page_by_page_restoration(tmp_path: Path):
    """C69a Req 12: 5 行多行文件（每行50字）在 read_max_chars=105 跨行截断下的多页连续续读复原。"""
    vault = tmp_path / "multiline_vault"
    vault.mkdir()
    raw_lines = [f"Line {i:02d}: " + ("x" * 40) for i in range(1, 6)]
    expected_full = "\n".join(raw_lines)
    (vault / "multi.md").write_text(expected_full, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n[index]\nread_max_chars = 105\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(vault), name="MultiVault")

    collected_parts = []
    curr_line: int | None = 1
    curr_char = 0
    pages = 0

    while curr_line is not None:
        pages += 1
        assert pages <= 5, "避免死循环分页"
        args: dict = {"source": "multi.md", "start_line": curr_line, "vault_path": "MultiVault"}
        if curr_char > 0:
            args["start_char"] = curr_char
        res = server._kb_read(args)
        collected_parts.append(res["content"])
        curr_line = res["next_start_line"]
        curr_char = res["next_start_char"] or 0

    assert "".join(collected_parts) == expected_full


def test_read_facade_and_server_contract(tmp_path: Path):
    """C69a Req 5 & 9: Facade indexer.read() 不截断字符，保持兼容；_read_result 返回 ReadResult。"""
    vault = tmp_path / "facade_vault"
    vault.mkdir()
    text = "A" * 50000
    (vault / "big.md").write_text(text, encoding="utf-8")

    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)

    # 1. 公开编程 read 不截断
    read_str = indexer.read("big.md")
    assert len(read_str) == 50000

    # 2. 私有 _read_result 返回 ReadResult
    r_res = indexer._read_result("big.md", max_chars=1000)
    assert isinstance(r_res, ReadResult)
    assert r_res.truncated is True
    assert len(r_res.content) == 1000


def test_read_file_missing_and_encoding_error(tmp_path: Path):
    """C69a Req 15: 丢失文件与坏编码异常 fail-closed，异常不泄露绝对路径。"""
    vault = tmp_path / "error_vault"
    vault.mkdir()
    (vault / "corrupt.md").write_bytes(b"\xff\xfe\x00\x00\xff")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ErrVault")

    # 1. 文件不存在
    with pytest.raises(ReadFileNotFoundError) as exc_info:
        server._kb_read({"source": "nonexistent.md", "vault_path": "ErrVault"})
    msg = str(exc_info.value)
    assert "nonexistent.md" in msg
    assert str(vault) not in msg, "禁止向调用方泄露物理绝对路径"

    # 2. 编码损坏
    with pytest.raises(ValueError) as exc_info2:
        server._kb_read({"source": "corrupt.md", "vault_path": "ErrVault"})
    msg2 = str(exc_info2.value)
    assert "corrupt.md" in msg2
    assert "UTF-8" in msg2
