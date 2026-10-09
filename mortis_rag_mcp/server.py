from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
import threading
import time
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config, resolve_config_path
from .doc_store import ControlStore, DocumentStore, resolve_storage_layout
from .indexer import Chunk, MarkdownIndexer, SearchFilter, path_prefix_match
from .ingest import IngestManager, INGEST_EXTS, make_ingest_manager
from ._server import dispatch_search as _dispatch_search, fanout_search as _fanout_search_impl
from .registry import VaultEntry, VaultRegistry, normalize_vault_key, registry_path

SERVER_INFO = {"name": "mortis-rag-mcp", "version": __version__, "title": "Mortis'RAG MCP"}

SERVER_INSTRUCTIONS = (
    "本服务器提供本地 Markdown 知识库检索。路由纪律："
    "1) 用户问题或上下文已明确目标库/目录时，kb_search 必须传 vault_path 或 path_prefix 定向检索；"
    "仅在目标模糊或确需跨库时省略 vault_path。"
    "2) 不确定有哪些库时先 kb_list 查看各库 description 再选库。"
    "3) kb_read 尽量带 start_line/end_line 限定范围，避免一次拉全篇。"
    "4) 环境状态以 ~/.mortis_rag_mcp/STATUS.md 为准：标注有效且未过期时，禁止做环境/依赖/key 预检，"
    "直接调用工具。状态过期或某次工具报错**不代表**要自动运行 --doctor（它会发起真实 API 探测）："
    "先按工具返回的错误处理；只有用户明确要求时才可以运行一次 python -m mortis_rag_mcp --doctor，"
    "仍为 ❌ 则不再重试，直接把错误与 STATUS.md 结论报给用户求助。"
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
                "path": {"type": "string", "description": "必填，要注册为知识库的文件夹绝对路径（等价别名 vault_path/vault/vault_name 亦可，与其余 kb_* 工具同义）"},
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
                "path": {"type": "string", "description": "必填，文件夹绝对路径（未注册则注册为 solo 库；已注册则转为 solo）；等价别名 vault_path/vault/vault_name 亦可"},
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
                "trust_parsed_documents": {"type": "boolean", "default": False, "description": "仅明确确认可信自有备份时启用；未知来源解析文档默认隔离"},
                "replace": {"type": "boolean", "default": False, "description": "显式替换当前文档generation，旧generation保留为备份"},
                "confirm_replace": {"type": "boolean", "default": False, "description": "确认替换目标及损失；replace=true时必需"},
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
            "description": "列出已索引的 Markdown 与纯文本文件（可前缀过滤 + 分页）。注意 limit 的缺省语义与 kb_search.limit 不同：这里不传 limit 就是返回全部已索引文件，大库请显式传 limit/offset 分页取用。",
            "inputSchema": {"type": "object", "properties": {
                "vault_path": {"type": "string", "description": vault_path_hint},
                "path_prefix": {"type": "string", "description": "可选，只列 source 以该前缀开头的文件（source 是库内相对 posix 路径），如 '教材/'；语义与 kb_search.path_prefix 一致"},
                "limit": {"type": "integer", "minimum": 1, "description": "可选，本页最多返回条数；**缺省返回全部已索引文件**（不是 kb_search.limit 的『缺省用 top_k』），大库请显式传"},
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "可选，跳过前 N 条（分页用；配合 next_offset 连续取页）"},
            }},
        },
        {
            "name": "kb_search",
            "description": "搜索知识库并返回结构化 chunks。路由纪律：用户问题或上下文已明确指向特定库/目录时，必须传 vault_path、vault_paths 或 path_prefix 定向检索（精度更高、噪音更少）；仅在目标模糊或确需跨库时才省略 vault_path 做跨库 fan-out（结果带 vault 字段；solo 库被跳过并在 excluded_solo 中列出）。不确定有哪些库时先 kb_list 看各库 description 再决定。",
            "inputSchema": {"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "default": 10}, "use_rerank": {"type": "boolean", "default": True},
                "vault_path": {"type": "string", "description": "可选，已注册知识库的名称或绝对路径；缺省时跨全部非 solo 注册库检索"},
                "vault_paths": {"type": "array", "items": {"type": "string"}, "description": "可选，知识库名称或绝对路径数组；用于定向组合检索指定的若干个库（Scoped Multi-Vault）"},
                "preview": {"type": "boolean", "default": False, "description": "可选，轻量预览模式：设为 true 时仅返回高光摘要与行号区间，不返回全文，有效节约模型上下文"},
                "compact": {"type": "boolean", "default": False, "description": "可选，极简预览，隐含preview，返回source/heading/lines/snippet；按行号+库回读，不返回id"},
                "mode": {"type": "string", "enum": ["full", "preview"], "default": "full", "description": "可选，检索结果呈现模式：'full'（默认，返回完整正文 content）或 'preview'（轻量高光预览，仅返回 snippet 与行号区间）"},
                "group_by_vault": {"type": "boolean", "default": False, "description": "可选，仅跨库检索（不传 vault_path）时生效：结果按知识库分组返回 groups，每组取 top_k 条"},
                "group_offsets": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}, "description": "可选，仅在 group_by_vault=true 时生效：指定各知识库的起始偏移量字典（键为库名或绝对路径，值为非负整数）。未指定的库默认回落到 offset 参数。"},
                "path_prefix": {"type": "string", "description": "可选，只保留 source 以该前缀开头的 chunk（source 是库内相对 posix 路径）。用户提到具体课程名/文件夹名/主题目录时，用它把检索限定在该子树，如 '教材/'、'数字电路/'"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选，frontmatter 标签过滤：命中任一标签即保留（大小写不敏感，自动去掉 '#' 前缀）"},
                "mtime_after": {"type": ["number", "string"], "description": "可选，只保留修改时间 >= 该值的文件；epoch 秒或 ISO 8601 字符串（如 '2026-01-01'）"},
                "mtime_before": {"type": ["number", "string"], "description": "可选，只保留修改时间 <= 该值的文件；epoch 秒或 ISO 8601 字符串"},
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "可选，跳过前 N 条结果（分页用）"},
                "limit": {"type": "integer", "minimum": 1, "description": "可选，本页最多返回条数；缺省时用 top_k"},
                "dedupe": {"type": "boolean", "default": True, "description": "可选，默认 true：正文完全相同的 chunk 只保留排在最前面的一条（重复备份/复制段落不再占多格 top_k）"},
                "budget_bytes": {"type": "integer", "description": "可选，输出最大 UTF-8 字节预算（[500, 100000]），超限截断并标 truncated: true；若元数据包络本身超出预算则返回 budget_exceeded: true 与 minimum_budget_bytes"},
                "exact_terms": {"type": "array", "items": {"type": "string"}, "description": "可选，专有名词显式硬包含词表（AND 语义，不区分大小写，最多 8 条每条≤100 字符）"},
            }},
        },
        {
            "name": "kb_read",
            "description": "读取知识库原文（只读磁盘原文，不触发同步、不调用 embedding API；建议带上 start_line/end_line 限定范围避免一次拉全篇）。多库环境下建议显式传 vault_path（fan-out 结果中的 source 是库内相对路径）。",
            "inputSchema": {"type": "object", "required": [], "properties": {
                "source": {"type": "string", "description": "原始相对路径，支持已提交虚拟PDF/Office/音频文档及物理文本"},
                "allow_stale": {"type": "boolean", "default": False, "description": "仅显式true允许source读取已变更源的存档；chunk_id仍严格核版本"},
                "media_refs_offset": {"type": "integer", "minimum": 0, "default": 0, "description": "虚拟文档媒体引用分页偏移"},
                "chunk_id": {"type": "string", "description": "可选，命中切片 ID（由 kb_search 返回），自动展开上下文；与 start_line/end_line/heading 互斥"},
                "expand_lines": {"type": "integer", "default": 30, "minimum": 0, "maximum": 500, "description": "可选，配合 chunk_id 使用：切片前后各展开行数（默认 30，范围 0-500）"},
                "heading": {"type": "string", "description": "可选，按原文章节标题精确定位段落（包含子标题）；多处同名标题报错引导改用行号；若同时传入 start_line/end_line 则行区间优先"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "start_char": {"type": "integer", "minimum": 0, "default": 0, "description": "可选，start_line 内 0-based Unicode 字符起始偏移量（默认 0），仅在显式指定 start_line 时有效，用于单行超过 read_max_chars 时的续读"},
                "vault_path": {"type": "string", "description": vault_path_hint},
            }},
        },
        {
            "name": "kb_read_media",
            "description": "读取明确库、源和revision下的媒体occurrence。默认仅metadata，不读取完整blob；inline显式返回标准MCP媒体块，遵循完整JSON字节预算。",
            "inputSchema": {"type": "object", "required": ["vault_path", "source", "revision_id", "occurrence_id"], "properties": {
                "vault_path": {"type": "string", "description": "必填，已注册知识库名称或绝对路径"},
                "source": {"type": "string", "description": "必填，原始库内相对路径，不接受URL"},
                "revision_id": {"type": "string"},
                "occurrence_id": {"type": "string"},
                "representation": {"type": "string", "enum": ["metadata", "inline"], "default": "metadata"},
                "variant": {"type": "string", "enum": ["preview", "original"], "default": "preview"},
                "budget_bytes": {"type": "integer", "minimum": 1, "maximum": 8388608, "default": 2097152},
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
            "description": "PDF/Office 文档摄取（默认关闭，需 [ingest] enabled=true）。action=submit：解析指定文档（sources 为库内相对路径列表；省略则扫描全库未解析/已变更文档），后台异步执行并立即返回任务列表；action=status：查进度（可带 job_id）；action=pending：只列待解析文档不启动；action=retry：对 failed/cancelled 的既有任务显式重试（结果未知的远端任务不会被重试）。虚拟存储（ingest.storage=virtual）下产物进文档库并由 kb_read 虚拟读取，legacy 下写入库内 .mortis-parsed/ 子目录；完成后都会自动进索引。",
            "inputSchema": {"type": "object", "required": ["action"], "properties": {
                "action": {"type": "string", "enum": ["submit", "status", "pending", "retry"],
                           "description": "submit=启动解析（异步）；status=查进度；pending=只列待解析；retry=重试失败/已取消的既有任务"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "可选，库内相对路径列表（如 ['教材/数电.pdf']）；仅 submit 有效，省略=扫描全库待解析"},
                "job_id": {"type": "string", "description": "可选，status 查单个任务；retry 必填（要重试的任务 id）"},
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
        """停掉所有知识库的文件监听与摄取扫描（幂等，可重复调用）。"""
        # C94：摄取 manager（可能是 virtual worker）必须先停，再关文档库连接。
        for manager in list(self._ingest_managers.values()):
            try:
                stop = getattr(manager, "stop", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
        self._ingest_managers.clear()
        for indexer in list(self._indexers.values()):
            try:
                indexer._ingest_hook = None
                indexer.stop_watching()
            except Exception:
                pass
            # v0.9.0 C92 接缝：文档库连接必须显式关闭，否则 Windows 上句柄会
            # 锁住缓存目录（后续清理/替换失败）。
            try:
                indexer.close_document_store()
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
                # C58d: start_watching 之前挂 hook，仅 effective=enabled && auto_watch 时挂
                # 惰性获取 manager，不在定义闭包时构造 manager，不默认创建 .mortis-parsed
                if self.config.ingest.enabled and self.config.ingest.auto_watch:
                    def _ingest_hook() -> Any:
                        mgr = self._ingest_manager_for(key)
                        # 返回 auto_submit 结果供 _ingest_scan_loop 判断是否需要
                        # 主动重扫（review R3：判稳滞留/扫描不完整时不依赖下一个事件）。
                        return mgr.auto_submit()
                    indexer._ingest_hook = _ingest_hook
                # review R1/F2：先发布进 _indexers 再启动监听。watcher 启动后文件
                # 事件即可触发 _ingest_hook → _ignore_provider；若此刻本库尚未发布，
                # provider 拿不到 indexer，auto_submit 会在无法判定豁免规则的窗口里
                # 提交本应被 .vaultignore/exclude 排除的文档。
                self._indexers[key] = indexer
                indexer.start_watching()
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
        # 别名口径与 kb_remove/_indexer_for/kb_read 等五处对齐：模型常按其余工具的习惯
        # 误传 vault_path（issue #3）。别名不在 schema properties 里（体积门禁），
        # 但 MCP 客户端不拒收 properties 之外的参数，且 schema description 已注明。
        path = str(
            arguments.get("path")
            or arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or ""
        ).strip()
        if not path:
            raise ValueError(
                "path is required for kb_init（等价别名：vault_path / vault / vault_name）"
            )
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
        hint = self._ingest_init_hint(doc_files)
        if hint:
            res["hint"] = hint
        return res

    def _ingest_init_hint(self, doc_files: int) -> str | None:
        if not doc_files:
            return None
        ingest_cfg = self.config.ingest
        effective_auto = ingest_cfg.enabled and ingest_cfg.auto_watch
        cap_mb = ingest_cfg.max_file_size_mb
        cap_str = f"{cap_mb}MiB" if cap_mb > 0 else "无限制"
        if effective_auto:
            return (
                f"检测到 {doc_files} 个 PDF/Office 文档。自动摄取已激活（单文件上限 {cap_str}），"
                "新文档将自动提交云端解析（云端解析可能产生费用/隐私影响）。"
                "可用 kb_ingest(action='status') 查看队列进度。"
            )
        if not ingest_cfg.enabled and ingest_cfg.auto_watch:
            return (
                f"检测到 {doc_files} 个 PDF/Office 文档。auto_watch=true 但 enabled=false，"
                "自动摄取未生效；需同时在 config/app.toml 设置 [ingest] enabled = true 并重启服务。"
                "如需检索，请先向用户确认后再开启，或通过 kb_ingest(action='pending') 查看待解析列表。"
            )
        if not ingest_cfg.enabled:
            return (
                f"检测到 {doc_files} 个 PDF/Office 文档。PDF 摄取层默认未启用；"
                "若用户需要检索这些文档，请先向用户确认，然后在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启服务，再调用 kb_ingest(action='pending') 查看待解析列表并在确认后 submit。"
                "未确认前不要自作主张开启。"
            )
        return (
            f"检测到 {doc_files} 个 PDF/Office 文档（单文件上限 {cap_str}），当前为手动摄取模式；"
            "可调用 kb_ingest(action='pending') 查看待解析列表，由用户确认后调用 kb_ingest(action='submit') 提交。"
        )

    def _kb_init_solo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """kb_init_solo：初始化/确保一个 solo 库（幂等三态）。

        未注册 → 注册为 solo；已注册普通库 → 原地转 solo（只改注册表布尔位，
        索引/缓存/watcher 不动）；已是 solo → 幂等确认。刻意不提供"转普通"
        的隐式路径（kb_init 保持重复注册报错）：避免模型日常重复调 kb_init
        时悄悄把 solo 库转回普通库、恰好暴露用户想隔离的内容——取消 solo
        必须显式走 kb_remove + kb_init 两步（缓存保留，零成本周转）。
        """
        path = str(
            arguments.get("path")
            or arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or ""
        ).strip()
        if not path:
            raise ValueError(
                "path is required for kb_init_solo（等价别名：vault_path / vault / vault_name）"
            )
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
            hint = self._ingest_init_hint(doc_files)
            if hint:
                res["hint"] = hint
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
        # C94：移库/移除库时必须连**摄取 manager**一起停掉，而不只是停 watch 扫描线程；
        # 否则 virtual worker 会拿着已失效的 store 继续跑（并在 old inode 上写成功）。
        removed_manager = self._ingest_managers.pop(key, None)
        if removed_manager is not None:
            try:
                stop = getattr(removed_manager, "stop", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
        watcher_stopped = False
        if indexer is not None:
            indexer._ingest_hook = None
            indexer.stop_watching()  # idempotent; joins the watch thread & scan thread
            watcher_stopped = True
            # C92 接缝：库被移出注册表后释放文档库连接——Windows 上未关闭的 sqlite
            # 句柄会锁住缓存文件，让后续清理/替换（移库、换机、显式 purge）失败。
            # 注意：文档库**保留**在缓存根（移库默认保 store 以便重新注册，§20.3），
            # 这里只关连接，不删资产。
            try:
                indexer.close_document_store()
            except Exception:
                pass
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
            # virtual 路径需要 indexer 的文档库接缝（唯一存储入口）：先确保 indexer 已建，
            # 且**在取 manager 锁之前**完成，避免 indexers_lock / ingest_managers_lock 嵌套。
            if str(getattr(self.config.ingest, "storage", "legacy")) == "virtual":
                try:
                    self._indexer_for({"vault_path": vault_path})
                except Exception:
                    pass
            with self._ingest_managers_lock:
                manager = self._ingest_managers.get(key)
                if manager is None:
                    def _on_job_finished(source: str, out_md: Path | str) -> None:
                        try:
                            indexer = self._indexers.get(key)
                            if indexer is not None:
                                indexer.request_refresh(immediate=True)
                        except Exception:
                            pass

                    def _ignore_provider() -> Any:
                        idx = self._indexers.get(key)
                        if idx is None:
                            # fail-closed 信号：auto_submit 本轮拒绝提交（review R1）。
                            return None
                        # 动态 matcher：同时覆盖 exclude_patterns 与 .vaultignore
                        # （此前只拿静态 exclude_patterns，vaultignore 豁免被绕过）。
                        return idx._ignore_matcher()

                    def _store_provider() -> Any:
                        idx = self._indexers.get(key)
                        if idx is None:
                            raise RuntimeError(
                                "virtual ingest requires an initialized indexer for this vault"
                            )
                        return idx.document_store(write=True)

                    manager = make_ingest_manager(
                        vault_path,
                        self.config.ingest,
                        store_provider=_store_provider,
                        on_job_finished=_on_job_finished,
                        ignore_provider=_ignore_provider,
                    )
                    self._ingest_managers[key] = manager
        return manager

    def _kb_ingest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "")).strip()
        if action not in {"submit", "status", "pending", "retry"}:
            raise ValueError("action must be one of: submit, status, pending, retry")
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
        if action == "retry":
            # E06：只转发 E02 已验的 failed/cancelled 重试；unknown 保状态与 next action。
            retry = getattr(manager, "retry", None)
            if retry is None:
                raise ValueError("action=retry requires ingest.storage=virtual (document store queue)")
            return retry(str(arguments.get("job_id", "")).strip())
        if action == "status":
            res = manager.status(str(arguments.get("job_id", "")).strip() or None)
            if not self.config.ingest.enabled and self.config.ingest.auto_watch:
                res["warning"] = "ingest.auto_watch=true 但 enabled=false，自动摄取未生效"
            return res
        force = bool(arguments.get("force", False))
        result = manager.submit(arguments.get("sources") or None, force=force)
        result["hint"] = (
            "解析在后台进行，用 kb_ingest(action='status') 查进度；"
            "done 的文档在 ingest.storage=virtual 时进入文档库（kb_read 走虚拟读取），"
            "legacy 时写入 .mortis-parsed/；两种存储下都会被 kb_search 检索。"
        )
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
        compact: bool = False,
        *,
        budget_bytes: int | None = None,
        exact_terms: list[str] | None = None,
        group_offsets: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return _fanout_search_impl(
            self,
            query=query,
            top_k=top_k,
            use_rerank=use_rerank,
            group_by_vault=group_by_vault,
            filters=filters,
            dedupe=dedupe,
            target_vaults=target_vaults,
            preview=preview,
            compact=compact,
            budget_bytes=budget_bytes,
            exact_terms=exact_terms,
            group_offsets=group_offsets,
        )

    # v0.8.0 Phase 1：call_tool 由巨型 if 链改为显式路由表（15 个 kb_* 工具 → 处理方法）。
    # 工具名、参数语义、返回结构与 unknown-tool 报错行为完全不变（MCP 协议契约）。
    _TOOL_ROUTE_TABLE: dict[str, str] = {
        "kb_init": "_kb_init",
        "kb_init_solo": "_kb_init_solo",
        "kb_remove": "_kb_remove",
        "kb_describe": "_kb_describe",
        "kb_list": "_kb_list",
        "kb_ingest": "_kb_ingest",
        "kb_set_weight": "_kb_set_weight",
        "kb_rebuild": "_kb_rebuild",
        "kb_export": "_kb_export",
        "kb_import": "_kb_import",
        "kb_search": "_kb_search",
        "kb_list_files": "_kb_list_files",
        "kb_read": "_kb_read",
        "kb_read_media": "_kb_read_media",
        "kb_stats": "_kb_stats",
        "kb_exempt": "_kb_exempt",
    }

    def call_tool(self, name: str, arguments: dict[str, Any], *,
                  request_id: Any = None) -> dict[str, Any]:
        diag_cfg = getattr(self.config, "diag", None)
        if not diag_cfg or not diag_cfg.enabled:
            # 默认未开启诊断日志时走纯净路径，零额外开销与零副作用
            arguments = _normalize_call_arguments(arguments)
            handler_name = self._TOOL_ROUTE_TABLE.get(name)
            if handler_name is None:
                raise ValueError(f"unknown tool: {name}")
            if name == "kb_read_media":
                # E08-f：把真实 JSON-RPC id 传进媒体 handler，预算按线上同一包络计量。
                raw_result = self._kb_read_media(arguments, request_id=request_id)
                return raw_result
            raw_result = getattr(self, handler_name)(arguments)
            return _text_content(raw_result)

        import time
        from . import diaglog

        corr_id = diaglog.generate_corr_id()
        t0 = time.perf_counter()
        try:
            arguments = _normalize_call_arguments(arguments)
            handler_name = self._TOOL_ROUTE_TABLE.get(name)
            if handler_name is None:
                raise ValueError(f"unknown tool: {name}")
            handler = getattr(self, handler_name)
            if name == "kb_read_media":
                _base = handler
                handler = lambda args, _base=_base: _base(args, request_id=request_id)

            raw_result = diaglog.instrument_call(
                server=self,
                tool_name=name,
                corr_id=corr_id,
                handler=handler,
                arguments=arguments,
                config=diag_cfg,
            )

            t_ser0 = time.perf_counter()
            response = raw_result if name == "kb_read_media" else _text_content(raw_result)
            ser_ms = round((time.perf_counter() - t_ser0) * 1000, 2)
            resp_bytes = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            result_count = diaglog.extract_result_count(raw_result)
            truncated = raw_result.get("truncated") if isinstance(raw_result, dict) else None

            diaglog.record(
                corr_id=corr_id,
                tool=name,
                stage="serialize",
                ms=ser_ms,
                result_count=result_count,
                response_bytes=resp_bytes,
                truncated=truncated,
                config=diag_cfg,
            )
            return response
        except Exception as exc:
            total_ms = round((time.perf_counter() - t0) * 1000, 2)
            diaglog.record(
                corr_id=corr_id,
                tool=name,
                stage="fail",
                ms=total_ms,
                error_code=diaglog.classify_error(exc),
                config=diag_cfg,
            )
            raise

    def _kb_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._list_vaults()

    def _kb_set_weight(self, arguments: dict[str, Any]) -> dict[str, Any]:
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
        return {"path": entry.path, "name": entry.name, "weight": entry.weight}
    def _kb_rebuild(self, arguments: dict[str, Any]) -> dict[str, Any]:
        indexer = self._indexer_for(arguments)
        indexer.rebuild()
        return indexer.stats()

    def _kb_export(self, arguments: dict[str, Any]) -> dict[str, Any]:
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
        return indexer.export_snapshot(out)

    def _kb_import(self, arguments: dict[str, Any]) -> dict[str, Any]:
        for key in ("trust_parsed_documents", "replace", "confirm_replace"):
            if not isinstance(arguments.get(key, False), bool):
                raise ValueError(f"{key} must be a boolean")
        if arguments.get("replace", False) and not arguments.get("confirm_replace", False):
            raise ValueError("replace=true requires explicit confirm_replace=true")
        indexer = self._indexer_for(arguments)
        snapshot = str(arguments.get("snapshot", "")).strip()
        if not snapshot:
            raise ValueError("snapshot is required for kb_import")
        force = arguments.get("force", False)
        if isinstance(force, str):
            force = force.strip().lower() in {"1", "true", "yes", "on"}
        # §20.2/§20.7F：信任门禁与显式替换必须逐项透传到导入实现，不能在 schema
        # 层校验完就丢掉——否则「未知来源快照默认隔离」和「replace 独立确认」
        # 都只是文档承诺。
        return indexer.import_snapshot(
            snapshot,
            force=bool(force),
            trust_parsed_documents=bool(arguments.get("trust_parsed_documents", False)),
            replace=bool(arguments.get("replace", False)),
            confirm_replace=bool(arguments.get("confirm_replace", False)),
        )
    def _kb_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _dispatch_search(self, arguments)

    def _kb_list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        indexer = self._indexer_for(arguments)
        indexer.request_refresh(for_read=True)
        r_status = indexer.refresh_status()
        files = indexer.list_files()
        # path_prefix 与 kb_search 同口径（共用 path_prefix_match），空值不过滤；
        # source 恒为库内相对 posix 路径，`../`/绝对路径只会零命中（天生 fail-closed）。
        prefix = str(arguments.get("path_prefix") or "").strip()
        if prefix:
            files = [f for f in files if path_prefix_match(str(f.get("source", "")), prefix)]
        # total 语义钉死：**前缀过滤后、切片前**的条目数（消费方靠它判断有没有下一页）
        total = len(files)
        offset = _parse_int(arguments.get("offset"))
        offset = max(0, offset) if offset is not None else 0
        limit = _parse_int(arguments.get("limit"))
        if limit is not None and limit < 1:
            limit = None
        page = files[offset:] if limit is None else files[offset:offset + limit]
        # 刻意不加 limit 上限：返回的是摘要 {source,title,chunks}（非全文 chunk），
        # 且「缺省 = 全量」是既有承诺；加上限反而改变默认行为。
        next_offset = None
        if limit is not None and offset + len(page) < total:
            next_offset = offset + len(page)
        res = {
            "files": page,
            "total": total,
            # 不叫 truncated：v0.8.0 的 truncated 是**字节预算截断**（fanout.apply_budget），
            # 同名不同义会让消费方误读
            "page_truncated": next_offset is not None,
            "next_offset": next_offset,
        }
        if r_status["indexing_in_progress"]:
            res["indexing_in_progress"] = True
            res["indexing_progress"] = r_status["indexing_progress"]
        if r_status.get("refresh_error"):
            res["indexing_error"] = r_status["refresh_error"]
        return res

    # ------------------------------------------------------------ chunk_id 跨库寻址（C56）

    # 跨库探测上限（C64 量测结论，见 Changelog）：单次 kb_read(chunk_id) 最多为探测
    # 消耗的库数与墙钟预算——两者先到先停，被裁掉的库计入「未探测」并在报错里如实列出。
    # 实测（10 库 × 60 文件 / 每库 113KB 文本，本机）：整条 kb_read 82ms、峰值 619KiB；
    # 单库临时探测约 5ms。限流是为了让「几十个巨型库」的场景不出现秒级无界等待。
    _PROBE_MAX_UNLOADED_VAULTS = 32
    _PROBE_BUDGET_SECONDS = 2.0

    @staticmethod
    def _chunk_attribution(entry: Any, indexer: MarkdownIndexer, chunk: Chunk) -> dict[str, Any]:
        """单命中时的库归属键。

        solo 库只给库名（用户裁定 D5）：显式寻址与 fan-out 搜索是两条口径——探测覆盖
        solo 库，但结果里不为它展开绝对路径，只点明「这是 solo」。
        """
        name = getattr(entry, "name", None) or Path(indexer.vault_path).name
        attr: dict[str, Any] = {"vault": name}
        if getattr(entry, "solo", False):
            attr["solo"] = True
        else:
            attr["vault_path"] = str(indexer.vault_path)
        return {"indexer": indexer, "chunk": chunk, "attribution": attr}

    @staticmethod
    def _chunk_miss_message(chunk_id: str, total: int, probed: int, skipped: list[tuple[str, str]]) -> str:
        """零命中报错：必须带上「共 N 个候选库、探测了 M 个、跳过 K 个（名字与原因）」。

        有库没被探到时，绝不能沿用「文件可能已修改」的判定——那是在谎报结论。
        """
        head = f"chunk_id not found: {chunk_id[:12]}…（共 {total} 个候选库，探测了 {probed} 个"
        if skipped:
            detail = "；".join(f"{name}（{reason}）" for name, reason in skipped)
            return (
                f"{head}，跳过 {len(skipped)} 个：{detail}）；"
                "有库未被探测，不能据此断定文件已修改，请重新 kb_search 获取新 id"
            )
        return f"{head}；文件可能已修改，请重新 kb_search 获取新 id）"

    @staticmethod
    def _chunk_incomplete_message(
        chunk_id: str,
        total: int,
        probed: int,
        hits: list[dict[str, Any]],
        skipped: list[tuple[str, str]],
    ) -> str:
        """存在跳过库且已命中不足 2 个时的 incomplete 报错（C65）：
        无法确认唯一性，必须要求调用方显式传 vault_path。
        """
        hit_names = [
            (getattr(h["entry"], "name", None) or Path(h["entry"].path).name)
            + ("（solo 库）" if getattr(h["entry"], "solo", False) else "")
            for h in hits
        ]
        hits_str = f"，已在 {len(hits)} 个库中命中（{', '.join(hit_names)}）" if hits else ""
        detail = "；".join(f"{name}（{reason}）" for name, reason in skipped)
        return (
            f"chunk_id 探测未完成（incomplete）: {chunk_id[:12]}…（共 {total} 个候选库，探测了 {probed} 个{hits_str}，"
            f"跳过 {len(skipped)} 个：{detail}）；"
            "存在未探测库，无法确认唯一性，请显式传 vault_path 指定目标库"
        )

    @staticmethod
    def _close_probe_indexer(probe: MarkdownIndexer) -> None:
        """关闭临时探测 indexer 持有的 sqlite 连接（探测不该留下句柄）。"""
        close_store = getattr(probe, "close_document_store", None)
        if callable(close_store):
            try:
                close_store()
            except Exception:
                pass
        fts = getattr(probe, "_fts", None)
        if fts is not None:
            try:
                fts.close()
            except Exception:
                pass
        backend = getattr(probe, "_vector_backend", None)
        closer = getattr(backend, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def _locate_chunk_for_read(self, chunk_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """定位 chunk_id 所属库并返回「可读的 indexer + 归属键」（C56 / C65）。

        - 显式传了库标识（vault_path / vault / vault_name / path）→ **只探该库**，保留既有语义；
        - 未传且注册表为空 → 抛出具名工具错误（引导 kb_init），不报 IndexError；
        - 未传且只注册了一个库 → 走既有单库路径（行为与修复前一致）；
        - 未传且注册了多库 → 遍历**全部注册库（含 solo）**做只读探测：
          已加载的读内存态，未加载的用 `MarkdownIndexer(..., load_vectors=False)` 临时轻量
          构造（不起 watcher、不注册 atexit、不触发 sync/embedding）；
        - 探测边界裁决（C65）：
          1. 两个以上命中 → 已证实歧义，提示选库；
          2. 存在 skipped 且命中不足两个 → 报 incomplete 并要求显式传 vault_path；
          3. 无 skipped 且单命中 → 展开；
          4. 完整零命中 → not found（文件可能已修改）。
        """
        explicit = str(
            arguments.get("vault_path")
            or arguments.get("vault")
            or arguments.get("vault_name")
            or arguments.get("path")
            or ""
        ).strip()
        entries = self.registry.load()

        if not explicit and not entries:
            raise ValueError("没有已注册的知识库，请先使用 kb_init 注册知识库")

        if explicit or len(entries) <= 1:
            target = arguments if explicit else {"vault_path": entries[0].path}
            indexer = self._indexer_for(target)
            indexer.request_refresh()
            r_status = indexer.refresh_status()
            entry = self.registry.get(str(indexer.vault_path))
            for c in indexer.all_chunks():
                if c.id == chunk_id:
                    return self._chunk_attribution(entry, indexer, c)
            is_cold = (
                indexer.last_sync is None
                and not getattr(indexer, "_chunks_cache_loaded", False)
                and len(indexer._chunks) == 0
            )
            if is_cold:
                raise ValueError(
                    f"知识库正在后台进行首次初始化构建，无法定位 chunk_id {chunk_id[:12]}…，请稍后重试"
                )
            raise ValueError(self._chunk_miss_message(chunk_id, max(len(entries), 1), 1, []))

        hits: list[dict[str, Any]] = []
        skipped: list[tuple[str, str]] = []
        probed = 0
        probe_started = time.monotonic()
        unloaded_probes = 0
        probes_to_close: list[MarkdownIndexer] = []
        try:
            for entry in entries:
                is_solo = bool(getattr(entry, "solo", False))
                name = getattr(entry, "name", None) or Path(entry.path).name
                label = f"{name}（solo 库）" if is_solo else (getattr(entry, "name", None) or entry.path)
                key = str(Path(entry.path).expanduser().resolve())
                indexer = self._indexers.get(key)
                probe: MarkdownIndexer | None = None
                if indexer is None:
                    # 上限只约束「需要临时构造的库」：已加载库读内存态，零成本且必须探
                    over_count = unloaded_probes >= self._PROBE_MAX_UNLOADED_VAULTS
                    over_time = (time.monotonic() - probe_started) > self._PROBE_BUDGET_SECONDS
                    if over_count or over_time:
                        reason = ("超过探测库数上限" if over_count else "超过探测时间预算")
                        skipped.append((label, f"{reason}（本次已探测 {probed} 个）"))
                        continue
                    unloaded_probes += 1
                    if not Path(entry.path).is_dir():
                        skipped.append((label, "目录已不存在"))
                        continue
                    try:
                        probe = MarkdownIndexer(entry.path, self.config, load_vectors=False)
                        probes_to_close.append(probe)
                    except Exception as exc:  # OSError / zlib.error / 缓存损坏 / 构造失败 …
                        reason = type(exc).__name__ if is_solo else f"{type(exc).__name__}: {exc}"[:100]
                        skipped.append((label, reason))
                        continue
                    if not getattr(probe, "_chunks_cache_loaded", False):
                        skipped.append((label, "尚无可探测文本索引"))
                        continue
                    indexer = probe
                else:
                    # review R2：首次同步未完成的库一律视为 incomplete——内存里
                    # 非空的 _chunks 只是部分扫描结果，不能据此宣告「已探测完整库」，
                    # 否则跨库 chunk_id 定位会在首次同步期间误报唯一命中。
                    if indexer.last_sync is None and not getattr(indexer, "_chunks_cache_loaded", False):
                        skipped.append((label, "尚无可探测文本索引"))
                        continue

                probed += 1
                try:
                    hit = next((c for c in indexer.all_chunks() if c.id == chunk_id), None)
                except Exception as exc:
                    reason = type(exc).__name__ if is_solo else f"{type(exc).__name__}: {exc}"[:100]
                    skipped.append((label, reason))
                    probed -= 1
                    continue
                if hit is not None:
                    hits.append({"entry": entry, "indexer": indexer, "chunk": hit, "probe": probe})

            if len(hits) > 1:
                listed = "\n".join(
                    f"  - {getattr(h['entry'], 'name', None) or Path(h['entry'].path).name}"
                    + ("（solo 库）" if getattr(h["entry"], "solo", False) else f"：{h['entry'].path}")
                    for h in hits
                )
                raise ValueError(
                    f"chunk_id {chunk_id[:12]}… 在 {len(hits)} 个库中同时命中"
                    "（id = sha1(source\\0index\\0content)，同一文件复制进多库会撞 id）。"
                    f"请显式传 vault_path 指定其中一个：\n{listed}"
                )

            if skipped:
                if hits:
                    raise ValueError(
                        self._chunk_incomplete_message(chunk_id, len(entries), probed, hits, skipped)
                    )
                raise ValueError(self._chunk_miss_message(chunk_id, len(entries), probed, skipped))

            if not hits:
                raise ValueError(self._chunk_miss_message(chunk_id, len(entries), probed, skipped))

            hit = hits[0]
            if hit["probe"] is not None:
                # 命中未加载库：提升为常驻 indexer（与显式传 vault_path 同款），
                # 并在新 indexer 上按 id 复核一次（缓存态一致时必然命中）。
                indexer = self._indexer_for({"vault_path": hit["entry"].path})
                chunk = next((c for c in indexer.all_chunks() if c.id == chunk_id), None)
                if chunk is None:
                    raise ValueError(
                        f"chunk_id {chunk_id[:12]}… 所属库索引已变化，请重新 kb_search 获取新 id"
                    )
            else:
                indexer, chunk = hit["indexer"], hit["chunk"]
            return self._chunk_attribution(hit["entry"], indexer, chunk)
        finally:
            for p in probes_to_close:
                self._close_probe_indexer(p)

    def _kb_read_media(self, arguments: dict[str, Any], *, request_id: Any = None) -> dict[str, Any]:
        from ._server.media_dispatch import dispatch_media_read
        return dispatch_media_read(self, arguments, request_id=request_id)

    def _kb_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        allow_stale = arguments.get("allow_stale", False)
        if not isinstance(allow_stale, bool):
            raise ValueError("allow_stale must be a boolean")
        media_refs_offset = arguments.get("media_refs_offset", 0)
        if isinstance(media_refs_offset, bool) or not isinstance(media_refs_offset, int) or media_refs_offset < 0:
            raise ValueError("media_refs_offset must be an integer >= 0")
        raw_source = arguments.get("source")
        source = str(raw_source).strip() if raw_source is not None else ""
        raw_chunk_id = arguments.get("chunk_id")
        chunk_id = str(raw_chunk_id).strip() if raw_chunk_id is not None else ""

        if not source and not chunk_id:
            raise ValueError("source or chunk_id is required for kb_read")

        raw_heading = arguments.get("heading")
        heading = str(raw_heading).strip() if raw_heading is not None and str(raw_heading).strip() != "" else None

        raw_start_line = arguments.get("start_line")
        raw_end_line = arguments.get("end_line")
        raw_start_char = arguments.get("start_char")

        # start_char validation (Req 11)
        start_char = 0
        if "start_char" in arguments:
            if chunk_id or heading:
                raise ValueError("start_char cannot be used with chunk_id or heading")
            if raw_start_char is not None:
                if isinstance(raw_start_char, bool):
                    raise ValueError(f"start_char must be an integer >= 0, got boolean {raw_start_char!r}")
                if isinstance(raw_start_char, float):
                    raise ValueError(f"start_char must be an integer >= 0, got float {raw_start_char!r}")
                if isinstance(raw_start_char, int):
                    val = raw_start_char
                elif isinstance(raw_start_char, str):
                    try:
                        val = int(raw_start_char.strip())
                    except ValueError:
                        raise ValueError(f"start_char must be an integer >= 0, got {raw_start_char!r}")
                else:
                    raise ValueError(f"start_char must be an integer >= 0, got {type(raw_start_char).__name__}")
                if val < 0:
                    raise ValueError(f"start_char must be >= 0, got {val}")
                start_char = val

        # start_line and end_line strict validation (Req 6)
        def _parse_strict_line(val: Any, name: str) -> int | None:
            if val is None:
                return None
            if isinstance(val, bool):
                raise ValueError(f"{name} must be an integer >= 1, got boolean {val!r}")
            if isinstance(val, float):
                raise ValueError(f"{name} must be an integer >= 1, got float {val!r}")
            if isinstance(val, int):
                if val < 1:
                    raise ValueError(f"{name} must be >= 1, got {val}")
                return val
            if isinstance(val, str):
                s = val.strip()
                if not s:
                    return None
                try:
                    parsed = int(s)
                except ValueError:
                    raise ValueError(f"{name} must be an integer >= 1, got {val!r}")
                if parsed < 1:
                    raise ValueError(f"{name} must be >= 1, got {parsed}")
                return parsed
            raise ValueError(f"{name} must be an integer >= 1, got {type(val).__name__}")

        req_start_line = _parse_strict_line(raw_start_line, "start_line")
        req_end_line = _parse_strict_line(raw_end_line, "end_line")

        if req_end_line is not None:
            cmp_start = req_start_line if req_start_line is not None else 1
            if req_end_line < cmp_start:
                raise ValueError(f"end_line must be >= start_line: end_line={req_end_line}, start_line={cmp_start}")

        if start_char != 0 and req_start_line is None:
            raise ValueError("start_char requires start_line to be specified")

        attribution: dict[str, Any] = {}
        if chunk_id:
            # chunk_id 与 start_line/end_line/heading 互斥，避免入参歧义与非预期展开
            if req_start_line is not None or req_end_line is not None or heading is not None:
                raise ValueError("chunk_id is mutually exclusive with start_line/end_line/heading")

            raw_expand = arguments.get("expand_lines")
            if raw_expand is None:
                expand_lines = 30
            else:
                parsed_expand = _parse_int(raw_expand)
                if parsed_expand is None:
                    expand_lines = 30
                else:
                    expand_lines = max(0, min(parsed_expand, 500))

            # 跨库自动寻址（C56）：显式指定库时只探该库；未指定且注册了多库时遍历全部
            # 注册库（含 solo）做只读探测；单库注册时等价于原单库路径。
            located = self._locate_chunk_for_read(chunk_id, arguments)
            indexer = located["indexer"]
            chunk = located["chunk"]
            attribution = located["attribution"]

            source = chunk.source
            # §20.7B：虚拟 chunk 的版本寻址必须用 chunk 自己捕获的 revision/SHA
            # （虚拟签名是版本标识，**不是**物理 SHA，绝不能拿它当物理文件校验值）。
            is_virtual_chunk = bool(chunk.metadata.get("revision_id"))
            expected_revision_id: str | None = None
            expected_render_sha256: str | None = None
            if is_virtual_chunk:
                expected_revision_id = str(chunk.metadata.get("revision_id") or "")
                expected_render_sha256 = str(chunk.metadata.get("render_sha256") or "")
                expected_sha256 = str(chunk.metadata.get("source_sha256") or "")
                if not (expected_revision_id and expected_render_sha256 and expected_sha256):
                    raise ValueError(
                        f"chunk_id {chunk_id[:12]}… 所在虚拟源 {source} 缺失 revision/SHA 记录（stale）；"
                        "请重新 kb_search 获取新 id"
                    )
                recorded_sig = None
            else:
                recorded_sig = indexer._signatures.get(source)
                if not recorded_sig:
                    raise ValueError(
                        f"chunk_id {chunk_id[:12]}… 所在源文件 {source} 缺失索引签名记录（stale）；"
                        "请重新 kb_search 获取新 id，或改用 source + heading / 行区间读取"
                    )
                expected_sha256 = recorded_sig

            c_start = chunk.metadata.get("start_line", 1)
            c_end = chunk.metadata.get("end_line", c_start)
            try:
                c_start_int = int(c_start)
            except (TypeError, ValueError):
                c_start_int = 1
            try:
                c_end_int = int(c_end)
            except (TypeError, ValueError):
                c_end_int = c_start_int
            start_line = max(1, c_start_int - expand_lines)
            end_line = c_end_int + expand_lines
            is_chunk_read = True
            echo_start_line = start_line
            echo_end_line = end_line
            chunk_id_hint = f"{chunk_id[:12]}… "
        else:
            expected_revision_id = None
            expected_render_sha256 = None
            indexer = self._indexer_for(arguments)
            indexer.request_refresh()
            is_chunk_read = False
            start_line = req_start_line
            end_line = req_end_line
            echo_start_line = req_start_line
            echo_end_line = req_end_line
            expected_sha256 = None
            chunk_id_hint = None

            # F5b: 双链与短名寻址
            # 1. 规范化输入：支持 Obsidian [[笔记名]]、[[笔记名|别名]]、[[笔记名#段落]] 语法
            clean_source = source
            orig_query = source
            if clean_source.startswith("[[") and clean_source.endswith("]]"):
                inner = clean_source[2:-2].strip()
                # 管道别名语法：[[Target|Display Label]] -> 目标笔记为 Target
                if "|" in inner:
                    inner = inner.partition("|")[0].strip()
                # 锚点语法：[[Target#Section]] -> 若未显式传 heading，则提取锚点作为 heading
                # 注意避免误伤笔记文件名本身含有 # 的情形（如 C#教程.md）
                if "#" in inner:
                    inner_norm = inner.replace("\\", "/")
                    target_direct = (indexer.vault_path / inner_norm).resolve()
                    known_stems = {Path(s).stem.casefold() for s in indexer._chunks.keys()}
                    if not (target_direct.is_file() or Path(inner_norm).stem.casefold() in known_stems):
                        note_part, _, head_part = inner.partition("#")
                        clean_source = note_part.strip()
                        if heading is None and head_part.strip():
                            heading = head_part.strip()
                    else:
                        clean_source = inner
                else:
                    clean_source = inner

            norm_clean = clean_source.replace("\\", "/")

            # 2. 检查直连文件是否存在（支持带后缀或已有完整路径但缺省 .md）
            direct_file_path = None
            try:
                candidate_path = (indexer.vault_path / norm_clean).resolve()
                if candidate_path.is_file() and candidate_path.suffix.lower() in indexer._READABLE_SUFFIXES:
                    direct_file_path = norm_clean
                elif ("/" in norm_clean or "\\" in norm_clean) and not candidate_path.suffix:
                    # 仅在包含路径分隔符时尝试补常见扩展名（避免把根目录短名抢先直连导致漏判短名歧义）
                    for sfx in indexer._READABLE_SUFFIXES:
                        with_sfx = (indexer.vault_path / f"{norm_clean}{sfx}").resolve()
                        if with_sfx.is_file():
                            direct_file_path = f"{norm_clean}{sfx}"
                            break
            except Exception:
                pass

            if direct_file_path is not None:
                source = direct_file_path
            elif "/" not in norm_clean and "\\" not in norm_clean:
                # 3. 短名寻址：在已索引文件集合中按 stem 大小写不敏感匹配
                target_stem = Path(norm_clean).stem.casefold()
                candidates = [s for s in sorted(indexer._chunks.keys()) if Path(s).stem.casefold() == target_stem]
                if len(candidates) == 1:
                    source = candidates[0]
                elif len(candidates) > 1:
                    raise ValueError(
                        f"ambiguous note name '{orig_query}' matches multiple files: {', '.join(candidates[:5])}..."
                    )
                else:
                    is_cold = (
                        not Path(norm_clean).suffix
                        and indexer.last_sync is None
                        and not getattr(indexer, "_chunks_cache_loaded", False)
                        and len(indexer._chunks) == 0
                    )
                    if is_cold:
                        raise ValueError(
                            f"知识库正在后台进行首次初始化构建，无法解析短文件名 '{orig_query}'，请稍后重试或使用完整路径"
                        )
                    source = clean_source
            else:
                source = clean_source

        read_max_chars = indexer.config.index.read_max_chars
        read_res = indexer._read_result(
            source=source,
            start_line=start_line,
            end_line=end_line,
            heading=heading,
            start_char=start_char,
            max_chars=read_max_chars,
            expected_sha256=expected_sha256,
            chunk_id_hint=chunk_id_hint,
            allow_stale=allow_stale,
            expected_revision_id=expected_revision_id,
            expected_render_sha256=expected_render_sha256,
        )

        if heading and req_start_line is None and req_end_line is None:
            echo_start_line = read_res.effective_start_line
            echo_end_line = read_res.effective_end_line

        result = {
            "source": source,
            "start_line": echo_start_line,
            "end_line": echo_end_line,
            "effective_start_line": read_res.effective_start_line,
            "effective_end_line": read_res.effective_end_line,
            "total_lines": read_res.total_lines,
            "content": read_res.content,
            "truncated": read_res.truncated,
            "content_end_line": read_res.content_end_line,
            "next_start_line": read_res.next_start_line,
            "next_start_char": read_res.next_start_char,
        }
        # 虚拟读取必须显式暴露版本/来源事实，并附媒体引用分页（§15.1/§15.4）：
        # 消费方据此选择 kb_read_media，而不是猜「哪张图对应哪段」。
        if read_res.source_kind == "virtual":
            result["source_kind"] = "virtual"
            result["revision_id"] = read_res.revision_id
            result["render_sha256"] = read_res.render_sha256
            result["line_basis"] = read_res.line_basis
            if read_res.quality is not None:
                result["quality"] = read_res.quality
            if read_res.coverage is not None:
                result["coverage"] = read_res.coverage
            if read_res.source_changed:
                result["source_changed"] = True
            result.update(self._media_refs_page(
                indexer, source, read_res.revision_id, media_refs_offset,
                text_range={"start_line": read_res.effective_start_line,
                            "end_line": read_res.effective_end_line}))
        if is_chunk_read:
            result["chunk_id"] = chunk.id
            result.update(attribution)
        return result

    def _media_refs_page(self, indexer: MarkdownIndexer, source: str, revision_id: str | None,
                         offset: int, text_range: dict[str, Any] | None = None) -> dict[str, Any]:
        """虚拟源的媒体引用一页（compact 键保持，缺值不臆造）。

        越权/过期/库不可用一律返回空页而不是让 kb_read 失败：正文读取本身已经
        通过 revision/SHA 核验，媒体引用只是附加上下文。

        E08-e：分页显式绑定 **revision + 文本范围 + offset**，续页不跨 revision、
        不丢不重；调用方据 `media_refs_revision_id` 校验续页仍在同一版本。
        """
        empty = {"media_refs": [], "media_refs_offset": offset, "next_media_refs_offset": None,
                 "media_refs_revision_id": revision_id}
        if text_range is not None:
            empty["media_refs_text_range"] = text_range
        if not revision_id:
            return empty
        limit = int(getattr(self.config.media, "refs_limit", 20))
        limit = max(1, min(limit, 100))
        try:
            store = indexer.document_store()
            rows = store.list_media(source, revision_id=revision_id, offset=offset, limit=limit + 1)
        except Exception:
            return empty
        page = rows[:limit]
        refs: list[dict[str, Any]] = []
        for row in page:
            entry: dict[str, Any] = {"occurrence_id": row.get("occurrence_id"), "kind": row.get("kind")}
            for key in ("page", "t_start_ms", "t_end_ms"):
                if row.get(key) is not None:
                    entry[key] = row[key]
            refs.append(entry)
        has_more = len(rows) > limit
        result = {"media_refs": refs, "media_refs_offset": offset,
                  "next_media_refs_offset": offset + len(page) if has_more else None,
                  "media_refs_revision_id": revision_id}
        if text_range is not None:
            result["media_refs_text_range"] = text_range
        # E08-d/联合音频出口：音频相邻片段按同一 revision + 毫秒区间合并展示去重叠，
        # 保留每个原始片段地址（occurrence_ids）。跨页稳定：按整 revision 有界列举计算。
        if any(str(row.get("kind")) == "audio" for row in rows):
            try:
                from ._indexer.media import merge_audio_segments
                all_rows = list(store.iter_media(source, revision_id=revision_id))
                result["media_audio_segments"] = merge_audio_segments(all_rows)
            except Exception:
                pass
        return result

    def _kb_stats(self, arguments: dict[str, Any]) -> dict[str, Any]:
        indexer = self._indexer_for(arguments)
        indexer.request_refresh(for_read=True)
        r_status = indexer.refresh_status()
        stats = indexer.stats()
        # E04-b：与检索/导入同一口径的 additive 索引状态（ready 不激活隔离事实）。
        index_state = indexer.index_state()
        stats["index_state"] = index_state["index_state"]
        stats["isolated_facts"] = index_state["isolated_facts"]
        if index_state["next_action"]:
            stats["next_action"] = index_state["next_action"]
        # 付费闸门状态必须可观测：否则「检索退回词法」看起来像向量算错，实际是
        # 等待显式重嵌授权（§20.7B）。这里只报告事实，不做任何隐式授权动作。
        unresolved_intents = indexer.unresolved_paid_intents()
        if unresolved_intents:
            # 结果未知的付费请求必须可观测，否则「嵌入静默暂停」看起来像坏了。
            stats["pending_paid_requests"] = [
                {"request_id": item["request_id"], "kind": item["kind"],
                 "state": item["state"], "attempt": item["attempt"]}
                for item in unresolved_intents
            ]
        if getattr(indexer, "_embedding_paused", False):
            stats["embedding_paused"] = True
            stats["embedding_paused_reason"] = (
                "paid embedding is paused: an earlier paid request has an unknown outcome; "
                "confirm or abandon those intents (see pending_paid_requests) before retrying"
                if unresolved_intents else
                "paid embedding requires explicit approval for the current profile "
                "(run --approve-reembedding <profile_fingerprint>)"
            )
        if getattr(indexer, "_paid_profile_requires_approval", False):
            stats["pending_reembedding_approval"] = True
            try:
                summary = indexer.reembedding_approval_summary()
            except Exception:
                summary = None
            if summary is not None:
                stats["reembedding_approval"] = summary
        if r_status["indexing_in_progress"]:
            stats["indexing_in_progress"] = True
            stats["indexing_progress"] = r_status["indexing_progress"]
        if r_status.get("refresh_error"):
            stats["indexing_error"] = r_status["refresh_error"]

        vault_key = str(indexer.vault_path.resolve())
        ingest_cfg = self.config.ingest
        effective_auto = bool(ingest_cfg.enabled and ingest_cfg.auto_watch)
        watch_method = self.config.watch_method
        fallback_int = self.config.watch_fallback_interval
        effective_interval = fallback_int if fallback_int > 0 else 30.0

        manager = self._ingest_managers.get(vault_key)
        last_scan_at = None
        last_err = None
        skipped_too_large = 0
        skipped_seen = 0
        skipped_ignored = 0
        # legacy 用 .ingest_state.json 快照；virtual 的账本在 store 里（无 state_path），
        # 这里必须容错，不能因为换了实现就让 kb_stats 抛错。
        state_path = getattr(manager, "state_path", None) if manager is not None else None
        if manager is not None and state_path is not None and state_path.exists():
            try:
                st_data = manager._load_state()
                aw_st = st_data.get("auto_watch", {})
                last_scan_at = aw_st.get("last_scan_at")
                last_err = aw_st.get("last_error") or None
                skipped_too_large = aw_st.get("skipped_too_large", 0)
                skipped_seen = aw_st.get("skipped_seen", 0)
                skipped_ignored = aw_st.get("skipped_ignored", 0)
            except Exception:
                pass

        stats["ingest_auto"] = {
            "configured": bool(ingest_cfg.auto_watch),
            "effective": bool(effective_auto),
            # 解析事实落点与网络策略必须可见：否则「为什么没有 .mortis-parsed 镜像」
            # 或「为什么云解析被拒」都要靠猜（§20.4 首次 5 分钟验）。
            "storage": str(getattr(ingest_cfg, "storage", "virtual")),
            "network_policy": str(getattr(ingest_cfg, "network_policy", "configured")),
            "audio_enabled": bool(getattr(ingest_cfg, "audio_enabled", False)),
            "max_file_size_mb": ingest_cfg.max_file_size_mb,
            "watch_method": watch_method,
            "effective_interval": effective_interval,
            "last_scan_at": last_scan_at,
            "last_error": last_err,
            "skipped_too_large": skipped_too_large,
            "skipped_seen": skipped_seen,
            "skipped_ignored": skipped_ignored,
        }
        return stats

    def _kb_exempt(self, arguments: dict[str, Any]) -> dict[str, Any]:
        indexer = self._indexer_for(arguments)
        action = str(arguments.get("action", "list")).strip()
        pattern = str(arguments.get("pattern", "")).strip()
        source = str(arguments.get("source", "")).strip()
        method = str(arguments.get("method", "frontmatter")).strip()
        if action == "list":
            return indexer.get_exemptions()
        if action == "add_pattern":
            if not pattern:
                raise ValueError("pattern is required for add_pattern")
            return indexer.add_exemption_pattern(pattern)
        if action == "remove_pattern":
            if not pattern:
                raise ValueError("pattern is required for remove_pattern")
            return indexer.remove_exemption_pattern(pattern)
        if action == "exempt_file":
            if not source:
                raise ValueError("source is required for exempt_file")
            return indexer.set_file_exemption(source, exempt=True, method=method)
        if action == "unexempt_file":
            if not source:
                raise ValueError("source is required for unexempt_file")
            return indexer.set_file_exemption(source, exempt=False, method=method)
        if action == "check":
            if not source:
                raise ValueError("source is required for check")
            return indexer.check_exemption(source)
        raise ValueError(f"unknown action for kb_exempt: {action}")

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
                return _json_result(request_id, self.call_tool(tool_name, raw_args, request_id=request_id))
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


def approve_reembedding(profile_fingerprint: str, *, config_path: str | Path | None = None,
                        vault_path: str | Path | None = None) -> dict[str, Any]:
    """写本机付费重嵌授权（§20.7B）。不调用任何外部 API，只记录精确 profile 授权。

    授权范围只覆盖该 fingerprint：配置再次变化（模型/模板/预处理/维度/切块代际）
    会得到新 fingerprint，旧授权自动失效，不会被沿用。
    """
    fingerprint = str(profile_fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("profile_fingerprint is required")
    config = load_config(resolve_config_path(config_path))
    if not getattr(config.cache, "enabled", False):
        raise ValueError("cache is disabled; paid re-embedding authorization requires [cache]")
    target_vault = Path(vault_path or config.vault_path).expanduser()
    layout = resolve_storage_layout(config, target_vault)
    control = ControlStore(layout)
    try:
        control.open(create=True, write=True)
        control.authorize_paid_profile(fingerprint, scope="reembedding",
                                      cost_summary={"basis": "unknown", "reason": "estimator not calibrated"},
                                      note=f"cli approved for {target_vault}")
    finally:
        control.close()
    return {"approved": True, "profile_fingerprint": fingerprint, "vault_path": str(target_vault),
            "requires_reembedding": True}


def migrate_ingest(*, apply: bool = False, config_path: str | Path | None = None,
                   vault_path: str | Path | None = None) -> dict[str, Any]:
    """旧 `.mortis-parsed` 镜像迁移入口（§20.1）：**默认 dry-run**，`apply=True` 才写 store。

    不联网、不删除旧镜像、不自动重嵌；无法证明归属/源 SHA 不符的项一律 pending_manual。
    """
    from .ingest.migration import migrate_legacy_mirrors

    config = load_config(resolve_config_path(config_path))
    if not getattr(config.cache, "enabled", False):
        raise ValueError("cache is disabled; mirror migration requires [cache]")
    target_vault = Path(vault_path or config.vault_path).expanduser()
    layout = resolve_storage_layout(config, target_vault)
    store = DocumentStore(layout, config)
    store.open(write=True)
    try:
        return migrate_legacy_mirrors(store, apply=bool(apply))
    finally:
        store.close()


def _explicit_config_problem(explicit: str | Path | None) -> str:
    """显式 `--app-config` 或**实际选中**的配置环境变量无效时返回原因（调用方退出 2）。

    优先级 CLI > 新 env (`MORTIS_RAG_CONFIG`) > 旧 env (`VAULT_MCP_CONFIG`) > 默认：
    坏路径**绝不**静默回落到另一份宿主配置；未选中的低优先级坏值不误伤。
    """
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            return f"invalid --app-config: {candidate} is not a readable file"
        return ""
    for name in ("MORTIS_RAG_CONFIG", "VAULT_MCP_CONFIG"):
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        if not Path(raw).expanduser().is_file():
            return f"invalid {name}: {raw} is not a readable file"
        break
    return ""


def _control_for(vault_path: str | Path, config_path: str | Path | None = None):
    config = load_config(resolve_config_path(config_path))
    target = Path(vault_path).expanduser()
    return resolve_storage_layout(config, target)


def list_requests(*, vault_path: str | Path, config_path: str | Path | None = None) -> dict[str, Any]:
    """只读列出本机付费请求意图（E06）：不发起任何网络请求，不授权任何 profile。"""
    from .doc_store import ControlStore

    layout = _control_for(vault_path, config_path)
    control = ControlStore(layout)
    try:
        control.open(create=False, write=False)
        rows = control.list_request_intents()
    finally:
        control.close()
    requests = [{"request_id": str(row["request_id"]), "kind": str(row["kind"]),
                 "state": str(row["state"]), "attempt": int(row["attempt"] or 0),
                 "next_action": _request_next_action(str(row["state"]))} for row in rows]
    return {"vault_path": str(Path(vault_path).expanduser()), "count": len(requests),
            "requests": requests,
            "hint": "只读列表：放弃用 --abandon-request REQUEST_ID（只改本机记录，不重发请求）"}


def _request_next_action(state: str) -> str:
    """按状态给出脱敏的下一步提示（不含 endpoint/密钥等内部字段）。"""
    if state == "prepared":
        return "outcome unknown: keep this record, query the original remote task, or abandon it explicitly"
    if state == "submission_unknown":
        return "remote outcome unknown: do not resend; confirm the original task or abandon this record"
    if state == "success":
        return "settled: no action"
    if state == "abandoned":
        return "abandoned: a later explicit submission may repeat work that was already paid for"
    return ""


def abandon_request(request_id: str, *, vault_path: str | Path,
                    config_path: str | Path | None = None) -> dict[str, Any]:
    """放弃一条未决付费请求意图（E06）。

    只改**该目标**记录的 prepared/submission_unknown → abandoned：不 POST、不 mark
    success、不授权 profile。终态/竞争丢失返回 `abandoned=False` 与当前状态。
    帮助文本明确：此后再次显式提交可能重复此前已处理的工作。
    """
    from .doc_store import ControlStore

    layout = _control_for(vault_path, config_path)
    control = ControlStore(layout)
    try:
        control.open(create=False, write=True)
        moved = control.mark_intent(request_id, "abandoned",
                                    reason="abandoned by explicit --abandon-request",
                                    expected_states=("prepared", "submission_unknown"))
        state = control.intent_state(request_id) or "unknown"
    finally:
        control.close()
    return {"request_id": request_id, "abandoned": bool(moved), "state": str(state),
            "next_action": _request_next_action(str(state)),
            "warning": ("abandoning only drops this local intent; a later explicit submission may "
                        "repeat work that was already processed (and billed) before")}


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="mortis-rag-mcp")
    parser.add_argument("--serve-mcp-stdio", action="store_true")
    parser.add_argument("--app-config", default=None)
    parser.add_argument("--doctor", action="store_true",
                        help="全量探测本机环境（含 API 真实调用）并重写 ~/.mortis_rag_mcp/STATUS.md 与 status.json")
    parser.add_argument("--approve-reembedding", default=None, metavar="PROFILE_FINGERPRINT",
                        help="显式授权精确 embedding profile 的付费重嵌（只写本机授权记录，不调用 API）")
    parser.add_argument("--migrate-ingest", action="store_true",
                        help="迁移旧 .mortis-parsed 镜像到文档库（默认 dry-run；不联网、不删除旧镜像）")
    parser.add_argument("--apply", action="store_true", help="仅 --migrate-ingest 可用：真正写库")
    parser.add_argument("--list-requests", action="store_true",
                        help="只读列出本机付费请求意图（需 --vault；不发起请求）")
    parser.add_argument("--abandon-request", default=None, metavar="REQUEST_ID",
                        help="放弃一条未决付费请求意图（需 --vault；只改本机记录，不重发、不授权）")
    parser.add_argument("--vault", default=None, help="管理操作（--approve-reembedding / --migrate-ingest / --list-requests / --abandon-request）的目标库路径")
    parser.add_argument("--quiet", action="store_true", help="静默模式，禁止输出到 stdout")
    args = parser.parse_args(argv)
    # 管理 action 互斥（E06）：一次只做一件事，避免组合出意外副作用。
    selected = [name for name, value in (
        ("--serve-mcp-stdio", args.serve_mcp_stdio),
        ("--doctor", args.doctor),
        ("--migrate-ingest", args.migrate_ingest),
        ("--approve-reembedding", args.approve_reembedding is not None),
        ("--list-requests", args.list_requests),
        ("--abandon-request", args.abandon_request is not None),
    ) if value]
    if len(selected) > 1:
        parser.error("management actions are mutually exclusive: " + ", ".join(selected))
    if args.apply and not args.migrate_ingest:
        parser.error("--apply is only valid with --migrate-ingest")
    if not selected and not args.serve_mcp_stdio:
        parser.error("--serve-mcp-stdio is required")
    if selected and "--serve-mcp-stdio" not in selected:
        # 管理操作必须显式指向目标库（正常 serve 不受此限制）。
        if not args.vault:
            parser.error("--vault is required for " + ", ".join(selected))
    # 坏配置绝不静默回落：显式 --app-config 或实际选中的 env 无效 → 退出 2。
    problem = _explicit_config_problem(args.app_config)
    if problem:
        sys.stderr.write(problem + "\n")
        return 2
    if args.list_requests:
        result = list_requests(vault_path=args.vault, config_path=args.app_config)
        if not args.quiet:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    if args.abandon_request is not None:
        result = abandon_request(args.abandon_request, vault_path=args.vault,
                                 config_path=args.app_config)
        if not args.quiet:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    if args.migrate_ingest:
        result = migrate_ingest(apply=args.apply, config_path=args.app_config, vault_path=args.vault)
        if not args.quiet:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    if args.approve_reembedding is not None:
        result = approve_reembedding(args.approve_reembedding, config_path=args.app_config,
                                     vault_path=args.vault)
        if not args.quiet:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    if args.doctor:
        from . import doctor
        return doctor.run(full=True, app_config=args.app_config, quiet=args.quiet)
    if not args.serve_mcp_stdio:
        parser.error("--serve-mcp-stdio is required")
    return serve_stdio(args.app_config)


if __name__ == "__main__":
    raise SystemExit(main())
