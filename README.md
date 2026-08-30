# Mortis'RAG MCP

A standard-library-only MCP server for Obsidian-style Markdown knowledge bases (formerly `vault-mcp` / Obsidian RAG MCP). It provides structured chunk search, raw source reads, incremental indexing, and a user-level vault registry that binds to **no hardcoded folder** — register any directory with a single `kb_init` call.

> **New here?** Follow [docs/QUICKSTART.md](docs/QUICKSTART.md): clone → install → configure your API key → wire up your MCP client → `kb_init` your notes folder → optionally install the companion skill from [`skills/vault-mcp/`](skills/vault-mcp/SKILL.md).

> **Release notes:** see [CHANGELOG.md](CHANGELOG.md) for per-version changes (0.4.0 = hybrid search + disk-backed vector backend).

## Implemented

- `kb_init` / `kb_unregister`: register / unregister any folder as a knowledge base (persistent registry, per-vault file watcher, optional cache purge).
- `kb_search`: return raw chunks with `id`, `content`, `score`, `source`, `title`, and metadata. Without `vault_path`, searches across **all registered vaults** (fan-out, query embedded once, merged + reranked in one pass) and tags each result with `vault` / `vault_name`.
- `kb_read`: read raw text by `source`, `heading`, or 1-based line range; it never calls an LLM.
- `kb_list` / `kb_stats` / `kb_vaults` / `kb_rebuild`: inspect and manage registered vaults.
- `kb_exempt`: exclude private/draft notes from retrieval (`.vaultignore` patterns or per-file frontmatter `rag: false`).
- `embedding.mode = "static"`: deterministic local hash embedding; no network or LLM call.
- `embedding.mode = "external"`: batch HTTP embedding requests.
- Optional HTTP reranker with automatic fallback to base retrieval on failure.
- Incremental add/modify/delete/rename handling with debounce polling watcher (one watcher per registered vault).
- **Disk cache**: chunk signatures + float32 embeddings are persisted (default `~/.vault_mcp_cache`), so restarts skip the full embedding pass (`kb_stats` reports `cache_enabled`).
- **Concurrent embedding**: changed files are embedded in parallel (`cache.embedding_max_workers`).
- **Cache placement**: `cache.placement = "vault"` stores each vault's vectors inside that vault's `.mcp_cache/` subfolder (configurable via `cache.subdir`) instead of the shared home cache. Recommended when distributing vault folders.
- **Hybrid search (0.4.0)**: `kb_search` fuses three routes with RRF — FTS5 BM25 (trigram tokenizer, native SQLite), vector cosine, and a bigram lexical layer (covers <3-char CJK queries that trigram can't match). Toggle with `[index] use_hybrid` (default true).
- **Disk-backed vector backend (0.4.0)**: `[vector] backend = "sqlite_vec"` (optional extra `pip install mortis-rag-mcp[vec]`) stores embeddings in a per-vault sqlite-vec table and keeps them off RAM — measured ~55MB freed at 13k x 1024-dim chunks, one-time migration from the existing cache without re-embedding; auto-falls back to the in-memory backend when sqlite-vec is unavailable.
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

If pip build isolation fails with `Cannot import 'setuptools.build_meta'` (some bundled runtimes), use:

```powershell
python -m pip install -e . --no-build-isolation
```

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

The tests (45 cases) cover registry round-trips and dedup, fan-out search across vaults, unregister + cache purge, missing-vault tolerance, legacy config auto-migration, exempt management, provider JSON handling, static no-network behavior, reranker fallback, add/modify/delete/rename lifecycle, Windows and Unicode paths, and stdio smoke behavior. Each stdio test uses its own registry via the `VAULT_MCP_REGISTRY` environment variable.

## Known limitations

- Base retrieval uses local token matching. Embeddings are generated and used for external semantic ranking, but there is no vector database or ANN index.
- Static embedding is a deterministic placeholder, not a semantic model. Configure an external provider for semantic recall.
- Provider response parsing expects `data[].embedding` for embeddings and `results[]` for reranking; vendor-specific adapters may be needed.
- The watcher uses standard-library polling rather than native OS events; suitable for personal vaults, not optimized for very large/high-churn ones.
- After a multi-vault fan-out search, `source` values are vault-relative — pass the matching `vault_path` to `kb_read`.
- The registry assumes a single server process per machine (atomic writes; no cross-process locking).
