# Mortis'RAG MCP

[![CI](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version: 0.8.1](https://img.shields.io/badge/Version-0.8.1-blue.svg)](CHANGELOG_user.md)

English | [简体中文](README.md)

> A standard-library-only MCP server for Obsidian and Markdown knowledge bases.
> Source files stay local. Semantic retrieval and document parsing send configured content to remote services when those services are enabled.

---

## Current 0.9.0 candidate (local preparation, not released)

Text templates cover indexing and queries. New vaults use `estimated_tokens`; existing settings/cache preserve `legacy_chars`. `.markdown` is indexed. Configured remote features do not require a new approval flow.

Local optional parsers cover PDF/DOCX/PPTX/XLSX. Parsed documents default to virtual storage; `storage="legacy"` explicitly retains physical mirrors. `kb_import` reports `index_state` (`empty/rebuilding/unverified/ready`): imported does not mean ready. `kb_ingest(action="retry", job_id=..., vault_path=...)` retries failed/cancelled jobs, not unknown submissions.

Use `--list-requests --vault "ABSOLUTE_VAULT_PATH"` to inspect requests, or `--abandon-request REQUEST_ID --vault "ABSOLUTE_VAULT_PATH"` to abandon a local intention without resubmitting or cancelling a remote task. `--doctor` may probe real endpoints and is not an automatic Agent preflight.

The 16-tool interface includes `kb_read_media`. Native media provider/index/read fixtures are connected, but the real embeddings media transport, complete audio adapter/decoder assembly, standalone image ingestion, and host playback remain unfinished or unverified. Proxy retrieval does not satisfy the native target. Stop old writers, back up cache/control state, and use a temporary vault before upgrading; old versions cannot read virtual-only facts. Cache reuse requires compatible profiles, not a blanket no-cost guarantee.

## 🌟 Key Features

- ⚡ **Compact Search & Section Read (0.8.1)**: Adds lightweight `compact=true` structured projection and whole-chunk budgeting cursors; `kb_read` resolves heading sections directly with accurate out-of-bounds line hints; searches use ready indexes while refresh runs in the background, though concurrent updates may still cause brief waits.
- 📂 **Zero Hardcoded Paths**: Attach any local folder as a knowledge base using `kb_init`. Persistent user-level registry without modifying configs or locking to fixed directories.
- 📖 **In-place Chunk Expansion & Wikilink Read (0.8.0)**: Read context directly via `kb_read(chunk_id=...)` without calculating line ranges; navigate `[[wikilinks]]` by short stem names automatically.
- 🏷️ **Aliases Retrieval & Exact Terms Hard Inclusion (0.8.0)**: Native frontmatter `aliases` search; guaranteed recall for proper nouns via `exact_terms` with multi-route fallback.
- 🛡️ **Search Output Budget & Local Diagnostic Log (0.8.0)**: Hard byte budget limit via `budget_bytes` prevents context overflow; privacy-safe local jsonl diagnostic logging.
- 📚 **Vault Alias & Multi-Vault Scoped Search (0.7.2)**: Query vaults by their registered friendly names (e.g. `vault_path="MyNotes"`) without writing long absolute paths. Target multiple vaults at once via `vault_paths`.
- 🔍 **Lightweight Preview & Two-Stage AX (0.7.2)**: Use `preview=true` to retrieve compact highlighted snippets and line numbers, then pinpoint details with `kb_read` without dumping full chunk contents.
- 📄 **Native Plain-Text .txt Ingestion (0.7.2)**: Plain text `.txt` files are indexed alongside Markdown, with built-in chapter heading recognition.
- 🔍 **Agent Trust Anchor, No Pre-flight Checks (0.7.1)**: One command (`python -m mortis_rag_mcp --doctor`) writes a local environment receipt (`STATUS.md`). Once an agent sees ✅, it **skips every environment / dependency / API-key pre-flight check** and queries your notes immediately instead of probing first. If something is actually broken, the receipt points to that single command — and a failed check never turns into a retry loop.
- 📄 **Document Parsing & Ingestion**: Configured PDF/Office parsing feeds retrieval without modifying source documents; virtual storage is the default. Standalone image and complete audio production ingestion remain unfinished.
- 🎯 **Intelligent Vault Routing (0.7.0)**: Add a natural-language description to each vault. AI agents pick the most relevant knowledge base automatically, cutting down noise and boosting response speed.
- 📊 **Tables**: Estimated mode preserves oversized HTML tables and skips their embedding. The legacy oversized multi-cell defect remains for address compatibility; upgrading does not silently rewrite it.
- 🔒 **Private Solo Vaults (solo)**: Register isolated vaults (`kb_init_solo`) that are excluded from global fan-out search and only queried when explicitly targeted.
- ⚡ **Hybrid Search & Incremental Sync**: Fuses full-text keyword retrieval with semantic vector recall and reranking. Native file watching and read-priority background refresh ensure notes remain queryable while updates are indexed.
- 📦 **Zero Runtime Dependencies**: Core features implemented with the standard library (`dependencies = []`). Lightweight and clean.

---

## 🚀 5-Minute Quick Start

### 1. Installation

Requires Python 3.10+:

```powershell
git clone https://github.com/moton16/Mortis-RAG-MCP.git
cd Mortis-RAG-MCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

### 2. Configuration

Copy the example configuration:

```powershell
Copy-Item .\config\app.toml.example .\config\app.toml
```

#### (1) Basic: Embedding API Key (for semantic note retrieval)
Recommended: SiliconFlow `BAAI/bge-m3` free tier:
- **Option 1 (Recommended)**: Set environment variable `MORTIS_RAG_API_KEY=your_api_key` (also backwards-compatible with `VAULT_MCP_API_KEY`).
- **Option 2**: Configure your endpoint and key directly in `config/app.toml`.

#### (2) Optional: MinerU Setup (0.7.0, for PDF & Office document ingestion)
If you want to search PDFs, Word, PPT, Excel, or images in your knowledge base:
1. Open `config/app.toml` and set `enabled = true` under `[ingest]`.
2. Configure your MinerU Token (two options):
   - **High-Precision Channel (Recommended)**: Get a free API token at [mineru.net](https://mineru.net), set environment variable `MINERU_API_TOKEN=your_token` (or set `api_key = "your_token"` under `[ingest]` in `config/app.toml`) for 1,000 free pages/day and large document support.
   - **Zero-Config Trial**: Leave `api_key` blank to use the public guest channel (best for quick tests under 20 pages).

### 3. Wire Up Your MCP Client

The server runs over standard stdio:

#### WorkBuddy / JSON Configuration
```json
{
  "mortis-rag-mcp": {
    "command": "python",
    "args": ["-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", "C:\\path\\to\\config\\app.toml"],
    "env": {
      "MORTIS_RAG_API_KEY": "your_api_key",
      "MINERU_API_TOKEN": "optional_mineru_token_for_pdf_ingestion"
    }
  }
}
```

#### Codex / Trae / TOML Configuration
```toml
[mcp_servers.mortis_rag_mcp]
command = "mortis-rag-mcp"
args = ["--serve-mcp-stdio", "--app-config", "C:\\path\\to\\config\\app.toml"]
enabled = true
```

### 4. Initialize and Use

Once connected, simply tell your AI assistant:

> "Please register my knowledge base using `kb_init`: `D:\MyNotes`"

The knowledge base is indexed in the background. You can now search conversationally:
> "Search my notes for circuit design latch concepts"
> "Find information about project architecture in my knowledge base"

---

## 🛠️ Common Tools Quick Reference

| Tool | Description |
|---|---|
| `kb_init` | Register a new knowledge base directory |
| `kb_init_solo` | Register/convert an isolated private vault (excluded from global fan-out) |
| `kb_list` | List all registered knowledge bases and their statuses |
| `kb_describe` | Set vault natural language description for intelligent AI routing (0.7.0) |
| `kb_search` | Hybrid semantic and keyword search (cross-vault, targeted vault/multi-vault, path filters, pagination, and preview mode) |
| `kb_read` | Read raw note content by file path or line range |
| `kb_ingest` | Ingest and parse PDF / Office documents into Markdown (0.7.0) |
| `kb_remove` | Safely remove a vault from the registry (never deletes local files) |
| `kb_stats` | Inspect vault health, file counts, and chunk statistics |

---

## 📚 Documentation & Navigation

- 📖 **User Quick Start Guide**: See [QUICKSTART_user.md](QUICKSTART_user.md) for full configuration and troubleshooting.
- 📝 **Release Changelog**: See [CHANGELOG_user.md](CHANGELOG_user.md) for version highlights and history.
- 🤖 **Companion Agent Skill**: See [skills/mortis-rag-mcp/SKILL.md](skills/mortis-rag-mcp/SKILL.md) for AI retrieval rules and routing matrix.
- 💻 **Developer Architecture Guide**: See [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md) & [docs/Quick-start_developer.md](docs/Quick-start_developer.md) for deep-dive architecture and contribution guidelines.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
