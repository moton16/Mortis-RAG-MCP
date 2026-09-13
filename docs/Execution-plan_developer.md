# Execution-plan：v0.7.0 开发执行方案（照做即可）

> 读者：下一位开发者（人类或 agent）。
> 用法：**按 Phase 顺序执行，每个 Phase 内的步骤按编号执行**。每一步都给出：改哪个文件、加/改什么代码（可直接粘贴）、写什么测试、commit 信息、要在 `Changelog_developer.md` 记什么。
> 铁律：
> 1. **不改本方案未提及的文件**；发现方案与现实冲突时停下来，在 `Changelog_developer.md` 记录冲突并找人确认，不要自由发挥。
> 2. 全程零第三方运行时依赖（`pyproject.toml` 的 `dependencies = []` 不许破）。HTTP 只用 `urllib`，序列化只用 `json`/`tomllib`。
> 3. 测试不许碰真实网络（monkeypatch 掉 HTTP 层）；不许在 `tests/` 之外留垃圾文件。
> 4. 每个 commit 后把对应条目追加进 `docs/Changelog_developer.md`（格式见该文件开头约定与本文 §6）。

---

## 0. 总览

| Phase | 目标 | 主要文件 | commit 数 |
|---|---|---|---|
| P0 | 检索评测 harness（之后所有调参的度量尺） | `scripts/eval_search.py`、`tests/eval/golden_queries.json` | 1 |
| P1 | 定向检索路由：vault description + fan-out hint + MCP instructions + 工具描述纪律 + skill 重写 | `registry.py`、`server.py`、`skills/mortis-rag-mcp/SKILL.md` | 3 |
| P2 | PDF/Office 摄取层（**默认关闭**、**按需异步**、内嵌 MinerU 客户端、产物收 `.mortis-parsed/`） | `config.py`、`mortis_rag_mcp/ingest/*`、`server.py`、`indexer.py`、README/QUICKSTART | 4 |
| P3 | 可选增强（title/alias boost、per-source 限流）——**先用 P0 跑基线，再决定做不做** | `indexer.py` | 0~1 |

背景结论（不要再重新论证，直接接受）：
- AI 该定向搜却全库搜的根因是**行为规则放错层**：工具描述只写机制不写纪律、MCP `instructions` 空着、skill 懒加载。P1 三层一起补。
- PDF 摄取必须是**摄取层**而非检索路径依赖：异步、可重试、产物落盘可审查。MinerU 官方 API 文档（https://mineru.net/apiManage/docs ）已核实，本文所有端点/参数/错误码均来自该文档。

---

## P0：检索评测 harness

**为什么第一个做**：P1/P2/P3 都会改检索行为，没有评测脚本就是闭眼调参。

### P0-1 新建 `tests/eval/golden_queries.json`

```json
{
  "说明": "金标准查询集。expect = 期望命中的 source 子串（库内相对路径）。path_prefix/vault_path 可选，用于验证定向检索。初始先放占位，各开发者按自己的库补充 20-30 条。",
  "queries": [
    {
      "vault": "D:/path/to/vault",
      "query": "TTL 与非门的噪声容限怎么算",
      "expect": "数电/",
      "path_prefix": "",
      "note": "概念型查询，期望命中数电笔记子树"
    }
  ]
}
```

### P0-2 新建 `scripts/eval_search.py`（完整文件，直接创建）

```python
"""检索评测：对金标准查询集跑 MarkdownIndexer.search，报告 Hit@K。

用法：
    python scripts/eval_search.py --golden tests/eval/golden_queries.json --k 5 [--config config/app.toml]
退出码：全部命中 0；有 miss 1（可挂 CI）。
注意：external embedding 模式下 sync/search 会真实调用 API，建议先用 static 模式
（config 里 [embedding] mode = "static"）跑回归，外部模式只用于最终抽查。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mortis_rag_mcp.config import load_config  # noqa: E402
from mortis_rag_mcp.indexer import MarkdownIndexer, SearchFilter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    config = load_config(args.config)
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    queries = golden["queries"]

    indexers: dict[str, MarkdownIndexer] = {}
    hits = 0
    misses: list[dict] = []
    for i, case in enumerate(queries, 1):
        vault = case["vault"]
        if vault not in indexers:
            idx = MarkdownIndexer(vault_path=vault, config=config)
            idx.sync()
            indexers[vault] = idx
        idx = indexers[vault]
        filters = None
        if case.get("path_prefix"):
            filters = SearchFilter(path_prefix=case["path_prefix"])
        t0 = time.time()
        results = idx.search(case["query"], top_k=args.k, use_rerank=True, filters=filters)
        elapsed = time.time() - t0
        sources = [c.source for c in results]
        ok = any(case["expect"] in s for s in sources)
        mark = "HIT " if ok else "MISS"
        print(f"[{i:02d}/{len(queries)}] {mark} ({elapsed:.2f}s) {case['query']!r} -> expect {case['expect']!r}")
        if ok:
            hits += 1
        else:
            misses.append({"query": case["query"], "expect": case["expect"], "got": sources[:3]})

    rate = hits / max(1, len(queries))
    print(f"\nHit@{args.k}: {hits}/{len(queries)} = {rate:.1%}")
    if misses:
        print("Misses:")
        for m in misses:
            print(f"  {m['query']!r} expect {m['expect']!r}, top3={m['got']}")
    return 0 if not misses else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

### P0-3 commit

- 信息：`test(eval): 检索评测 harness（Hit@K 金标准回归脚本）`
- `Changelog_developer.md` 条目见 §6 模板 C0。

### P0 验收

- [ ] `python scripts/eval_search.py --golden tests/eval/golden_queries.json` 能跑通（哪怕只有 1 条占位查询）
- [ ] 后续每个 Phase 结束时跑一次并记录 Hit@K 到 commit message body

---

## P1：定向检索路由（治"该定向却全库搜"）

三层一起补：协议层（instructions）→ 工具层（描述纪律 + description 字段 + hint）→ skill 层（重写）。

### P1-1 `registry.py`：VaultEntry 加 `description` 字段

1. `VaultEntry` dataclass（约 118 行）追加字段：

```python
    description: str = ""  # v4（0.7.0）：库的语义说明（如"数电教材+课件"），供模型定向选库
```

2. `load()`（约 165-186 行的字段还原处）加：

```python
                    # description 是 v4 新增字段：老 toml 里没有 → ""（不参与任何旧逻辑，纯元数据）。
                    description = str(raw.get("description", "") or "")
```

   并在构造 `VaultEntry(...)` 时传 `description=description`。

3. `save()`（约 208-211 行，`solo` 行之后）加：

```python
            # 空串不写入，保持老文件干净；序列化与 path/name 同款 json.dumps 防注入。
            if entry.description:
                lines.append(f"description = {json.dumps(entry.description, ensure_ascii=False)}")
```

4. `add()` 签名（约 226 行）加 `description: str = ""`，构造 `VaultEntry` 时传入。

5. 仿照 `set_weight`（约 266 行）新增：

```python
    def set_description(self, path: str | os.PathLike[str], description: str) -> VaultEntry:
        """更新库描述。与 set_weight 同款：load→改→save，进程锁+跨进程文件锁。"""
        with self._lock, _process_file_lock(self._lock_path):
            resolved = str(Path(path).expanduser().resolve())
            entries = self.load()
            for entry in entries:
                if normalize_vault_key(entry.path) == normalize_vault_key(resolved):
                    entry.description = description.strip()
                    self.save(entries)
                    return entry
            raise ValueError(f"vault not registered: {resolved}")
```

   注意：`VaultEntry` 是 `@dataclass(slots=True)` 非 frozen，直接赋值即可（`set_solo` 已有同款写法）。

### P1-2 `server.py`：工具定义与 handler

1. **`_tool_definitions()` 三处改动**：

   a. `kb_init` 的 `inputSchema.properties` 加：

```python
                "description": {"type": "string", "description": "可选，一句话说明这个库装什么（如 '数电教材+课件'）。写给未来的检索路由看：模型靠它判断该不该定向搜这个库，务必具体。"},
```

   b. `kb_search` 的 `description` **整段替换**为：

```
搜索知识库并返回结构化 chunks。路由纪律：用户问题或上下文已明确指向特定库/目录时，必须传 vault_path 或 path_prefix 定向检索（精度更高、噪音更少）；仅在目标模糊或确需跨库时才省略 vault_path 做跨库 fan-out（结果带 vault 字段；solo 库被跳过并在 excluded_solo 中列出）。不确定有哪些库时先 kb_list 看各库 description 再决定。
```

   `path_prefix` 参数描述替换为：

```
可选，只保留 source 以该前缀开头的 chunk（source 是库内相对 posix 路径）。用户提到具体课程名/文件夹名/主题目录时，用它把检索限定在该子树，如 '教材/'、'数字电路/'
```

   c. 新增工具 `kb_describe`（仿 `kb_set_weight` 的 schema 位置）：

```python
        {
            "name": "kb_describe",
            "description": "设置/更新知识库的描述（一句话说明这个库装什么，供检索路由定向选库用）。与 kb_set_weight 平行的元数据工具。",
            "inputSchema": {"type": "object", "required": ["vault_path", "description"], "properties": {
                "vault_path": {"type": "string", "description": "必填，已注册知识库的绝对路径"},
                "description": {"type": "string", "description": "必填，库内容的一句话描述，要具体（差：'笔记'；好：'数字电路教材解析稿+课件'）"},
            }},
        },
```

2. **`_kb_init`**：`name_arg` 之后加 `desc = str(arguments.get("description", "")).strip()`，`registry.add(resolved, name_arg)` 改为 `registry.add(resolved, name_arg, description=desc)`（两处：try 与 except OSError 的 persist=False 分支）。返回 dict 加 `"description": entry.description`。

3. **`_list_vaults`**：items.append 的 dict 里加 `"description": entry.description,`。

4. **`call_tool`** 分发加：

```python
        if name == "kb_describe":
            return _text_content(self._kb_describe(arguments))
```

   并新增 handler（放在 `_kb_remove` 之后）：

```python
    def _kb_describe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = str(arguments.get("vault_path", "")).strip()
        desc = str(arguments.get("description", "")).strip()
        if not raw or not desc:
            raise ValueError("vault_path and description are required for kb_describe")
        path = self._resolve_vault_path(raw)
        entry = self.registry.set_description(path, desc)
        return {"path": entry.path, "name": entry.name, "description": entry.description}
```

5. **fan-out hint**：`_fanout_search` 两个 return 处（groups 与平铺，约 599/620 行），在 return 前构造：

```python
        result: dict[str, Any] = {...}  # 原 return 的 dict 改为先赋值给 result
        if len(searched) > 1:
            names = [Path(s).name for s in searched]
            result["hint"] = (
                f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
                "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
            )
        return result
```

6. **MCP instructions**（协议层，server.py 顶部加常量 + initialize 响应加字段）：

```python
SERVER_INSTRUCTIONS = (
    "本服务器提供本地 Markdown 知识库检索。路由纪律："
    "1) 用户问题或上下文已明确目标库/目录时，kb_search 必须传 vault_path 或 path_prefix 定向检索；"
    "仅在目标模糊或确需跨库时省略 vault_path。"
    "2) 不确定有哪些库时先 kb_list 查看各库 description 再选库。"
    "3) kb_read 尽量带 start_line/end_line 限定范围，避免一次拉全篇。"
)
```

   initialize 响应（约 775 行）改为：

```python
            return _json_result(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            })
```

### P1-3 测试 `tests/test_registry_server.py`（追加用例）

```python
def test_vault_description_roundtrip(tmp_path):
    """description 注册写入→load 还原→set_description 更新→老 toml（无该键）回退 ''。"""
    # 1) registry.add(path, "n", description="数电教材") → load() 取回 description == "数电教材"
    # 2) registry.set_description(path, "数电教材+课件") → load() 取回 == "数电教材+课件"
    # 3) 手工写一个不含 description 键的 vaults.toml → load() description == ""
```

`tests/test_mcp_stdio.py` 追加：initialize 响应含 `instructions` 字段且非空；`tools/list` 含 `kb_describe`；调用 `kb_describe` 后 `kb_list` 返回对应 description。

### P1-4 重写 `skills/mortis-rag-mcp/SKILL.md`（**整文件替换**为以下内容）

```markdown
---
name: mortis-rag-mcp
description: "调用 Mortis'RAG MCP 检索本地知识库。触发词：搜知识库、查笔记、kb_search、vault 检索、RAG 搜索、mortis rag。"
version: 5.0.0
---

# mortis-rag-mcp 检索路由（0.7.0）

连接即读 server 的 `instructions`（路由纪律已内嵌）。本文件只补判定表与反模式。

## 检索路由判定表（按序匹配，命中即执行）

| # | 条件（判定信号） | 动作 |
|---|---|---|
| 1 | 用户提到库名/课程名/文件夹名/主题目录（如"数电""教材"） | 先 `kb_list` 匹配 `name`/`description` → 命中则 `kb_search` 必须带 `vault_path`；指向子目录再加 `path_prefix='目录/'` |
| 2 | 问题是明确概念/术语/人名/缩写 | 直接 `kb_search(query, top_k=5)`；多库时优先按 #1 定向 |
| 3 | 问题模糊、探索性（"我最近学过什么""哪都可能有"） | 不传 `vault_path` fan-out，用结果里的 `vault` 字段判断来源，第二轮按 #1 定向收窄 |
| 4 | `kb_search` 返回带 `hint` | 读它。它在告诉你上次检索太宽，下次定向 |
| 5 | 命中后要看原文 | `kb_read(source, vault_path, start_line, end_line)`，禁一次拉全篇 |

## 反模式（禁止）

- 禁止在条件 #1 命中时省略 `vault_path`"先看看"——定向永远先于试探。
- 禁止对 solo 库（私密/试验）做无 `vault_path` 的全局搜并声称"没搜到"。
- 禁止用 `kb_rebuild` 修少量失败文件；正确姿势是反复 `kb_stats`/`kb_search` 触发增量 sync。
  `kb_rebuild` = 全量重嵌（几百~几千次 API 调用，免费档必爆限流），仅在换模型/维度时用。

## 工具一句话清单（细节以工具自带 schema 为准，此处不抄）

kb_search（检索）/ kb_read（读原文）/ kb_list（列库+description）/ kb_describe（设库描述）/
kb_init / kb_init_solo（私密库）/ kb_remove / kb_list_files / kb_stats（健康看 failed_files）/
kb_set_weight / kb_exempt（豁免私密）/ kb_rebuild（高危，见上）/ kb_export / kb_import / kb_ingest（PDF 摄取，默认关闭）

## 管理机制

混合检索 = FTS5 BM25 + 向量余弦 + bigram 词法三路 RRF + rerank。2 字中文与短英文缩写有兜底，正常搜即可。
配置链：`--app-config` > `VAULT_MCP_CONFIG` > `~/.vault_mcp/config.toml` > 内置默认。
PDF 摄取层默认关闭：`kb_ingest` 报 disabled 时，引导用户在 config/app.toml 设 `[ingest] enabled=true` 重启后用。
```

### P1-5 commit（拆 3 个）

1. `feat(registry,server): vault description 字段 + kb_describe 工具 + MCP instructions + 检索路由纪律`
2. `feat(server): fan-out 结果 hint（跨库检索提示定向）`
3. `docs(skill): SKILL.md 5.0 重写——判定表化、删 schema 速查表、补反模式`

### P1 验收

- [ ] `pytest tests/test_registry_server.py tests/test_mcp_stdio.py -q` 全绿
- [ ] 手工：注册两个库 → 不带 vault_path 搜 → 结果含 `hint`；带 vault_path 搜 → 无 hint
- [ ] 跑 P0 eval，记录 Hit@K（此轮为基线）

---

## P2：PDF/Office 摄取层（默认关闭、按需异步）

### P2-0 设计约束（用户硬性要求，不许讨价还价）

1. **默认不启用**：`[ingest] enabled` 默认 `false`。README 与 QUICKSTART_user 必须明确写"初次部署时不启用"（文案见 P2-7，逐字粘贴）。
2. **按需异步**：**不做** watcher 自动摄取。只有 `kb_ingest(action="submit")` 被显式调用时才解析，后台线程执行，立即返回 job 列表。
3. **产物收子目录**：解析产物一律写 `<vault>/.mortis-parsed/`，镜像源相对路径，原目录零污染。
4. **可分发**：MinerU 调用内嵌为 `mortis_rag_mcp/ingest/` 模块，纯标准库 `urllib`，不依赖任何外部 MCP/包。
5. **HTML 表格**：脚本处理（`ingest/tables.py`），chunker 加原子块保护（P2-6）。
6. **agent 引导一键启动**：agent 部署时由 `kb_init` 返回的 `hint` 告知 PDF 存在与开启方法（P2-5.4），用户确认后 agent 改配置 + 调 `kb_ingest`。

### P2-1 `config.py`：新增 `IngestConfig`

在 `CacheConfig` 之后加：

```python
@dataclass(slots=True)
class IngestConfig:
    enabled: bool = False                # 默认关闭：PDF 摄取层opt-in，README/QUICKSTART 必须明确
    api_key: str = ""                    # MinerU v4 token（支持 ${ENV_VAR} 插值）；空串 → Agent 免登通道
    model_version: str = "vlm"           # v4 模型：pipeline / vlm（官方推荐 vlm）
    language: str = "ch"
    is_ocr: bool = False
    enable_formula: bool = True
    enable_table: bool = True
    poll_interval: float = 3.0           # 轮询间隔（秒）
    poll_timeout: float = 600.0          # 单文件解析最长等待（秒）
    output_dirname: str = ".mortis-parsed"  # 产物子目录（vault 根下，镜像源结构）
    pymupdf_fallback: bool = True        # 云端通道全失败时本地兜底（需可选依赖 pymupdf）
    convert_small_tables: bool = True    # 小表格 HTML→markdown pipe；含跨行跨列的保留 HTML
    table_convert_max_cells: int = 60
```

`AppConfig` 加 `ingest: IngestConfig = field(default_factory=IngestConfig)`；`load_config` 里仿照其他 section 解析 `[ingest]`（`api_key` 走 `_env()` 插值，数值走 `_numeric`）。
`config/app.toml.example` 追加（注释必须保留，这是给用户看的）：

```toml
# ── PDF/Office 摄取层（默认关闭）────────────────────────────
# 开启后可用 kb_ingest 把库里的 PDF/DOCX/PPTX/XLSX 解析成 Markdown 收进
# .mortis-parsed/ 子目录并纳入索引。解析走 MinerU 云端 API：
#   - 配了 api_key → v4 精准接口（≤200MB/200页，每天 1000 页高优先级额度）
#   - 没配 api_key → Agent 免登接口（≤10MB/20页，IP 限频，零配置试用）
# 云端都失败且装了 pymupdf（pip install pymupdf）→ 本地纯文本兜底。
[ingest]
enabled = false
# api_key = "${MINERU_API_KEY}"
# model_version = "vlm"
```

### P2-2 新建 `mortis_rag_mcp/ingest/__init__.py`

```python
"""PDF/Office 摄取层：MinerU 云端解析 → Markdown 落盘 .mortis-parsed/。

设计约束：默认关闭（IngestConfig.enabled=False）；仅 kb_ingest 显式触发；
纯标准库实现；云端失败可降级 pymupdf（可选依赖）。
"""
from .worker import IngestManager, INGEST_EXTS, AGENT_EXTS

__all__ = ["IngestManager", "INGEST_EXTS", "AGENT_EXTS"]
```

### P2-3 新建 `mortis_rag_mcp/ingest/mineru.py`（完整文件）

```python
"""MinerU 云端解析客户端（纯标准库 urllib，零第三方依赖）。

双通道（官方文档 https://mineru.net/apiManage/docs 已核实）：
- v4 精准 API（需 token）：POST /api/v4/file-urls/batch 申请上传链接 →
  PUT 上传文件（不设 Content-Type）→ GET /api/v4/extract-results/batch/{batch_id}
  轮询 → state=done 后下载 full_zip_url（zip，内含 full.md）。
  限制：≤200MB、≤200页；每天 1000 页高优先级额度。
- Agent 轻量 API（免 token）：POST /api/v1/agent/parse/file 得 (task_id, file_url) →
  PUT 上传 → GET /api/v1/agent/parse/{task_id} 轮询 → done 后下载 markdown_url。
  限制：≤10MB、≤20页、IP 限频（超限 HTTP 429）；仅 PDF/图片/DOCX/PPTX/XLSX。
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

V4_BASE = "https://mineru.net/api/v4"
AGENT_BASE = "https://mineru.net/api/v1/agent"
V4_MAX_BYTES = 200 * 1024 * 1024
AGENT_MAX_BYTES = 10 * 1024 * 1024
# v4 全格式；Agent 接口不含老 Office 三件套（doc/ppt/xls）
V4_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
AGENT_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}
# 终态错误码：额度/超限类不值得退避重试，直接失败让上层降级
_V4_FATAL_CODES = {-60005, -60006, -60017, -60018, -60019}  # 大小/页数超限、重试上限、每日额度
_AGENT_FATAL_CODES = {-30001, -30002, -30003, -30004}


class MineruError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


@dataclass(slots=True)
class ParsedDocument:
    markdown: str
    images: dict[str, bytes]   # 图片相对路径（如 images/xxx.jpg）→ 字节；Agent 通道恒为空
    channel: str               # "v4" | "agent"
    model: str                 # 实际使用的模型版本


def _http_json(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 429:
            retry_after = float(exc.headers.get("Retry-After", "60"))
            raise MineruError(f"rate limited (429), retry after {retry_after}s",
                              http_status=429, retryable=True) from exc
        raise MineruError(f"HTTP {exc.code}: {body}", http_status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"network error: {exc}", retryable=True) from exc


def _http_bytes(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"download failed: {exc}", retryable=True) from exc


def _put_upload(url: str, payload: bytes, timeout: float) -> None:
    # 官方文档明确：上传无须设置 Content-Type
    req = urllib.request.Request(url, data=payload, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201):
                raise MineruError(f"upload failed: HTTP {resp.status}", http_status=resp.status)
    except urllib.error.HTTPError as exc:
        raise MineruError(f"upload failed: HTTP {exc.code}", http_status=exc.code,
                          retryable=exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"upload network error: {exc}", retryable=True) from exc


class MineruClient:
    def __init__(self, api_key: str = "", *, model_version: str = "vlm", language: str = "ch",
                 is_ocr: bool = False, enable_formula: bool = True, enable_table: bool = True,
                 timeout: float = 30.0):
        self.api_key = api_key.strip()
        self.model_version = model_version
        self.language = language
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.timeout = timeout

    # ---------------------------------------------------------------- public

    def channel_for(self, path: Path) -> str:
        """通道选择：有 token 且格式/大小合规 → v4；无 token 且 ≤10MB 且 Agent 格式 → agent；
        都不满足抛 MineruError（上层决定是否 pymupdf 兜底）。"""
        ext = path.suffix.lower()
        size = path.stat().st_size
        if self.api_key:
            if ext not in V4_EXTS:
                raise MineruError(f"v4 unsupported ext: {ext}", code=-60002)
            if size > V4_MAX_BYTES:
                raise MineruError(f"file exceeds v4 200MB limit: {size}", code=-60005)
            return "v4"
        if ext not in AGENT_EXTS:
            raise MineruError(f"agent channel unsupported ext (need v4 token): {ext}", code=-30002)
        if size > AGENT_MAX_BYTES:
            raise MineruError(f"file exceeds agent 10MB limit (need v4 token): {size}", code=-30001)
        return "agent"

    def parse(self, path: Path, *, poll_interval: float = 3.0, poll_timeout: float = 600.0) -> ParsedDocument:
        channel = self.channel_for(path)
        if channel == "v4":
            return self._parse_v4(path, poll_interval=poll_interval, poll_timeout=poll_timeout)
        return self._parse_agent(path, poll_interval=poll_interval, poll_timeout=poll_timeout)

    # ---------------------------------------------------------------- v4

    def _v4_headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def _parse_v4(self, path: Path, *, poll_interval: float, poll_timeout: float) -> ParsedDocument:
        # 1) 申请上传链接（本地文件走 file-urls/batch；单次 ≤50 个，本客户端一次一文件一批）
        body = {
            "files": [{
                "name": path.name,
                "data_id": path.stem[:120],
                "is_ocr": self.is_ocr,
            }],
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        req = urllib.request.Request(f"{V4_BASE}/file-urls/batch",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=self._v4_headers(), method="POST")
        result = _http_json(req, self.timeout)
        if result.get("code") != 0:
            raise MineruError(f"v4 apply upload url failed: {result.get('msg')}",
                              code=result.get("code"),
                              retryable=result.get("code") not in _V4_FATAL_CODES)
        batch_id = result["data"]["batch_id"]
        upload_url = result["data"]["file_urls"][0]
        # 2) PUT 上传（系统自动提交解析任务）
        _put_upload(upload_url, path.read_bytes(), max(self.timeout, 300.0))
        # 3) 轮询批量结果
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            poll_req = urllib.request.Request(f"{V4_BASE}/extract-results/batch/{batch_id}",
                                              headers=self._v4_headers())
            data = _http_json(poll_req, self.timeout)
            if data.get("code") != 0:
                raise MineruError(f"v4 poll failed: {data.get('msg')}", code=data.get("code"))
            item = (data["data"].get("extract_result") or [{}])[0]
            state = item.get("state", "")
            if state == "done":
                zip_bytes = _http_bytes(item["full_zip_url"], max(self.timeout, 300.0))
                markdown, images = self._extract_zip(zip_bytes)
                return ParsedDocument(markdown=markdown, images=images,
                                      channel="v4", model=self.model_version)
            if state == "failed":
                raise MineruError(f"v4 parse failed: {item.get('err_msg')}", retryable=False)
            time.sleep(poll_interval)  # waiting-file/pending/running/converting 继续等
        raise MineruError(f"v4 poll timeout after {poll_timeout}s (batch_id={batch_id})", retryable=True)

    @staticmethod
    def _extract_zip(zip_bytes: bytes) -> tuple[str, dict[str, bytes]]:
        """官方 zip 结构：full.md + images/ + *.json。只取 full.md 与图片。"""
        markdown = ""
        images: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith("full.md"):
                    markdown = zf.read(name).decode("utf-8")
                elif "/" in name and name.lower().rsplit(".", 1)[-1] in {
                    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
                }:
                    images[name] = zf.read(name)
        if not markdown:
            raise MineruError("full.md not found in result zip", retryable=False)
        return markdown, images

    # ---------------------------------------------------------------- agent

    def _parse_agent(self, path: Path, *, poll_interval: float, poll_timeout: float) -> ParsedDocument:
        body = {
            "file_name": path.name,
            "language": self.language,
            "enable_table": self.enable_table,
            "is_ocr": self.is_ocr,
            "enable_formula": self.enable_formula,
        }
        req = urllib.request.Request(f"{AGENT_BASE}/parse/file",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        result = _http_json(req, self.timeout)
        if result.get("code") != 0:
            raise MineruError(f"agent submit failed: {result.get('msg')}", code=result.get("code"),
                              retryable=result.get("code") not in _AGENT_FATAL_CODES)
        task_id = result["data"]["task_id"]
        _put_upload(result["data"]["file_url"], path.read_bytes(), max(self.timeout, 300.0))
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            poll_req = urllib.request.Request(f"{AGENT_BASE}/parse/{task_id}")
            data = _http_json(poll_req, self.timeout)
            if data.get("code") != 0:
                raise MineruError(f"agent poll failed: {data.get('msg')}", code=data.get("code"))
            item = data["data"]
            state = item.get("state", "")
            if state == "done":
                md_bytes = _http_bytes(item["markdown_url"], max(self.timeout, 120.0))
                return ParsedDocument(markdown=md_bytes.decode("utf-8"), images={},
                                      channel="agent", model="pipeline-light")
            if state == "failed":
                raise MineruError(f"agent parse failed: {item.get('err_msg')}",
                                  code=item.get("err_code"),
                                  retryable=item.get("err_code") not in _AGENT_FATAL_CODES)
            time.sleep(poll_interval)  # waiting-file/uploading/pending/running 继续等
        raise MineruError(f"agent poll timeout after {poll_timeout}s (task_id={task_id})", retryable=True)
```

### P2-4 新建 `mortis_rag_mcp/ingest/tables.py`（完整文件）

```python
"""HTML 表格处理：MinerU 的表格输出是 HTML（rowspan/colspan 只有 HTML 表达得了）。

两条职责：
1) iter_table_blocks：供 chunker 做原子块保护（表格不被从中间切开）。
2) convert_small_tables：可选地把规整小表格转成 markdown pipe（LLM 更易读）；
   含 rowspan/colspan 或大表格保留 HTML——硬转 pipe 会塌掉（第三方 benchmark 实测教训）。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_TABLE_OPEN = re.compile(r"<table[\s>]", re.IGNORECASE)
_TABLE_CLOSE = re.compile(r"</table\s*>", re.IGNORECASE)
_SPAN_ATTR = re.compile(r"rowspan\s*=|colspan\s*=", re.IGNORECASE)


def iter_table_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """返回所有 <table>...</table> 块的 (start, end) 行号区间（闭区间）。
    用配对计数而不是单行判断：MinerU 输出的表格可能跨行。"""
    blocks: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, line in enumerate(lines):
        opens = len(_TABLE_OPEN.findall(line))
        closes = len(_TABLE_CLOSE.findall(line))
        if opens and depth == 0:
            start = i
        depth += opens - closes
        if depth <= 0 and start >= 0:
            blocks.append((start, i))
            depth = 0
            start = -1
    if start >= 0:  # 未闭合（解析质量差时）→ 保护到文件尾，宁可不切也不错切
        blocks.append((start, len(lines) - 1))
    return blocks


class _TableParser(HTMLParser):
    """把单个 <table> 解析成二维网格（忽略跨行跨列——带 span 的在调用侧已被拦截）。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _table_to_pipe(table_html: str) -> str | None:
    parser = _TableParser()
    parser.feed(table_html)
    rows = parser.rows
    if not rows or not rows[0]:
        return None
    width = max(len(r) for r in rows)
    if any(len(r) != width for r in rows):  # 行宽不齐 → 不转（多半是隐性合并）
        return None
    out = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def convert_small_tables(md_text: str, *, max_cells: int = 60) -> str:
    """把规整小表格转成 pipe；含 span / 超 max_cells / 行宽不齐的保留 HTML 原样。"""
    lines = md_text.splitlines()
    blocks = iter_table_blocks(lines)
    if not blocks:
        return md_text
    for start, end in reversed(blocks):  # 倒序替换，行号不失效
        html = "\n".join(lines[start:end + 1])
        cells = len(re.findall(r"<t[dh][\s>]", html, re.IGNORECASE))
        if cells > max_cells or _SPAN_ATTR.search(html):
            continue
        pipe = _table_to_pipe(html)
        if pipe is not None:
            lines[start:end + 1] = pipe.splitlines()
    return "\n".join(lines)


def split_large_table(lines: list[str], max_lines: int) -> list[list[str]]:
    """超大表格按 </tr> 边界切片，每片补 <table></table> 包裹，保证每个分片都是合法片段。
    供 chunker 处理 > 2×chunk_size 的巨型表格（教材里的大参数表）。"""
    parts: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if len(current) >= max_lines and "</tr>" in line.lower():
            parts.append(current)
            current = []
    if current:
        parts.append(current)
    if len(parts) <= 1:
        return parts
    wrapped = []
    for part in parts:
        body = [l for l in part if not _TABLE_OPEN.search(l) and not _TABLE_CLOSE.search(l)]
        wrapped.append(["<table>", *body, "</table>"])
    return wrapped
```

### P2-5 新建 `mortis_rag_mcp/ingest/worker.py`（完整文件）

```python
"""摄取任务管理器：按需异步解析 vault 里的 PDF/Office 文档。

硬性设计：
- 不做 watcher 自动摄取；只有 submit() 被显式调用才启动后台线程。
- 产物写 <vault>/<output_dirname>/（默认 .mortis-parsed/），镜像源相对路径。
- 状态文件 <output_dirname>/.ingest_state.json（原子写：tmp+replace，与 registry 同款）。
- 幂等：state 里记录源文件 sha256；未变 → skip；变了 → 重解析覆盖。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from ..config import IngestConfig
from .mineru import AGENT_EXTS, MineruClient, MineruError
from .tables import convert_small_tables

INGEST_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
_STATE_NAME = ".ingest_state.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class IngestManager:
    def __init__(self, vault_path: str | Path, config: IngestConfig):
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.config = config
        self.out_root = self.vault_path / config.output_dirname
        self.state_path = self.out_root / _STATE_NAME
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._client: MineruClient | None = None

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "jobs": {}}

    def _save_state(self, state: dict) -> None:
        _atomic_write_json(self.state_path, state)

    # ------------------------------------------------------------ public

    def scan_pending(self) -> list[dict]:
        """列出待解析文档：未解析过的，或源文件 sha256 已变化的。"""
        state = self._load_state()
        done = {j["source"]: j for j in state["jobs"].values() if j["state"] == "done"}
        pending: list[dict] = []
        for path in sorted(self.vault_path.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in INGEST_EXTS:
                continue
            rel = path.relative_to(self.vault_path).as_posix()
            if rel.startswith(self.config.output_dirname + "/"):  # 产物目录不摄取
                continue
            digest = _sha256(path)
            old = done.get(rel)
            if old is None or old.get("sha256") != digest:
                pending.append({"source": rel, "sha256": digest,
                                "reason": "new" if old is None else "changed"})
        return pending

    def submit(self, sources: list[str] | None = None) -> dict:
        """提交摄取任务（异步）。sources 为库内相对路径列表；None → scan_pending() 全量。"""
        if not self.config.enabled:
            raise ValueError(
                "ingest disabled: PDF 摄取层默认关闭。请在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启 MCP 服务后重试。"
            )
        if sources is None:
            targets = self.scan_pending()
        else:
            targets = []
            for rel in sources:
                path = (self.vault_path / rel).resolve()
                if not path.is_file() or path.suffix.lower() not in INGEST_EXTS:
                    raise ValueError(f"not an ingestible document: {rel}")
                targets.append({"source": Path(rel).as_posix(), "sha256": _sha256(path),
                                "reason": "explicit"})
        state = self._load_state()
        jobs = []
        for t in targets:
            job_id = uuid.uuid4().hex[:12]
            state["jobs"][job_id] = {
                "job_id": job_id, "source": t["source"], "sha256": t["sha256"],
                "state": "queued", "channel": None, "error": "",
                "submitted_at": time.time(), "finished_at": None, "output": None,
                "parse_quality": None,
            }
            jobs.append(state["jobs"][job_id])
        self._save_state(state)
        if jobs and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                            name="ingest-worker")
            self._worker.start()
        return {"submitted": len(jobs), "jobs": jobs}

    def status(self, job_id: str | None = None) -> dict:
        state = self._load_state()
        if job_id:
            job = state["jobs"].get(job_id)
            if job is None:
                raise ValueError(f"unknown job_id: {job_id}")
            return {"job": job}
        jobs = sorted(state["jobs"].values(), key=lambda j: -j["submitted_at"])
        summary: dict[str, int] = {}
        for j in jobs:
            summary[j["state"]] = summary.get(j["state"], 0) + 1
        return {"summary": summary, "jobs": jobs[:20]}

    # ------------------------------------------------------------ worker

    def _client_or_make(self) -> MineruClient:
        if self._client is None:
            c = self.config
            self._client = MineruClient(
                c.api_key, model_version=c.model_version, language=c.language,
                is_ocr=c.is_ocr, enable_formula=c.enable_formula,
                enable_table=c.enable_table,
            )
        return self._client

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                state = self._load_state()
                job = next((j for j in state["jobs"].values() if j["state"] == "queued"), None)
                if job is None:
                    return
                job["state"] = "parsing"
                self._save_state(state)
            try:
                self._run_job(job)
                job["state"] = "done"
                job["finished_at"] = time.time()
            except Exception as exc:  # 单 job 失败不拖垮队列
                job["state"] = "failed"
                job["error"] = str(exc)[:500]
                job["finished_at"] = time.time()
            finally:
                with self._lock:
                    state = self._load_state()
                    state["jobs"][job["job_id"]] = job
                    self._save_state(state)

    def _run_job(self, job: dict) -> None:
        src = self.vault_path / job["source"]
        rel = Path(job["source"])
        out_md = self.out_root / rel.parent / (rel.stem + ".md")
        out_md.parent.mkdir(parents=True, exist_ok=True)
        try:
            parsed = self._client_or_make().parse(
                src, poll_interval=self.config.poll_interval,
                poll_timeout=self.config.poll_timeout,
            )
            job["channel"] = parsed.channel
            job["parse_quality"] = "full" if parsed.channel == "v4" else "light"
            markdown = parsed.markdown
            # v4 zip 图片落盘 assets/ 并把 md 里的 images/ 前缀改写过去
            if parsed.images:
                assets = self.out_root / rel.parent / (rel.stem + ".assets")
                assets.mkdir(parents=True, exist_ok=True)
                for name, blob in parsed.images.items():
                    (assets / Path(name).name).write_bytes(blob)
                markdown = markdown.replace("images/", f"{rel.stem}.assets/")
        except MineruError as exc:
            if not self.config.pymupdf_fallback:
                raise
            markdown = self._pymupdf_fallback(src)
            job["channel"] = "pymupdf"
            job["parse_quality"] = "fallback"
            job["error"] = f"cloud failed, local fallback used: {exc}"[:300]
        if self.config.convert_small_tables:
            markdown = convert_small_tables(
                markdown, max_cells=self.config.table_convert_max_cells)
        header = (
            "---\n"
            f"source_pdf: {json.dumps(job['source'], ensure_ascii=False)}\n"
            f"source_sha256: {job['sha256']}\n"
            f"parsed_by: {job['channel']}\n"
            f"parsed_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"parse_quality: {job['parse_quality']}\n"
            "---\n\n"
        )
        tmp = out_md.with_suffix(".md.tmp")
        tmp.write_text(header + markdown, encoding="utf-8")
        tmp.replace(out_md)
        job["output"] = out_md.relative_to(self.vault_path).as_posix()

    @staticmethod
    def _pymupdf_fallback(path: Path) -> str:
        """云端全挂时的本地纯文本兜底（可选依赖；版面/表格/公式无保障）。"""
        try:
            import pymupdf  # type: ignore
        except ImportError as exc:
            raise MineruError("cloud channels failed and pymupdf not installed") from exc
        doc = pymupdf.open(str(path))
        return "\n\n".join(page.get_text() for page in doc)
```

### P2-6 `server.py` 集成

1. `_tool_definitions()` 加 `kb_ingest`：

```python
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
```

2. `call_tool` 分发 + handler：

```python
        if name == "kb_ingest":
            return _text_content(self._kb_ingest(arguments))
```

```python
    def _ingest_manager_for(self, vault_path: str) -> IngestManager:
        key = str(Path(vault_path).resolve())
        manager = self._ingest_managers.get(key)
        if manager is None:
            manager = IngestManager(vault_path, self.config.ingest)
            self._ingest_managers[key] = manager
        return manager

    def _kb_ingest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "")).strip()
        if action not in {"submit", "status", "pending"}:
            raise ValueError("action must be one of: submit, status, pending")
        vault = self._resolve_vault_path(str(arguments.get("vault_path", "")).strip()
                                         or self._default_vault_path())
        manager = self._ingest_manager_for(vault)
        if action == "pending":
            return {"pending": manager.scan_pending()}
        if action == "status":
            return manager.status(str(arguments.get("job_id", "")).strip() or None)
        result = manager.submit(arguments.get("sources") or None)
        result["hint"] = ("解析在后台进行，用 kb_ingest(action='status') 查进度；"
                          "done 的文档已写入 .mortis-parsed/ 并可被 kb_search 检索。")
        # 触发一次增量同步，把已落盘产物立即纳入索引（watcher 也会捕获，双保险）
        indexer = self._indexers.get(str(Path(vault).resolve()))
        if indexer is not None:
            threading.Thread(target=indexer.sync, daemon=True, name="ingest-sync").start()
        return result
```

   `VaultMcpServer.__init__` 加 `self._ingest_managers: dict[str, IngestManager] = {}`；文件顶部 `from .ingest import IngestManager, INGEST_EXTS`。

3. **`_kb_init` 加 PDF 检测 hint**（`md_files` 计数之后）：

```python
        doc_files = sum(
            1 for p in Path(entry.path).rglob("*")
            if p.is_file() and p.suffix.lower() in INGEST_EXTS
        )
        result = {..., "md_files": md_files, "ingestible_docs": doc_files}
        if doc_files and not self.config.ingest.enabled:
            result["hint"] = (
                f"检测到 {doc_files} 个 PDF/Office 文档。PDF 摄取层默认未启用；"
                "若用户需要检索这些文档，请先向用户确认，然后在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启服务，再调用 kb_ingest(action='submit')。"
                "未确认前不要自作主张开启。"
            )
        elif doc_files:
            result["hint"] = (
                f"检测到 {doc_files} 个 PDF/Office 文档，可调用 "
                "kb_ingest(action='pending') 查看待解析列表。"
            )
        return result
```

   （`_kb_init_solo` 的新注册分支做同样处理。）

4. **`kb_stats` 加摄取段（可选，P2 最后有时间再做）**：stats dict 加 `"ingest": {"enabled": bool, **manager.status()["summary"]}`。

### P2-7 `indexer.py`：chunker 表格原子块保护

在 `_make_chunks`（约 1477 行）现有代码块围栏跟踪旁加 HTML 表格跟踪。改动点：逐行扫描处引入 `table_depth` 计数器，规则与代码块围栏完全一致——**表格块内部不触发标题切分、不作为切分边界**：

```python
from .ingest.tables import iter_table_blocks  # 文件顶部

# _make_chunks 内，在按行处理前先算表格块区间：
table_blocks = iter_table_blocks(lines)          # [(start, end)] 闭区间
# 逐行循环里，当前行号落在任一区间内部（非首行）时：
#   - 不把该行当 heading 候选
#   - 不在该行前断开 chunk
# 实现建议：把 table_blocks 转成 set 或用指针推进（区间有序，O(1) 摊销）。
```

补充规则：单个表格块 > `2 × chunk_size` 时，用 `ingest.tables.split_large_table` 按 `</tr>` 边界切片，每片独立成 chunk（保留 `<table>` 包裹）。

**缓存代际**：表格保护改变切块结果 → `_cache_meta()`（约 681 行）的 meta dict 加 `"table_guard": True`，让旧缓存自动失效重建（文本层秒级重建，向量按 chunk.id/content_hash 命中，参照 0.5.0 的零重嵌先例验证）。

### P2-8 文档文案（逐字粘贴）

**README.zh-CN.md** 在"调用方法"前插入一节：

```markdown
## PDF / Office 文档摄取（默认关闭）

知识库索引只覆盖 Markdown。**初次部署时 PDF 摄取层是不启用的**——你的 PDF、
课件、表格文档不会自动进入检索，也不会产生任何云端 API 调用。

需要检索这些文档时，两步开启：

1. 编辑 `config/app.toml`，把 `[ingest]` 下 `enabled` 改为 `true`，重启 MCP 服务。
   解析走 MinerU 云端：配了 `api_key` 用精准接口（≤200MB/200页，每天 1000 页
   高优先级额度）；没配也能用免登轻量接口（≤10MB/20页）。云端都失败且本地装了
   `pymupdf` 时自动降级为纯文本兜底。
2. 让 AI 调用 `kb_ingest(action='submit')`（或指定单个文件）。
   解析产物统一收进库内 `.mortis-parsed/` 子目录（原文件夹不会多出任何文件），
   完成后立即可被 `kb_search` 检索。想只搜解析稿：`path_prefix='.mortis-parsed/'`。

如果你是通过 AI agent 部署的：agent 在 `kb_init` 时会收到文档数量提示并询问你
是否开启，你确认后它会替你完成上面两步。
```

**QUICKSTART_user.md** 第 2 节"配置"追加第 6 条：

```markdown
6. （可选）PDF/Office 摄取：**默认关闭，初次部署不用管**。需要检索 PDF 时再
   把 `[ingest] enabled` 改 `true` 并重启，然后让 AI 跑 `kb_ingest`。
   解析产物在 `.mortis-parsed/`，不会弄乱你的原目录。
```

### P2-9 测试（全部 mock，不碰真实网络）

| 文件 | 用例 |
|---|---|
| `tests/test_ingest_mineru.py` | 通道选择（有/无 token、超 10MB、老格式 doc 走 agent 报错）；429 → MineruError(retryable=True)；quota 错误码 -60018 → 不重试；`_extract_zip` 取 full.md+图片；轮询 done/failed/timeout 三路径（monkeypatch `_http_json`/`_put_upload`/`_http_bytes`） |
| `tests/test_ingest_tables.py` | 跨行表格块识别；未闭合表格保护到文件尾；小表转 pipe；含 colspan 不转；行宽不齐不转；`split_large_table` 每片合法 |
| `tests/test_ingest_worker.py` | disabled 报错文案含 `enabled = true`；submit→worker→done 全流程（mock MineruClient.parse）；幂等（同 sha256 不再 pending）；源文件变更后重新 pending；单 job 失败不影响后续 job；产物路径在 `.mortis-parsed/` 且 frontmatter 五字段齐全 |
| `tests/test_ingest_server.py` | `tools/list` 含 `kb_ingest`；`kb_init` 对含 PDF 的目录返回 `ingestible_docs` 与 hint（disabled 时文案含"先向用户确认"） |

### P2-10 commit（拆 4 个）

1. `feat(config,ingest): IngestConfig（默认关闭）+ MinerU 双通道客户端（v4/Agent 免登）`
2. `feat(ingest): 任务管理器（按需异步、.mortis-parsed 落盘、sha256 幂等、pymupdf 兜底）+ HTML 表格处理`
3. `feat(server,indexer): kb_ingest 工具 + kb_init 文档检测 hint + chunker 表格原子块保护`
4. `docs: README/QUICKSTART_user 增补 PDF 摄取章节（默认关闭 + agent 引导开启）`

### P2 验收

- [ ] `pytest tests/ -q` 全绿（含原 184 用例无回归）
- [ ] 手工端到端：tmp 库放一个小 PDF → enabled=true → submit → status 轮询到 done → `.mortis-parsed/` 有 md → `kb_search` 能命中 → 原目录无新增文件
- [ ] enabled=false 时 `kb_ingest` 报错文案包含开启指引
- [ ] 无 token 时 ≤10MB 小 PDF 走 Agent 通道成功（真实网络抽查一次即可，不进 CI）

---

## P3：可选增强（先做 P0 基线，用数据决定）

> 这两项**不是必做**。流程：P2 完成后跑 eval 记录 Hit@K → 逐项实现 → 再跑 eval → 无提升就 revert。

1. **title/alias boost**（`indexer.py._hybrid_rank` 尾部）：查询串（小写、去空白）完整出现在 `chunk.title` → `score *= 1.15`；frontmatter `aliases:` 列表解析进 chunk.metadata，查询与某 alias 完全相等 → 该 chunk 直接排第 1。改 `_cache_meta` 加 `"alias_boost": True`。
2. **per-source 限流**（`search()` 过滤阶段）：融合排序后、rerank 前，同一 `source` 最多保留 `per_source_cap` 个候选（默认 3，配置 `[index] per_source_cap`，0=不限）。注意与 fan-out 的 per-vault 语义区分。

---

## 6. `Changelog_developer.md` 条目模板

每个 commit 落地后，**追加**（不覆盖）一条，格式遵守该文件开头约定：

```
### C0 — <GitHub用户名>,<日期>,<agent名>,<模型名> — test(eval): 检索评测 harness
- 新增 scripts/eval_search.py（Hit@K 回归）、tests/eval/golden_queries.json 骨架
- 动机：P1-P3 都会动检索行为，先立度量尺
- 验证：占位查询跑通；基线 Hit@5 = __%（填实测值）

### C1 — ... — feat(registry,server): vault description + kb_describe + instructions + 路由纪律
- registry.py：VaultEntry.description / set_description（load/save 兼容老 toml）
- server.py：kb_init 收 description；_list_vaults 返回 description；新增 kb_describe；
  kb_search/path_prefix 描述加入路由纪律；initialize 响应加 instructions
- 测试：test_registry_server.py（roundtrip/老 toml 回退）、test_mcp_stdio.py（instructions/schema）
- 验证：pytest 相关文件全绿；eval Hit@5 = __%（对比 C0 基线）

### C2 — ... — feat(server): fan-out hint
### C3 — ... — docs(skill): SKILL.md 5.0 重写
### C4 — ... — feat(config,ingest): IngestConfig + MinerU 双通道客户端
### C5 — ... — feat(ingest): 任务管理器 + 表格处理
### C6 — ... — feat(server,indexer): kb_ingest + hint + 表格保护
### C7 — ... — docs: README/QUICKSTART_user 增补摄取章节
```

---

## 7. 总验收 checklist（全部打勾才可发 0.7.0 release）

- [ ] `pytest tests/ -q` 全绿；eval Hit@K 不低于 P0 基线
- [ ] 新工具 schema 总字符增量 ≤ 2.5k（`python -c "import json;from mortis_rag_mcp.server import _tool_definitions;print(len(json.dumps(_tool_definitions(),ensure_ascii=False)))"`，当前基线 6093）
- [ ] 默认配置下（ingest 关闭）无任何网络行为变化、无新文件产生
- [ ] `CHANGELOG_user.md` 按 release 惯例补 0.7.0 条目（只讲功能与体验，不讲技术细节）
- [ ] `docs/Changelog_developer.md` 每个 commit 条目齐全
