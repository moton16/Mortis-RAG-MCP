# Mortis'RAG MCP

[![CI](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Version: 0.9.0](https://img.shields.io/badge/Version-0.9.0-blue.svg)](CHANGELOG_user.md)

[English](README_EN.md) | 简体中文

> 面向 Obsidian 与本地知识库的现代化 RAG（检索增强生成）MCP 服务。
> 纯标准库高能实现，源文件安全驻留本地；深度支持语义检索、跨模态以文搜图、虚拟文档库、多库智能路由与精准阅读。

---

## 🚀 当前版本：v0.9.0（重大里程碑发布）

v0.9.0 迎来了架构级的全面跃迁，正式切换至商业友好的 **Apache-2.0** 开源许可，并带来以下核心突破：

- 🏢 **全新虚拟文档库（Virtual Doc Store）**：外部文档（PDF/Word/PPT/Excel）解析产物默认入库底层虚拟文档系统，**源文件零改写、原笔记目录零污染（不再生成 `.mortis-parsed/` 镜像文件）**，内置代际管理、CAS 校验与段级原子断点续传。
- 🖼️ **跨模态图文检索打通（Text-to-Image）**：打通多模态向量对齐空间，支持**直接使用自然语言文本检索召回笔记中的插图与图表**；新增 `kb_read_media` 工具支持在受控预算（`budget_bytes`）内安全直读媒体（注：文字检索图片已跑通，部分外部客户端富媒体展示与特定付费端点装配尚未全量验收）。
- 🧩 **自适应 Token 估算切块（`estimated_tokens`）**：新知识库默认启用 Token 粒度自适应切片，语义上下文更连贯完整；建库与检索支持独立 `{text}` 提示词模板注入；原生收录 `.markdown` 扩展名。
- 🔄 **工业级摄取重试与未决请求台账**：`kb_ingest` 全新支持 `retry` 动作，失败/取消任务可精准断点续跑；CLI 新增 `--list-requests` 与 `--abandon-request` 管控未决请求意图。
- 🧹 **架构解耦与纯粹化（Breaking Change）**：彻底剔除重型且低频的外部 ffmpeg 音频转录链路，配置中残留的 `[audio]` 字段自动静默兼容忽略；已有文本笔记索引与向量缓存 100% 平滑继承。
- 🪟 **Windows 平台加固与防死锁**：彻底修复 UTF-8 BOM 配置文件被静默错解进而被 doctor 误判为 BROKEN 的陈年顽疾；重构读写并发锁，消除异步索引自排队假死。

完整变更细节与升级指引见 [CHANGELOG_user.md](CHANGELOG_user.md)。

---

## 🌟 核心特性

- 🖼️ **跨模态图文检索与媒体直读（0.9.0）**：支持多模态向量嵌入空间，支持以文搜图（Text-to-Image）；新增 `kb_read_media` 工具精准定位并按字节预算（`budget_bytes`）按需读取多媒体内容，杜绝上下文溢出。
- 🏢 **非侵入式虚拟文档库（0.9.0）**：外部复杂文档解析结果默认收敛至用户缓存层虚拟文档系统，保持本地笔记目录 100% 纯净，支持段级断点续传与多代版本原子恢复。
- 🧩 **自适应 Token 估算切块与统一模板（0.9.0）**：基于 Token 估算的自适应语义切块（`estimated_tokens`），长文本切分自然连贯；建库与查询支持统一与独立的 `{text}` 提示词模板定制。
- 🔄 **摄取断点重试与意图台账（0.9.0）**：`kb_ingest` 支持指定 `job_id` 对失败或取消的任务进行断点重试；CLI 随时查阅与管理未决任务，索引生命周期（`index_state`）多态解耦。
- ⚡ **紧凑初筛与章节直读（0.8.1）**：新增 `compact=true` 极简结构化投影与整块预算续页控制；`kb_read` 支持按 `heading` 物理章节与小说分卷直读，附带行号越界精准诊断。
- 📂 **自由挂载，零路径绑定**：通过 `kb_init` 一键挂载任意本地文件夹为知识库，持久化注册表管理，不绑定死路径，换电脑或多库迁移极简。
- 📖 **切片原地展开与双链直读（0.8.0）**：命中切片后直接由 `kb_read(chunk_id=...)` 原地展开上下文，省去手工换算行号；遇到 `[[双链]]` 引用直接按短名快速寻址阅读。
- 🏷️ **别名检索与硬词保底（0.8.0）**：原生支持 Obsidian frontmatter `aliases` 别名；支持 `exact_terms` 专有名词硬包含保底，生僻术语与专有代号绝不漏召回。
- 🛡️ **搜索预算控制与本地诊断（0.8.0）**：支持 `budget_bytes` 字节硬预算，避免大搜索撑爆模型上下文或击穿客户端缓冲区；支持脱敏本地诊断日志。
- 📚 **库名直呼与多库定向（0.7.2）**：检索时可直接传库名（如 `vault_path="我的笔记"`），无需拼接 Windows 漫长路径；支持通过 `vault_paths` 数组同时指定多个目标库定向检索。
- 🔍 **轻量预览与二段式精读（0.7.2）**：支持 `preview=true` 快速返回高光切片与行号，正文配合 `kb_read` 按需精准精读，大幅降低模型 Token 冗余。
- 📄 **多格式原生收录（.md / .txt / .markdown）**：纯文本 `.txt`（小说/分卷/资料）与 `.markdown` 与标准 Markdown 享有同等一等公民地位，支持小说章节标题自动识别。
- 🔍 **Agent 信任锚，免预检开箱即搜（0.7.1）**：一条 `python -m mortis_rag_mcp --doctor` 生成本机环境凭证（`STATUS.md`）。AI 助手读到 ✅ 即**不再做任何环境/依赖/key 预检**，首次提问就直接检索，省掉每次调用前的反复试探。
- 🎯 **智能定向路由与权重分配（0.7.0）**：支持为知识库配置自然语言描述（`kb_describe`），支持跨库检索权重调整（`kb_set_weight`），AI 检索时按意图精准选库。
- 🔒 **私密独立库（solo）隔离**：支持注册独立私密库（`kb_init_solo`），默认不参与跨库全局搜索，仅在显式指定时查询，妥善保护个人隐私。
- ⚡ **混合检索与毫秒级增量同步**：融合关键词全文检索（BM25/FTS）与语义向量召回，配合自动重排序；支持原生文件监听与读优先后台增量同步，笔记随写随搜。
- 📦 **轻量纯粹，零沉重运行时依赖**：核心功能纯标准库实现（`dependencies = []`），轻量可靠，可选依赖（`pymupdf`、`sqlite-vec` 等）按需热插拔。

---

## 🛠️ 完整工具一览（16 个核心 MCP 工具）

Mortis'RAG MCP 提供了 16 个标准化 MCP 工具，覆盖从知识库注册、混合检索、多模态直读到文档摄取与运维全生命周期：

### 1. 知识库挂载与多库管理
| 工具 | 用途说明 |
|---|---|
| `kb_init` | 注册并挂载新知识库（指定物理文件夹路径与显示名称） |
| `kb_init_solo` | 注册/转换为私密独立库（默认排除在跨库盲搜外，仅显式指定时检索） |
| `kb_list` | 查看当前已注册的全部知识库列表、路径与状态 |
| `kb_describe` | 设置知识库自然语言描述，引导 AI 按意图进行智能路由选库 |
| `kb_set_weight` | 设置指定知识库在跨库联合检索中的打分权重比重 |
| `kb_remove` | 从注册表移除知识库（安全操作，绝不删除本地实际笔记文件） |

### 2. 混合检索与原文精读
| 工具 | 用途说明 |
|---|---|
| `kb_search` | 语义向量与关键词混合检索（支持跨库/多库定向、目录过滤、分页、`preview` 预览模式、`compact` 紧凑投影与 `exact_terms` 硬词保底） |
| `kb_read` | 精准读取原文笔记（支持按物理文件路径、行号区间、切片编号 `chunk_id` 原地展开、双链 `[[短名]]` 寻址或 `heading` 章节直读） |
| `kb_read_media` | **（0.9.0 新增）** 按版本（`revision_id`）与位置（`occurrence_id`）精准读取笔记中的图片与多媒体内容，支持受预算保护直读媒体块 |

### 3. 文档摄取与外部格式解析
| 工具 | 用途说明 |
|---|---|
| `kb_ingest` | 摄取并解析知识库内的 PDF / Office / 外部文档（支持 `submit` 提交、`status` 查状态、`pending` 查进度、`retry` 任务断点恢复） |

### 4. 状态浏览、豁免与维护备份
| 工具 | 用途说明 |
|---|---|
| `kb_stats` | 查看知识库文件数、切片数、向量模型与底层加速依赖状态 |
| `kb_list_files` | 分页浏览知识库已收录的文件列表，支持目录前缀过滤 |
| `kb_exempt` | 管理知识库豁免规则与单文件排除（即时生效，毫秒级响应） |
| `kb_rebuild` | 强制全量重新构建指定知识库的全文与向量索引 |
| `kb_export` | 导出知识库配置、权重与元数据备份快照 |
| `kb_import` | 从备份恢复知识库元数据，解耦反馈索引构建状态（`index_state`） |

---

## 🚀 5 分钟快速上手

### 1. 安装

环境要求：Python 3.10+。

```powershell
git clone https://github.com/moton16/Mortis-RAG-MCP.git
cd Mortis-RAG-MCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

> **可选依赖**：
> - 本地 Office/PDF 解析支持：`pip install -e ".[docs]"`
> - 磁盘向量数据库加速：`pip install -e ".[vec]"`

### 2. 配置

从模板复制配置文件：

```powershell
Copy-Item .\config\app.toml.example .\config\app.toml
```

#### (1) 基础配置：Embedding API Key（用于笔记语义检索）
推荐使用免费档硅基流动 `BAAI/bge-m3`：
- **方式一（推荐）**：设置系统环境变量 `MORTIS_RAG_API_KEY=你的API密钥`（同时兼容旧名 `VAULT_MCP_API_KEY`）。
- **方式二**：直接在 `config/app.toml` 中配置你的服务商地址与密钥。

#### (2) 可选配置：MinerU 接入（用于 PDF / Office 文档解析摄取）
如果需要检索知识库内的 PDF、Word、PPT、Excel 文件：
1. 打开 `config/app.toml`，在 `[ingest]` 小节将 `enabled = true`。
2. 配置 MinerU Token（两种方式）：
   - **高精度通道（推荐）**：前往 [mineru.net](https://mineru.net) 免费获取 API Token，设置系统环境变量 `MINERU_API_TOKEN=你的Token`（或在 `config/app.toml` 的 `[ingest]` 中填写 `api_key = "你的Token"`），享受每日 1000 页额度并支持文档插图提取。
   - **免登试用通道**：留空 `api_key` 即可直接使用（适合 20 页以内的日常小文档体验；该通道为轻量纯文本提取，不返回图片）。

### 3. 接入 AI 客户端

服务以标准 stdio 模式运行，直接在你的 MCP 客户端中配置即可：

#### WorkBuddy / 自定义 JSON 连接器
```json
{
  "mortis-rag-mcp": {
    "command": "python",
    "args": ["-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", "C:\\你的路径\\config\\app.toml"],
    "env": {
      "MORTIS_RAG_API_KEY": "你的API密钥",
      "MINERU_API_TOKEN": "可选，用于PDF解析的MinerU密钥"
    }
  }
}
```

#### Codex / Trae / TOML 配置
```toml
[mcp_servers.mortis_rag_mcp]
command = "mortis-rag-mcp"
args = ["--serve-mcp-stdio", "--app-config", "C:\\你的路径\\config\\app.toml"]
enabled = true
```

### 4. 初始化与使用

连接成功后，在对话中对 AI 助手说：

> "帮我用 `kb_init` 注册知识库：`D:\我的笔记`"

知识库即可在后台自动建立索引。之后只需自然提问：
> "搜一下数电笔记里关于触发器的内容"
> "查一下知识库里关于项目架构的说明"

---

## 📚 详细文档与导航

- 📖 **新手完整指南**：详见 [QUICKSTART_user.md](QUICKSTART_user.md)（更详尽的安装排错、0.9 升级与恢复及配置项说明）。
- 📝 **版本更新日志**：详见 [CHANGELOG_user.md](CHANGELOG_user.md)（各版本详细更新说明与功能亮点）。
- 🤖 **AI 助手配套技能**：详见 [skills/mortis-rag-mcp/SKILL.md](skills/mortis-rag-mcp/SKILL.md)（为智能体提供最佳检索路由纪律）。
- 💻 **开发者技术说明书**：详见 [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md) 与 [docs/Quick-start_developer.md](docs/Quick-start_developer.md)（底层架构设计、二次开发与技术流水）。

---

## 📄 License

本项目自 v0.9.0 起采用 [Apache License 2.0](LICENSE) 开源许可（v0.8.1 及更早版本仍为 MIT，已发布版本的历史授权不受影响）。

可选解析依赖（如 PyMuPDF）由用户自行安装，不在本项目的 Apache-2.0 授权范围内，各自的许可证见 [NOTICE](NOTICE)。
