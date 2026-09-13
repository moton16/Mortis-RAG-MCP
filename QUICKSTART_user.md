# Mortis'RAG MCP 快速开始（下载后初始化指南）

> 面向拿到本仓库的新用户 / 新设备：从 clone 到知识库检索可用，大约 5 分钟。
> 全程只需要：Python 3.10+、一个 embedding API key（推荐硅基流动免费档 bge-m3）。

## 0. 前置说明

- 你的笔记目录、注册表、API key **全部在你自己的机器上**，仓库里不含任何个人配置（`config/app.toml` 已被 .gitignore 排除）。
- 首次启动后，所有知识库关系由**用户级注册表** `~/.vault_mcp/vaults.toml` 管理，不写在代码或仓库里。

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
# python -m pip install numpy
# 可选：磁盘向量后端（向量不占内存，13k 切片约省 55MB RAM）：
# python -m pip install "mortis-rag-mcp[vec]"   # 然后配置里开 [vector] backend = "sqlite_vec"
```

## 2. 配置

```powershell
Copy-Item .\config\app.toml.example .\config\app.toml
```

编辑 `config/app.toml`，按需修改：

1. `[embedding]`：`mode` 改 `"external"`、填 endpoint/model/dimension（硅基流动 bge-m3 为 `BAAI/bge-m3`、1024 维、`send_dimensions = false`）
2. API key 二选一：
   - 环境变量（推荐）：设置 `VAULT_MCP_API_KEY=<你的key>`，配置里保持 `${VAULT_MCP_API_KEY}` 即可
   - 或直接在配置里写死 `${别的环境变量名}`（支持 `${ENV_VAR}` 插值）
3. `[reranker]`：要精排就 `enabled = true`（bge-reranker-v2-m3 免费）
4. `[vector]`：默认 `backend = "memory"`（向量驻内存）；想省内存改成 `"sqlite_vec"`（需先装 `mortis-rag-mcp[vec]`，首次切换自动迁移旧缓存，零重嵌）
5. 分发给别人的库文件夹想连缓存一起带走：`[cache]` 里 `placement = "vault"`

## 3. 接入 MCP 客户端

服务以 stdio 运行，由客户端拉起。任选一种：

**WorkBuddy / 通用自定义连接器（JSON）**

```json
{
  "mortis-rag-mcp": {
    "command": "C:\\path\\to\\Mortis-RAG-MCP\\.venv\\Scripts\\python.exe",
    "args": ["-m", "vault_mcp", "--serve-mcp-stdio", "--app-config", "C:\\path\\to\\Mortis-RAG-MCP\\config\\app.toml"],
    "env": { "PYTHONPATH": "C:\\path\\to\\Mortis-RAG-MCP", "VAULT_MCP_API_KEY": "你的key" }
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

- 这一步会把文件夹**注册进 `~/.vault_mcp/vaults.toml`**（持久化，重启不丢），后台建立索引并开始监听文件变化。
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
| 搜索（跨库） | `kb_search {query}` |
| 搜索（指定库） | `kb_search {query, vault_path}` |
| 只搜某目录 / 某标签 / 某时间段 | `kb_search {query, path_prefix? tags? mtime_after? mtime_before?}`（0.5.0） |
| 翻页 | `kb_search {query, offset, limit}`（0.5.0） |
| 「这个库更重要」 | `kb_set_weight {vault_path, weight}` + 可选 `kb_search {group_by_vault: true}`（0.5.0） |
| 读原文 | `kb_read {source, vault_path}` |
| 排除私密笔记 | `kb_exempt {action: "add_pattern" / "exempt_file"}` |
| 索引出错了 | 看 `kb_stats` 的 `failed_files`（0.5.0 起重启也不丢）；反复调 `kb_stats` 触发增量补齐 |
| 换设备 / 换目录迁移 | 旧机器 `kb_export` → 新机器 `kb_init` + `kb_import`（0.5.0，导入后 0 次重新 embedding） |
| 换 embedding 模型 | `kb_rebuild`（高危：全量重新 embedding，注意 API 限流，见 SKILL.md） |

## 7. 常见问题

- **搜索结果为空**：先 `kb_list` 确认库已注册且 `exists=true`（solo 库不参与全局检索，需显式传 `vault_path`）；再 `kb_stats` 确认 files>0。
- **failed_files 有值**：多为 embedding API 限流/网络错误。0.5.0 起请求自动重试（指数退避、429 遵循 Retry-After）并按批切分，单次限流不再打垮整个索引；仍失败就隔几分钟反复 `kb_stats` 让增量 sync 自动补。
- **换设备**：简单场景 clone → 装包 → 配 key → 对笔记文件夹 `kb_init`（首次全量建索引，花一次 embedding 钱）；想省这笔钱就先在旧机器 `kb_export` 导出索引快照，新机器 `kb_init` 后 `kb_import` 导入——导入后 0 次重嵌。
- **Windows 中文乱码**：服务端已强制 stdio UTF-8；确保客户端也以 UTF-8 收发。
