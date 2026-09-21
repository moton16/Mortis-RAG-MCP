from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mortis_rag_mcp.indexer import Chunk, MarkdownIndexer


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "VAULT_MCP_REGISTRY": str(config.parent / "vaults.toml")},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_extract_snippet_unit():
    # 1. 空内容
    assert MarkdownIndexer._extract_snippet("", ["test"]) == ""

    # 2. 短于 max_len 内容原样返回
    short_text = "这是一段很短的正文。"
    assert MarkdownIndexer._extract_snippet(short_text, ["正文"]) == short_text

    # 3. 关键词高光定位
    prefix = "前置冗余说明内容" * 15
    keyword = "核心关键密码12345"
    suffix = "后续无关填充文本" * 15
    long_text = f"{prefix} {keyword} {suffix}"

    snippet = MarkdownIndexer._extract_snippet(long_text, ["密码"], max_len=60)
    assert keyword in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    assert len(snippet) <= 70

    # 4. 未命中关键词时 fallback 取开头并加省略号
    snippet_no_hit = MarkdownIndexer._extract_snippet(long_text, ["不存在的词"], max_len=50)
    assert snippet_no_hit.startswith("前置冗余说明内容")
    assert snippet_no_hit.endswith("...")


def test_chunk_to_dict_preview():
    long_content = "第一行正文。\n" + "中间填充数据 " * 50 + "\n目标关键词出现在这里。\n" + "末尾数据 " * 50
    chunk = Chunk(
        id="c1",
        content=long_content,
        source="doc.md",
        title="Doc Title",
        metadata={"heading": "Section 1", "start_line": 10, "end_line": 25, "source_pdf": "doc.pdf"},
        score=0.95,
    )

    # 默认 preview=False (full mode)
    full_dict = chunk.to_dict(preview=False)
    assert "content" in full_dict
    assert full_dict["content"] == long_content
    assert "snippet" not in full_dict
    assert full_dict["heading"] == "Section 1"
    assert full_dict["start_line"] == 10
    assert full_dict["end_line"] == 25
    assert full_dict["source_pdf"] == "doc.pdf"

    # preview=True (preview mode)
    preview_dict = chunk.to_dict(preview=True, query_tokens=["关键词"])
    assert "content" not in preview_dict
    assert "snippet" in preview_dict
    assert "关键词" in preview_dict["snippet"]
    assert preview_dict["char_count"] == len(long_content)
    assert preview_dict["heading"] == "Section 1"
    assert preview_dict["start_line"] == 10
    assert preview_dict["end_line"] == 25
    assert preview_dict["source_pdf"] == "doc.pdf"

    # 校验 payload 大小大幅降低 (元数据固定开销下，长正文压缩率显著)
    assert len(preview_dict["snippet"]) < len(long_content) * 0.3
    full_json = json.dumps(full_dict, ensure_ascii=False)
    preview_json = json.dumps(preview_dict, ensure_ascii=False)
    assert len(preview_json) < len(full_json) * 0.6


def test_kb_search_preview_mode_integration(tmp_path):
    vault = tmp_path / "vault_preview"
    vault.mkdir(parents=True)
    vault_b = tmp_path / "vault_b"
    vault_b.mkdir(parents=True)
    (vault_b / "guide.md").write_text("# 飞船维护手册\n这是飞船的日常检修与星际跃迁引擎保养指南。", encoding="utf-8")

    # 写入长篇文档
    long_paragraph = "这是用于测试长段落展开的文章正文内容，包含大量的背景描述。" * 20
    target_part = "【特定高价值信号：星际跃迁引擎超光速公式 E=mc^3】"
    doc_content = f"# 宇宙航行指南\n\n{long_paragraph}\n\n{target_part}\n\n{long_paragraph}\n"
    (vault / "starship.md").write_text(doc_content, encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "StarshipVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b), "name": "MaintenanceVault"}}},
        # 4. 正常全量检索 (默认 preview=False)
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "星际跃迁引擎", "vault_path": "StarshipVault"}}},
        # 5. 预览模式 (preview=True)
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "星际跃迁引擎", "vault_path": "StarshipVault", "preview": True}}},
        # 6. 预览模式 (mode="preview")
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "星际跃迁引擎", "vault_path": "StarshipVault", "mode": "preview"}}},
        # 7. 跨库分组模式下的 preview=True
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "星际跃迁引擎", "group_by_vault": True, "preview": True}}},
    ]

    responses = _run_stdio(config, requests)

    for i, resp in enumerate(responses):
        assert "error" not in resp, f"Request {i+1} failed with error: {resp.get('error')}"

    # 4. full mode 验证
    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    assert len(r4["chunks"]) > 0
    full_chunk = r4["chunks"][0]
    assert "content" in full_chunk
    assert "snippet" not in full_chunk
    assert "星际跃迁引擎" in full_chunk["content"]

    # 5. preview=True 验证
    r5 = json.loads(responses[4]["result"]["content"][0]["text"])
    assert len(r5["chunks"]) > 0
    preview_chunk = r5["chunks"][0]
    assert "content" not in preview_chunk
    assert "snippet" in preview_chunk
    assert "星际跃迁引擎" in preview_chunk["snippet"]
    assert preview_chunk["start_line"] >= 1
    assert preview_chunk["end_line"] >= preview_chunk["start_line"]
    assert preview_chunk["char_count"] > len(preview_chunk["snippet"])

    # 6. mode="preview" 验证
    r6 = json.loads(responses[5]["result"]["content"][0]["text"])
    chunk_r6 = r6["chunks"][0]
    assert "content" not in chunk_r6
    assert "snippet" in chunk_r6

    # 7. group_by_vault preview=True 验证
    r7 = json.loads(responses[6]["result"]["content"][0]["text"])
    assert "groups" in r7
    group_chunks = r7["groups"][0]["chunks"]
    assert len(group_chunks) > 0
    assert "snippet" in group_chunks[0]
    assert "content" not in group_chunks[0]

    # Payload 瘦身幅度检验：正文截断压缩显著，整体返回体大幅减轻
    assert len(preview_chunk["snippet"]) < len(full_chunk["content"]) * 0.3
    raw_full_size = len(json.dumps(r4, ensure_ascii=False))
    raw_preview_size = len(json.dumps(r5, ensure_ascii=False))
    assert raw_preview_size < raw_full_size * 0.7


def test_snippet_centers_on_chinese_keyword():
    # 前文 500 字无关内容 + 中文关键词句；断言 snippet 包含该关键词且不以正文开头
    prefix = "无关背景内容填充段落，" * 50
    keyword = "【噪声容限关键计算】"
    suffix = "后续无关填充段落，" * 50
    long_text = f"{prefix}{keyword}{suffix}"

    # 模拟真实中文整句查询（无空格分词，作为单个长 token 传入）
    query_tokens = ["噪声容限怎么计算"]
    snippet = MarkdownIndexer._extract_snippet(long_text, query_tokens, max_len=150)

    assert "噪声容限" in snippet
    assert not snippet.startswith("无关背景内容填充段落")
    assert snippet.startswith("...")
    assert snippet.endswith("...")


def test_snippet_handles_single_cjk_char_query():
    # 单字中文兜底，不越界
    prefix = "前置冗余数据" * 40
    target = "【算】"
    suffix = "后置冗余数据" * 40
    long_text = f"{prefix}{target}{suffix}"

    snippet = MarkdownIndexer._extract_snippet(long_text, ["算"], max_len=60)
    assert "算" in snippet
    assert not snippet.startswith("前置冗余数据")
    assert snippet.startswith("...")

    # 单字不在文本中，安全回退到开头，不越界崩溃
    snippet_no_hit = MarkdownIndexer._extract_snippet(long_text, ["错"], max_len=50)
    assert snippet_no_hit.startswith("前置冗余数据")
    assert snippet_no_hit.endswith("...")

