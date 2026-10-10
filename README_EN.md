# Mortis'RAG MCP

[![CI](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Version: 0.9.0](https://img.shields.io/badge/Version-0.9.0-blue.svg)](CHANGELOG_user.md)

English | [简体中文](README.md)

> A modern, standard-library-first RAG (Retrieval-Augmented Generation) MCP server for Obsidian and local knowledge bases.
> Source files stay securely on your machine. Rich support for hybrid search, text-to-image multimodal retrieval, virtual doc store, intelligent multi-vault routing, and pinpoint reading.

---

## 🚀 Current Release: v0.9.0 (Major Milestone Release)

v0.9.0 marks a comprehensive architectural leap and officially relicenses under the business-friendly **Apache License 2.0**, bringing the following core advancements:

- 🏢 **New Virtual Doc Store (`doc_store`)**: Structured parse facts from external documents (PDF/Word/PPT/Excel) now store in a decoupled virtual document database by default. **Zero modifications to original source notes and zero workspace pollution (no scattered `.mortis-parsed/` mirror files)**, backed by generational revision control, CAS verification, and section-level atomic checkpoint recovery.
- 🖼️ **Cross-Modal Text-to-Image Retrieval**: Bridges aligned multimodal embedding vector spaces, enabling users to **search and recall illustrations and diagrams directly via natural language text queries**; adds the `kb_read_media` tool to safely read media content under strict byte budgets (`budget_bytes`).
- 🧩 **Adaptive Token-Estimated Chunking (`estimated_tokens`)**: New knowledge bases default to token-aware chunking for superior semantic continuity and boundary precision; supports custom `{text}` prompt templates for both indexing and querying; natively indexes `.markdown` note files.
- 🔄 **Production Ingest Retry & Request Ledger**: `kb_ingest` introduces the `retry` action to resume failed or cancelled parsing tasks with checkpoint accuracy; adds CLI commands `--list-requests` and `--abandon-request` to inspect and abandon local intention records.
- 🧹 **Architectural Decoupling & Cleanup (Breaking Change)**: Completely removes deprecated, heavyweight ffmpeg-based audio transcription and transcoding pipelines. Lingering `[audio]` config sections are safely and silently ignored; all existing text notes, indexes, and vector caches are 100% seamlessly preserved.
- 🪟 **Windows Hardening & Concurrency Deadlock Fix**: Fully fixes UTF-8 BOM silent misparsing that historically caused legitimate configs to be falsely reported as BROKEN by `--doctor`; refactors read-write locks to eliminate self-enqueuing deadlocks during background indexing.

Full release notes and upgrade details can be found in [CHANGELOG_user.md](CHANGELOG_user.md).

---

## 🌟 Key Features

- 🖼️ **Cross-Modal Text-to-Image & Media Read (0.9.0)**: Direct semantic retrieval of embedded images via natural language queries; `kb_read_media` accurately inspects and returns media chunks bounded by `budget_bytes` to prevent context buffer overflow.
- 🏢 **Non-Invasive Virtual Doc Store (0.9.0)**: Decoupled parsing storage keeps your note directory 100% pristine; provides section-level crash recovery and multi-generational atomic publication.
- 🧩 **Adaptive Token Chunking & Custom Templates (0.9.0)**: Token-aware chunking (`estimated_tokens`) produces natural semantic boundaries; independent `{text}` prompt template injection for embedding and querying.
- 🔄 **Ingest Retry & Request Ledger (0.9.0)**: Resumes document parsing via `kb_ingest(action="retry", job_id=...)`; CLI management for pending intentions with decoupled `index_state` lifecycle reporting.
- ⚡ **Compact Search & Section Read (0.8.1)**: Lightweight `compact=true` structured projection with whole-chunk budgeting cursors; `kb_read` resolves heading sections directly with accurate out-of-bounds line hints.
- 📂 **Zero Hardcoded Paths**: Attach any local folder as a knowledge base using `kb_init`. Persistent user-level registry without locking to fixed directory paths.
- 📖 **In-place Chunk Expansion & Wikilink Read (0.8.0)**: Read context directly via `kb_read(chunk_id=...)` without calculating line ranges; navigate `[[wikilinks]]` by short stem names automatically.
- 🏷️ **Aliases Retrieval & Exact Terms Hard Inclusion (0.8.0)**: Native frontmatter `aliases` search; guaranteed recall for proper nouns via `exact_terms` with multi-route fallback.
- 🛡️ **Search Output Budget & Local Diagnostic Log (0.8.0)**: Hard byte budget limit via `budget_bytes` prevents context overflow; privacy-safe local jsonl diagnostic logging.
- 📚 **Vault Alias & Multi-Vault Scoped Search (0.7.2)**: Query vaults by registered friendly names (e.g. `vault_path="MyNotes"`) without writing long paths. Target multiple vaults at once via `vault_paths`.
- 🔍 **Lightweight Preview & Two-Stage Reading (0.7.2)**: Use `preview=true` to retrieve compact highlighted snippets and line numbers, then pinpoint details with `kb_read`.
- 📄 **Native Multi-Format Support (.md / .txt / .markdown)**: Plain text `.txt` files (novels/docs) and `.markdown` files are indexed alongside `.md` with built-in chapter heading recognition.
- 🔍 **Agent Trust Anchor, No Pre-flight Checks (0.7.1)**: One command (`python -m mortis_rag_mcp --doctor`) writes a local receipt (`STATUS.md`). Agents seeing ✅ skip every repetitive environment / API pre-flight check.
- 🎯 **Intelligent Vault Routing (0.7.0)**: Add a natural-language description to each vault (`kb_describe`) and tune weights (`kb_set_weight`); AI agents pick the most relevant knowledge bases automatically.
- 🔒 **Private Solo Vaults (solo)**: Register isolated vaults (`kb_init_solo`) excluded from global fan-out search and only queried when explicitly targeted.
- ⚡ **Hybrid Search & Millisecond Incremental Sync**: Fuses full-text keyword retrieval (BM25/FTS) with semantic vector recall and reranking; native file watching keeps notes queryable with read priority.
- 📦 **Zero Heavy Runtime Dependencies**: Core features implemented purely with the standard library (`dependencies = []`). Optional extras (`pymupdf`, `sqlite-vec`) plug in on demand.

---

## 🛠️ Complete MCP Tools (16 Tools)

Mortis'RAG MCP provides 16 standardized MCP tools covering the entire lifecycle from vault registration and hybrid retrieval to multimodal media inspection and document maintenance:

### 1. Vault Management & Multi-Vault Routing
| Tool | Description |
|---|---|
| `kb_init` | Register and attach a new knowledge base directory with a friendly name |
| `kb_init_solo` | Register or convert a vault into an isolated private solo vault |
| `kb_list` | List all registered knowledge bases, physical paths, and current status |
| `kb_describe` | Set a natural language description to guide intelligent agent routing |
| `kb_set_weight` | Configure scoring weight multiplier for a vault in cross-vault searches |
| `kb_remove` | Unregister a knowledge base (safe operation, never deletes local notes) |

### 2. Hybrid Retrieval & Precise Reading
| Tool | Description |
|---|---|
| `kb_search` | Hybrid vector & keyword search (supports multi-vault scoping, path filters, pagination, `preview`, `compact`, and `exact_terms`) |
| `kb_read` | Read raw notes (supports line ranges, `chunk_id` in-place expansion, `[[wikilink]]` stems, or `heading` sections) |
| `kb_read_media` | **(New in 0.9.0)** Read embedded images and media by `revision_id` and `occurrence_id` with strict `budget_bytes` control |

### 3. Document Ingestion & Parsing
| Tool | Description |
|---|---|
| `kb_ingest` | Manage and parse external PDF / Office documents (`submit`, `status`, `pending`, and checkpoint-aware `retry`) |

### 4. Health, Maintenance & Migration
| Tool | Description |
|---|---|
| `kb_stats` | Inspect vault stats, file counts, chunk counts, model profiles, and acceleration status |
| `kb_list_files` | Browse indexed files with pagination and directory prefix filtering |
| `kb_exempt` | Manage ignore rules and per-file exemptions with instant effect |
| `kb_rebuild` | Force a clean rebuild of keyword and vector indexes for a vault |
| `kb_export` | Export vault metadata, weights, and configuration backup snapshots |
| `kb_import` | Restore metadata and report decoupled index build status (`index_state`) |

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

> **Optional Extras**:
> - Local Office/PDF parsing: `pip install -e ".[docs]"`
> - Disk vector database acceleration: `pip install -e ".[vec]"`

### 2. Configuration

Copy configuration from template:

```powershell
Copy-Item .\config\app.toml.example .\config\app.toml
```

#### (1) Base Setup: Embedding API Key
Recommended free tier: SiliconFlow `BAAI/bge-m3`:
- **Option 1 (Recommended)**: Set environment variable `MORTIS_RAG_API_KEY=your_key` (also backwards-compatible with `VAULT_MCP_API_KEY`).
- **Option 2**: Configure your provider and key in `config/app.toml`.

#### (2) Optional Setup: MinerU Integration (PDF / Office Document Parsing)
To parse PDFs, Word docs, PPTs, or Excel spreadsheets:
1. In `config/app.toml`, set `enabled = true` under `[ingest]`.
2. Configure MinerU Token:
   - **High Precision (Recommended)**: Get a free API Token at [mineru.net](https://mineru.net) (1000 free pages/day with image extraction), and set `MINERU_API_TOKEN=your_token`.
   - **Free Agent Channel**: Leave `api_key` empty for quick evaluation (<20 pages; text-only extraction without images).

### 3. Connect to AI Clients

Runs via standard stdio mode:

#### WorkBuddy / Custom JSON Connector
```json
{
  "mortis-rag-mcp": {
    "command": "python",
    "args": ["-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", "C:\\path\\to\\config\\app.toml"],
    "env": {
      "MORTIS_RAG_API_KEY": "your_api_key",
      "MINERU_API_TOKEN": "optional_mineru_token"
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

### 4. Initialize and Query

Once connected, ask your AI assistant:

> "Help me register my vault using `kb_init`: `D:\MyNotes`"

Indexes build automatically in the background. Then query naturally:
> "Search my notes for how the project architecture is designed"

---

## 📚 Documentation Navigation

- 📖 **User Quick Start & Upgrade Guide**: See [QUICKSTART_user.md](QUICKSTART_user.md).
- 📝 **Release Changelog**: See [CHANGELOG_user.md](CHANGELOG_user.md).
- 🤖 **AI Agent Skill**: See [skills/mortis-rag-mcp/SKILL.md](skills/mortis-rag-mcp/SKILL.md).
- 💻 **Developer Guide**: See [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md) and [docs/Quick-start_developer.md](docs/Quick-start_developer.md).

---

## 📄 License

Licensed under the [Apache License 2.0](LICENSE) since v0.9.0 (v0.8.1 and earlier remain under MIT).

Optional dependencies (e.g. PyMuPDF) are installed by users and not covered under the Apache-2.0 license of this project; see [NOTICE](NOTICE).
