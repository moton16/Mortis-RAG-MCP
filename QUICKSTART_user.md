# Mortis'RAG MCP 快速开始（下载后初始化指南）

> 面向拿到本仓库的新用户 / 新设备：从 clone 到知识库检索可用，大约 5 分钟。
> 全程只需要：Python 3.10+、一个 embedding API key（推荐硅基流动免费档 bge-m3）。

## 0. 前置说明

- 你的笔记目录、注册表、API key **全部在你自己的机器上**，仓库里不含任何个人配置（`config/app.toml` 已被 .gitignore 排除）。
- 首次启动后，所有知识库关系由**用户级注册表** `~/.mortis_rag_mcp/vaults.toml`（兼容旧路径 `~/.vault_mcp/vaults.toml`）管理，不写在代码或仓库里。

### 0.1 从旧版本升级上来（0.7.1 变更要点）

0.7.1 把用户数据目录一起更名了，**自动迁移，不需要你动手**。四点需要知道：

1. **目录改名（首次启动自动完成）**：`~/.vault_mcp` → `~/.mortis_rag_mcp`；缓存目录 `~/.vault_mcp_cache` → `~/.mortis_rag_mcp_cache`。旧路径仍被兼容读取，迁移只做一次原子改名，不复制、不删数据。
2. **环境变量改名（旧名永久兼容）**：`VAULT_MCP_API_KEY` → `MORTIS_RAG_API_KEY`；配置与注册表覆盖变量同理：`VAULT_MCP_CONFIG` → `MORTIS_RAG_CONFIG`、`VAULT_MCP_REGISTRY` → `MORTIS_RAG_REGISTRY`。新名优先，旧名继续生效。
3. **一键体检（新增）**：`python -m mortis_rag_mcp --doctor` 会在终端打印体检报告并写入 `~/.mortis_rag_mcp/STATUS.md`。AI 助手读到 ✅ 就不再重复预检环境（不查 venv、不点依赖、不探活 API），直接检索。
4. **若要退回 0.7.1 之前的老版本**：必须把 `~/.mortis_rag_mcp` **和** `~/.mortis_rag_mcp_cache` **两个**目录都手工改回旧名——只改前一个，库列表会回来但缓存全部失效、所有笔记要重新嵌入（用付费 embedding 会重新计费）。完整说明见 [CHANGELOG_user.md](CHANGELOG_user.md) 的「回退须知」。

### 0.2 0.7.2 新用法速查

v0.7.2 带来了更自然便捷的检索与交互体验：

1. **库名直呼**：`kb_search` 的 `vault_path` 支持直接传知识库显示名称（如 `vault_path="我的笔记"`），不用再费力拼装 Windows 漫长路径。
2. **多库定向圈选**：支持通过 `vault_paths=["知识库A", "知识库B"]` 一次性圈选多个目标库联合检索；即使是被设为私密独立库（solo）的知识库，只要在此显式点名即可参与联合召回。
3. **二段式精准精读（省 Token 模式）**：
   - 第一步（找锚点）：调用 `kb_search(..., preview=true)`，检索仅返回高光摘要窗口、行号与字符数，大幅精简上下文占用；
   - 第二步（按需精读）：根据命中结果的 `source`、`start_line` 与 `end_line`，按需调用 `kb_read` 读取切题正文，告别全篇冗余注入。
4. **构建防假死与进度感知**：首次建库或后台构建期间若返回 `status: "indexing"`，会携带构建进度信息，稍候片刻等待后台构建即可，不再发生前台卡死。

### 0.3 0.8.1 新用法速查

v0.8.1 进一步强化了检索初筛、章节直读与后台刷新体验：

1. **紧凑初筛模式（`compact=true`）**：
   - 适合大候选召回（如 `top_k >= 5`）：调用 `kb_search(query, compact=true, budget_bytes=3500)`，仅返回文档路径、行号区间与标题信息，去除大段正文和 chunk_id，极大减轻宿主上下文与缓冲区压力。
   - 回读正文：直接根据返回的 `vault`、`source` 与行号调用 `kb_read(source, start_line, end_line, vault_path)` 精准精读。
2. **物理章节直读与重名消歧**：
   - `kb_read(source="...", heading="## 章节名", vault_path="...")`：直接读取该章节物理范围（含子章节，直到下一个同级或更高级标题）。
   - 若文件中存在多个同名标题，系统会返回所有候选章节的起始物理行号（如报错 `heading '章节名' … 中存在 2 处同名标题 (起始行: 42, 108)；存在歧义…`），此时带上 `start_line` 即可直接消歧读取。
   - 越界安全：若请求行号超出文件末尾，系统会明确报错并附带文件实际总行数（如 `start_line 超出文件行数范围 (requested: 500, actual: 320, …)`，响应字段 `total_lines`），方便调整。
3. **后台刷新与只读优先**：
   - 当知识库后台正在增量同步或嵌入计算时，搜索优先使用当前已就绪的文本索引，不在前台执行全量同步或等待嵌入请求；并发更新时仍可能短暂等待，并非所有请求都能立即返回。
4. **文档自动摄取安全保障**：
   - `[ingest] auto_watch` 默认关闭（`false`），绝不未经用户显式配置擅自向云端上传解析文件。
   - 默认单文件上限 `max_file_size_mb = 20`，超过 20MiB 的文件自动跳过，防误传超大文档。

## 1. 安装

```powershell
git clone https://github.com/moton16/Mortis-RAG-MCP.git
cd Mortis-RAG-MCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
# 若报 Cannot import 'setuptools.build_meta'（Codex/Trae 等捆绑 Python 常见）：
# 这是环境缺/坏 setuptools，不是本包问题。先修构建环境，再重试：
# python -m pip install --upgrade pip setuptools wheel
# 仍失败（如离线/构建隔离异常）再退而求其次：
# python -m pip install -e . --no-build-isolation
# 可选加速（批量余弦用 numpy）：
# python -m pip install numpy   # 等价写法：python -m pip install "mortis-rag-mcp[accel]"
# 可选：磁盘向量后端（向量不占内存，13k 切片约省 55MB RAM）：
# python -m pip install "mortis-rag-mcp[vec]"   # 然后配置里开 [vector] backend = "sqlite_vec"
```

上面两个可选依赖**建议装上**，大库体验更好：

- `numpy` 让检索排序快约一个数量级（批量余弦）；
- `sqlite-vec` 让向量落盘、不占内存（装完在 `config/app.toml` 里把 `[vector]` 的 `backend` 改为 `"sqlite_vec"`）。

**不装也完全能用**：没有 numpy 时自动回退内置标量计算（结果一致，只是慢些）；没有 sqlite-vec 时用默认的内存后端，核心功能不受影响。一次装齐：

```powershell
python -m pip install "mortis-rag-mcp[accel,vec]"
```

## 2. 配置

```powershell
Copy-Item .\config\app.toml.example .\config\app.toml
```

编辑 `config/app.toml`，按需修改：

1. `[embedding]`：`mode` 改 `"external"`、填 endpoint/model/dimension（硅基流动 bge-m3 为 `BAAI/bge-m3`、1024 维、`send_dimensions = false`）
2. API key 二选一：
   - 环境变量（推荐）：设置 `MORTIS_RAG_API_KEY=<你的key>`（亦兼容旧名 `VAULT_MCP_API_KEY`），配置里保持 `${MORTIS_RAG_API_KEY}` 即可
   - 或直接在配置里写死 `${别的环境变量名}`（支持 `${ENV_VAR}` 插值）
3. `[reranker]`：要精排就 `enabled = true`（bge-reranker-v2-m3 免费）
4. `[vector]`：默认 `backend = "memory"`（向量驻内存）；想省内存改成 `"sqlite_vec"`（需先装 `mortis-rag-mcp[vec]`，首次切换自动迁移旧缓存，零重嵌）
5. 分发给别人的库文件夹想连缓存一起带走：`[cache]` 里 `placement = "vault"`
6. （可选）PDF/Office 文档摄取（MinerU）：
   - **默认关闭**，若仅检索 Markdown 笔记无需配置。
   - 如需检索 PDF/Office 文档，在 `config/app.toml` 中将 `[ingest] enabled = true`。
   - **配置 MinerU Token**：
     - **推荐（v4 高精度通道）**：前往 [mineru.net](https://mineru.net) 免费获取 API Token，设置系统环境变量 `MINERU_API_TOKEN=<你的Token>`（或在 `[ingest]` 中填写 `api_key = "你的Token"`），享受每日 1000 页额度与大文件支持。
     - **免登测试**：不填 `api_key` 自动走轻量免登通道（适合单次 20 页内的小文件体验）。
   - 解析产物自动存放于 `.mortis-parsed/` 独立目录，不会修改或污染原笔记。

## 3. 接入 MCP 客户端

服务以 stdio 运行，由客户端拉起。任选一种：

**WorkBuddy / 通用自定义连接器（JSON）**

```json
{
  "mortis-rag-mcp": {
    "command": "C:\\path\\to\\Mortis-RAG-MCP\\.venv\\Scripts\\python.exe",
    "args": ["-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", "C:\\path\\to\\Mortis-RAG-MCP\\config\\app.toml"],
    "env": {
      "PYTHONPATH": "C:\\path\\to\\Mortis-RAG-MCP",
      "MORTIS_RAG_API_KEY": "你的key",
      "MINERU_API_TOKEN": "可选，用于PDF解析的MinerU密钥"
    }
  }
}
```

**Codex / TOML 配置**

```toml
[mcp_servers.mortis_rag_mcp]
command = "C:\\path\\to\\Mortis-RAG-MCP\\.venv\\Scripts\\mortis-rag-mcp.exe"
args = ["--serve-mcp-stdio", "--app-config", "C:\\path\\to\\Mortis-RAG-MCP\\config\\app.toml"]
enabled = true
```

> 路径按你的实际 clone 位置替换。装过包的话 `mortis-rag-mcp` 在 PATH 上可直接用。

## 4. 初始化知识库（关键一步）

MCP 连上后，对 AI 说一句（或手动发 tools/call）：

```json
{"name": "kb_init", "arguments": {"path": "D:\\我的笔记", "name": "我的笔记"}}
```

- 这一步会把文件夹**注册进 `~/.mortis_rag_mcp/vaults.toml`**（持久化，重启不丢），后台建立索引并开始监听文件变化。
- 想分库管理（比如"工作"、"世界观"分开搜）：每个文件夹各 `kb_init` 一次。
- 验证：`kb_list` 列注册库，`kb_stats` 看 files/chunks/failed_files。
- 搜索：不传 `vault_path` 时自动跨全部非 solo 注册库检索；结果里的 `vault` 字段标明命中哪个库。想让某个库不参与全局检索（私密库等）：用 `kb_init_solo` 注册或转换，显式传 `vault_path` 才能搜它。

## 5. 安装配套 Skill（可选，推荐 AI 助手用户）

仓库 `skills/mortis-rag-mcp/SKILL.md` 是配套的调用技能（教 AI 正确路由、避坑 rebuild 限流等）。按你的助手平台的 skills 目录放置：

- **WorkBuddy**：复制到 `~/.workbuddy/skills/mortis-rag-mcp/SKILL.md`（Windows 即 `C:\Users\<你>\.workbuddy\skills\`）
- 其他支持 SKILL.md 规范的 agent（Claude Code / OpenCode 等）：复制到对应 skills 目录

```powershell
Copy-Item .\skills\mortis-rag-mcp\SKILL.md "$env:USERPROFILE\.workbuddy\skills\mortis-rag-mcp\SKILL.md"
```

装好后，对 AI 说"搜知识库 / 查笔记 / kb_search xxx"即可自动触发。

## 6. 日常使用速查

| 动作 | 工具 |
|---|---|
| 注册新知识库 | `kb_init {path, name?}` |
| 注册独立库（不参与全局检索） | `kb_init_solo {path, name?}`（0.6.0） |
| 看有哪些库 | `kb_list` |
| 设置库描述（引导定向选库） | `kb_describe {vault_path, description}`（0.7.0） |
| 搜索（跨库） | `kb_search {query, preview?, compact?, budget_bytes?}` |
| 搜索（指定库/多库定向） | `kb_search {query, vault_path? vault_paths? preview?, compact?}`（0.7.2/0.8.1） |
| 只搜某目录 / 某标签 / 某时间段 | `kb_search {query, path_prefix? tags? mtime_after? mtime_before?}`（0.5.0） |
| 翻页 / 预算分批 | `kb_search {query, offset, limit, group_offsets?}`（0.5.0/0.8.1） |
| 「这个库更重要」 | `kb_set_weight {vault_path, weight}` + 可选 `kb_search {group_by_vault: true}`（0.5.0） |
| 读原文 / 读章节 | `kb_read {source, vault_path, start_line?, end_line?, heading?}`（0.8.1 支持 heading） |
| 摄取 PDF / Office 文档 | `kb_ingest {action: "pending" / "submit" / "status", vault_path?}`（0.7.0，需配置开启） |
| 排除私密笔记 | `kb_exempt {action: "add_pattern" / "exempt_file"}` |
| 索引出错了 | 看 `kb_stats` 的 `failed_files`（0.5.0 起重启也不丢）；反复调 `kb_stats` 触发增量补齐 |
| 换设备 / 换目录迁移 | 旧机器 `kb_export` → 新机器 `kb_init` + `kb_import`（0.5.0，导入后 0 次重新 embedding） |
| 换 embedding 模型 | `kb_rebuild`（高危：全量重新 embedding，注意 API 限流，见 SKILL.md） |

## 7. 常见问题

- **搜索结果为空**：先 `kb_list` 确认库已注册且 `exists=true`（solo 库不参与全局检索，需显式传 `vault_path`）；再 `kb_stats` 确认 files>0。
- **failed_files 有值**：多为 embedding API 限流/网络错误。0.5.0 起请求自动重试（指数退避、429 遵循 Retry-After）并按批切分，单次限流不再打垮整个索引；仍失败就隔几分钟反复 `kb_stats` 让增量 sync 自动补。
- **换设备**：简单场景 clone → 装包 → 配 key → 对笔记文件夹 `kb_init`（首次全量建索引，花一次 embedding 钱）；想省这笔钱就先在旧机器 `kb_export` 导出索引快照，新机器 `kb_init` 后 `kb_import` 导入——导入后 0 次重嵌。
- **Windows 中文乱码**：服务端已强制 stdio UTF-8；确保客户端也以 UTF-8 收发。

## 8. 升级已有部署（Windows 避坑指南）

当仓库更新代码并重新安装时（例如通过 `git pull` 拉取更新后），Windows 系统常因进程占用导致升级报错，请参考以下指引：

### 8.1 现象与原因
- **安装报错**：若 MCP 客户端（如 Claude Desktop、Codex、WorkBuddy 等）处于开启或连接状态，Windows 操作系统会锁定正在运行的控制台入口文件（`mortis-rag-mcp.exe`），导致重新安装时报 `PermissionError: [WinError 5] 拒绝访问`。
- **更新不生效**：仅执行 `git pull` 只更新了磁盘上的源文件，若后台正在运行的 Python 进程未退出，已加载到内存的模块并不会自动热加载，必须完全重启服务进程。

### 8.2 安全升级步骤
1. **先在客户端关闭或禁用连接器**：
   - 在客户端设置中临时关闭/停用 mortis-rag-mcp 连接；
   - 确认进程已退出（单纯“重启终端”不一定能释放客户端后台托管的子进程）。
2. **（可选）只读定位占用进程**：
   - 打开 PowerShell，运行只读命令查看是否仍有残留实例：
     ```powershell
     Get-CimInstance Win32_Process |
         Where-Object { $_.Name -in @('mortis-rag-mcp.exe', 'vault-mcp.exe') -or $_.CommandLine -like '*mortis_rag_mcp*' } |
         Select-Object ProcessId, Name, ExecutablePath, CommandLine
     ```
   - 若客户端已关闭但仍有残留进程，确认 PID 归属本项目后可执行 `Stop-Process -Id <确认过的PID>` 释放。若客户端有自动重启守护，必须先在客户端停用连接器，避免循环 kill。
3. **使用虚拟环境的 Python 执行安装**：
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -e .
   ```
   *注意：切勿使用 `pip uninstall` 尝试规避锁，锁未释放时卸载同样会失败；系统重启仅作为最后排障手段。*
4. **推荐更稳妥的客户端连接器配置**：
   在客户端中直接使用虚拟环境中的 `python.exe` 配合模块启动参数，可避免 console exe 入口文件被锁：
   - `command`: `C:\path\to\Mortis-RAG-MCP\.venv\Scripts\python.exe`
   - `args`: `["-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", "C:\path\to\Mortis-RAG-MCP\config\app.toml"]`
5. **验证新版本**：
   重新启用客户端连接器，发起 MCP 连接，确认 initialize 返回的版本号或调用 `kb_list` 正常返回。

