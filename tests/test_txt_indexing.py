from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


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


def test_txt_indexing_and_chapter_headings(tmp_path):
    vault = tmp_path / "book_vault"
    vault.mkdir(parents=True)

    # 包含中文章节标记的纯文本小说
    novel_txt = (
        "第一章 异界苏醒\n"
        "林云睁开双眼，发现自己身处一片神秘古林之中，四周古木参天。\n\n"
        "第二章 神秘传承\n"
        "一道青色流光划破天际，径直没入林云的眉心深处，激荡起古老符文。\n"
    )
    (vault / "novel.txt").write_text(novel_txt, encoding="utf-8")

    # 包含长句但非标题的正文段落（F-09 门禁测试）
    body_guard_txt = (
        "第十章是全书核心概念汇总。这里有句号，而且是一句普通正文，绝对不能被误判为章节标题。\n"
        "普通正文第二行继续叙述。\n"
    )
    (vault / "guard.txt").write_text(body_guard_txt, encoding="utf-8")

    # 放置未支持格式文件，用于验证 F-08
    (vault / "paper.epub").write_bytes(b"dummy epub content")
    (vault / "backup.zip").write_bytes(b"dummy zip content")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "BookVault"}}},
        # 1. 检索第一章内容
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "异界苏醒 古木参天", "vault_path": "BookVault"}}},
        # 2. 检索第二章内容
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "神秘传承 古老符文", "vault_path": "BookVault"}}},
        # 3. F-01: 测试 kb_read 原生读取 .txt 原文，确保不被沙箱拦截
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_read", "arguments": {"source": "novel.txt", "start_line": 1, "end_line": 3, "vault_path": "BookVault"}}},
        # 4. F-08: 验证 kb_stats 包含 skipped_unsupported 统计
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_stats", "arguments": {"vault_path": "BookVault"}}},
        # 5. 验证 F-09: guard.txt 的 heading 不能是包含句号的长段落
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "逻辑代数 核心概念", "vault_path": "BookVault"}}},
    ]
    responses = _run_stdio(config, requests)

    # 验证 2: kb_init 报告中包含 skipped_unsupported
    init_res = json.loads(responses[1]["result"]["content"][0]["text"])
    assert "skipped_unsupported" in init_res
    assert init_res["skipped_unsupported"].get("epub") == 1
    assert init_res["skipped_unsupported"].get("zip") == 1

    # 验证 3: 第一章检索并验证 heading
    r3 = json.loads(responses[2]["result"]["content"][0]["text"])
    assert len(r3["chunks"]) > 0
    c3 = r3["chunks"][0]
    assert c3["source"] == "novel.txt"
    heading3 = c3.get("heading") or c3["metadata"].get("heading", "")
    assert "第一章 异界苏醒" in heading3

    # 验证 4: 第二章检索并验证 heading
    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    assert len(r4["chunks"]) > 0
    c4 = r4["chunks"][0]
    assert c4["source"] == "novel.txt"
    heading4 = c4.get("heading") or c4["metadata"].get("heading", "")
    assert "第二章 神秘传承" in heading4

    # 验证 5 (F-01): kb_read 读取 .txt 文件成功，无沙箱错误
    r5 = responses[4]["result"]
    assert r5.get("isError") is not True
    read_data = json.loads(r5["content"][0]["text"])
    assert "异界苏醒" in read_data["content"]

    # 验证 6 (F-08): kb_stats 正确返回 skipped_unsupported
    r6 = json.loads(responses[5]["result"]["content"][0]["text"])
    assert "skipped_unsupported" in r6
    assert r6["skipped_unsupported"].get("epub") == 1
    assert r6["skipped_unsupported"].get("zip") == 1

    # 验证 7 (F-09): 门禁生效，长句带句号未被作为 heading
    r7 = json.loads(responses[6]["result"]["content"][0]["text"])
    if r7["chunks"]:
        guard_chunk = next((c for c in r7["chunks"] if c["source"] == "guard.txt"), None)
        if guard_chunk:
            guard_heading = guard_chunk.get("heading") or guard_chunk["metadata"].get("heading", "")
            assert "这里有句号" not in guard_heading


def test_txt_equal_length_incremental_update(tmp_path):
    vault = tmp_path / "txt_vault"
    vault.mkdir()
    txt_file = vault / "data.txt"
    txt_file.write_text("AAAA BBBB CCCC", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests1 = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "TxtVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "AAAA", "vault_path": "TxtVault"}}},
    ]
    resp1 = _run_stdio(config, requests1)
    r3 = json.loads(resp1[2]["result"]["content"][0]["text"])
    assert len(r3["chunks"]) > 0

    # 等长替换: "AAAA BBBB CCCC" -> "ZZZZ BBBB CCCC" (14 字符等长)
    time.sleep(0.05)
    txt_file.write_text("ZZZZ BBBB CCCC", encoding="utf-8")

    requests2 = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "ZZZZ", "vault_path": "TxtVault"}}},
    ]
    resp2 = _run_stdio(config, requests2)
    r_update = json.loads(resp2[1]["result"]["content"][0]["text"])
    assert len(r_update["chunks"]) > 0
    assert "ZZZZ" in r_update["chunks"][0]["content"]
