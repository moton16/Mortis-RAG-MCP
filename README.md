# Mortis'RAG MCP

A standard-library-only MCP server for Obsidian-style Markdown knowledge bases (formerly `vault-mcp` / Obsidian RAG MCP). It provides structured chunk search, raw source reads, incremental indexing, and a user-level vault registry that binds to **no hardcoded folder** — register any directory with a single `kb_init` call.

> **New here?** Follow [docs/QUICKSTART.md](docs/QUICKSTART.md): clone → install → configure your API key → wire up your MCP client → `kb_init` your notes folder → optionally install the companion skill from [`skills/vault-mcp/`](skills/vault-mcp/SKILL.md).

> **Release notes:** see [CHANGELOG.md](CHANGELOG.md) for per-version changes (0.5.0 = embedding resilience, search filters/dedup, per-vault weights, native watcher, index snapshots).

## Implemented

- `kb_init` / `kb_unregister`: register / unregister any folder as a knowledge base (persistent registry, per-vault file watcher, optional cache purge).
- `kb_search`: return raw chunks with `id`, `content`, `score`, `source`, `title`, and metadata. Without `vault_path`, searches across **all registered vaults** (fan-out, query embedded once, merged + reranked in one pass) and tags each result with `vault` / `vault_name`.
  - **Filters & pagination (0.5.0)**: `path_prefix` (per-directory), `tags` (frontmatter), `mtime_after` / `mtime_before` (epoch seconds or ISO 8601), `offset` / `limit` paging, and `group_by_vault` for grouped fan-out results. Filtering runs before rerank so quota is never spent on filtered-out chunks.
  - **Dedup (0.5.0)**: `dedupe = true` (default) keeps only the first chunk of byte-identical content — duplicate backups no longer fill up top_k, and identical paragraphs are embedded once and share vectors.
- `kb_read`: read raw text by `source`, `heading`, or 1-based line range; it never calls an LLM.
- `kb_list` / `kb_stats` / `kb_vaults` / `kb_rebuild`: inspect and manage registered vaults.
- `kb_set_weight` (0.5.0): per-vault retrieval weight (0 < w <= 100); fan-out scores are scaled by the vault weight before the global sort.
- `kb_export` / `kb_import` (0.5.0): pack the chunks + vector + FTS cache layers into a zip snapshot and restore them on another machine — the next sync after an import performs **zero embedding API calls**. Members are whitelist-validated and model/dimension mismatches are refused unless `force = true`.
- `kb_exempt`: exclude private/draft notes from retrieval (`.vaultignore` patterns or per-file frontmatter `rag: false`).
- `embedding.mode = "static"`: deterministic local hash embedding; no network or LLM call.
- `embedding.mode = "external"`: batch HTTP embedding requests with retry + exponential backoff (`max_retries`, `retry_backoff`; honors `Retry-After` on 429) and automatic batch splitting by `batch_size` (0.5.0) — a rate-limited file no longer fails the whole indexing pass.
- **failed_files persistence (0.5.0)**: the failure map survives restarts (`kb_stats` keeps reporting the last round's failures); cleared automatically once a file embeds successfully.
- Optional HTTP reranker with automatic fallback to base retrieval on failure.
- Incremental add/modify/delete/rename handling with a per-vault watcher: **native Windows directory events** (0.5.0, `ReadDirectoryChangesW` via ctypes, `[index] watch_method = "auto"`) with debounced syncs, a 30s reconciliation sweep, and automatic fallback to the legacy 0.25s polling loop (`"poll"` keeps the old behavior).
- **Disk cache**: chunk signatures + float32 embeddings are persisted (default `~/.vault_mcp_cache`), so restarts skip the full embedding pass (`kb_stats` reports `cache_enabled`). Unchanged syncs no longer rewrite the cache files.
- **Concurrent embedding**: changed files are embedded in parallel (`cache.embedding_max_workers`).
- **Cache placement**: `cache.placement = "vault"` stores each vault's vectors inside that vault's `.mcp_cache/` subfolder (configurable via `cache.subdir`) instead of the shared home cache. Recommended when distributing vault folders.
- **Hybrid search (0.4.0)**: `kb_search` fuses three routes with RRF — FTS5 BM25 (trigram tokenizer, native SQLite), vector cosine, and a bigram lexical layer (covers <3-char CJK queries that trigram can't match). Toggle with `[index] use_hybrid` (default true); tune per-route width / rerank cap with `rrf_per_route` / `rerank_cap` (0.5.0).
- **Disk-backed vector backend (0.4.0)**: `[vector] backend = "sqlite_vec"` (optional extra `pip install mortis-rag-mcp[vec]`) stores embeddings in a per-vault sqlite-vec table and keeps them off RAM — measured ~55MB freed at 13k x 1024-dim chunks, one-time migration from the existing cache without re-embedding; auto-falls back to the in-memory backend when sqlite-vec is unavailable.
- **Image caption injection (0.5.0, opt-in)**: `[index] inject_image_captions = true` makes image alt text and Obsidian `![[path|caption]]` captions searchable by injecting a `[图片: …]` line after each image. Off by default: enabling it changes chunk ids and therefore re-embeds the corpus.
- Config resolution chain: `--app-config` > `VAULT_MCP_CONFIG` env var > `~/.vault_mcp/config.toml` > built-in defaults. A legacy `[vault].path` in the config is auto-imported into the registry on first run. API keys fall back to the `VAULT_MCP_API_KEY` environment variable.
- Ignores `.obsidian`, the cache subfolder, non-Markdown files, and common temporary Markdown files. `source` is always vault-relative with `/` separators.

## Installation

Requires Python 3.10+:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If pip build isolation fails with `Cannot import 'setuptools.build_meta'` (some bundled runtimes like Codex/Trae ship a stripped pip/setuptools), this is an environment problem, not a package problem — the build backend needs setuptools. Fix the build environment first, then retry:

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
```

Only if that still fails (e.g. offline / broken isolation) fall back to:

```powershell
python -m pip install -e . --no-build-isolation
```

Note: "zero dependencies" refers to **runtime** dependencies (`dependencies = []`); building from source requires setuptools as the PEP 517 build backend, which pip provides automatically in a healthy environment.

## Configuration

Copy `config/app.toml.example` to `config/app.toml` (or `~/.vault_mcp/config.toml`) and fill in your provider values. There is **no `[vault] path`** — knowledge bases are managed by the registry:

1. Start the server and call `kb_init` with the absolute path of your notes folder:

```json
{"name": "kb_init", "arguments": {"path": "D:\\Notes\\MyVault", "name": "MyVault"}}
```

2. Set your embedding API key via the environment (`VAULT_MCP_API_KEY`) or `${ENV_VAR}` interpolation in the config. Keys are sent as `Authorization: Bearer ...`, never in the JSON body.

Example config:

```toml
[embedding]
mode = "external"
endpoint = "https://api.siliconflow.cn/v1/embeddings"
model = "BAAI/bge-m3"
dimension = 1024
send_dimensions = false
api_key = "${VAULT_MCP_API_KEY}"
timeout = 60
max_retries = 3        # 0.5.0: extra attempts after the first failure (exponential backoff)
batch_size = 32        # 0.5.0: max texts per HTTP request (<=0 = legacy single request)
retry_backoff = 1.0    # 0.5.0: base seconds for the backoff; Retry-After wins on 429

[reranker]
enabled = true
endpoint = "https://api.siliconflow.cn/v1/rerank"
model = "BAAI/bge-reranker-v2-m3"
api_key = "${VAULT_MCP_API_KEY}"
timeout = 60

[index]
chunk_size = 1200
chunk_overlap = 150
debounce_seconds = 0.5
rrf_per_route = 40             # 0.5.0: RRF candidates per route (was hardcoded)
rerank_cap = 60                # 0.5.0: max chunks per rerank API call (was hardcoded)
watch_method = "auto"          # 0.5.0: auto | native | poll (0.4.x behavior)
watch_fallback_interval = 30.0 # 0.5.0: seconds between reconciliation sweeps (0 = off)
inject_image_captions = false  # 0.5.0: opt-in; ON = image alt/captions searchable BUT full re-embed

[cache]
enabled = true
embedding_max_workers = 2
placement = "home"
subdir = ".mcp_cache"
```

External embedding request body:

```json
{"model":"embedding-model-name","input":["text 1","text 2"]}
```

External reranker request body:

```json
{"model":"reranker-model-name","query":"user query","documents":["candidate 1","candidate 2"]}
```

## Start

```powershell
mortis-rag-mcp --serve-mcp-stdio --app-config .\config\app.toml
```

Without installing the console script:

```powershell
python -m vault_mcp --serve-mcp-stdio --app-config .\config\app.toml
```

stdout contains only JSON-RPC responses. The server supports `initialize`, `tools/list`, `tools/call`, and `ping` over newline-delimited JSON input.

## MCP client configuration

```toml
[mcp_servers.mortis_rag_mcp]
command = "C:\\path\\to\\Mortis-RAG-MCP\\.venv\\Scripts\\mortis-rag-mcp.exe"
args = ["--serve-mcp-stdio", "--app-config", "C:\\path\\to\\Mortis-RAG-MCP\\config\\app.toml"]
enabled = true
```

If the console script is on the client process `PATH`, `command = "mortis-rag-mcp"` is sufficient.

## Tests

```powershell
python -m pytest -q
```

The tests ((`python -m pytest -q` all green)) cover registry round-trips and dedup, fan-out search across vaults, unregister + cache purge, missing-vault tolerance, legacy config auto-migration, exempt management, provider JSON handling, static no-network behavior, reranker fallback, add/modify/delete/rename lifecycle, Windows and Unicode paths, and stdio smoke behavior. Each stdio test uses its own registry via the `VAULT_MCP_REGISTRY` environment variable. 0.5.0 adds coverage for embedding retry/backoff/batching, failed-file persistence, search filters and pagination, content-hash dedup, per-vault weights, native watcher integration (including a no-op-sync convergence test), image caption injection, and snapshot export/import round-trips (zero re-embed acceptance).

## Known limitations

- Base retrieval uses local token matching. Embeddings are generated and used for external semantic ranking, but there is no vector database or ANN index.
- Static embedding is a deterministic placeholder, not a semantic model. Configure an external provider for semantic recall.
- Provider response parsing expects `data[].embedding` for embeddings and `results[]` for reranking; vendor-specific adapters may be needed.
- The watcher defaults to native `ReadDirectoryChangesW` events on Windows (`[index] watch_method = \"auto\"`); non-Windows, `\"poll\"`, or a native-start failure fall back to the legacy 0.25s polling loop, with a 30s full-reconciliation safety net while the native watcher is active. Very large / high-churn vaults are not yet benchmarked.
- After a multi-vault fan-out search, `source` values are vault-relative — pass the matching `vault_path` to `kb_read`.
- The registry assumes a single server process per machine (atomic writes; no cross-process locking).
