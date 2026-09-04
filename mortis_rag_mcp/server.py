from __future__ import annotations

import atexit
import json
import sys
import threading
from argparse import ArgumentParser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config, resolve_config_path
from .indexer import Chunk, MarkdownIndexer, SearchFilter, dedupe_by_content_hash, rerank_chunks
from .registry import VaultEntry, VaultRegistry, registry_path

SERVER_INFO = {"name": "mortis-rag-mcp", "version": "0.6.0", "title": "Mortis'RAG MCP"}


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


def _parse_top_k(value: Any, maximum: int) -> int:
    """top_k 解析：非法值退回默认 10，并夹到 [1, config.max_top_k]。

    此前这里是裸 int()，LLM 传个 "10.5" 或 null 就让整次搜索报错；
    传 10**9 则让 sqlite-vec 去建千万级 KNN 堆。
    """
    parsed = _parse_int(value)
    if parsed is None:
        return 10
    return max(1, min(parsed, maximum))


def _search_filter(arguments: dict[str, Any], max_limit: int = 200) -> SearchFilter:
    """从 kb_search 的 arguments 构造 SearchFilter，全部字段都可缺省。"""
    limit = _parse_int(arguments.get("limit"))
    if limit is not None and limit < 1:
        limit = None
    if limit is not None:
        limit = min(limit, max_limit)
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
            "description": "注册（初始化）一个文件夹为知识库：校验目录、写入用户级注册表（跨重启保留）、后台建立索引并启动文件监听。首次使用或要纳入新文件夹时调用；要注册不参与全局检索的独立库用 kb_init_solo。",
            "inputSchema": {"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string", "description": "必填，要注册为知识库的文件夹绝对路径"},
                "name": {"type": "string", "description": "可选，显示名，默认取文件夹名"},
            }},
        },
        {
            "name": "kb_remove",
            "description": "从注册表移除一个已注册的知识库：停止文件监听并移除注册（不影响文件夹本身）。移除后想恢复参与全局检索，重新 kb_init 即可（磁盘缓存保留，秒级恢复、无需重新 embedding）。",
            "inputSchema": {"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string", "description": "必填，已注册知识库的绝对路径"},
                "purge_cache": {"type": "boolean", "default": False, "description": "是否同时删除该库的磁盘索引缓存"},
            }},
        },
        {
            "name": "kb_list",
            "description": "列出所有已注册的知识库（含 solo 标记、存活状态与索引进度）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "kb_init_solo",
            "description": "初始化一个 solo（独立）知识库：不参与跨库全局检索（kb_search 不传 vault_path 时跳过它），只有显式传 vault_path 才会被搜索。三种输入：(1) 未注册的文件夹 → 注册为 solo 库并后台建索引；(2) 已注册的普通库 → 原地转为 solo（索引/缓存/监听不动，秒级）；(3) 已是 solo → 幂等确认。取消 solo 用 kb_remove 后重新 kb_init（缓存保留，0 次重新 embedding）。",
            "inputSchema": {"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string", "description": "必填，文件夹绝对路径（未注册则注册为 solo 库；已注册则转为 solo）"},
                "name": {"type": "string", "description": "可选，显示名，默认取文件夹名（仅未注册时生效）"},
            }},
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
            "name": "kb_export",
            "description": "把知识库的索引快照（chunks + 向量 + FTS）导出为 zip 文件，用于换机/换目录迁移，导入后无需全量重新 embedding。要求缓存已启用且完成过至少一次索引。",
            "inputSchema": {"type": "object", "required": ["out_path"], "properties": {
                "out_path": {"type": "string", "description": "必填，快照输出路径（.zip）"},
                "vault_path": {"type": "string", "description": vault_path_hint},
            }},
        },
        {
            "name": "kb_import",
            "description": "从 kb_export 生成的快照恢复索引缓存（先 kb_init 注册目标目录再调用）。导入后的下一次同步应当 0 次 embedding 调用；快照的向量模型/维度与本机配置不一致时拒绝，除非 force=true（此时只导入文本层并本地重嵌）。",
            "inputSchema": {"type": "object", "required": ["snapshot"], "properties": {
                "snapshot": {"type": "string", "description": "必填，快照 zip 文件路径"},
                "force": {"type": "boolean", "default": False, "description": "模型/维度不一致时强制导入（仅文本层，向量重算）"},
                "vault_path": {"type": "string", "description": vault_path_hint},
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
            "name": "kb_list_files",
            "description": "列出已索引的 Markdown 文件。可传 vault_path 指定知识库。",
            "inputSchema": {"type": "object", "properties": {"vault_path": {"type": "string", "description": vault_path_hint}}},
        },
        {
            "name": "kb_search",
            "description": "搜索知识库并返回结构化原始 chunks。不传 vault_path 时跨全部非 solo 注册库 fan-out 检索（结果带 vault 字段；solo 库被跳过并在 excluded_solo 中列出）；solo 库必须显式传 vault_path 才会被搜索。",
            "inputSchema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "default": 10}, "use_rerank": {"type": "boolean", "default": True},
                "vault_path": {"type": "string", "description": "可选，已注册知识库的绝对路径；缺省时跨全部非 solo 注册库检索"},
                "group_by_vault": {"type": "boolean", "default": False, "description": "可选，仅跨库检索（不传 vault_path）时生效：结果按知识库分组返回 groups，每组取 top_k 条"},
                "path_prefix": {"type": "string", "description": "可选，只保留 source 以该前缀开头的 chunk（source 是库内相对 posix 路径，如 '教材/'）"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选，frontmatter 标签过滤：命中任一标签即保留（大小写不敏感，自动去掉 '#' 前缀）"},
                "mtime_after": {"type": ["number", "string"], "description": "可选，只保留修改时间 >= 该值的文件；epoch 秒或 ISO 8601 字符串（如 '2026-01-01'）"},
                "mtime_before": {"type": ["number", "string"], "description": "可选，只保留修改时间 <= 该值的文件；epoch 秒或 ISO 8601 字符串"},
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "可选，跳过前 N 条结果（分页用）"},
                "limit": {"type": "integer", "minimum": 1, "description": "可选，本页最多返回条数；缺省时用 top_k"},
                "dedupe": {"type": "boolean", "default": True, "description": "可选，默认 true：正文完全相同的 chunk 只保留排在最前面的一条（重复备份/复制段落不再占多格 top_k）"},
            }},
        },
        {
            "name": "kb_read",
            "description": "读取知识库原文（不调用 LLM 生成回答；若索引有未同步的变更会先触发一次增量同步，可能调用 embedding API，建议带上 start_line/end_line 限定范围避免一次拉全篇）。多库环境下建议显式传 vault_path（fan-out 结果中的 source 是库内相对路径）。",
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
        self._indexers_lock = threading.Lock()
        self._startup_lock = threading.Lock()
        self._started = False
        self._migrate_legacy()
        # 所有已注册库在后台线程串行预索引，MCP 握手（initialize）永不阻塞。
        self._start_background_index()
        # 原生监听持有目录句柄；serve_stdio 有 finally 清理，但嵌入式用法
        # （测试、脚本）没有——注册 atexit 保证句柄在进程退出前释放。
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        """停掉所有知识库的文件监听（幂等，可重复调用）。"""
        for indexer in list(self._indexers.values()):
            try:
                indexer.stop_watching()
            except Exception:
                pass

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
                "vault_path must be an absolute path; call kb_list to list registered vaults, or kb_init to register a folder"
            )
        candidate = str(p.resolve())
        if for_registration:
            if not p.is_dir():
                raise ValueError(f"not a readable directory: {candidate}")
            return candidate
        if self.registry.get(candidate) is None:
            raise ValueError(
                f"vault not registered: {candidate}; call kb_init first, or kb_list to list registered vaults"
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
        if indexer is not None:
            return indexer
        # 双检锁：后台启动线程（_startup_index_all）与 stdio 主线程会同时走到
        # 这里，此前两者各造一个 MarkdownIndexer、各跑一次全量 sync、各起一个
        # watcher；败者被字典覆盖后再也拿不到引用，它的 watcher 线程与目录句柄
        # 永久泄漏（Windows 上句柄还会锁住目录，导致无法重命名/删除）。
        with self._indexers_lock:
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
                "solo": entry.solo,
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

    def _kb_init_solo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """kb_init_solo：初始化/确保一个 solo 库（幂等三态）。

        未注册 → 注册为 solo；已注册普通库 → 原地转 solo（只改注册表布尔位，
        索引/缓存/watcher 不动）；已是 solo → 幂等确认。刻意不提供"转普通"
        的隐式路径（kb_init 保持重复注册报错）：避免模型日常重复调 kb_init
        时悄悄把 solo 库转回普通库、恰好暴露用户想隔离的内容——取消 solo
        必须显式走 kb_remove + kb_init 两步（缓存保留，零成本周转）。
        """
        path = str(arguments.get("path", "")).strip()
        if not path:
            raise ValueError("path is required for kb_init_solo")
        name_arg = str(arguments.get("name", "")).strip() or None
        resolved = self._resolve_vault_path(path, for_registration=True)
        existing = self.registry.get(resolved)
        if existing is None:
            # Register first (fail fast, no half state), then build the indexer.
            try:
                entry = self.registry.add(resolved, name_arg, solo=True)
            except OSError:
                entry = self.registry.add(resolved, name_arg, solo=True, persist=False)
            indexer = self._indexer_for({"vault_path": entry.path})
            threading.Thread(target=indexer.sync, daemon=True, name="vault-init").start()
            md_files = sum(1 for _ in Path(entry.path).rglob("*.md") if _.is_file())
            return {
                "solo": True,
                "registered": True,
                "path": entry.path,
                "name": entry.name,
                "indexing": "started in background",
                "md_files": md_files,
            }
        entry = self.registry.set_solo(existing.path, True)
        return {
            "solo": True,
            "registered": False,
            "switched": not existing.solo,
            "path": entry.path,
            "name": entry.name,
        }

    def _kb_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = str(arguments.get("path", "")).strip()
        if not raw:
            raise ValueError("path is required for kb_remove")
        purge = bool(arguments.get("purge_cache", False))
        # 与 kb_set_weight 一致：统一解析为规范化绝对路径再查注册表，
        # 避免相对路径按服务进程 CWD 解析出不可预测的行为。
        path = self._resolve_vault_path(raw)
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
            "removed": True,
            "path": entry.path,
            "name": entry.name,
            "watcher_stopped": watcher_stopped,
            "cache_purged": cache_purged,
        }

    def _fanout_search(self, query: str, top_k: int, use_rerank: bool, group_by_vault: bool = False, filters: SearchFilter | None = None, dedupe: bool = True) -> dict[str, Any]:
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
        all_entries = self.registry.load()
        # solo 库只在显式指定 vault_path 时被检索；fan-out 跳过它们并原样
        # 报告在 excluded_solo 里，否则"忘了一个库是 solo"会变成检索黑洞。
        excluded_solo = [entry.path for entry in all_entries if entry.solo]
        entries = [
            entry for entry in all_entries
            if Path(entry.path).is_dir() and not entry.solo
        ]
        if not entries:
            if excluded_solo and len(excluded_solo) == len(all_entries):
                raise ValueError(
                    "no searchable vaults: all registered vaults are solo (excluded from global search); pass an explicit vault_path to search one"
                )
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
                chunks = indexer.search(query, per_vault_k, False, query_vector=query_vector, filters=per_vault_filters, dedupe=dedupe)
                for chunk in chunks:
                    merged.append((entry, chunk))
                searched.append(entry.path)
            except Exception as exc:
                errors[entry.path] = str(exc)

        # 库级权重：分数乘以该库 weight 后再参与全局排序。
        # 使用 replace 生成打分副本，避免污染 indexer 内存常驻对象。
        merged = [
            (entry, replace(chunk, score=chunk.score * entry.weight))
            for entry, chunk in merged
        ]

        merged.sort(key=lambda pair: (-pair[1].score, pair[1].source, pair[1].metadata["chunk_index"]))
        pairs = merged
        # 跨库再去重一次：同一份内容可能躺在两个库里（比如一个库是另一个的备份）。
        if dedupe:
            pairs = dedupe_by_content_hash(pairs, chunk_of=lambda pair: pair[1])
        if use_rerank and merged:
            provider = None
            for indexer in list(self._indexers.values()):
                if indexer.reranker_provider is not None:
                    provider = indexer.reranker_provider
                    break
            if provider is not None:
                # rerank 的候选池必须来自去重+加权后的 pairs，而不是未去重的
                # merged：否则 449-472 行的去重被这条路径整体撤销（默认
                # use_rerank=True 时两个卖点在默认路径上互相抵消）。
                pool = [chunk for _, chunk in pairs]
                reranked = rerank_chunks(query, pool, provider, cap=self.config.rerank_cap)
                origin_map: dict[str, list[VaultEntry]] = {}
                for entry, chunk in pairs:
                    origin_map.setdefault(chunk.id, []).append(entry)
                new_pairs = []
                for chunk in reranked:
                    entries = origin_map.get(chunk.id)
                    if entries:
                        new_pairs.append((entries.pop(0), chunk))
                    elif pairs:
                        new_pairs.append((pairs[0][0], chunk))
                pairs = new_pairs
                # rerank 覆盖了 chunk.score，把库级权重乘回去，否则 465-466 行
                # 的加权在这条路径上失效。
                pairs = [
                    (entry, replace(chunk, score=chunk.score * entry.weight))
                    for entry, chunk in pairs
                ]

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
            return {"groups": groups, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}

        pairs = pairs[start:end]

        out_chunks = []
        for entry, chunk in pairs:
            data = chunk.to_dict()
            data["vault"] = entry.path
            data["vault_name"] = entry.name
            out_chunks.append(data)
        return {"chunks": out_chunks, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 防御：某些客户端会把工具名/畸形数据当 arguments 透传（如逐字符拆分的 dict），
        # 这里归一化为空 dict，避免被当作 vault_path 等参数误解析。
        if not isinstance(arguments, dict):
            arguments = {}
        if name == "kb_init":
            return _text_content(self._kb_init(arguments))
        if name == "kb_init_solo":
            return _text_content(self._kb_init_solo(arguments))
        if name == "kb_remove":
            return _text_content(self._kb_remove(arguments))
        if name == "kb_list":
            return _text_content(self._list_vaults())
        if name == "kb_set_weight":
            # 统一走 _resolve_vault_path：此前直接按原始字符串查注册表，
            # 相对路径会按 stdio 服务进程的任意 CWD 解析，行为不可预测。
            vault_path = self._resolve_vault_path(str(arguments.get("vault_path") or "").strip())
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
        if name == "kb_export":
            indexer = self._indexer_for(arguments)
            out_path = str(arguments.get("out_path", "")).strip()
            if not out_path:
                raise ValueError("out_path is required for kb_export")
            # 信任边界：out_path 直通 tmp.replace(out)，此前零校验 —— 被提示
            # 注入或跑偏的 LLM 可以用 zip 字节原子覆盖任意用户可写文件（文档、
            # 配置、.ssh/authorized_keys）。对比 kb_read 特意做了 _safe_path
            # 沙箱，这里至少要做到：绝对路径 + .zip 后缀 + 不静默覆盖。
            out = Path(out_path).expanduser()
            if not out.is_absolute():
                raise ValueError("out_path must be an absolute path for kb_export")
            if out.suffix.lower() != ".zip":
                raise ValueError("out_path must end with .zip for kb_export")
            if out.exists():
                overwrite = str(arguments.get("overwrite", "")).strip().lower()
                if overwrite not in {"1", "true", "yes", "on"}:
                    raise ValueError(
                        f"out_path already exists: {out}; pass overwrite=true to replace it"
                    )
            return _text_content(indexer.export_snapshot(out))
        if name == "kb_import":
            indexer = self._indexer_for(arguments)
            snapshot = str(arguments.get("snapshot", "")).strip()
            if not snapshot:
                raise ValueError("snapshot is required for kb_import")
            force = arguments.get("force", False)
            if isinstance(force, str):
                force = force.strip().lower() in {"1", "true", "yes", "on"}
            return _text_content(indexer.import_snapshot(snapshot, force=bool(force)))
        if name == "kb_search":
            explicit = str(arguments.get("vault_path") or "").strip()
            if not explicit:
                entries = self.registry.load()
                # 唯一的注册库是 solo 库时，不传 vault_path 的检索同样算"全局
                # 检索"，必须拒绝而不是悄悄搜它——否则单库用户的 solo 等于没设。
                # （检查放在这里而不是 _default_vault_path：后者被 kb_read/
                # kb_stats 等管理类工具共用，单库默认它们是合理的。）
                if len(entries) == 1 and entries[0].solo:
                    raise ValueError(
                        f"vault '{entries[0].name}' is solo (excluded from global search); "
                        "pass an explicit vault_path to search it"
                    )
                if len(entries) > 1:
                    query = str(arguments.get("query", ""))
                    top_k = _parse_top_k(arguments.get("top_k", 10), self.config.max_top_k)
                    use_rerank = arguments.get("use_rerank", True)
                    if isinstance(use_rerank, str):
                        use_rerank = use_rerank.strip().lower() in {"1", "true", "yes", "on"}
                    group_by_vault = arguments.get("group_by_vault", False)
                    if isinstance(group_by_vault, str):
                        group_by_vault = group_by_vault.strip().lower() in {"1", "true", "yes", "on"}
                    dedupe = arguments.get("dedupe", True)
                    if isinstance(dedupe, str):
                        dedupe = dedupe.strip().lower() not in {"0", "false", "no", "off"}
                    return _text_content(self._fanout_search(query, top_k, bool(use_rerank), bool(group_by_vault), _search_filter(arguments, self.config.max_top_k), bool(dedupe)))
        indexer = self._indexer_for(arguments)
        indexer.sync()
        if name == "kb_list_files":
            return _text_content({"files": indexer.list_files()})
        if name == "kb_search":
            query = str(arguments.get("query", ""))
            top_k = _parse_top_k(arguments.get("top_k", 10), self.config.max_top_k)
            use_rerank = arguments.get("use_rerank", True)
            if isinstance(use_rerank, str):
                use_rerank = use_rerank.strip().lower() in {"1", "true", "yes", "on"}
            dedupe = arguments.get("dedupe", True)
            if isinstance(dedupe, str):
                dedupe = dedupe.strip().lower() not in {"0", "false", "no", "off"}
            results = indexer.search(query, top_k, bool(use_rerank), filters=_search_filter(arguments, self.config.max_top_k), dedupe=bool(dedupe))
            return _text_content({"chunks": [chunk.to_dict() for chunk in results]})
        if name == "kb_read":
            source = str(arguments.get("source", "")).strip()
            if not source:
                raise ValueError("source is required for kb_read")
            heading = arguments.get("heading")
            start_line = _parse_int(arguments.get("start_line"))
            end_line = _parse_int(arguments.get("end_line"))
            if start_line is not None and start_line < 1:
                raise ValueError("start_line must be >= 1")
            if end_line is not None and start_line is not None and end_line < start_line:
                raise ValueError("end_line must be >= start_line")
            if heading and start_line is None and end_line is None:
                matches = [chunk for chunk in indexer.all_chunks() if chunk.source == source and chunk.metadata.get("heading") == heading]
                if not matches:
                    raise ValueError(f"heading not found: {heading}")
                start_line = min(chunk.metadata["start_line"] for chunk in matches)
                end_line = max(chunk.metadata["end_line"] for chunk in matches)
            text = indexer.read(source, start_line, end_line)
            # 无范围时整篇塞进单个 text 块会撑爆模型上下文/客户端消息上限，
            # 给一个保守上限并明确告知被截断，引导调用方用 start_line 续读。
            truncated = False
            if len(text) > 20000:
                text = text[:20000]
                truncated = True
            return _text_content({
                "source": source,
                "start_line": start_line,
                "end_line": end_line,
                "content": text,
                "truncated": truncated,
            })
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
                # MCP 规范：工具执行失败应以 CallToolResult{isError:true} 返回，
                # 模型看到错误内容可以自我纠正（比如先 kb_init 再重试）。此前
                # 一律转成协议级 -32602，很多客户端会直接中断整个回合。只有
                # 意外异常（非 ValueError/OSError）才降级为 -32000。
                return _json_result(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": str(exc)}, ensure_ascii=False
                                ),
                            }
                        ],
                        "isError": True,
                    },
                )
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
    try:
        server = VaultMcpServer(config_path)
    except Exception as exc:
        # 配置错误必须给出人类可读的报错而不是裸 traceback 退出：
        # 客户端只会看到"连接已关闭"，完全不知道是自己 toml 写错了。
        # PR 新增的 8 个配置键让这个失败面显著变大。
        sys.stderr.write(f"Configuration error: {exc}\n")
        sys.stderr.flush()
        return 2
    return _serve_stdio(server)


def _serve_stdio(server: VaultMcpServer) -> int:
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
                # 笔记内容里可能夹带孤立代理项（surrogate，来自 os 解码的文件名
                # 或粘贴内容）。注意：json.dumps(ensure_ascii=False) 对代理项并
                # 不报错，UnicodeEncodeError 发生在 write() 编码那一刻 —— 只包
                # dumps 是死代码，write 也必须在 try 内，否则异常逃出循环直接
                # 杀掉进程。回退到 ensure_ascii=True 会把代理项转成转义序列，
                # 序列化与写出都能通过。
                try:
                    sys.stdout.write(
                        json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                    sys.stdout.flush()
                except UnicodeEncodeError:
                    sys.stdout.write(
                        json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
                    )
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
