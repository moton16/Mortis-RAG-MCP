# Mortis'RAG MCP

[![CI](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/moton16/Mortis-RAG-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version: 0.9.0](https://img.shields.io/badge/Version-0.9.0-blue.svg)](CHANGELOG_user.md)

[English](README_EN.md) | 简体中文

> 面向 Obsidian 与 Markdown 笔记的本地知识库 RAG（检索增强生成）MCP 服务。
> 源文件保留在本地，支持语义检索、定向路由和跨库搜索；启用远端 embedding/解析时，相应内容会发送至配置的服务。

---

## 当前候选：0.9.0（本地准备，尚未发布）

- 文本 `{text}` 模板贯通建库和查询；新库默认 `estimated_tokens`，旧库按已有参数/缓存保留 `legacy_chars`，新增 `.markdown`。显式启用的远端配置正常工作，不增设费用审批流程。
- PDF/DOCX/PPTX/XLSX 可按配置走本地可选解析器；virtual 为默认解析存储，原文件不改写、不新建镜像；旧镜像仅在逐项来源证据匹配时排除。缺依赖不等于该格式验证成功。
- `kb_import` 返回 `index_state=empty/rebuilding/unverified/ready`；导入成功不等于索引 ready。`kb_ingest(action="retry", job_id=..., vault_path=...)` 重试 failed/cancelled，unknown 不自动重发。
- CLI：`--list-requests --vault "库的绝对路径"` 查看未决记录；`--abandon-request REQUEST_ID --vault "库的绝对路径"` 仅放弃本机意图，不取消服务端任务、不重发。
- **已移除**：音频转录与音频转码能力从本版本整体移除，配置里的 `[audio]` 段与 `audio_enabled` 开关一并消失。旧配置里若还留着这些键会被直接忽略（不报错、不影响其它配置），建议顺手删掉。
- 16 个工具包含 `kb_read_media`，可按 source/revision/occurrence 获取引用和受预算约束的媒体。**用文字直接检索到知识库里的图片**已在本机验证跑通；**在真实客户端里显示图片，以及付费媒体端点的通用装配尚未验收**；代理图注不是原生媒体的替代。
- 升级/恢复和可复制操作见 [快速开始 §0.4](QUICKSTART_user.md#04-09-候选升级与恢复)。显式 `--doctor` 可能请求真实端点，Agent 不以缺失 STATUS.md 为由自动运行它。

## 🌟 核心特性

- ⚡ **紧凑初筛与章节直读（0.8.1）**：新增 `compact=true` 极简结构化投影与整块预算续页控制；`kb_read` 支持按 `heading` 物理章节直读与越界精确行诊断；检索优先使用已就绪索引，增量刷新在后台执行，但并发更新时仍可能短暂等待。
- 📂 **自由挂载，零路径绑定**：通过 `kb_init` 一键挂载任意本地文件夹为知识库，配置持久化保存，不绑定死路径，换电脑或多库管理极简。
- 📖 **切片原地展开与双链直读（0.8.0）**：命中切片后直接由 `kb_read(chunk_id=...)` 原地展开上下文，省去手工换算行号；遇到 `[[双链]]` 引用直接按短名快速寻址阅读。
- 🏷️ **别名检索与硬词保底（0.8.0）**：原生支持 frontmatter `aliases` 别名；支持 `exact_terms` 专有名词硬包含保底，专有代号与生僻术语绝不漏召回。
- 🛡️ **搜索预算控制与本地诊断（0.8.0）**：支持 `budget_bytes` 字节硬预算，避免大搜索撑爆上下文或击穿宿主缓冲区；支持脱敏本地诊断日志。
- 📚 **库名直呼与多库定向（0.7.2）**：检索时可直接传库名（如 `vault_path="我的笔记"`）而无需拼接 Windows 漫长物理路径；支持通过 `vault_paths` 数组同时指定多个目标库定向检索。
- 🔍 **轻量预览与二段式精读（0.7.2）**：支持 `preview=true` 快速返回高光切片与行号，正文配合 `kb_read` 按需精准精读，有效降低上下文冗余。
- 📄 **纯文本 .txt 原生收录（0.7.2）**：纯文本 `.txt`（小说/分卷/资料）与 Markdown 享有同等索引地位，支持小说章节标题自动识别。
- 🔍 **Agent 信任锚，免预检开箱即搜（0.7.1）**：一条 `python -m mortis_rag_mcp --doctor` 生成本机环境凭证（`STATUS.md`）。AI 助手读到 ✅ 即**不再做任何环境/依赖/key 预检**，首次提问就直接检索，省掉每次调用前的反复试探；真出问题才提示你跑那一条命令，且失败不会陷入重试死循环。
- 📄 **文档解析与摄取**：按配置将 PDF/Office 解析结果纳入搜索，原文件不改写；单独放一张图片直接入库摄取仍未闭合，参见上述候选状态。
- 🎯 **智能定向路由（0.7.0）**：支持为知识库添加一句话自然语言描述，AI 检索时按意图精准选库，大幅减少无关库干扰，回答更快更准。
- 📊 **表格排版**：estimated 模式保留超限 HTML 表格并跳过其向量请求；legacy 极端超长多格行仍有旧版截失问题，为保持旧地址不在升级时静默改写。
- 🔒 **私密独立库（solo）**：支持注册独立私密库（`kb_init_solo`），默认不参与跨库全局搜索，仅在明确指定时查询，妥善保护个人隐私。
- ⚡ **混合检索与增量同步**：融合关键词全文检索与语义向量召回，配合自动重排序；支持后台增量同步与只读优先检索，笔记随写随搜。
- 📦 **轻量纯粹**：核心功能纯标准库实现，无冗余第三方运行时依赖。

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

### 2. 配置

从模板复制配置文件：

```powershell
Copy-Item .\config\app.toml.example .\config\app.toml
```

#### (1) 基础配置：Embedding API Key（用于笔记语义检索）
推荐使用免费档硅基流动 `BAAI/bge-m3`：
- **方式一（推荐）**：设置系统环境变量 `MORTIS_RAG_API_KEY=你的API密钥`（同时兼容旧名 `VAULT_MCP_API_KEY`）。
- **方式二**：直接在 `config/app.toml` 中配置你的服务商地址与密钥。

#### (2) 可选配置：MinerU 接入（0.7.0，用于 PDF / Office 文档解析摄取）
如果需要检索知识库内的 PDF、Word、PPT、Excel 或图片文件：
1. 打开 `config/app.toml`，在 `[ingest]` 小节将 `enabled = true`。
2. 配置 MinerU Token（两种方式）：
   - **高精度通道（推荐）**：前往 [mineru.net](https://mineru.net) 免费获取 API Token，设置系统环境变量 `MINERU_API_TOKEN=你的Token`（或在 `config/app.toml` 的 `[ingest]` 中填写 `api_key = "你的Token"`），享受每日 1000 页额度与大文件支持。
   - **免登试用通道**：留空 `api_key` 即可直接使用（适合 20 页以内的日常小文档体验）；该通道**只取回解析出的文字**，不带文档里的图片。

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

## 🛠️ 常用工具一览

| 工具 | 用途说明 |
|---|---|
| `kb_init` | 注册新知识库（指定文件夹路径与名称） |
| `kb_init_solo` | 注册/转换为私密独立库（不参与跨库全局搜索） |
| `kb_list` | 查看已注册的全部知识库列表与状态 |
| `kb_describe` | 设置知识库自然语言描述，引导 AI 精准定向检索（0.7.0） |
| `kb_search` | 语义与关键词混合检索（支持跨库、指定库/多库定向、目录过滤、分页与 preview 预览模式） |
| `kb_read` | 快速按文件路径或行号读取原文笔记 |
| `kb_ingest` | 摄取并解析知识库内的 PDF / Office 文档（0.7.0，按需开启） |
| `kb_remove` | 从注册表移除知识库（安全操作，不删除本地实际文件） |
| `kb_stats` | 查看知识库文件数、切片数与索引健康度 |

---

## 📚 详细文档与导航

- 📖 **新手完整指南**：详见 [QUICKSTART_user.md](QUICKSTART_user.md)（更详尽的安装排错与完整配置项说明）。
- 📝 **版本更新日志**：详见 [CHANGELOG_user.md](CHANGELOG_user.md)（各版本更新说明与功能亮点）。
- 🤖 **AI 助手配套技能**：详见 [skills/mortis-rag-mcp/SKILL.md](skills/mortis-rag-mcp/SKILL.md)（为智能体提供最佳检索路由纪律）。
- 💻 **开发者技术说明书**：详见 [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md) 与 [docs/Quick-start_developer.md](docs/Quick-start_developer.md)（底层架构设计、二次开发与技术流水）。

---

## 📄 License

本项目采用 [MIT License](LICENSE) 开源许可。
