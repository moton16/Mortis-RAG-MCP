# Mortis'RAG MCP

[![CI](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version: 0.7.0](https://img.shields.io/badge/Version-0.7.0-blue.svg)](CHANGELOG_user.md)

English | [简体中文](README.md)

> A standard-library-only MCP server for Obsidian and Markdown knowledge bases.
> Keep all your notes private on your local machine with semantic retrieval, intelligent routing, PDF/Office document ingestion, and multi-vault search out of the box.

---

## 🌟 Key Features

- 📂 **Zero Hardcoded Paths**: Attach any local folder as a knowledge base using `kb_init`. Persistent user-level registry without modifying configs or locking to fixed directories.
- 📄 **Document Parsing & Ingestion (0.7.0)**: Automatically converts PDF, Word, PPT, Excel, and images into Markdown for seamless retrieval. Parsed files reside cleanly in `.mortis-parsed/` without modifying or polluting source documents.
- 🎯 **Intelligent Vault Routing (0.7.0)**: Add a natural-language description to each vault. AI agents pick the most relevant knowledge base automatically, cutting down noise and boosting response speed.
- 📊 **Table Structure Preservation (0.7.0)**: Complex tables and headers remain intact across chunk boundaries, ensuring clean and legible table search results.
- 🔒 **Private Solo Vaults (solo)**: Register isolated vaults (`kb_init_solo`) that are excluded from global fan-out search and only queried when explicitly targeted.
- ⚡ **Sub-second Hybrid Search**: Fuses full-text keyword retrieval with semantic vector recall and reranking. Native file watching ensures changes are indexed incrementally in real time.
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
| `kb_search` | Hybrid semantic and keyword search (cross-vault or targeted) |
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
