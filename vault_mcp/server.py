from __future__ import annotations

import json
import sys
import threading
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config, resolve_config_path
from .indexer import Chunk, MarkdownIndexer, SearchFilter, rerank_chunks
from .registry import VaultEntry, VaultRegistry, registry_path

SERVER_INFO = {"name": "mortis-rag-mcp", "version": "0.4.1", "title": "Mortis'RAG MCP"}


def _parse_epoch(value: Any) -> float | None:
    """把 MCP 参数解析成 epoch 秒：接受数字、数字字符串和 ISO 8601 字符串。

    解析不出来就返回 None（该条件不生效），绝不抛异常——参数来自外部客户端，
    一个拼写错误的时间不该让整个 kb_search 失败。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_tags(value: Any) -> list[str] | None:
    """tags 参数归一化：逗号分隔的字符串和字符串数组都接受，空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        items: list[Any] = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return None
    tags = [str(item).strip() for item in items if str(item).strip()]
    return tags or None


def _parse_int(value: Any) -> int | None:
    """防御式整数解析：解析失败返回 None（该条件不生效）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _search_filter(arguments: dict[str, Any]) -> SearchFilter:
    """从 kb_search 的 arguments 构造 SearchFilter，全部字段都可缺省。"""
    limit = _parse_int(arguments.get("limit"))
    if limit is not None and limit < 1:
        limit = None
    offset = _parse_int(arguments.get("offset"))
    return SearchFilter(
        path_prefix=str(arguments.get("path_prefix") or "").strip(),
        tags=_parse_tags(arguments.get("tags")),
        mtime_after=_parse_epoch(arguments.get("mtime_after")),
        mtime_before=_parse_epoch(arguments.get("mtime_before")),
        offset=max(0, offset) if offset is not None else 0,
        limit=limit,
    )


def _json_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _json_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_definitions() -> list[dict[str, Any]]:
    vault_path_hint = "可选，已注册知识库的绝对路径；仅注册了一个库时可省略"
    return [
        {
            "name": "kb_init",
            "description": "注册（初始化）一个文件夹为知识库：校验目录、写入用户级注册表（跨重启保留）、后台建立索引并启动文件监听。首次使用或要纳入新文件夹时调用。",
            "inputSchema": {"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string", "description": "必填，要注册为知识库的文件夹绝对路径"},
                "name": {"type": "string", "description": "可选，显示名，默认取文件夹名"},
            }},
        },
        {
            "name": "kb_unregister",
            "description": "注销一个已注册的知识库：停止文件监听并从注册表移除（不影响文件夹本身）。",
            "inputSchema": {"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string", "description": "必填，已注册知识库的绝对路径"},
                "purge_cache": {"type": "boolean", "default": False, "description": "是否同时删除该库的磁盘索引缓存"},
            }},
        },
        {
            "name": "kb_vaults",
            "description": "列出所有已注册的知识库（含存活状态与索引进度）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "kb_set_weight",
            "description": "设置知识库的检索权重：跨库检索时该库所有 chunk 的分数会乘以该系数，用于表达\"这个库更重要\"（默认 1.0，取值 0 < w <= 100）。",
            "inputSchema": {"type": "object", "required": ["vault_path", "weight"], "properties": {
                "vault_path": {"type": "string", "description": "必填，已注册知识库的绝对路径"},
                "weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 100, "description": "必填，权重系数，取值 0 < weight <= 100；1.0 为默认不放大"},
            }},
        },
        {
            "name": "kb_rebuild",
            "description": "删除指定知识库的磁盘缓存并强制全量重建索引（首次建库或内容大改后用）。",
            "inputSchema": {"type": "object", "properties": {
                "vault_path": {"type": "string", "description": vault_path_hint},
            }},
        },
        {
            "name": "kb_list",
            "description": "列出已索引的 Markdown 文件。可传 vault_path 指定知识库。",
            "inputSchema": {"type": "object", "properties": {"vault_path": {"type": "string", "description": vault_path_hint}}},
        },
        {
            "name": "kb_search",
            "description": "搜索知识库并返回结构化原始 chunks。不传 vault_path 时跨全部注册库 fan-out 检索（结果带 vault 字段）。",
            "inputSchema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "default": 10}, "use_rerank": {"type": "boolean", "default": True},
                "vault_path": {"type": "string", "description": "可选，已注册知识库的绝对路径；缺省时跨全部注册库检索"},
                "group_by_vault": {"type": "boolean", "default": False, "description": "可选，仅跨库检索（不传 vault_path）时生效：结果按知识库分组返回 groups，每组取 top_k 条"},
                "path_prefix": {"type": "string", "description": "可选，只保留 source 以该前缀开头的 chunk（source 是库内相对 posix 路径，如 '教材/'）"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选，frontmatter 标签过滤：命中任一标签即保留（大小写不敏感，自动去掉 '#' 前缀）"},
                "mtime_after": {"type": ["number", "string"], "description": "可选，只保留修改时间 >= 该值的文件；epoch 秒或 ISO 8601 字符串（如 '2026-01-01'）"},
                "mtime_before": {"type": ["number", "string"], "description": "可选，只保留修改时间 <= 该值的文件；epoch 秒或 ISO 8601 字符串"},
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "可选，跳过前 N 条结果（分页用）"},
                "limit": {"type": "integer", "minimum": 1, "description": "可选，本页最多返回条数；缺省时用 top_k"},
            }},
        },
        {
            "name": "kb_read",
            "description": "读取知识库原文，不调用 LLM。多库环境下建议显式传 vault_path（fan-out 结果中的 source 是库内相对路径）。",
            "inputSchema": {"type": "object", "required": ["source"], "properties": {
                "source": {"type": "string"}, "heading": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1},
                "vault_path": {"type": "string", "description": vault_path_hint},
            }},
        },
        {"name": "kb_stats", "description": "返回指定知识库的索引状态、失败文件、最后同步时间和模型信息。", "inputSchema": {"type": "object", "properties": {
            "vault_path": {"type": "string", "description": vault_path_hint},
        }}},
        {
            "name": "kb_exempt",
            "description": "查看、添加、删除知识库的 RAG 豁免项（排除不希望被检索的私密/草稿内容）。支持查看规则、添加/删除 .vaultignore 通配符、标记/取消单个文件豁免、检查文件豁免状态。",
            "inputSchema": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add_pattern", "remove_pattern", "exempt_file", "unexempt_file", "check"],
                        "description": "操作类型：list (列出当前豁免规则与统计), add_pattern (向 .vaultignore 添加排除通配符), remove_pattern (从 .vaultignore 移除规则), exempt_file (将单个文件设为豁免), unexempt_file (取消单个文件豁免), check (检测某个文件是否被豁免及原因)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "排除规则通配符，用于 add_pattern / remove_pattern（例如 '日记/*', '*.draft.md', '私密/'）",
                    },
                    "source": {
                        "type": "string",
                        "description": "文件相对路径，用于 exempt_file / unexempt_file / check（例如 '日记/2026-08-19.md'）",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["frontmatter", "ignore_file"],
                        "default": "frontmatter",
                        "description": "单文件豁免机制：'frontmatter' (修改文件标头写入 rag: false) 或 'ignore_file' (写入 .vaultignore)",
                    },
                    "vault_path": {
                        "type": "string",
                        "description": "可选，已注册知识库的绝对路径；仅注册了一个库时可省略",
                    },
                },
            },
        },
    ]


def _text_content(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


class VaultMcpServer:
    def __init__(self, config_path: str | Path | None = None) -> None:
        # 0.3.0 起配置解析链：显式 --app-config > VAULT_MCP_CONFIG 环境变量
        # > ~/.vault_mcp/config.toml > 内置默认值。源码中不再有任何个人路径。
        self.config = load_config(resolve_config_path(config_path))
        self.registry = VaultRegistry()
        self._indexers: dict[str, MarkdownIndexer] = {}
        self._startup_lock = threading.Lock()
        self._started = False
        self._migrate_legacy()
        # 所有已注册库在后台线程串行预索引，MCP 握手（initialize）永不阻塞。
        self._start_background_index()

    def _migrate_legacy(self) -> None:
        """First run after upgrading: import the legacy [vault].path into the
        registry so this device keeps working with zero manual steps."""
        if self.registry.path.is_file():
            return
        legacy = self.config.vault_path
        if not legacy or not Path(legacy).is_dir():
            return
        try:
            self.registry.add(legacy)
        except OSError:
            # Registry file unwritable: degrade to session-only registration
            # instead of blocking startup.
            try:
                self.registry.add(legacy, persist=False)
            except ValueError:
                pass

    def _start_background_index(self) -> None:
        with self._startup_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._startup_index_all, daemon=True, name="vault-startup").start()

    def _startup_index_all(self) -> None:
        # One thread walks every registered vault sequentially: N vaults must
        # not fire N concurrent embedding storms at the external API.
        for entry in self.registry.load():
            if not Path(entry.path).is_dir():
                continue
            try:
                indexer = self._indexer_for({"vault_path": entry.path})
                indexer.sync()
            except Exception:
                continue

    def _resolve_vault_path(self, vault_path: str, *, for_registration: bool = False) -> str:
        """Resolve a vault path against the user-level registry.

        0.3.0: the old single-root containment check (LFI guard) is replaced by
        a registration allow-list — only folders explicitly registered via
        kb_init can be indexed or read, which keeps arbitrary-directory access
        opt-in instead of inherited from a hardcoded path.
        """
        p = Path(vault_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                "vault_path must be an absolute path; call kb_vaults to list registered vaults, or kb_init to register a folder"
            )
        candidate = str(p.resolve())
        if for_registration:
            if not p.is_dir():
                raise ValueError(f"not a readable directory: {candidate}")
            return candidate
        if self.registry.get(candidate) is None:
            raise ValueError(
                f"vault not registered: {candidate}; call kb_init first, or kb_vaults to list registered vaults"
            )
        return candidate

    def _default_vault_path(self) -> str:
        """Default vault when a call omits vault_path: the single registered
        vault. With multiple vaults the caller must be explicit (kb_search
        fans out before this is consulted)."""
        entries = self.registry.load()
        if not entries:
            raise ValueError("no vault registered; call kb_init with your notes folder first")
        if len(entries) == 1:
            return entries[0].path
        listed = "\n".join(f"  - {entry.path} ({entry.name})" for entry in entries)
        raise ValueError(f"multiple vaults registered; pass an explicit vault_path:\n{listed}")

    def _indexer_for(self, arguments: dict[str, Any]) -> MarkdownIndexer:
        raw = str(arguments.get("vault_path") or "").strip()
        if not raw:
            raw = self._default_vault_path()
        vault_path = self._resolve_vault_path(raw)
        key = str(Path(vault_path).expanduser().resolve())
        indexer = self._indexers.get(key)
        if indexer is None:
            indexer = MarkdownIndexer(vault_path, self.config)
            indexer.start_watching()
            self._indexers[key] = indexer
        return indexer

    def _list_vaults(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for entry in self.registry.load():
            exists = Path(entry.path).is_dir()
            key = str(Path(entry.path).resolve())
            indexer = self._indexers.get(key)
            items.append({
                "name": entry.name,
                "path": entry.path,
                "registered_at": entry.registered_at,
                "weight": entry.weight,
                "exists": exists,
                "indexed": indexer is not None,
                "files": len(indexer._chunks) if indexer is not None else None,
                "last_sync": indexer.last_sync if indexer is not None else None,
            })
        return {"vaults": items}

    def _kb_init(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = str(arguments.get("path", "")).strip()
        if not path:
            raise ValueError("path is required for kb_init")
        name_arg = str(arguments.get("name", "")).strip() or None
        resolved = self._resolve_vault_path(path, for_registration=True)
        # Register first (fail fast, no half state), then build the indexer.
        try:
            entry = self.registry.add(resolved, name_arg)
        except OSError:
            entry = self.registry.add(resolved, name_arg, persist=False)
        indexer = self._indexer_for({"vault_path": entry.path})
        threading.Thread(target=indexer.sync, daemon=True, name="vault-init").start()
        md_files = sum(1 for _ in Path(entry.path).rglob("*.md") if _.is_file())
        return {
            "registered": True,
            "path": entry.path,
            "name": entry.name,
            "indexing": "started in background",
            "md_files": md_files,
        }

    def _kb_unregister(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = str(arguments.get("path", "")).strip()
        if not path:
            raise ValueError("path is required for kb_unregister")
        purge = bool(arguments.get("purge_cache", False))
        entry = self.registry.get(path)
        if entry is None:
            raise ValueError(f"vault not registered: {path}")
        key = str(Path(entry.path).resolve())
        indexer = self._indexers.pop(key, None)
        watcher_stopped = False
        if indexer is not None:
            indexer.stop_watching()  # idempotent; joins the watch thread
            watcher_stopped = True
        self.registry.remove(entry.path)
        cache_purged = False
        if purge and indexer is not None:
            cache_purged = indexer.purge_cache()
        return {
            "unregistered": True,
            "path": entry.path,
            "name": entry.name,
            "watcher_stopped": watcher_stopped,
            "cache_purged": cache_purged,
        }

    def _fanout_search(self, query: str, top_k: int, use_rerank: bool, group_by_vault: bool = False, filters: SearchFilter | None = None) -> dict[str, Any]:
        """Search across every registered (existing) vault, merge and rerank once.

        group_by_vault=True 时返回按库分组的结果（每组 top_k 条），否则平铺返回。
        filters 的过滤条件对每个库分别生效，分页（offset/limit）只在最后合并
        排序后的全局结果上做一次——否则各库各翻一页，合并出来的顺序没有意义。
        """
        per_vault_k = max(top_k, 20)
        # 单库检索只吃过滤条件，不吃分页：分页留到全局合并之后。
        per_vault_filters: SearchFilter | None = None
        if filters is not None:
            per_vault_filters = SearchFilter(
                path_prefix=filters.path_prefix,
                tags=filters.tags,
                mtime_after=filters.mtime_after,
                mtime_before=filters.mtime_before,
            )
        entries = [entry for entry in self.registry.load() if Path(entry.path).is_dir()]
        if not entries:
            raise ValueError("no readable registered vaults; call kb_init first")
        merged: list[tuple[VaultEntry, Chunk]] = []
        searched: list[str] = []
        errors: dict[str, str] = {}

        # Embed the query exactly once for the whole fan-out.
        query_vector = None
        if self.config.embedding.mode == "external":
            try:
                first = self._indexer_for({"vault_path": entries[0].path})
                query_vector = first.embedding_provider.embed([query])[0]
            except Exception as exc:
                errors["_query_embedding"] = str(exc)

        for entry in entries:
            try:
                indexer = self._indexer_for({"vault_path": entry.path})
                indexer.sync()
                chunks = indexer.search(query, per_vault_k, False, query_vector=query_vector, filters=per_vault_filters)
                for chunk in chunks:
                    merged.append((entry, chunk))
                searched.append(entry.path)
            except Exception as exc:
                errors[entry.path] = str(exc)

        # 库级权重：分数乘以该库 weight 后再参与全局排序。
        # 直接改 chunk.score 是安全的：每次检索都会由 indexer 的 _hybrid_rank /
        # lexical 分支整体重算分数（chunk 本身是每次搜索新构造的对象），这里放大
        # 不会污染后续查询。全部 weight 为默认 1.0 时结果与加权前逐字节一致。
        for entry, chunk in merged:
            chunk.score = chunk.score * entry.weight

        merged.sort(key=lambda pair: (-pair[1].score, pair[1].source, pair[1].metadata["chunk_index"]))
        pairs = merged
        if use_rerank and merged:
            provider = None
            for indexer in self._indexers.values():
                if indexer.reranker_provider is not None:
                    provider = indexer.reranker_provider
                    break
            if provider is not None:
                pool = [chunk for _, chunk in merged]
                reranked = rerank_chunks(query, pool, provider, cap=self.config.rerank_cap)
                origin = {id(chunk): entry for entry, chunk in merged}
                pairs = [(origin[id(chunk)], chunk) for chunk in reranked]

        if filters is not None:
            start, end = filters.page_slice(top_k)
        else:
            start, end = 0, max(0, top_k)

        if group_by_vault:
            # 分组模式：保持融合后的组内顺序，按库切桶；组顺序取各组最高分降序。
            # 分页在这里是"每组各翻一页"——全局先切一刀会让低分库整组消失，
            # 那不是分组检索要的语义。offset/limit 缺省时等价于原来的 top_k 截断。
            buckets: dict[str, dict[str, Any]] = {}
            for entry, chunk in pairs:
                group = buckets.get(entry.path)
                if group is None:
                    group = {"vault": entry.path, "vault_name": entry.name, "chunks": []}
                    buckets[entry.path] = group
                data = chunk.to_dict()
                data["vault"] = entry.path
                data["vault_name"] = entry.name
                group["chunks"].append(data)
            for group in buckets.values():
                group["chunks"] = group["chunks"][start:end]
            # 切片后桶可能空了，直接丢掉（max 不接受空序列）。
            groups = sorted(
                (group for group in buckets.values() if group["chunks"]),
                key=lambda group: -max(chunk["score"] for chunk in group["chunks"]),
            )
            return {"groups": groups, "searched": searched, "errors": errors}

        pairs = pairs[start:end]

        out_chunks = []
        for entry, chunk in pairs:
            data = chunk.to_dict()
            data["vault"] = entry.path
            data["vault_name"] = entry.name
            out_chunks.append(data)
        return {"chunks": out_chunks, "searched": searched, "errors": errors}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 防御：某些客户端会把工具名/畸形数据当 arguments 透传（如逐字符拆分的 dict），
        # 这里归一化为空 dict，避免被当作 vault_path 等参数误解析。
        if not isinstance(arguments, dict):
            arguments = {}
        if name == "kb_init":
            return _text_content(self._kb_init(arguments))
        if name == "kb_unregister":
            return _text_content(self._kb_unregister(arguments))
        if name == "kb_vaults":
            return _text_content(self._list_vaults())
        if name == "kb_set_weight":
            vault_path = str(arguments.get("vault_path", "")).strip()
            if not vault_path:
                raise ValueError("vault_path is required for kb_set_weight")
            if "weight" not in arguments:
                raise ValueError("weight is required for kb_set_weight")
            try:
                weight = float(arguments["weight"])
            except (TypeError, ValueError):
                raise ValueError(f"weight must be a number in (0, 100]: {arguments['weight']}")
            entry = self.registry.set_weight(vault_path, weight)
            return _text_content({"path": entry.path, "name": entry.name, "weight": entry.weight})
        if name == "kb_rebuild":
            indexer = self._indexer_for(arguments)
            indexer.rebuild()
            return _text_content(indexer.stats())
        if name == "kb_search":
            explicit = str(arguments.get("vault_path") or "").strip()
            if not explicit and len(self.registry.load()) > 1:
                query = str(arguments.get("query", ""))
                top_k = int(arguments.get("top_k", 10))
                use_rerank = arguments.get("use_rerank", True)
                if isinstance(use_rerank, str):
                    use_rerank = use_rerank.strip().lower() in {"1", "true", "yes", "on"}
                group_by_vault = arguments.get("group_by_vault", False)
                if isinstance(group_by_vault, str):
                    group_by_vault = group_by_vault.strip().lower() in {"1", "true", "yes", "on"}
                return _text_content(self._fanout_search(query, top_k, bool(use_rerank), bool(group_by_vault), _search_filter(arguments)))
        indexer = self._indexer_for(arguments)
        indexer.sync()
        if name == "kb_list":
            return _text_content({"files": indexer.list_files()})
        if name == "kb_search":
            query = str(arguments.get("query", ""))
            top_k = int(arguments.get("top_k", 10))
            use_rerank = arguments.get("use_rerank", True)
            if isinstance(use_rerank, str):
                use_rerank = use_rerank.strip().lower() in {"1", "true", "yes", "on"}
            results = indexer.search(query, top_k, bool(use_rerank), filters=_search_filter(arguments))
            return _text_content({"chunks": [chunk.to_dict() for chunk in results]})
        if name == "kb_read":
            source = str(arguments.get("source", ""))
            heading = arguments.get("heading")
            start_line = arguments.get("start_line")
            end_line = arguments.get("end_line")
            if heading and start_line is None and end_line is None:
                matches = [chunk for chunk in indexer.all_chunks() if chunk.source == source and chunk.metadata.get("heading") == heading]
                if not matches:
                    raise ValueError(f"heading not found: {heading}")
                start_line = min(chunk.metadata["start_line"] for chunk in matches)
                end_line = max(chunk.metadata["end_line"] for chunk in matches)
            text = indexer.read(source, start_line, end_line)
            return _text_content({"source": source, "start_line": start_line, "end_line": end_line, "content": text})
        if name == "kb_stats":
            return _text_content(indexer.stats())
        if name == "kb_exempt":
            action = str(arguments.get("action", "list")).strip()
            pattern = str(arguments.get("pattern", "")).strip()
            source = str(arguments.get("source", "")).strip()
            method = str(arguments.get("method", "frontmatter")).strip()
            if action == "list":
                return _text_content(indexer.get_exemptions())
            if action == "add_pattern":
                if not pattern:
                    raise ValueError("pattern is required for add_pattern")
                return _text_content(indexer.add_exemption_pattern(pattern))
            if action == "remove_pattern":
                if not pattern:
                    raise ValueError("pattern is required for remove_pattern")
                return _text_content(indexer.remove_exemption_pattern(pattern))
            if action == "exempt_file":
                if not source:
                    raise ValueError("source is required for exempt_file")
                return _text_content(indexer.set_file_exemption(source, exempt=True, method=method))
            if action == "unexempt_file":
                if not source:
                    raise ValueError("source is required for unexempt_file")
                return _text_content(indexer.set_file_exemption(source, exempt=False, method=method))
            if action == "check":
                if not source:
                    raise ValueError("source is required for check")
                return _text_content(indexer.check_exemption(source))
            raise ValueError(f"unknown action for kb_exempt: {action}")
        raise ValueError(f"unknown tool: {name}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "initialize":
            return _json_result(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO})
        if method == "ping":
            return _json_result(request_id, {})
        if method == "tools/list":
            return _json_result(request_id, {"tools": _tool_definitions()})
        if method == "tools/call":
            params = request.get("params") or {}
            try:
                return _json_result(request_id, self.call_tool(str(params.get("name", "")), params.get("arguments") or {}))
            except (ValueError, TypeError, OSError) as exc:
                return _json_error(request_id, -32602, str(exc))
            except Exception as exc:
                return _json_error(request_id, -32000, str(exc))
        if request_id is None:
            return None
        return _json_error(request_id, -32601, f"method not found: {method}")


def serve_stdio(config_path: str | Path | None = None) -> int:
    # Windows 下 Python stdio 默认 GBK，MCP 协议要求 UTF-8。
    # 不强制的话：中文 query 进进程变乱码（检索全灭）、中文结果输出变乱码。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    server = VaultMcpServer(config_path)
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                response = server.handle(request)
            except json.JSONDecodeError as exc:
                response = _json_error(None, -32700, str(exc))
            except Exception as exc:
                response = _json_error(None, -32000, str(exc))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        return 0
    finally:
        for indexer in server._indexers.values():
            indexer.stop_watching()


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="vault-mcp")
    parser.add_argument("--serve-mcp-stdio", action="store_true")
    parser.add_argument("--app-config", default=None)
    args = parser.parse_args(argv)
    if not args.serve_mcp_stdio:
        parser.error("--serve-mcp-stdio is required")
    return serve_stdio(args.app_config)


if __name__ == "__main__":
    raise SystemExit(main())
