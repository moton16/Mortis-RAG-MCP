# 媒体与虚拟文档按需参考（main v0.9.0）

仅在读取图片、判断跨模态召回、摄取外部文档或恢复任务时加载；实际调用以当前服务 schema 为准。
本页区分“已发布解析事实”“派生索引可用”“原生媒体向量”和“宿主展示”，不以其中一项代替其余验收。

## 1. 先选调用，再判断远端影响

- 已有文档：定向 `kb_search` → `kb_read` → 用返回的媒体地址调用 `kb_read_media`；不为查看图片自动摄取。
- `[ingest] enabled=false`、`auto_watch=false` 均为默认值；状态查看不是开启摄取或上传的授权。
- `kb_ingest(action="submit")` 是异步解析；`vault_path` 经统一寻址接受注册库名或绝对路径。显式给 `sources`，避免省略或空列表变成全库待解析文档扫描；CLI 的 `--vault` 则仍用绝对路径。
- `pending` 只列候选，`status` 查任务；`done` 表示解析事实发布，不保证 embedding/index 已 ready。
- 本地 PDF/DOCX/PPTX/XLSX 解析依赖可选 docs 组件；`routing="auto"` 在允许云路由时可能转 MinerU。
- `network_policy="local_only"` 限制解析路由，不等于 embedding/rerank 离线；外部模式搜索、同步也可能调用端点。
- `kb_read` 不触发同步/embedding；`kb_read_media` 读取库中资产，不下载 URL；preview 可生成并缓存派生变体。
- 不因 STATUS 缺失自动运行 `--doctor`；该显式诊断可能访问真实端点。

## 2. native、proxy 与 embedding space

- `media_proxy` 是已有 caption/OCR/邻近正文构成的确定性文本代理；它不识别像素、不生成 OCR/图注。
- proxy 可以参与词法/文本向量召回；“搜到图注”不等于原生以文搜图，也不证明视觉语义理解。
- `media_native` 来自媒体字节的原生 embedding；缺媒体向量不会把 synthetic 描述送文本端点冒充 native。
- 同一 occurrence 的 native/proxy 去重；成功 native 对应的 proxy 停止文本嵌入，但保留词法入口。
- native 要有显式模态、alignment、媒体 preprocess/endpoint revision、MIME、限额和端点证据引用。
- 未声明媒体模态则没有 native provider；声明不完整则 route 不可用，保留文本/proxy 路径，不自动探活。
- 进入同一检索空间还要求正文与媒体的非空 `alignment_space_id` 一致、`effective_dim` 一致。
- 相同模型名或相同维度均不足以证明对齐；adapter 的存在也不证明任意模型品牌/部署已验收。
- `space_id` 是完整 resolved profile 的 fingerprint；endpoint、模板、维度、模态等身份变更会影响向量复用。
- 查询/文档模板分别应用；跨库查询只复用相同 space 的查询向量，勿手工混用不同 profile 的向量。
- 换模型/模板/切块可能重嵌并调用远端；不要沿用“任意升级零费用”的承诺或为补媒体自动全量 rebuild。

## 3. 虚拟 source、版本与两种分页

- 默认 `ingest.storage="virtual"`：解析正文/媒体入文档库，原始文件不改写，不新生成 `.mortis-parsed/` 镜像。
- `source` 仍是原始库内相对 POSIX 路径，如 `教材/电路.pdf` 或 `images/装置.png`，不是缓存路径或镜像路径。
- `kb_read` 读已 committed 的 rendered Markdown；其行号不是 PDF 页码/Office 物理坐标。
- 查看 `source_kind`、`revision_id`、`render_sha256`、`line_basis`、`quality`/`coverage`；缺信息不补造。
- 普通命中可用 `chunk_id` 回读；compact 没有 id，用 `vault` + `source` + `lines` 回读。
- proxy 合成正文不是原文；缺 `anchor_available` 时勿把其 synthetic 行号当原文位置。
- 正文截断沿 `next_start_line` + `next_start_char` 续读，不简单使用 `end_line+1`。
- 媒体地址从 `media_refs` 取 `occurrence_id`，配合该页的 source/vault/revision；不猜图片路径或 occurrence。
- refs 每页默认 20 项（配置最多 100）；续页把 `next_media_refs_offset` 传为 `media_refs_offset`。
- `kb_read` 没有 `revision_id` 入参：每页核对 `revision_id`/`media_refs_revision_id`，换版就丢旧 offset，从 0 重读。
- 保持正文读取范围一致并核对 `media_refs_text_range`；refs 当前按整份 revision 分页，不是该段配图过滤器。
- 固定版本核验防止旧 chunk 与新媒体混用；`MEDIA_UNAVAILABLE`/stale 时重新读 source、重新选择地址。
- `allow_stale=true` 仅用于仍可访问的虚拟 source 存档正文；旧 chunk 严格核版本，媒体读取没有此绕过参数。

## 4. `kb_read_media` 的真实参数与预算

必填：`vault_path`（注册库名或绝对路径）、`source`、`revision_id`、`occurrence_id`；source 不接受 URL。
可选：`representation="metadata"|"inline"`（默认 metadata），`variant="preview"|"original"`（默认 preview）。
`budget_bytes` 是整数，默认 **2097152（2 MiB）**，范围 **1..8388608（8 MiB）**；没有 `inline=true` 参数。
预算计算成功响应的完整 UTF-8 JSON-RPC 包络，包含 request id、元数据、MCP content 和 base64 开销，不是原图字节数。
metadata 默认不读取完整 blob；inline 返回标准 text + image/audio content 块，不保证客户端实际显示。
光栅 inline 支持 PNG/JPEG/WebP；SVG/其他 MIME 不因扩展名相似而被当作可内联图片。
优先 preview；预览不可用会报错，不静默返回 original。确需原图时显式选择 original。
`MEDIA_TOO_LARGE` 整包拒绝，不截断 base64；按返回信息改 metadata/preview 或提高预算（仍受 blob 读取上限约束）。
返回的 `host_media_verified=false` 不是图片不存在，而是没有宿主展示验收事实。

需要定位/检查元数据时使用下例；已有确切地址且用户要求图片内容时可直接 inline，不固定多加一次 metadata 调用。下列占位地址必须替换成 `kb_read` 实际返回值：
```json
{"name":"kb_read_media","arguments":{"vault_path":"目标库","source":"教材/电路.pdf","revision_id":"返回的revision","occurrence_id":"返回的occurrence"}}
```
需要图片内容时：
```json
{"name":"kb_read_media","arguments":{"vault_path":"目标库","source":"教材/电路.pdf","revision_id":"返回的revision","occurrence_id":"返回的occurrence","representation":"inline","variant":"preview","budget_bytes":2097152}}
```

## 5. 独立图片与音频边界

- PNG/JPEG/WebP 独立图片已支持 **显式 submit + virtual**；继续使用已有队列、blob/occurrence 发布与索引链。
- 图片不进 `pending`/auto_watch 全库扫描；legacy 存储拒绝图片提交；不支持的后缀或 magic 不符会明确失败。
- `caption` 仅是用户提供的标题/说明，未提供时回退文件名；没有 OCR、自动 caption、外链下载或原文件改写。
- 图片代理正文是 `user_metadata`，没有原文字符锚点/页码；宽高不是正文坐标，不宣称图片内文字已可检索。
- caption 发布后成为事实；当前发布前只保存在进程内，若中途重启可回退文件名，应核对最终正文/元数据。
- 图片摄取成功不自动证明 native 对齐或宿主展示；是否获得原生向量仍取决于第 2 节的实际 provider。
```json
{"name":"kb_ingest","arguments":{"action":"submit","vault_path":"目标库","sources":["images/装置.png"],"caption":"用户提供的实验装置说明"}}
```
- 新音频转录/ffmpeg 解码摄取已移除，旧 `[audio]`/`audio_enabled` 键忽略；勿提交音频期待转录。
- 保留原生音频 embedding transport、旧/已有音频 occurrence 展示，不等于新音频摄取可用。
- 已发布且带时间范围的可验证 PCM WAV 段可显式 inline original；默认图片 preview 不代替音频段。
- 文档媒体以真实解析结果为准；MinerU agent 通道仅正文，不应承诺图片字节、结构化 JSON 或页级锚点。

## 6. 恢复与 refresh 决策

- virtual 的 `retry` 必须带既有 `job_id`，仅普通 failed/cancelled → queued；不重复 submit 相同哈希绕过失败。
```json
{"name":"kb_ingest","arguments":{"action":"retry","vault_path":"目标库","job_id":"返回的job_id"}}
```
- 仅兼容且仍有效的完成 checkpoint 可复用；cancelled 重试会清理失去 blob 保护的 checkpoint，不保证零重复解析。
- `SUBMISSION_UNKNOWN` 或远端 submission 阶段结果不明，即使 state=failed/cancelled 也不重排、不自动补发；先核原任务。
- job_id 与请求台账 request_id 是两套身份；台账的 `prepared`/`submission_unknown` 未决意图会阻止同身份重发。
- CLI `--list-requests --vault "C:\YourVault"` 只读本机台账；`--vault` 用绝对路径，不是注册库名。
- 显式 `--abandon-request REQUEST_ID --vault "C:\YourVault"` 只放弃该本机意图，不取消远端、不变成功、不自动 retry。
- 放弃后再提交可能重复处理/计费；放弃 request 也不自动解除 ingest job 的 unknown，按返回 `next_action` 处理。
- warm 索引 refresh 在后台排队/构建时保留旧可用结果；状态 `rebuilding`/`indexing_in_progress` 不要求先清缓存。
- 重新解析的新候选在 commit 前不替换 active；保留旧事实不等于绕过源 SHA/权限/豁免核验，新 revision 后旧地址失效。
- 正文成功、媒体失败保留正文与成功媒体批次；已确认合同失败与 unknown 分开处理，后者不自动重发。

## 7. 核实入口（维护本页时）

- schema/refs：`mortis_rag_mcp/server.py:280-306,345-356,1781-1839`；预算：`mortis_rag_mcp/_server/media_dispatch.py:94-124`。
- native/proxy/space：`mortis_rag_mcp/_indexer/sync_engine.py:470-489,824-858`；`tests/test_media_production_wiring.py:253-313,479-501`；`tests/test_text_profile_wiring.py:120-140`。
- 独立图片：`mortis_rag_mcp/ingest/images.py:85-129`；`tests/test_image_source_ingest.py:84-136,174-193`。
- retry/台账/refresh：`mortis_rag_mcp/ingest/worker.py:1226-1268`；`tests/test_request_lifecycle.py:51-72`；`tests/test_read_stale.py:24-67`；`tests/test_e20_media_outcome.py:125-158`。
