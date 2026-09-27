from __future__ import annotations

import ast
import glob
from pathlib import Path
import pytest

import mortis_rag_mcp
import mortis_rag_mcp.indexer as indexer_mod


def test_package_init_exports_frozen():
    """断言 mortis_rag_mcp 顶层 __all__ 严格冻结为 7 项，绝不扩容。"""
    expected = {
        "AppConfig",
        "EmbeddingConfig",
        "RerankerConfig",
        "VectorConfig",
        "Chunk",
        "MarkdownIndexer",
        "load_config",
    }
    assert set(mortis_rag_mcp.__all__) == expected
    assert len(mortis_rag_mcp.__all__) == 7


def test_indexer_facade_public_exports_present():
    """断言 indexer.py Facade 的所有公开 API、历史测试锚点与常量全员在场。"""
    required_symbols = [
        # 公开主类与数据结构
        "MarkdownIndexer",
        "Chunk",
        "SearchFilter",
        "IgnoreMatcher",
        "dedupe_by_content_hash",
        "rerank_chunks",
        # 缓存编解码类
        "_CacheCodec",
        "_VectorsCodec",
        # 扫描与时间戳判据锚点
        "_probe_mtime_tick_ns",
        "_FUTURE_MTIME_RECHECK_LIMIT",
        "_FUTURE_MTIME_MIN_OBSERVATION_GAP_NS",
        "_MTIME_TICK_COARSE_NS",
        "_MTIME_TRUST_MARGIN_NS",
        # 切块与正则锚点
        "_MD_IMAGE_RE",
        "_WIKI_IMAGE_RE",
        "_FENCE_RE",
        "_FENCE_START_RE",
        "_HEADING_RE",
        "_CHAPTER_HEADING_RE",
        "_IMAGE_EXTS",
        "_INDEXABLE_TEXT_EXTS",
        "_BLOCK_IGNORE_START",
        "_BLOCK_IGNORE_END",
        "_image_note",
        "_image_notes_for_line",
        "_inject_image_notes",
        "_is_chapter_heading",
        # 检索辅助
        "_extract_snippet",
        "_candidate_terms",
        "_EMB_DTYPE",
        "_RRF_K",
        "_WORD_RE",
        "_ASCII_RE",
        "_CJK_RE",
        "_SHORT_STOPWORDS",
        "_to_emb",
        # 快照与监听常量
        "_SNAPSHOT_FORMAT",
        "_SNAPSHOT_VERSION",
        "_SNAPSHOT_MEMBERS",
        "_SNAPSHOT_MEMBER_LIMITS",
        "_FS_MAX_DEBOUNCE_WAIT",
    ]

    missing = [sym for sym in required_symbols if not hasattr(indexer_mod, sym)]
    assert not missing, f"Facade missing required exports: {missing}"


def test_markdown_indexer_methods_contract():
    """断言 MarkdownIndexer 核心方法契约完整可用（薄委托覆盖）。"""
    required_methods = [
        "sync",
        "try_sync_with_guard",
        "search",
        "all_chunks",
        "export_snapshot",
        "import_snapshot",
        "get_exemptions",
        "add_exemption_pattern",
        "remove_exemption_pattern",
        "check_exemption",
        "set_file_exemption",
        "purge_cache",
        "rebuild",
        "start_watching",
        "stop_watching",
        "_safe_path",
        "_fts_query",
        "_hybrid_rank",
        "_semantic_rank",
        "_cosine",
        "_chunk_file",
        "_make_chunks",
        "_new_chunk",
    ]

    for m in required_methods:
        assert hasattr(indexer_mod.MarkdownIndexer, m), f"MarkdownIndexer missing method: {m}"


def test_submodules_no_runtime_reverse_import_of_facade():
    """断言 _indexer/ 与 _server/ 所有私有子模块在运行时均无反向导入 indexer Facade。

    仅允许在 if TYPE_CHECKING: 守卫块中导入以满足类型提示。
    """
    root = Path(indexer_mod.__file__).parent
    private_files = list((root / "_indexer").glob("*.py")) + list((root / "_server").glob("*.py"))

    for py_file in private_files:
        with open(py_file, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(py_file))

        # 找到所有非 TYPE_CHECKING 块的 import
        for node in tree.body:
            if isinstance(node, ast.If):
                # 检查是否为 if TYPE_CHECKING:
                cond_src = ast.unparse(node.test)
                if "TYPE_CHECKING" in cond_src:
                    continue  # 忽略 TYPE_CHECKING 保护内的导入

            for subnode in ast.walk(node):
                if isinstance(subnode, ast.Import):
                    for alias in subnode.names:
                        assert "indexer" not in alias.name, f"{py_file.name} imports {alias.name} at runtime"
                elif isinstance(subnode, ast.ImportFrom):
                    mod = subnode.module or ""
                    assert not mod.endswith("indexer") and mod != "indexer", (
                        f"{py_file.name} imports from {mod} at runtime"
                    )
