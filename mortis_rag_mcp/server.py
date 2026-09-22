from __future__ import annotations

import atexit
import json
import os
import re
import sys
import threading
from argparse import ArgumentParser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config, resolve_config_path
from .indexer import Chunk, MarkdownIndexer, SearchFilter, dedupe_by_content_hash, rerank_chunks
from .ingest import IngestManager, INGEST_EXTS
from .registry import VaultEntry, VaultRegistry, normalize_vault_key, registry_path

SERVER_INFO = {"name": "mortis-rag-mcp", "version": "0.7.3", "title": "Mortis'RAG MCP"}

SERVER_INSTRUCTIONS = (
    "本服务器提供本地 Markdown 知识库检索。路由纪律："
    "1) 用户问题或上下文已明确目标库/目录时，kb_search 必须传 vault_path 或 path_prefix 定向检索；"
    "仅在目标模糊或确需跨库时省略 vault_path。"
    "2) 不确定有哪些库时先 kb_list 查看各库 description 再选库。"
    "3) kb_read 尽量带 start_line/end_line 限定范围，避免一次拉全篇。"
    "4) 环境状态以 ~/.mortis_rag_mcp/STATUS.md 为准：标注有效且未过期时，禁止做环境/依赖/key 预检，"
    "直接调用工具；若状态为 ❌，仅允许运行一次 python -m mortis_rag_mcp --doctor 重测，仍为 ❌ 则严禁重试，直接报错向用户求助。"
)


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


def _tokenize_query(query: str) -> list[str]:
    if not query:
        return []
    return [t.strip() for t in re.findall(r"[\w\u4e00-\u9fff]+", query) if t.strip()]


def _camel_to_snake(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def _normalize_call_arguments(raw_arguments: Any) -> dict[str, Any]:
    """归一化工具入参：兼容 JSON 字符串、嵌套包装（input/args 等）与驼峰命名。"""
    args = raw_arguments
    if isinstance(args, str):
        s = args.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    return {}
            except Exception:
                return {}
        else:
            return {}
    if not isinstance(args, dict):
        return {}

    # 单层嵌套解包：适配常见外层包装（input, arguments, params, parameters, args）
    if len(args) == 1:
        k, v = next(iter(args.items()))
        if k in {"input", "arguments", "params", "parameters", "args"} and isinstance(v, dict):
            args = v

    normalized: dict[str, Any] = {}
    for k, v in args.items():
        k_str = str(k)
        snake_k = _camel_to_snake(k_str)
        if k_str not in normalized:
            normalized[k_str] = v
        if snake_k not in normalized:
            normalized[snake_k] = v
    return normalized


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
                "description": {"type": "string", "description": "可选，一句话说明这个库装什么（如 '数电教材+课件'）。写给未来的检索路由看：模型靠它判断该不该定向选库，务必具体。"},
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
            "name": "kb_describe",
            "description": "设置/更新知识库的描述（一句话说明这个库装什么，供检索路由定向选库用）。与 kb_set_weight 平行的元数据工具。",
            "inputSchema": {"type": "object", "required": ["vault_path", "description"], "properties": {
                "vault_path": {"type": "string", "description": "必填，已注册知识库的绝对路径"},
                "description": {"type": "string", "description": "必填，库内容的一句话描述，要具体（差：'笔记'；好：'数字电路教材解析稿+课件'）"},
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
            "description": "列出已索引的 Markdown 与纯文本文件。可传 vault_path 指定知识库。",
            "inputSchema": {"type": "object", "properties": {"vault_path": {"type": "string", "description": vault_path_hint}}},
        },
        {
            "name": "kb_search",
            "description": "搜索知识库并返回结构化 chunks。路由纪律：用户问题或上下文已明确指向特定库/目录时，必须传 vault_path、vault_paths 或 path_prefix 定向检索（精度更高、噪音更少）；仅在目标模糊或确需跨库时才省略 vault_path 做跨库 fan-out（结果带 vault 字段；solo 库被跳过并在 excluded_solo 中列出）。不确定有哪些库时先 kb_list 看各库 description 再决定。",
            "inputSchema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "default": 10}, "use_rerank": {"type": "boolean", "default": True},
                "vault_path": {"type": "string", "description": "可选，已注册知识库的名称或绝对路径；缺省时跨全部非 solo 注册库检索"},
                "vault_paths": {"type": "array", "items": {"type": "string"}, "description": "可选，知识库名称或绝对路径数组；用于定向组合检索指定的若干个库（Scoped Multi-Vault）"},
                "preview": {"type": "boolean", "default": False, "description": "可选，轻量预览模式：设为 true 时仅返回高光摘要与行号区间，不返回全文，有效节约模型上下文"},
                "mode": {"type": "string", "enum": ["full", "preview"], "default": "full", "description": "可选，检索结果呈现模式：'full'（默认，返回完整正文 content）或 'preview'（轻量高光预览，仅返回 snippet 与行号区间）"},
                "group_by_vault": {"type": "boolean", "default": False, "description": "可选，仅跨库检索（不传 vault_path）时生效：结果按知识库分组返回 groups，每组取 top_k 条"},
                "path_prefix": {"type": "string", "description": "可选，只保留 source 以该前缀开头的 chunk（source 是库内相对 posix 路径）。用户提到具体课程名/文件夹名/主题目录时，用它把检索限定在该子树，如 '教材/'、'数字电路/'"},
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
            "description": "读取知识库原文（只读磁盘原文，不触发同步、不调用 embedding API；建议带上 start_line/end_line 限定范围避免一次拉全篇）。多库环境下建议显式传 vault_path（fan-out 结果中的 source 是库内相对路径）。",
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
        {
            "name": "kb_ingest",
            "description": "PDF/Office 文档摄取（默认关闭，需 [ingest] enabled=true）。action=submit：解析指定文档（sources 为库内相对路径列表；省略则扫描全库未解析/已变更文档），后台异步执行并立即返回任务列表；action=status：查进度（可带 job_id）；action=pending：只列待解析文档不启动。产物写入库内 .mortis-parsed/ 子目录，完成后自动进索引。",
            "inputSchema": {"type": "object", "required": ["action"], "properties": {
                "action": {"type": "string", "enum": ["submit", "status", "pending"],
                           "description": "submit=启动解析（异步）；status=查进度；pending=只列待解析"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "可选，库内相对路径列表（如 ['教材/数电.pdf']）；仅 submit 有效，省略=扫描全库待解析"},
                "job_id": {"type": "string", "description": "可选，仅 status：查单个任务"},
                "vault_path": {"type": "string", "description": "可选，已注册知识库的绝对路径；仅注册了一个库时可省略"},
            }},
        },
    ]


def _text_content(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


_EXCLUDED_SCAN_DIRS = {".git", "node_modules", ".venv", ".trash", ".obsidian", ".stversions", ".stfolder", ".DS_Store"}


def _count_vault_docs(vault_path: str | Path, output_dirname: str = ".mortis-parsed") -> tuple[int, int, dict[str, int]]:
    """统计 vault 内的文本笔记数（.md/.txt）、可摄取文档数与跳过的未收录格式。
    使用 scandir 剪枝遍历，排除 .git/node_modules/产物目录 (D8b, D17)。
    """
    vpath = Path(vault_path).expanduser().resolve()
    out_name = (output_dirname or ".mortis-parsed").strip("/\\ ")
    md_count = 0
    doc_count = 0
    unsupported: dict[str, int] = {}
    entries = [vpath]
    while entries:
        curr = entries.pop()
        try:
            with os.scandir(curr) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in _EXCLUDED_SCAN_DIRS or entry.name == out_name:
                                continue
                            entries.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if entry.name.startswith((".", "~")):
                                continue
                            name_lower = entry.name.lower()
                            suffix = Path(name_lower).suffix
                            if suffix in {".md", ".markdown", ".txt"}:
                                md_count += 1
                            elif suffix in INGEST_EXTS:
                                doc_count += 1
                            else:
                                ext = suffix.lstrip(".")
                                if ext:
                                    unsupported[ext] = unsupported.get(ext, 0) + 1
                    except OSError:
                        continue
        except OSError:
            continue
    return md_count, doc_count, unsupported


class VaultMcpServer:
    def __init__(self, config_path: str | Path | None = None) -> None:
        # 0.3.0 起配置解析链：显式 --app-config > VAULT_MCP_CONFIG 环境变量
        # > ~/.vault_mcp/config.toml > 内置默认值。源码中不再有任何个人路径。
        self.config = load_config(resolve_config_path(config_path))
        self.registry = VaultRegistry()
        self._indexers: dict[str, MarkdownIndexer] = {}
        self._ingest_managers: dict[str, IngestManager] = {}
        self._indexers_lock = threading.Lock()
        self._ingest_managers_lock = threading.Lock()
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

    def _resolve_vault_path(self, raw_identifier: str, *, for_registration: bool = False) -> str:
        """解析库标识符（支持已注册库名、别名或绝对物理路径）。

        解析优先级：
        1. 若非注册模式：优先按注册库的 name 进行大小写不敏感匹配；
        2. 若未命中库名，则作为文件系统路径解析（自动规范化跨平台反斜杠）；
        3. 严禁未注册相对路径逃逸。
        """
        raw = str(raw_identifier or "").strip().strip("\"'")
        if not raw:
            raise ValueError("vault identifier is required; call kb_list to see registered vaults")

        if not for_registration:
            # 1. 优先尝试名称/别名匹配（兼容带尾部斜杠的情形）
            clean_name = raw.rstrip("/\\")
            named_matches = self.registry.get_by_name(clean_name)
            if len(named_matches) == 1:
                return named_matches[0].path
            elif len(named_matches) > 1:
                options = "\n".join(f"  - {e.name}: {e.path}" for e in named_matches)
                raise ValueError(
                    f"ambiguous vault name '{clean_name}' matches multiple vaults:\n{options}\n"
                    "Please specify the explicit physical path instead."
                )

        # 2. 路径处理（规范化跨平台反斜杠）
        norm = raw.replace("/", "\\") if os.name == "nt" else raw
        p = Path(norm).expanduser()
        if not p.is_absolute():
            if not for_registration:
                entries = self.registry.load()
                names = [f"'{e.name}'" for e in entries]
                raise ValueError(
                    f"vault '{raw}' is neither a registered vault name nor an absolute path. "
                    f"Available vaults: {', '.join(names) if names else 'none'}"
                )
            raise ValueError(f"vault_path must be an absolute path for registration: {raw}")

        candidate = str(p.resolve())
        if for_registration:
            if not p.is_dir():
                raise ValueError(f"not a readable directory: {candidate}")
            return candidate

        entry = self.registry.get(candidate)
        if entry is None:
            raise ValueError(
                f"vault not registered: {candidate}; call kb_init first, or kb_list to list registered vaults"
            )
        return entry.path

    def _parse_vault_targets(self, arguments: dict[str, Any]) -> list[str]:
        """从入参中提取目标库列表（支持 vault_paths 数组、vault_path 逗号分隔或单值）。

        安全门禁（F-05）：
        严禁盲目 split(",")。优先将输入整体作为库名/路径匹配，
        仅在整体未命中且包含逗号时才尝试拆分；以 vault_paths 数组作为推荐标准。
        """
        raw_list: list[str] = []
        has_vault_paths_key = "vault_paths" in arguments
        vp = arguments.get("vault_paths") if has_vault_paths_key else None
        if vp:
            if isinstance(vp, (list, tuple)):
                for item in vp:
                    s = str(item).strip()
                    if s:
                        raw_list.append(s)
            else:
                # F-05 同款逗号安全：字符串形态按逗号拆分，绝不静默回落全局检索
                raw_list.extend(p.strip() for p in str(vp).split(",") if p.strip())

        # 若显式传入了 vault_paths 键但解析为空：
        # 若同时传入了有效的单库参数（vault_path/vault/vault_name/path），优先走单库通道；
        # 否则严格抛出 ValueError 杜绝静默回落全局盲搜（C38 F-01 守卫）。
        if has_vault_paths_key and not raw_list:
            single_val = arguments.get("vault_path") or arguments.get("vault") or arguments.get("vault_name") or arguments.get("path")
            if not single_val:
                raise ValueError(
                    "vault_paths is empty; pass at least one vault name or absolute path, "
                    "or omit it to search all non-solo vaults"
                )

        if not raw_list:
            val = arguments.get("vault_path") or arguments.get("vault") or arguments.get("vault_name") or arguments.get("path")
            if val:
                if isinstance(val, (list, tuple)):
                    raw_list.extend(str(x).strip() for x in val if str(x).strip())
                else:
                    s = str(val).strip()
                    if s:
                        # F-05: 优先整体探测
                        is_whole_match = False
                        if self.registry.get_by_name(s):
                            is_whole_match = True
                        elif self.registry.get(s) is not None:
                            is_whole_match = True
                        else:
                            try:
                                if Path(s).expanduser().is_dir():
                                    is_whole_match = True
                            except Exception:
                                pass

                        if is_whole_match or ("," not in s):
                            raw_list.append(s)
                        else:
                            parts = [p.strip() for p in s.split(",") if p.strip()]
                            raw_list.extend(parts)

        seen = set()
        deduped = []
        for t in raw_list:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped

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
        raw = str(
            arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or arguments.get("path")
            or ""
        ).strip()
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
                "description": entry.description,
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
        desc = str(arguments.get("description", "")).strip()
        resolved = self._resolve_vault_path(path, for_registration=True)
        # Register first (fail fast, no half state), then build the indexer.
        try:
            entry = self.registry.add(resolved, name_arg, description=desc)
        except OSError:
            entry = self.registry.add(resolved, name_arg, description=desc, persist=False)
        indexer = self._indexer_for({"vault_path": entry.path})
        threading.Thread(target=indexer.sync, daemon=True, name="vault-init").start()
        md_files, doc_files, unsupported = _count_vault_docs(entry.path, self.config.ingest.output_dirname)
        res = {
            "registered": True,
            "path": entry.path,
            "name": entry.name,
            "description": entry.description,
            "indexing": "started in background",
            "md_files": md_files,
            "ingestible_docs": doc_files,
        }
        if unsupported:
            res["skipped_unsupported"] = unsupported
        if doc_files and not self.config.ingest.enabled:
            res["hint"] = (
                f"检测到 {doc_files} 个 PDF/Office 文档。PDF 摄取层默认未启用；"
                "若用户需要检索这些文档，请先向用户确认，然后在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启服务，再调用 kb_ingest(action='submit')。"
                "未确认前不要自作主张开启。"
            )
        elif doc_files:
            res["hint"] = (
                f"检测到 {doc_files} 个 PDF/Office 文档，可调用 "
                "kb_ingest(action='pending') 查看待解析列表。"
            )
        return res

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
            md_files, doc_files, unsupported = _count_vault_docs(entry.path, self.config.ingest.output_dirname)
            res = {
                "solo": True,
                "registered": True,
                "path": entry.path,
                "name": entry.name,
                "indexing": "started in background",
                "md_files": md_files,
                "ingestible_docs": doc_files,
            }
            if unsupported:
                res["skipped_unsupported"] = unsupported
            if doc_files and not self.config.ingest.enabled:
                res["hint"] = (
                    f"检测到 {doc_files} 个 PDF/Office 文档。PDF 摄取层默认未启用；"
                    "若用户需要检索这些文档，请先向用户确认，然后在 config/app.toml 设置 "
                    "[ingest] enabled = true 并重启服务，再调用 kb_ingest(action='submit')。"
                    "未确认前不要自作主张开启。"
                )
            elif doc_files:
                res["hint"] = (
                    f"检测到 {doc_files} 个 PDF/Office 文档，可调用 "
                    "kb_ingest(action='pending') 查看待解析列表。"
                )
            return res
        entry = self.registry.set_solo(existing.path, True)
        return {
            "solo": True,
            "registered": False,
            "switched": not existing.solo,
            "path": entry.path,
            "name": entry.name,
        }

    def _kb_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = str(
            arguments.get("path")
            or arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or ""
        ).strip()
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

    def _kb_describe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = str(
            arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or arguments.get("path")
            or ""
        ).strip()
        desc = str(arguments.get("description", "")).strip()
        if not raw or not desc:
            raise ValueError("vault_path and description are required for kb_describe")
        path = self._resolve_vault_path(raw)
        entry = self.registry.set_description(path, desc)
        return {"path": entry.path, "name": entry.name, "description": entry.description}

    def _ingest_manager_for(self, vault_path: str) -> IngestManager:
        key = str(Path(vault_path).resolve())
        manager = self._ingest_managers.get(key)
        if manager is None:
            with self._ingest_managers_lock:
                manager = self._ingest_managers.get(key)
                if manager is None:
                    def _on_job_finished(out_path: str) -> None:
                        indexer = self._indexers.get(key)
                        if indexer is not None:
                            threading.Thread(target=indexer.sync, daemon=True, name="ingest-sync").start()

                    manager = IngestManager(vault_path, self.config.ingest, on_job_finished=_on_job_finished)
                    self._ingest_managers[key] = manager
        return manager

    def _kb_ingest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "")).strip()
        if action not in {"submit", "status", "pending"}:
            raise ValueError("action must be one of: submit, status, pending")
        target_raw = (
            arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or arguments.get("path")
            or ""
        )
        vault = self._resolve_vault_path(str(target_raw).strip() or self._default_vault_path())
        manager = self._ingest_manager_for(vault)
        if action == "pending":
            return {"pending": manager.scan_pending()}
        if action == "status":
            return manager.status(str(arguments.get("job_id", "")).strip() or None)
        force = bool(arguments.get("force", False))
        result = manager.submit(arguments.get("sources") or None, force=force)
        result["hint"] = ("解析在后台进行，用 kb_ingest(action='status') 查进度；"
                          "done 的文档已写入 .mortis-parsed/ 并可被 kb_search 检索。")
        return result

    def _fanout_search(
        self,
        query: str,
        top_k: int,
        use_rerank: bool,
        group_by_vault: bool = False,
        filters: SearchFilter | None = None,
        dedupe: bool = True,
        target_vaults: list[str] | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Search across registered vaults (either globally or scoped to target_vaults), merge and rerank once.

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
        vault_name_map = {entry.path: entry.name for entry in all_entries}

        target_keys: set[str] | None = None
        if target_vaults is not None and len(target_vaults) > 0:
            target_keys = {normalize_vault_key(self._resolve_vault_path(t)) for t in target_vaults}

        entries: list[VaultEntry] = []
        excluded_solo: list[str] = []

        for entry in all_entries:
            if not Path(entry.path).is_dir():
                continue
            entry_key = normalize_vault_key(entry.path)
            if target_keys is not None:
                # 定向多库检索（Scoped Multi-Vault）：显式包含的 solo 库允许合法参与检索
                if entry_key in target_keys:
                    entries.append(entry)
            else:
                # 全局盲搜：跳过所有 solo 库并汇报
                if entry.solo:
                    excluded_solo.append(entry.path)
                else:
                    entries.append(entry)

        if not entries:
            if target_keys is not None:
                raise ValueError(f"none of the requested vaults could be searched: {target_vaults}")
            if excluded_solo and len(excluded_solo) == len(all_entries):
                raise ValueError(
                    "no searchable vaults: all registered vaults are solo (excluded from global search); "
                    "pass an explicit vault_path or vault_paths to search"
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
                sync_ok = indexer.try_sync_with_guard(timeout=1.5)
                if not sync_ok and len(indexer._chunks) == 0:
                    errors[entry.path] = "indexing in progress"
                    continue
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

        query_tokens = _tokenize_query(query)
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
                data = chunk.to_dict(preview=preview, query_tokens=query_tokens)
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
            res: dict[str, Any] = {"groups": groups, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}
            if len(searched) > 1 and not target_vaults:
                names = [vault_name_map.get(s, Path(s).name) for s in searched]
                res["hint"] = (
                    f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
                    "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
                )
            return res

        pairs = pairs[start:end]

        out_chunks = []
        for entry, chunk in pairs:
            data = chunk.to_dict(preview=preview, query_tokens=query_tokens)
            data["vault"] = entry.path
            data["vault_name"] = entry.name
            out_chunks.append(data)
        res = {"chunks": out_chunks, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}
        if len(searched) > 1 and not target_vaults:
            names = [vault_name_map.get(s, Path(s).name) for s in searched]
            res["hint"] = (
                f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
                "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
            )
        return res

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 归一化入参（防卫畸形数据、反序列化 JSON 字符串、解包嵌套、驼峰映射）
        arguments = _normalize_call_arguments(arguments)
        if name == "kb_init":
            return _text_content(self._kb_init(arguments))
        if name == "kb_init_solo":
            return _text_content(self._kb_init_solo(arguments))
        if name == "kb_remove":
            return _text_content(self._kb_remove(arguments))
        if name == "kb_describe":
            return _text_content(self._kb_describe(arguments))
        if name == "kb_list":
            return _text_content(self._list_vaults())
        if name == "kb_ingest":
            return _text_content(self._kb_ingest(arguments))
        if name == "kb_set_weight":
            # 统一走 _resolve_vault_path：此前直接按原始字符串查注册表，
            # 相对路径会按 stdio 服务进程的任意 CWD 解析，行为不可预测。
            target_raw = (
                arguments.get("vault_path")
                or arguments.get("vault")
                or arguments.get("vault_name")
                or arguments.get("path")
                or ""
            )
            vault_path = self._resolve_vault_path(str(target_raw).strip())
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
            targets = self._parse_vault_targets(arguments)
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
            preview_val = arguments.get("preview", False)
            if isinstance(preview_val, str):
                preview = preview_val.strip().lower() in {"1", "true", "yes", "on"}
            else:
                preview = bool(preview_val)
            if arguments.get("mode") == "preview":
                preview = True

            search_filters = _search_filter(arguments, self.config.max_top_k)
            query_tokens = _tokenize_query(query)

            # 单库检索通道（传入单个目标）
            if len(targets) == 1:
                resolved_single = self._resolve_vault_path(targets[0])
                indexer = self._indexer_for({"vault_path": resolved_single})
                sync_ok = indexer.try_sync_with_guard(timeout=1.5)
                if not sync_ok and len(indexer._chunks) == 0:
                    return _text_content({
                        "status": "indexing",
                        "message": "知识库正在后台进行首次初始化构建与嵌入计算，请稍候...",
                        "progress": indexer._sync_progress,
                        "retry_after": 3,
                        "chunks": [],
                    })
                results = indexer.search(query, top_k, bool(use_rerank), filters=search_filters, dedupe=bool(dedupe))
                res_dict: dict[str, Any] = {"chunks": [chunk.to_dict(preview=preview, query_tokens=query_tokens) for chunk in results]}
                if not sync_ok:
                    res_dict["indexing_in_progress"] = True
                    res_dict["indexing_progress"] = indexer._sync_progress
                return _text_content(res_dict)

            # 未指定目标时的默认单库处理
            if not targets:
                entries = self.registry.load()
                if len(entries) == 1 and entries[0].solo:
                    raise ValueError(
                        f"vault '{entries[0].name}' is solo (excluded from global search); "
                        "pass an explicit vault_path to search it"
                    )
                if len(entries) == 1:
                    indexer = self._indexer_for({"vault_path": entries[0].path})
                    sync_ok = indexer.try_sync_with_guard(timeout=1.5)
                    if not sync_ok and len(indexer._chunks) == 0:
                        return _text_content({
                            "status": "indexing",
                            "message": "知识库正在后台进行首次初始化构建与嵌入计算，请稍候...",
                            "progress": indexer._sync_progress,
                            "retry_after": 3,
                            "chunks": [],
                        })
                    results = indexer.search(query, top_k, bool(use_rerank), filters=search_filters, dedupe=bool(dedupe))
                    res_dict = {"chunks": [chunk.to_dict(preview=preview, query_tokens=query_tokens) for chunk in results]}
                    if not sync_ok:
                        res_dict["indexing_in_progress"] = True
                        res_dict["indexing_progress"] = indexer._sync_progress
                    return _text_content(res_dict)

            # 跨库检索（Scoped 定向多库 或 全局盲搜）
            return _text_content(self._fanout_search(
                query, top_k, bool(use_rerank), bool(group_by_vault), search_filters, bool(dedupe),
                target_vaults=targets if targets else None,
                preview=preview,
            ))

        if name in {"kb_list_files", "kb_read", "kb_stats", "kb_exempt"}:
            indexer = self._indexer_for(arguments)
            if name == "kb_list_files":
                indexer.try_sync_with_guard(timeout=1.0)
                return _text_content({"files": indexer.list_files()})
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
                indexer.try_sync_with_guard(timeout=1.0)
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
            return _json_result(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            })
        if method == "ping":
            return _json_result(request_id, {})
        if method == "tools/list":
            return _json_result(request_id, {"tools": _tool_definitions()})
        if method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict):
                params = {}
            tool_name = str(params.get("name") or request.get("name") or "").strip()
            raw_args = params.get("arguments")
            if raw_args is None:
                for alt in ("input", "args", "parameters"):
                    if alt in params:
                        raw_args = params[alt]
                        break
            if raw_args is None and "arguments" in request:
                raw_args = request.get("arguments")
            if raw_args is None:
                flat = {k: v for k, v in params.items() if k != "name"}
                raw_args = flat if flat else {}
            try:
                return _json_result(request_id, self.call_tool(tool_name, raw_args))
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


def _refresh_status_async(config_path: str | Path | None) -> None:
    """启动后后台轻量刷新 STATUS.md。派生数据失败完全吞掉，静默模式严防污染 stdio。"""
    def _worker() -> None:
        try:
            from . import doctor
            doctor.run(full=False, app_config=str(config_path) if config_path else None, quiet=True)
        except Exception:
            pass
    threading.Thread(target=_worker, name="status-refresh", daemon=True).start()


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
    _refresh_status_async(config_path)
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
    parser = ArgumentParser(prog="mortis-rag-mcp")
    parser.add_argument("--serve-mcp-stdio", action="store_true")
    parser.add_argument("--app-config", default=None)
    parser.add_argument("--doctor", action="store_true",
                        help="全量探测本机环境（含 API 真实调用）并重写 ~/.mortis_rag_mcp/STATUS.md 与 status.json")
    parser.add_argument("--quiet", action="store_true", help="静默模式，禁止输出到 stdout")
    args = parser.parse_args(argv)
    if args.doctor:
        from . import doctor
        return doctor.run(full=True, app_config=args.app_config, quiet=args.quiet)
    if not args.serve_mcp_stdio:
        parser.error("--serve-mcp-stdio is required")
    return serve_stdio(args.app_config)


if __name__ == "__main__":
    raise SystemExit(main())
