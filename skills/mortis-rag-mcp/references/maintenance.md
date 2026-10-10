# 管理、迁移与恢复（v0.9.0，按需读取）

仅在注册、排除、维护、迁移或诊断任务中读本页；正常检索沿入口及检索/媒体参考执行。
只执行用户指定库、动作与写入范围；查询不授权上传/改配置，注销不授权清事实，迁移不授权覆盖。不另建审批系统。

## 16 个工具按任务分组

|任务|工具|
|---|---|
|注册与维护（7）|`kb_init`、`kb_init_solo`、`kb_remove`、`kb_describe`、`kb_set_weight`、`kb_exempt`、`kb_rebuild`|
|清点与状态（3）|`kb_list`、`kb_list_files`、`kb_stats`|
|快照迁移（2）|`kb_export`、`kb_import`|
|解析任务（1）|`kb_ingest`|
|检索与回读（3）|`kb_search`、`kb_read`、`kb_read_media`|

需要清单、维护进度或失败原因才查状态，不当查询前置预检。

## 注册、solo、移除与元数据
- `kb_init` 用绝对 `path`，可带 `name/description`；后台索引，注册成功不表示 ready。重复注册不靠 remove/init 消除提示。
- `kb_init_solo`：未注册则建库；普通库原地转 solo；已 solo 幂等确认。转换不动索引/监听，`name` 仅新注册生效。
- 取消 solo 走 `kb_remove(purge_cache=false)` → `kb_init`；恢复需保留的名称、description、weight，不当元数据无损转换。
- `kb_remove` 必填 `path`：停监听和摄取 manager、注销，不删笔记目录。默认保留缓存；`purge_cache=true` 只清派生层，保留文档事实/control 账本，以 `cache_purged` 判断清理结果。
- 16 工具没有解析事实 purge 工具，不臆造参数；`kb_rebuild` 同样保留事实，不等于清空库。
- `kb_set_weight(vault_path, weight)` 只改跨库排序；`kb_describe(vault_path, description)` 写非空具体描述；均不重嵌、不修漏检。

## 豁免：选对写入面
- `kb_exempt`：`list/check` 查看；`add_pattern/remove_pattern` 要 `pattern`，写 `.vaultignore`；`exempt_file/unexempt_file/check` 要库内相对 `source`。
- 默认 `frontmatter` 改标头 `rag: false`；不改正文或目标为 `.txt`/解析源则选 `ignore_file`。解除后仍可能被其他规则排除，按 `check` 判断。
- 添加排除即时撤销可见性，取消后等增量恢复，不 rebuild；solo 不代替文件排除。
```json
{"name":"kb_exempt","arguments":{"vault_path":"已注册库","action":"exempt_file","source":"日记/今天.md","method":"ignore_file"}}
```

## 快照与导入：三个 flag 各管一件事
- `kb_export` 要启用 cache 且有索引，`out_path` 用新的绝对 `.zip` 路径。目标先注册再导入；遇 `IMPORT_BUSY/IMPORT_CONFLICT` 先查 writer/冲突，不反复强导。
- 快照不替代源目录、注册表/配置、完整 cache/control（文档代、blob、journal）的备份；另存完整副本及 SHA256。
- **信任** `trust_parsed_documents=true`：确认可信备份并核源 SHA 后启用；默认隔离事实，源不符仍不激活。
- **替换** `replace=true, confirm_replace=true`：已有 committed 文档默认拒绝；用户要求覆盖并明确损失后同传，旧代为 `retained_backup`。
- **兼容** `force=true`：允许 profile/切块不相容迁移，可能丢向量重算；不等于信任、替换或解除 unknown，不承诺零费用。
- v1/v2 不发布包内正文/FTS；`files=0/chunks=0/text_published=false` 可是正常结果。相容向量待本机源核验后复用，导入成功不等于 ready。
```json
{"name":"kb_import","arguments":{"vault_path":"已注册目标库","snapshot":"C:\\Backups\\vault.zip"}}
```

## 成功标志之后看 `index_state/next_action`

|状态|下一步|
|---|---|
|`empty`|核注册、本地源与排除规则；已注册就等同步，不重复 init。|
|`rebuilding`|等后台工作；明确的派生失败先修再同步，不紧密自旋。|
|`unverified`|核隔离事实的源 SHA/归属，按授权重解析或可信导入，不自动激活。|
|`ready`|可见索引就绪，仍检查相关 `failed_files/next_action`，不等于所有资产健康。|

- 导入登记刷新，以 `refresh_requested/next_action` 跟踪；stats 仅请求后台刷新，不前台 sync。
- `kb_list_files` 默认列全部已索引源，大库用 `limit/offset/next_offset`；不证明磁盘全收录。`skipped_unsupported` 不等于解析失败，另看 `failed_files/indexing_error`。

## 格式、切块与向量空间
- `.md/.markdown/.txt` 原生索引，无需 docs extras/MinerU；PDF/DOCX/PPTX/XLSX 本地解析需 `mortis-rag-mcp[docs]`；摄取/路由见媒体参考。
- 新库默认 `estimated_tokens`，为未离线校准的 Unicode 估算，非实际 tokenizer；未显式选新 mode 的旧配置/缓存保留 `legacy_chars` 和参数。保旧地址则显式保旧 mode。
- 超上限完整表格标 `embedding_disabled` 保留正文，不反复补嵌。
- 模型、endpoint/revision、维度、query/document template、预处理参与空间身份；同名/同维不代表可混用。模板各须且仅须一个完整 `{text}`。
- 普通变更/少量失败用增量补缺；换空间/切块则改有效配置、重启后 sync 按新身份对账，确需全量清派生层才 rebuild。
- rebuild 可重算全库 embedding，但不重新调用 MinerU、不改 committed 正文/occurrence。正常配置不为 profile 漂移新增审批；旧 pending 标志不阻塞，保留的 `--approve-reembedding` 不是必经步骤。
- 控制面不可用、revoked 或未决请求仍阻断相关外发；不清账本/换 cache 身份绕过。

## 配置、STATUS 与诊断
- 配置优先级：`--app-config` > `MORTIS_RAG_CONFIG` > `VAULT_MCP_CONFIG` > `~/.mortis_rag_mcp/config.toml` > `~/.vault_mcp/config.toml` > 默认；不合并所有文件。
- 显式/选中环境路径缺失即失败；不查未命中的低优先级项，无配置走默认合法。key/注册表/cache 覆盖变量新名优先旧名。
- STATUS 成功且 7 天内直接工作；缺失/过期/失败也不自动预检。实际错误优先，只查有关层；生成 STATUS 的“缺失即 doctor”旧提示不扩展授权。
- 用户要求诊断才 doctor：探真实端点、写 STATUS/status.json。当前 CLI 要 `--vault 绝对路径`，诊断仍针对整体配置/注册表；失败一次即报告，不循环探活。
- 注册表拒读/解析未知不是空表；恢复权限/原表，不保存空表覆盖。会话内注册不保证跨重启保留。
- 内存可检索不证明写盘成功；按 `next_action` 的 persistence layer/path/errno 修空间/权限，保存或重建派生层后重开验证，不清事实。

## 请求 journal：unknown 不自动重试
- job_id 与 request_id 分开；`prepared/submission_unknown` 保留记录、查原远端任务，不自动重放。`attempt` 增长不是重发许可。
- `pending_paid_requests/embedding_paused_reason` 是索引批量暂停，不是坏向量或所有查询都停。
- `--list-requests` 只读；用户决定放弃才 `--abandon-request`，只改本机意图，不取消远端/标 success/重发；再次提交可能重复计费。
```powershell
python -m mortis_rag_mcp --app-config "C:\Config\app.toml" --list-requests --vault "C:\Notes"
python -m mortis_rag_mcp --app-config "C:\Config\app.toml" --abandon-request REQUEST_ID --vault "C:\Notes"
```
- 上述查看/放弃是独立动作，勿无条件连跑；CLI `--vault` 用绝对路径，一次一个 action，`--apply` 仅配迁移。
- `kb_ingest(action="retry", job_id=实际ID)` 只恢复 virtual 的 failed/cancelled；failed 保有效 checkpoint，cancelled 清失去保护的段。unknown 先解决请求。

## 迁移、降级与事实恢复
- 停服务/writer、关句柄，保完整旧副本并在副本验证；新旧 writer 不并行改同库。旧版读不到 virtual-only 事实，降级恢复旧备份，不原地降 schema。
- 旧目录独占时 rename，失败回读旧路径；两侧并存可能只迁注册表，核实际配置/缓存落点。退到 0.7.1 前恢复用户/cache 两个旧名及旧变量，勿覆盖已有目录。
- `--migrate-ingest --vault 绝对路径` 默认 dry-run：不迁事实、不联网、不重嵌；但 CLI 写开/可能初始化文档库，不当无副作用预检。用户授权实际迁移才加 `--apply`。
- 核 frontmatter、源 SHA、安全路径、唯一 done 账本与媒体归属；`ready` 仅候选，`pending_manual` 待核，`already_migrated` 跳过。
- 不删旧镜像，仅精确排除已证明且已迁镜像；不整目录忽略 `.mortis-parsed/`，不覆盖 modern/hidden/unverified 事实。
- 活动数据库缺失/零长度恢复完整备份，不建空库替代。旧坐标/全文件 SHA 错误在副本重摄取成新 revision；rebuild 只修派生状态，unknown 先核请求。
- 音频转录/ffmpeg 转码及 `[audio]/audio_enabled` 已移除；旧键忽略，不靠恢复配置/安装 ffmpeg 重开。残留音频 embedding/occurrence 读取不等于转录。
