"""MarkdownIndexer 私有实现包（v0.8.0 自单体 indexer.py 模块化提取）。

依赖方向（单向、自底向上，严禁运行时反向导入 ``mortis_rag_mcp.indexer``
Facade；类型标注仅允许 ``TYPE_CHECKING`` 例外）::

    models  <-  cache_codec
    models  <-  chunking / scanning / search / sync_engine（P3/P4）
    models / cache_codec  <-  snapshot / exemptions / watch（P5）

公开符号一律经 ``mortis_rag_mcp.indexer`` Facade re-export；本包内部符号
（含下划线前缀）不属于公开 API。
"""
