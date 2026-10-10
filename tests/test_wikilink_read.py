from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """自动将注册表重定向至单测独立 tmp_path，杜绝污染全局 ~/.mortis_rag_mcp/vaults.toml。"""
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    """通过 stdio 进程启动真实 MCP Server 验证 JSON-RPC 往返与 stderr 纯净度。"""
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    reg_file = str(config.parent / "vaults.toml")
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "MORTIS_RAG_REGISTRY": reg_file, "VAULT_MCP_REGISTRY": reg_file},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, f"stderr 必须保持干净，实际输出: {proc.stderr}"
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_wikilink_read_unique_short_name(tmp_path: Path):
    """F5b: 深层子目录笔记通过无后缀短名与带后缀短名均可唯一命中并正确读取。"""
    vault = tmp_path / "notes_vault"
    sub_dir = vault / "courses" / "network"
    sub_dir.mkdir(parents=True)
    target_file = sub_dir / "计算机网络体系.md"
    target_file.write_text("# 计算机网络体系\n深入浅出OSI七层参考模型\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="UniVault")
    indexer = server._indexer_for({"vault_path": "UniVault"})
    indexer.sync()

    # 1. 无后缀短名寻址
    res1 = server._kb_read({"source": "计算机网络体系", "vault_path": "UniVault"})
    assert res1["source"] == "courses/network/计算机网络体系.md"
    assert "深入浅出OSI七层参考模型" in res1["content"]

    # 2. 带后缀短名寻址
    res2 = server._kb_read({"source": "计算机网络体系.md", "vault_path": "UniVault"})
    assert res2["source"] == "courses/network/计算机网络体系.md"
    assert "深入浅出OSI七层参考模型" in res2["content"]

    # 3. 大小写不敏感与双链括号语法寻址
    res3 = server._kb_read({"source": "[[计算机网络体系]]", "vault_path": "UniVault"})
    assert res3["source"] == "courses/network/计算机网络体系.md"
    assert "深入浅出OSI七层参考模型" in res3["content"]


def test_wikilink_read_case_insensitivity(tmp_path: Path):
    """F5b: 英文文件名大小写不敏感匹配 (casefold)。"""
    vault = tmp_path / "case_vault"
    sub = vault / "deep" / "path"
    sub.mkdir(parents=True)
    (sub / "OperatingSystem.md").write_text("# OS Principles\nProcess and Thread\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="CaseVault")
    indexer = server._indexer_for({"vault_path": "CaseVault"})
    indexer.sync()

    # 小写查询
    res = server._kb_read({"source": "operatingsystem", "vault_path": "CaseVault"})
    assert res["source"] == "deep/path/OperatingSystem.md"
    assert "Process and Thread" in res["content"]


def test_wikilink_read_ambiguous_matches(tmp_path: Path):
    """F5b: 多个子目录下同名笔记命中多义，抛出 ValueError 并列出最多 5 个候选。"""
    vault = tmp_path / "ambig_vault"
    dir_a = vault / "folder_a"
    dir_b = vault / "folder_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    (dir_a / "summary.md").write_text("Summary A\n", encoding="utf-8")
    (dir_b / "summary.md").write_text("Summary B\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="AmbigVault")
    indexer = server._indexer_for({"vault_path": "AmbigVault"})
    indexer.sync()

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"source": "summary", "vault_path": "AmbigVault"})

    msg = str(exc_info.value)
    assert "ambiguous note name 'summary' matches multiple files:" in msg
    assert "folder_a/summary.md" in msg
    assert "folder_b/summary.md" in msg


def test_wikilink_read_md_and_txt_collision(tmp_path: Path):
    """F5b: .md 与 .txt 同 stem 撞名计入多义，绝不静默偏袒任一格式。"""
    vault = tmp_path / "collision_vault"
    dir_a = vault / "docs"
    dir_b = vault / "raw"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    (dir_a / "readme.md").write_text("# Markdown Readme\n", encoding="utf-8")
    (dir_b / "readme.txt").write_text("Plain text Readme\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ColVault")
    indexer = server._indexer_for({"vault_path": "ColVault"})
    indexer.sync()

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"source": "readme", "vault_path": "ColVault"})

    msg = str(exc_info.value)
    assert "ambiguous note name 'readme' matches multiple files:" in msg
    assert "docs/readme.md" in msg
    assert "raw/readme.txt" in msg


def test_wikilink_read_short_name_with_heading(tmp_path: Path):
    """F5b: 短名定位到深层文件后，继续按 heading 提取对应段落并返回行号。"""
    vault = tmp_path / "heading_vault"
    sub = vault / "knowledge" / "cs"
    sub.mkdir(parents=True)

    content = (
        "# 概述\n"
        "这里是概述内容\n\n"
        "## 传输层协议\n"
        "TCP 是面向连接的可靠传输协议。\n"
        "UDP 是无连接的不可靠传输协议。\n\n"
        "## 应用层协议\n"
        "HTTP/DNS/SMTP\n"
    )
    (sub / "protocol.md").write_text(content, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="HeadVault")
    indexer = server._indexer_for({"vault_path": "HeadVault"})
    indexer.sync()

    # 以短名 + heading 提取
    res = server._kb_read({"source": "protocol", "heading": "传输层协议", "vault_path": "HeadVault"})
    assert res["source"] == "knowledge/cs/protocol.md"
    assert "TCP 是面向连接的可靠传输协议" in res["content"]
    assert "应用层协议" not in res["content"]
    assert res["start_line"] >= 3
    assert res["end_line"] >= res["start_line"]


def test_wikilink_read_zero_match_retains_original_error(tmp_path: Path):
    """F5b: 零命中时保持原有报错行为（无后缀报扩展名错误，有合法后缀但不存在报 FileNotFoundError）。"""
    vault = tmp_path / "zero_vault"
    vault.mkdir()
    (vault / "existing.md").write_text("Hello\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ZeroVault")
    indexer = server._indexer_for({"vault_path": "ZeroVault"})
    indexer.sync()

    # 无后缀没有物理命中也没有virtual事实，按现行virtual合同拒绝。
    with pytest.raises(ValueError, match="UNAVAILABLE: virtual source 'completely_nonexistent'"):
        server._kb_read({"source": "completely_nonexistent", "vault_path": "ZeroVault"})

    # 2. 有合规后缀但不存在：保留原有 FileNotFoundError
    with pytest.raises(FileNotFoundError):
        server._kb_read({"source": "completely_nonexistent.md", "vault_path": "ZeroVault"})


def test_wikilink_read_exempted_file_not_accessible(tmp_path: Path):
    """F5b: 豁免/忽略文件未入索引 (_chunks)，天然不可被短名寻址。"""
    vault = tmp_path / "exempt_vault"
    vault.mkdir(parents=True)
    (vault / ".vaultignore").write_text("secret_folder/\n", encoding="utf-8")
    sub = vault / "secret_folder"
    sub.mkdir(parents=True)
    (sub / "confidential.md").write_text("# Top Secret\nConfidential data\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ExVault")
    indexer = server._indexer_for({"vault_path": "ExVault"})
    indexer.sync()

    # confidential.md 命中 .vaultignore，未入索引，不进入 _chunks
    assert "secret_folder/confidential.md" not in indexer._chunks

    # 短名寻址不得复活豁免文件/虚拟事实。
    with pytest.raises(ValueError, match="UNAVAILABLE: virtual source 'confidential'"):
        server._kb_read({"source": "confidential", "vault_path": "ExVault"})


def test_wikilink_read_root_file_and_slashed_path_unaffected(tmp_path: Path):
    """F5b: 根目录存在同名文件或传入已含斜杠的路径时，不触发短名逻辑直接直连访问。"""
    vault = tmp_path / "direct_vault"
    vault.mkdir(parents=True)
    sub = vault / "sub"
    sub.mkdir(parents=True)
    (vault / "note.md").write_text("Root note content\n", encoding="utf-8")
    (sub / "note.md").write_text("Sub note content\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="DirVault")
    indexer = server._indexer_for({"vault_path": "DirVault"})
    indexer.sync()

    # 1. 根目录存在 note.md：直接读取根目录文件，不抛歧义异常（支持 note.md 与 [[note.md]]）
    res_root = server._kb_read({"source": "note.md", "vault_path": "DirVault"})
    assert res_root["source"] == "note.md"
    assert "Root note content" in res_root["content"]

    res_root_wiki = server._kb_read({"source": "[[note.md]]", "vault_path": "DirVault"})
    assert res_root_wiki["source"] == "note.md"
    assert "Root note content" in res_root_wiki["content"]

    # 2. 传含斜杠路径：直接精准读取子目录文件（支持 sub/note.md、[[sub/note.md]] 与 [[sub/note]]）
    res_sub = server._kb_read({"source": "sub/note.md", "vault_path": "DirVault"})
    assert res_sub["source"] == "sub/note.md"
    assert "Sub note content" in res_sub["content"]

    res_sub_wiki = server._kb_read({"source": "[[sub/note.md]]", "vault_path": "DirVault"})
    assert res_sub_wiki["source"] == "sub/note.md"
    assert "Sub note content" in res_sub_wiki["content"]

    res_sub_noext = server._kb_read({"source": "[[sub/note]]", "vault_path": "DirVault"})
    assert res_sub_noext["source"] == "sub/note.md"
    assert "Sub note content" in res_sub_noext["content"]

    # 3. 传无后缀的短名 note 时，仍应命中多义并报错
    with pytest.raises(ValueError, match="ambiguous note name 'note' matches multiple files:"):
        server._kb_read({"source": "note", "vault_path": "DirVault"})

    with pytest.raises(ValueError, match="ambiguous note name '\\[\\[note\\]\\]' matches multiple files:"):
        server._kb_read({"source": "[[note]]", "vault_path": "DirVault"})


def test_wikilink_read_piped_alias_syntax(tmp_path: Path):
    """F5b: Obsidian 管道别名语法 [[Target|Alias]] 正确提取目标笔记。"""
    vault = tmp_path / "piped_vault"
    sub = vault / "wiki" / "deep"
    sub.mkdir(parents=True)
    (sub / "Architecture.md").write_text("# Architecture\nMicroservices and Event-Driven\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="PipedVault")
    indexer = server._indexer_for({"vault_path": "PipedVault"})
    indexer.sync()

    # [[Architecture|系统架构]]
    res = server._kb_read({"source": "[[Architecture|系统架构]]", "vault_path": "PipedVault"})
    assert res["source"] == "wiki/deep/Architecture.md"
    assert "Microservices and Event-Driven" in res["content"]


def test_wikilink_read_heading_anchor_syntax(tmp_path: Path):
    """F5b: Obsidian 锚点语法 [[Target#Heading]] 或 [[Target#Heading|Alias]] 提取章节。"""
    vault = tmp_path / "anchor_vault"
    sub = vault / "notes"
    sub.mkdir(parents=True)
    doc_content = (
        "# Summary\nOverview text\n\n"
        "## Core Principles\nFirst principle of software engineering.\n\n"
        "## Anti-Patterns\nWhat not to do.\n"
    )
    (sub / "DesignDoc.md").write_text(doc_content, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="AnchorVault")
    indexer = server._indexer_for({"vault_path": "AnchorVault"})
    indexer.sync()

    # 1. [[DesignDoc#Core Principles]] 自动提取章节
    res1 = server._kb_read({"source": "[[DesignDoc#Core Principles]]", "vault_path": "AnchorVault"})
    assert res1["source"] == "notes/DesignDoc.md"
    assert "First principle of software engineering" in res1["content"]
    assert "Anti-Patterns" not in res1["content"]

    # 2. [[DesignDoc#Core Principles|核心原则]] 锚点 + 别名复合语法
    res2 = server._kb_read({"source": "[[DesignDoc#Core Principles|核心原则]]", "vault_path": "AnchorVault"})
    assert res2["source"] == "notes/DesignDoc.md"
    assert "First principle of software engineering" in res2["content"]


def test_stdio_wikilink_read(tmp_path: Path):
    """F5b stdio 集成：通过 MCP JSON-RPC 调用短名寻址。"""
    vault = tmp_path / "stdio_wiki_vault"
    sub = vault / "articles" / "ai"
    sub.mkdir(parents=True)
    (sub / "AgentDesign.md").write_text("# Agent Design\nAutonomous agent patterns\n", encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")

    # kb_init 的 sync 在后台线程执行，kb_read 在首建完成前短名寻址会查空 _chunks。
    # 拆成两个 stdio 会话：第二会话的 kb_stats 走 try_sync_with_guard，在锁空闲时
    # 同步完成首建，消除对后台线程时序的依赖（CI 慢解释器上曾稳定复现竞态）。
    init_requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "WikiVault"}}},
    ]
    _run_stdio(app_toml, init_requests)

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_stats", "arguments": {"vault_path": "WikiVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_read", "arguments": {"source": "[[AgentDesign]]", "vault_path": "WikiVault"}}},
    ]
    responses = _run_stdio(app_toml, requests)
    read_res = json.loads(responses[2]["result"]["content"][0]["text"])

    assert read_res["source"] == "articles/ai/AgentDesign.md"
    assert "Autonomous agent patterns" in read_res["content"]
