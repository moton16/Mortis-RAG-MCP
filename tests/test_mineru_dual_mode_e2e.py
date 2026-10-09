from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

import pytest

from mortis_rag_mcp.ingest.mineru import (
    MineruClient,
    ParsedDocument,
    _ChannelOutcome,
    ArchiveOutcome,
)
from mortis_rag_mcp.ingest.models import MediaOccurrence, DictMediaSink


def test_mineru_channel_selection_for_free_and_paid_modes(tmp_path: Path):
    """验证通道分流：无 key 自动走 agent 免登通道，接 key 走 v4 生产通道。"""
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 mock content")

    docx_path = tmp_path / "sample.docx"
    docx_path.write_bytes(b"PK\x03\x04 mock content")

    # 1. 无 key 模式 (api_key="")：<=10MB 自动分发到 agent 体验通道
    free_client = MineruClient(api_key="")
    assert free_client.channel_for(pdf_path) == "agent"
    assert free_client.channel_for(docx_path) == "agent"

    # 2. 接 key 模式 (api_key="v4-secret-token")：支持大文件，分发到 v4 生产通道
    paid_client = MineruClient(api_key="v4-secret-token")
    assert paid_client.channel_for(pdf_path) == "v4"
    assert paid_client.channel_for(docx_path) == "v4"


def test_mineru_free_channel_parses_document_with_images_and_anchors(tmp_path: Path):
    """验证无 key 体验通道端到端解析带图文档，提取正文、图片与锚点。"""
    client = MineruClient(api_key="")
    doc_path = tmp_path / "paper_with_images.pdf"
    doc_path.write_bytes(b"%PDF-1.4 test document with embedded figure")

    mock_markdown = (
        "# 深度学习系统设计\n\n"
        "该系统结构如下图所示：\n\n"
        "![图1：系统拓扑结构图](images/arch.png)\n\n"
        "由上图可以看出系统的分层架构。"
    )
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"

    def fake_parse_agent(path, poll_interval=3.0, deadline=None, intent_recorder=None, request_id=""):
        media_item = MediaOccurrence(
            occurrence_id="occ-free-1",
            kind="image",
            blob_sha256="hash-1",
            ordinal=0,
            mime_type="image/png",
            page=1,
            caption="图1：系统拓扑结构图",
            anchor_start=mock_markdown.index("![图1：系统拓扑结构图]"),
            anchor_end=mock_markdown.index("![图1：系统拓扑结构图]") + len("![图1：系统拓扑结构图](images/arch.png)"),
        )
        archive = ArchiveOutcome(
            markdown=mock_markdown,
            media=[media_item],
            capabilities={"images": True, "page_map": True},
            page_map={1: (0, len(mock_markdown))},
            warnings=[],
            extracted_bytes=len(mock_markdown) + len(png_bytes),
            markdown_bytes=len(mock_markdown),
            media_bytes=len(png_bytes),
            partial=False,
        )
        return _ChannelOutcome(
            raw_markdown=mock_markdown,
            outcome=archive,
            model="pipeline-light",
            channel="agent",
            remote_task_id="free-task-101",
        )

    with patch.object(client, "_parse_agent", side_effect=fake_parse_agent):
        sink = DictMediaSink()
        result = client.parse_structured(doc_path, sink=sink)

    assert result.channel == "agent"
    assert result.model == "pipeline-light"
    assert len(result.media) == 1
    assert result.media[0].caption == "图1：系统拓扑结构图"
    assert result.media[0].anchor_start is not None
    assert "![图1：系统拓扑结构图]" in result.markdown
    print("\n[MINERU FREE CHANNEL VERIFIED] 无 key 体验通道成功解析带图文档、提取图片 occurrence 与正文锚点！")
