<!-- /autoplan restore point: C:\Users\芝士雪豹\.gstack\projects\moton16-Mortis-RAG-MCP\feat-v0.8.1-autoplan-restore-20261005-a8fbb4d.md -->

# v0.8.1 执行计划：Issue #5 剩余工作 + Issue #6 全量落地

> 审核日期：2026-10-05，America/New_York。
> 代码基线：`feat/v0.8.1`，HEAD `a8fbb4d`；对照基线 `origin/main` / v0.8.0 `82989c8`。
> 本轮只审核、重写计划与验证现有代码，不实现功能、不提交、不推送、不操作真实知识库。
> 状态：**文档编写与审核完成，可按§3顺序实施**。本轮不实现代码、不发布。
> 需求来源：[GitHub #5](https://github.com/moton16/Mortis-RAG-MCP/issues/5)、[GitHub #6](https://github.com/moton16/Mortis-RAG-MCP/issues/6)。
> #5 最后更新 2026-09-29；#6 最后更新 2026-10-04。本轮通过 Exa 读取正文、`gh issue view` 核对状态与评论，两者均 OPEN、无评论。

## 0. 接手说明

**本轮范围收口（用户2026-10-05最新指示）**：优先完成#5剩余与#6本身，不把额外防御性工程作为本版前置。不新增摄取worker所有权/租约机制、上传临时副本、检索事务代际或锁框架改造。这些审查建议只作为后续记录；本版继续复用现有manager、文件锁、同步与读快照实现。历史已确认的20MiB策略、默认自动关闭、现有沙箱/ignore规则保留，不属于新增架构。

### 0.1 这份文档如何执行

1. 先读本节、§1 当前事实、§2 契约，再从 §3 的第一个未完成任务顺序执行。
2. 每张任务卡均按「前置条件 → 文件/符号 → 编码步骤 → 测试 → 验收 → 提交边界」执行。不要自行替换成另一套设计。
3. `[x]` 表示在本轮基线中已核实；`[ ]` 表示还需执行。写了测试规格不等于测试已存在，写了方案不等于功能已实现。
4. 每卡通过后就地打勾，记录实际 commit、测试数量、命令与残余风险；技术账写 `docs/Changelog_developer.md`，不要只改 Worklog。
5. 符号名是定位主键，行号只是 `a8fbb4d` 的辅助锚点。改完不沿用旧行数。
6. 上一版 1865 行计划及三轮审核可以用 `git show a8fbb4d:docs/v0.8.1/PLAN.md` 查看。完整原文还原点见文件首行，SHA256 为 `397343dd5f816b2b2eeef8e02487f4fa27207047654936d5d60704903939d805`。
7. 本版合并了历史有效裁定、删除了相互冲突的过期施工指令。历史观点仅作审计，不得覆盖本版任务卡。

### 0.2 先读哪些文件

| 顺序 | 文件 | 用途 |
|---|---|---|
| 1 | `docs/Quick-start_developer.md` | 分层、零依赖、测试与记账约定 |
| 2 | `docs/PROJECT_GUIDE.md` | 架构、缓存不变量、协议、安全与并发 |
| 3 | `docs/Docs_Folder-descriptions.md` | 五份主文档与版本目录的分工 |
| 4 | 本文件 | 本版本唯一执行清单 |
| 5 | `docs/Execution-plan_developer.md` | 未排期项；不能把其中“现状”当作已实现证明 |
| 按需 | `docs/v0.8.1/Worklog_2026-09-29.md` | 历史尝试、已提交工作与当时量测，不是当前工作树 |

源码优先于现状文档。主文档还含旧包名、旧线程流程和旧工具表，§6 指定收口位置；不为这些偏差开展无关重构。

### 0.3 开工命令与环境

在仓库根目录的 PowerShell 执行，每行单独运行：

```powershell
Set-Location 'D:\dependency\Mortis-RAG-MCP'
git status --short --branch
git branch --show-current
git log --oneline -10
git diff origin/main...HEAD --stat
git stash list
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe -m pytest tests/test_version_sync.py -q
```

检查结果：分支应为 `feat/v0.8.1`；本轮开始时工作树干净。已有 `stash@{0}` 是历史 MinerU header 修复，**不要 apply/pop/drop**。若 HEAD 已推进，只核对本文件受影响符号与新增提交，不重做已完成卡。若有用户改动，保留并基于它继续。

venv 不存在时才建；不因为读计划而升级依赖，不启动真实 `--doctor`，不读取用户 key：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install pytest
```

Windows 安装前按 C59 关闭占用该项目入口的 MCP 客户端。可选 numpy/sqlite-vec 不作为基础安装前置。

### 0.4 本轮实际验证

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ingest_worker.py tests/test_ingest_mineru.py tests/test_isolation_guard.py tests/test_kb_read_chunkid.py tests/test_budget_bytes.py tests/test_preview_mode.py tests/test_wikilink_read.py tests/test_anti_contention.py -q
```

- 未设置 UTF-8 的第一次：72 passed / 1 failed。失败是 `test_cache_env_inherited_by_subprocess` 的子进程输出使用本机编码，父进程强按 UTF-8 解码，产生 `UnicodeDecodeError`，随后 `stdout=None`。不是 worker fixture 报错。
- 设置与 CI 相同的 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` 后：**73 passed，10.32s**。
- 恢复后另跑不重叠的8文件：`test_version_sync/test_facade_freeze/test_facade_seam/test_cache_codec_roundtrip/test_watch_integration/test_p5_lifecycle/test_scoped_search/test_multivault`，**49 passed，44.00s**。两组共 **122 passed**，只说明当前已提交功能的靶向基线，不证明待实施功能通过。
- 历史 Worklog 中 worker 的 16 个 fixture setup errors 本轮未复现，不能列为现存阻塞项，更不能以删除 conftest 隔离夹具“修复”。
- 离线 Golden：仓库 `tests/eval/vault`、static384、reranker/ingest/diag关闭、外置独立cache；**Hit@5=1/1，MRR@5=1.000**。Golden只有1条，不能据此声称真实小说/课程库100%命中。
- schema list 基线实测：`origin/main` 12563 bytes，本轮HEAD 13412 bytes（**+6.758%**）；15工具。`{"tools":...}`结果包装为13423 bytes，勿与纯list混比。基线函数用AST从Git对象中只提取 `_tool_definitions` 求值，无checkout/服务启动。
- 未跑全量，未调用外部 embedding/reranker/MinerU，未验证真实 OSS 上传。新功能尚无验证结论。

可复跑命令与已生成隔离配置：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_version_sync.py tests/test_facade_freeze.py tests/test_facade_seam.py tests/test_cache_codec_roundtrip.py tests/test_watch_integration.py tests/test_p5_lifecycle.py tests/test_scoped_search.py tests/test_multivault.py -q
.\.venv\Scripts\python.exe scripts/eval_search.py --golden tests/eval/golden_queries.json --config 'C:\Users\芝士雪豹\.gstack\projects\moton16-Mortis-RAG-MCP\feat-v0.8.1-eval-offline-20261005.toml' --k 5
```

上述外置配置是本机审查产物，其他机器按C70.3创建自己的临时配置；不复制个人绝对路径进公共指南。

## 1. 当前事实与差异审核

### 1.1 已完成工作：不要重复开发

| 状态 | 卡/范围 | 提交 | 源码证据与本轮结论 |
|---|---|---|---|
| [x] | Lane E 转义清理、五主文档白名单 | `e5eb33d` / `96e205f` / `eaceabf` | `.gitignore`、主文档已落地；不用再清理一遍 |
| [x] | C53 MinerU PUT header | `11fa7c5` | `_put_upload` 显式空 Content-Type；本机回环回归测试存在；**真实云端验收仍待做** |
| [x] | C63 版本单一真源 | `23e555a` | 包 `__version__` → `SERVER_INFO`、diaglog；发布仍需把值从 0.8.0 改为 0.8.1 |
| [x] | C54 + P0 测试隔离/竞态测试 | `58cfe6b` | conftest 配置 pin、per-test 缓存 env、NO_STATUS_HOOK；P0 改带超时轮询 |
| [x] | C55 handler 别名 | `65a53ff` | init/solo 认 `path/vault_path/vault/vault_name`，description 与错误说明已补；schema 仍 required path |
| [x] | C56 跨库 chunk 寻址主流程 | `65a53ff` | 含 solo、临时 load_vectors=False 探测、句柄清理、候选歧义报错；安全缺口见 C65 |
| [x] | C57+C62 文件分页/前缀 | `65a53ff` | `total` 过滤后计数、`page_truncated`、`next_offset`；不传分页仍全量 |
| [x] | C64 初步成本控制 | `65a53ff` | 临时库数上限32、循环间时间检查2s；这是软预算，不是单次磁盘操作硬超时 |
| [x] | 版本目录跟踪 | `a8fbb4d` | `.gitignore` 已 `!docs/v0.8.1/`；本文件不再是被忽略的本地草稿 |

C54b 的实际实现是 conftest 覆盖默认缓存根，而非逐个补齐所有测试 TOML。**显式 `[cache] dir` 优先于 env**，所以不能宣传为覆盖所有显式配置；继续保留已有临时目录设置即可。

### 1.2 尚未完成与失效记录

| 范围 | 当前代码事实 | 执行位置 |
|---|---|---|
| A4 完成回调 | worker 调 `on_job_finished(source, out_md)`，server 仍定义单参数回调 | C66，与异步 refresh 共用 |
| C58/C61 | `IngestConfig` 无 `auto_watch/max_file_size_mb`，watch 无 ingest 标志/hook，worker 无 auto_submit | C58a–C58d，**从现行源码实现** |
| C59 | 没有完整 Windows 升级锁说明 | C59 |
| C60/T8 | 版本仍0.8.0，用户日志/开发者记账/使用纪律未收口 | C70/C60 |
| Worklog 的 Lane D | 曾记7个未提交文件与719行增量，但当前工作树干净、HEAD 无这些改动 | 只可参考设计，不得假定代码可直接继续 |
| “下一步直接 /ship” | 代码尚未完成，不具备发布条件 | 先过所有卡与§7门禁 |
| 根 TODOS.md | 历史已裁定不建立第二套待办 | 使用 `docs/Execution-plan_developer.md` |

### 1.3 Issue #6：逐条核验，不按 RFC 字面重复造轮子

| #6 项 | 事实/置信度 | 要做的增量 |
|---|---|---|
| 精简初筛投影 | `Chunk.to_dict(preview=True)` 仍带 id/score/title/metadata/char_count 等，事实成立 | C67：新增 opt-in `compact`，不默认破坏旧 preview |
| 完整 chunk 预算 | `apply_budget` 已对 dict/list 裁剪、JSON 合法；**首条超限会二分裁正文/摘要** | C68：去首条字符串裁剪，明确放不下与分组续页；不是“修损坏 JSON” |
| 读取行号越界 | `indexer.read` 在 `start>end` 时直接 `""` | C69a：具名诊断、文件实际总行数、修复动作 |
| heading 直读 | schema 与 handler **已经有 heading**；当前为同名 chunk 的 min/max 行号并集 | C69b：物理原文标题定位、同级/更高级边界、重复标题歧义、未索引文件 |
| 40s+ 搜索毛刺 | `try_sync_with_guard` 只限制 acquire，拿锁后同步 `_sync_locked()` 可执行网络补嵌 | C66：MCP 读路径请求异步 refresh，读取已有索引；40s生产现象本轮未实测复现 |

具体基线锚点：`models.py::Chunk.to_dict`(99)、`fanout.py::apply_budget`(36)、`indexer.py::try_sync_with_guard`(510)、`indexer.py::read`(958)、`server.py::_kb_read`(1175，heading并集1299)、`server.py::_ingest_manager_for`(785，单参回调792)。

### 1.4 已提交功能中本轮新增审核缺口

1. **C56 未完整探测却宣称唯一**：32库/2s预算、目录缺失、无可用文本缓存等会跳过候选库；当前只要 `hits==1` 即展开。这不能证明另一个未探测库没有同 ID。C65 改为 incomplete 时要求显式库，不自动猜。
2. **零注册库 IndexError**：`_locate_chunk_for_read` 的 `len(entries)<=1` 分支访问 `entries[0]`。C65 改为正常工具错误，引导注册。
3. **load_vectors=False 不是全路径保证**：配置 sqlite_vec、可选后端不可用后回退 memory 时，构造器仍调用 `_load_vectors_cache()`。C65 补 gate 与专项用例。
4. **大库全局分页不是无限游标**：fan-out 每库最多取 `max(top_k,20)`，深 offset 可能候选池先耗尽；现有 grouped next_offset 还把“总返回数”当“各组偏移”。C68 给预算内明确续页契约，不承诺无限遍历全库。
5. **自动失败免重试不能靠500条 job历史**：被剪枝的 failed 会再次被当新文档。C58b 增 source/hash 的有界于源文件数的独立账本，不能沿用“取 max 就绕开剪枝”的旧论断。
6. **标题读取不能依赖切块产物行号**：图片注入、chunk_overlap、同名标题/空章节会让并集与物理章节不同。C69b 读原文定位，不改 chunker 或缓存。

## 2. 范围与契约

### 2.1 保留的历史用户裁定

| 裁定 | 本版执行口径 |
|---|---|
| D1 | 一次发布0.8.1，#5七组需求全部完成；本轮增加#6，不拆版 |
| D2 | `max_file_size_mb=20`，所有摄取入口统一策略上限；`0`显式不限；以 `1024*1024` 计字节，文档注明MiB |
| D3 | 保留配置pin、缓存env、宿主隔离，不逐个大改安全测试配置 |
| D4 | P0竞态搭车已完成；转义清理前置已完成；不再删除其他本地文件 |
| D5 | chunk_id显式寻址覆盖solo；solo结果/候选只显示库名和solo标记，不能展开该库绝对路径；全局搜索仍排除solo，显式Scoped仍可搜solo |
| X3 | 待办用 `docs/Execution-plan_developer.md`，不新建根TODOS/CLAUDE/AGENTS；v0.8.1版本目录已获跟踪例外 |

### 2.2 本轮推荐的确定契约

以下是施工默认方案，用户可在最终确认时覆盖；开发者不要二次选型。

| 维度 | 固定方案 |
|---|---|
| 搜索呈现 | 新增 `compact:boolean=false`；true隐含preview，四键投影 `source/heading/lines/snippet`；普通preview/full的chunk字段不删不改，C66刷新状态可加在外层 |
| compact单库 | 顶层带 `vault`、`vault_name` 以便回读；source始终库内相对posix路径，不含绝对库路径 |
| compact跨库平铺 | 每条另带 `vault`（绝对库标识）供寻址，不重复vault_name；显式Scoped solo遵循原搜索输出口径 |
| compact分组 | vault/vault_name仅在group；每条四键，无重复库字段；不依赖已移除score排序，排序在投影前完成 |
| budget | 保留整个已投影chunk的最长前缀；绝不跳过大的首条，也不裁正文/snippet；预算包括 `_text_content` 的MCP content包装，不包括JSON-RPC id外壳/换行 |
| 分组续页 | 新增可选 `group_offsets:object`，仅group_by_vault=true可用；键为返回的vault标识，值为该组0-based偏移，原样接收group_next_offsets；继续走同一fan-out路径，不切单库排序 |
| 首条放不下 | 空chunks，`truncated=true/returned=0/next_offset=原offset`，短 `budget_hint` 提示开compact或增加预算；不把游标推进到没给用户的chunk |
| envelope放不下 | 合法结果，`budget_exceeded=true`，声明不可满足元数据最低字节数；不删除searched/errors/solo，不假称硬预算已满足 |
| heading | 已有参数升级实现；trim后精确、区分大小写；含选中标题到下一个同级或更高级标题之前；包含子标题 |
| 重复heading | `ValueError` 列出最多5个物理起始行，改用行区间；不默选第一，不新增occurrence参数 |
| 标题语法 | 复用现有ATX/中文章节/Chapter判据、围栏/表格/frontmatter保护；不承诺Setext、GitHub slug、完整Markdown解析器 |
| heading+行号 | 行区间继续优先，heading不参与定位；保持旧参数组合语义，description写清；chunk_id仍与三者互斥 |
| bounds | 行号1-based闭区间；end超EOF钳制；非空文件start超EOF报错；空文件无显式区间合法空读取，显式区间报错且total_lines=0 |
| MCP warm reads | `kb_search`、文件读取、chunk定位、list_files、stats前台不再承担sync；request_refresh后按现有索引读 |
| empty vs cold | 不用 `len(_chunks)==0` 单独判首次构建；`last_sync is None`且无可用缓存才返回indexing，合法空库/全豁免库正常空结果 |
| fresh保证 | “读优先”不是“即时新鲜”或“上一轮完整事务快照”；单请求取稳定chunk集合，FTS/向量仍最终一致，刷新中可含已更新文件与旧文件；query embedding/rerank仍可能联网 |
| auto_watch | 默认false、必须enabled才生效；原生+poll+原生失败回退+启动扫描均支持；0.25s poll不扫描/哈希PDF |
| failed自动重试 | 连续未变的最新源版本，done/failed/active均不自动重提；A→B→A是两次实际sha变化，可再次入队A，不承诺所有历史hash永久封存；手动可重试、force不绕cap/沙箱 |
| 摄取队列 | 复用现有IngestManager与state文件锁；本版不新增worker独占/租约/恢复机制，保留现有恢复语义 |
| disable | `enabled=false`时保留手工pending/status读取能力；`auto_watch=true`仅提示无效，不使服务启动失败 |

### 2.3 不变量与 NOT In Scope

必须保持：`dependencies=[]`、15个工具名称与协议版本、包 `__all__` 7项、Chunk字段顺序、chunk id公式、缓存二进制格式/代际、单次fan-out query embedding、过滤/去重先于rerank、权重乘回、solo选择语义、注册表与 `_safe_path` 沙箱、`_sync_lock -> _cache_lock`。

本版不做：新REST服务、新CLI shutdown命令、分布式任务队列、Linux/macOS原生监听、全CommonMark解析、按事件局部索引重写、完整检索后端代际/事务快照、SQLite chunk主键数据库（当前文本缓存是bin，不是“现成SQLite chunk表”）、无限深度搜索游标、切块/评分算法优化、真实API自动CI、删除用户home/cache、自动改真实配置、默认打开自动上传。

上述延后项在C70同步到 `docs/Execution-plan_developer.md`，记录问题/证据/触发/验收；不能写成“v0.8.1已实现”。

### 2.4 方案取舍与时间审讯

| 方案 | 完整度/风险 | 取舍 |
|---|---|---|
| A 只补#6参数、缩短等锁 | 低；拿锁后仍同步补嵌，重复heading仍错 | 拒绝，未解决根因 |
| B 复用Facade与私有包，投影、预算、原文读取、可合并后台刷新、opt-in摄取 | 本版完整；状态与生命周期有测试成本 | **采用**；标准库、现有持久化与测试入口 |
| C 新SQLite chunk表、完整Markdown AST、任务框架、新同步引擎 | 长期弹性高，但数据迁移/依赖/回滚代价高 | 延后；不符合补丁版本边界 |

```text
CURRENT                         THIS RELEASE                     12-MONTH IDEAL
已修部分#5 + 旧preview     ->    完整#5 + 紧凑初筛/可恢复阅读  ->    局部对账/可量测延迟
增量同步占请求线程               现有索引先读、刷新后台            更强一致性与深分页
默认手动摄取                     明确授权的自动摄取                更细额度/队列治理
```

小时1（人类）：确认真实分支、现有测试与契约，不续接不存在的Lane D。小时2–3：完成refresh与摄取边界，解决线程停止和failed免重试。小时4–5：实现投影/预算/标题/越界，处理分组游标与原文行号。小时6+：协议集成、云端授权验收、文档和CI。实际工作量超过6小时：人类约4–7工作日；agent执行估计5–10小时，均为排期估计，不是测试或性能事实。

## 3. 执行顺序与所有权

| 顺序 | 状态 | 卡 | 目标 | 依赖 | 估计：人类 / agent |
|---|---|---|---|---|---|
| 0 | [x] | Intake | 基线/需求/73项验证/旧计划还原点 | 无 | 已完成 |
| 1 | [x] | C65 | 已提交跨库寻址的fail-closed补强 | Intake | 2–4h / 30–60m |
| 2 | [x] | C66 | 可合并后台refresh、所有MCP只读路径、A4回调 | C65 | 1–2d / 1.5–3h |
| 3 | [x] | C58a | auto_watch与size配置 | Intake | 1–2h / 20–40m |
| 4 | [ ] | C58b | 统一size闸门、自动判据账本、单队列 | C58a | 0.5–1d / 1–2h |
| 5 | [ ] | C58c+C61 | native/poll触发、扫描合并与生命周期 | C66,C58b | 0.5–1d / 1–2h |
| 6 | [ ] | C58d | server/doctor/hint接线与集成 | C58c | 2–4h / 30–60m |
| 7 | [ ] | C67 | compact全路由投影 | C66 | 2–4h / 30–60m |
| 8 | [ ] | C68 | 完整chunk预算与续页 | C67 | 4–6h / 45–90m |
| 9 | [ ] | C69a | 原文读范围/越界/总行数 | C66 | 2–4h / 30–60m |
| 10 | [ ] | C69b | 物理heading章节读取 | C69a | 4–6h / 45–90m |
| 11 | [ ] | C59 | Windows升级占用说明 | 任意；代码不变 | 0.5–1h / 10–20m |
| 12 | [ ] | C70/T8 | 使用纪律、现状文档、欠账、离线评测 | 所有代码卡 | 2–4h / 30–60m |
| 13 | [ ] | C60 | 版本/最终验收/CI/发布交接 | C70 | 2–4h+CI / 30–60m+CI |

以上编号沿用C53–C64；新增C65–C70。C58拆成子卡但不占新的主编号；C61合入C58c，A4合入C66。C60编号旧但**最后执行**。

推荐单人顺序执行。多人只可在互不重叠的文件中并行准备测试；`server.py/indexer.py/watch.py/worker.py` 单一负责人。禁止C66与C58c同时编辑watch，禁止C67与C68同时编辑fanout，禁止C69a与C69b同时编辑read。每笔代码提交只stage该卡文件，配套Changelog条目跟随；不要 `git add -A`。

## 4. 逐卡施工

### [x] C65：跨库 chunk 寻址的完整性与探测边界

**目标**：修已提交 C56/C64 的未覆盖路径，不重写寻址架构。P1，先于全部阅读新功能。

**文件/符号**：
`server.py::_locate_chunk_for_read/_chunk_miss_message/_chunk_attribution/_close_probe_indexer`；
`indexer.py::__init__/_load_chunks_cache`；
`tests/test_kb_read_chunkid.py`。不要改注册表格式、solo字段或chunk id。

**步骤**：

1. 先加测试：零注册库 `kb_read(chunk_id=...)` 经 `handle` 必须 `isError=true`，文本含 `kb_init`，不能协议 `-32000` / IndexError。
2. 在解析默认单库之前显式检查 entries为空。保留显式库白名单解析；不要把参数当路径直接构造indexer。
3. 临时构造器记录文本缓存是否**成功加载且meta匹配**。新增私有 `_chunks_cache_loaded=False`，在 `_load_chunks_cache` 接受有效文件后设true，合法空缓存也true；缺失/损坏/代际不符保持false。不改变codec。
4. 未加载且无有效文本缓存的库计 `skipped(...,"尚无可探测文本索引")`，不是已探测零命中。不得触发sync/provider/watcher来“补证据”。
5. 遍历全部已加载候选库；临时库仍按32个、循环间2s软预算。已经加载但`last_sync=None`且无有效缓存/可用chunks的库也记未完成，不把空初始态当证明。
6. 汇总按顺序裁决：两个以上命中 → 已证实歧义；存在skipped且命中不足两个 → **incomplete**，列已命中/未探测库名、原因、总数，要求 `vault_path`；无skipped且单命中 → 展开；完整零命中 → not found。不要为返回一个结果而默选已加载库。
7. solo候选/跳过原因只给库名、solo标记，不把原始exception中的绝对路径拼进去；普通库沿用既有库归属字段。
8. 临时probe收集与finally统一清理：零命中、单命中、歧义、异常、incomplete都关闭FTS/vector连接。不关闭server已有常驻indexer。
9. `__init__` 中sqlite_vec不可用→memory回退的 `_load_vectors_cache` 也必须受 `load_vectors` gate；真正load_vectors=True时原行为不变。
10. 命中临时库需提升常驻实例时，重新按id取chunk；若取不到，不用旧probe chunk兜底，而是明确“索引已变化，重新kb_search”，避免旧行号冒充现行命中。
11. 维持探测入口不加provider调用、不写向量、不开始监听的契约；**FTS构造可能写派生数据仍是已申报残余**，不能把它写成严格只读或2s硬上限。

**测试规格**：

| 新用例 | 设置 | 断言 |
|---|---|---|
| `test_chunk_read_no_registered_vaults` | 空注册表 | 具名工具错误，不IndexError |
| `test_chunk_probe_one_hit_with_unprobed_vault_is_incomplete` | 唯一已探命中+超库数/超时间两种 | 要求显式库，不能返回content |
| `test_chunk_probe_missing_cache_is_unprobed` | 未加载库无bin | skipped说明，不谎报已修改 |
| `test_chunk_probe_valid_empty_cache_is_complete` | 合法空缓存 | 是完整已探零命中 |
| `test_chunk_probe_solo_diagnostics_do_not_leak_path` | solo缺目录/坏缓存/碰撞 | 文本无solo绝对路径 |
| `test_probe_load_vectors_false_survives_backend_fallback` | 强制sqlite_vec→memory | `_load_vectors_cache`调用0 |
| `test_chunk_probe_closes_resources_on_all_outcomes` | 带close spy的probe | 各退出路径恰当释放，不关常驻 |
| `test_probe_promoted_chunk_disappeared` | promote前换缓存 | 重新搜索提示，不使用旧chunk |

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_kb_read_chunkid.py tests/test_facade_freeze.py tests/test_vector_backend.py -q
```

**完成标准**：上表与原有跨库/solo/歧义/显式库用例通过；C64保留软预算说明。提交建议 `fix(read): fail closed on incomplete chunk probes`，附C65技术账。

### [x] C66：读优先后台刷新 + A4摄取完成回调

**目标**：只读MCP前台不跑全库sync、不等待_sync_lock；合并刷新请求；保留编程API同步语义。P1。

**文件/符号**：
`indexer.py` 的状态初始化、Facade方法；
`_indexer/watch.py::_start_fs_scheduler/_fs_scheduler_loop/_run_sync_quietly/stop_watching`；
`_server/search_dispatch.py::dispatch_search`；
`_server/fanout.py::fanout_search`；
`server.py::_locate_chunk_for_read/_kb_read/_kb_list_files/_kb_stats/_ingest_manager_for/shutdown`。
新建 `tests/test_read_stale.py`，补 `tests/test_anti_contention.py/test_diaglog.py/test_ingest_server.py/test_p5_lifecycle.py`。

**使用现有线程的设计**：

```text
MCP read/request_refresh -> brief condition lock -> set dirty -> return immediately
                                                     |
existing vault-fs-debounce thread <-------------------+
  wait/debounce/throttle -> _run_sync_quietly -> sync (_sync_lock -> _cache_lock)
                                     |
                               success/error + progress
                                     |
               dirty arriving DURING sync -> one subsequent pass

MCP search -> existing chunks/FTS/vectors -> serialize
             no wait for sync; NOT a cross-backend transaction snapshot
```

**步骤**：

1. 测试先固定“不等同步”的正确性，而非只改timeout：用 `Event` 卡住 `_sync_locked`，请求线程必须在release Event之前返回已有结果。test teardown无条件release并join。设2s上限只用于发现死锁，不把本机毫秒作为CI门禁。
2. Facade新增 `request_refresh(*, immediate: bool=False) -> bool` 薄委托至watch；true表示请求已接受/合并，**不表示已刷新完成**。false仅表示正在停止、不接受。保留 `sync()`、`try_sync_with_guard(timeout=...)` 的既有API，不能让旧调用者以为sync完成了。
3. 复用 `_fs_requested/_fs_debounce_cv`，增加最少状态：`_fs_refresh_immediate`、`_fs_scheduler_start_lock`、`_refresh_error`、`_refresh_requested_at`。初始化默认空/False，全部短锁读写；调度器启动双检，必须先保存thread引用再start，防并发请求各起一条。
4. `request_refresh` 检查 `_stopping/_watch_stop`，停止后不得clear stop自行复活；无调度器的独立indexer可以首次启动一个调度线程，调用方最终须stop。不创建每请求Timer/Thread。
5. dirty在进入sync前清零，释放条件锁后才执行sync；sync期间新请求重新置dirty，结束后最多合并一轮。调度器不持条件锁等待_sync_lock或网络；请求线程只持短锁，不触碰同步锁。
6. 为连续warm reads设置私有 `_READ_REFRESH_MIN_INTERVAL_SECONDS=1.0`，成功刷新之后1s内的普通read请求合并/延后，不能每个query都全库扫描。原生文本事件、完成回调 `immediate=True` 可提前唤醒，但仍保留dirty合并；sync失败沿用现有指数退避上限5s，immediate不得绕过失败冷却。
7. `_run_sync_quietly` 保留线程不死与失败计数，额外记录简短可观测 `_refresh_error`；成功清零。不把原文/key写diag，错误详情只在显式kb_stats诊断中展示并限长。
8. 新增只读 `refresh_status()` 返回快照：
   `last_sync`、`refresh_pending`、`indexing_in_progress`（pending或_indexing/_sync_state非idle）、`indexing_progress`拷贝、`refresh_error`。
   复用条件锁保护新增调度字段，进度直接复制既有dict；不获取_sync_lock，不新增状态锁/集合锁，不改同步引擎逐文件发布方式。沿用现有all_chunks与检索实现；这些观测字段不是跨后端原子快照。文档只承诺“当前可用索引先返回，刷新在后台”，不承诺上一轮完整事务。
9. 替换显式单库、缺省单库、Scoped多库、全局fan-out中的 **每一处** `try_sync_with_guard` 为 `request_refresh`+现存search。单库两分支应共用一个小helper/一致逻辑，不新建service。
10. 首次索引判定：`last_sync is None`且无可用文本缓存/chunks → 返回原 `status="indexing"/retry_after=3/chunks=[]`。warm缓存即使last_sync未建立也允许读；完成过的空库/全豁免库正常 `chunks=[]`，不是无限indexing。
11. warm检索在pending/active时追加现有 `indexing_in_progress` 与进度；仅有后台错误时追加简短 `indexing_error`。这些字段表示“结果来自本请求所取当前可用chunk集合，后端可处刷新中”，不保证请求结束即最新。
12. fan-out新增 `indexing_vaults`（库标识→进度/错误），总标记true表示至少一库刷新；cold库计入errors但不阻断其他warm库。单次query embedding仍只一次，不在cold库上重复尝试逐库embedding。不把cached-score排序改成投影后排序。
13. source文件读取在路径解析后直接读磁盘，不前台sync；直接合法文件未索引也能读。短名解析仍仅按现存索引消歧，cold未命中返回明确indexing/重试信息而非虚构“文件不存在”。
14. chunk显式定位先请求刷新、查当前chunk。无命中且cold时返回明确重试；warm未命中说明ID可能过期。C65跨库完整性规则保持不变。
15. stale chunk元数据不能悄悄读错章节：读取该文件原文快照；若当前内容级sha256与indexer记录签名不符，返回“chunk_id stale，重新kb_search/改source+heading”的工具错误，不把旧行号范围中的新正文当旧chunk。未记录签名也fail-visible；不等待刷新替它决定。
16. list_files/stats同样请求后台刷新后返回现有结果并带状态，冷库计数可能为0，必须说明pending；手工 `kb_rebuild/kb_import/kb_export` 的一致性/破坏性操作仍保留原同步锁行为。
17. **A4**：`_on_job_finished(source: str, out_md: Path)` 与worker二参数契约对齐，只从已存在indexer请求 `immediate=True` refresh；不要再起一条 `ingest-sync` 直接跑sync。不存在indexer时不在回调中创建它，后续显式读取/启动流程负责构造。
18. shutdown/remove按现有stop_watching流程停止本卡新增的刷新调度；停止标志、notify、锁外join继续复用既有生命周期。不要借此重构server启动线程/注册表remove流程。新增线程的停止测试留在本卡，完整服务级启动竞态另记后续。
19. 更新诊断埋点：前台sync阶段只计调度耗时，不把query embedding时间重复算入sync/retrieve；保留四阶段顺序与corr_id，调整现有monkeypatch测试入口而不是删测试。

**需要新增/修订的分支测试**：

| 流程 | 必测情况 |
|---|---|
| 前台隔离 | warm锁空闲但sync人为慢；warm锁已占用；cold static/external；暖缓存last_sync=None |
| 完成语义 | 合法空库、全豁免库；后台失败后仍读旧结果并报错误 |
| 合并 | 100请求只一调度线程；sync中有事件只一后续轮；1s内reads不100次扫描 |
| 生命周期 | stop中拒请求；shutdown不重启；活线程保引用；重复start/stop幂等 |
| fan-out | 两库cold/warm混合、Scoped solo、全局排除solo、query只embed一次 |
| 读 | 直连未索引文件、短名cold、stale chunk签名、chunk不存在、索引变空 |
| A4 | 真worker mock解析落盘→双参回调→request_refresh spy→后续能搜索；回调异常记录不杀队列 |
| 可观测性 | status快照不等锁；diag四阶段顺序、无正文/路径/key |

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_read_stale.py tests/test_anti_contention.py tests/test_diaglog.py tests/test_multivault.py tests/test_scoped_search.py tests/test_ingest_server.py tests/test_p5_lifecycle.py tests/test_sync_engine.py tests/test_concurrency_hardening.py tests/test_search_oracle.py -q
```

**完成标准**：测试用Event证明前台返回早于sync release；不是把1.5s变0.1s。现有同期排序/去重/筛选oracle通过。提交 `fix(search): serve existing indexes while refresh runs in background`，A4记同一条技术账。残余：构造未加载大库、FTS短事务竞争、query embedding/rerank仍可能慢，分别可观测，不宣称都已解决。

### [x] C58a：自动摄取配置与统一size策略

**前置**：无代码前置，可在C66后顺序做。文件：`config.py::IngestConfig/AppConfig.__post_init__/load_config`、worker的ImportError fallback dataclass、`config/app.toml.example`；新建 `tests/test_ingest_auto.py`。

1. dataclass尾部新增 `auto_watch: bool=False`、`max_file_size_mb: int=20`，不改变原字段位置与默认enabled。
2. `load_config` 从 `[ingest]` 读取。auto_watch只接受真正bool；字符串 `"false"`不得经bool变True并授权上传，错误为具名ValueError。max用 `_numeric(...int,20,minimum=0)`，拒绝bool、负数、非整数、NaN/Inf。
3. 编程构造AppConfig也校验这两个值；worker fallback dataclass同步同默认。`enabled=false + auto_watch=true`合法但不生效，doctor/hint警告，不使服务起不来。
4. 尺寸上限按 `cap_bytes = max_file_size_mb * 1024 * 1024`，0返回不限。边界 `size==cap`允许，`size>cap`拦。
5. example增明确注释：默认不上传、自动扫描会包含启用时既有文档、所有格式 `INGEST_EXTS`、20MiB上限、0不限、上传可能产生云端费用/隐私影响、修改后重启。
6. 只新增配置，不改chunker/meta，不触发全库重嵌。不得新增默认enabled=true或代用户填写api_key。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ingest_auto.py tests/test_path_migration.py tests/test_doctor.py -q
```

**验收**：默认双关闭、20MiB；bool拒收、零/边界校验与TOML/Python3.10 fallback通过。建议提交 `feat(config): add opt-in ingest auto watch and size policy`。

### [ ] C58b：size闸门、自动候选与持久化免重试

**前置**：C58a。文件：`ingest/worker.py`，`tests/test_ingest_auto.py/test_ingest_worker.py/test_adversarial_v070.py`。新增逻辑留在manager，不让watch直接负责job状态；不改registry锁API。

**状态数据契约**：

```text
.ingest_state.json                 (保留 version/jobs，旧数据可读)
  jobs: <原任务历史，仍最多约500终态记录>
  auto_seen:
    relative_source: {sha256, state, submitted_at, last_job_id}
  auto_watch:
    {last_scan_at, last_error, submitted, skipped_too_large,
     skipped_seen, skipped_ignored}

auto_seen <= 当前/历史源文件数，不复制正文/路径绝对值/key
连续源未变 + done/failed/queued/parsing -> skip
源hash变 -> eligible -> under lock recheck -> enqueue one
手动submit -> 同步账本当前状态，允许失败重试
.ingest.lock -> 复用现有state文件锁
```

1. **先加size测试**：分别覆盖显式、扫描、auto、force、恢复queued、入队后变大。mock `_client_or_make/channel_for/_pymupdf_fallback` 计数，超策略cap必须全为0。
2. 新增manager私有 `_size_limit_bytes/_check_file_size`（名字可按已有风格，语义固定）。显式sources先全部_validate/stat/size，再算hash/创建任何job；任一超限整批ValueError，不“先入一半再失败”。
3. 扫描size检查放在sha256之前；pending保留new/changed条目结构，对超限项返回 `reason="too_large"`、`size`、`limit_bytes`，无需hash。submit(None)过滤超限并返回 `skipped_too_large`；不是所有pending项都可直接入队。
4. `_run_job` 在沙箱校验后、创建产物目录/构造client/parse前再stat+size；恢复旧queued仍拦。force只强制解析，不绕过size或沙箱。
5. 策略cap不替代MinerU通道cap：配置20MiB内但Agent通道>10MB触发原MineruError时，保留PDF/PyMuPDF既有fallback；不是PDF不启用本地兜底。retryable网络失败依旧failed，不固化fallback。
6. 保持 `_validate_safe_source` 路径检查，拒绝绝对路径、`..`、出vault symlink。自动扫描 `scandir(...follow_symlinks=False)`，不跟随目录/文件链接；工作入口再resolve校验。后缀限INGEST_EXTS。
7. 不把 `scan_pending()` 改成“自动失败免重试”。它仍是用户pending清单；仅按size扩充拒绝原因。自动新增 `_auto_pending()`，在其内部复用现有遍历/默认排除清单与新的可选ignore predicate。
8. ignore来源：server把indexer的IgnoreMatcher规则供应器注入manager，扫描每轮重取最新 `.vaultignore/config.exclude_patterns`；不是构造时永久快照。默认排除 `_EXCLUDED_DIR_NAMES`、output_dirname与assets/temp/state目录。
9. 对自动候选先stat+size+ignore，再算hash；重复扫描可用本轮/manager内stat缓存优化，但粗刻度/mtime回拨仍需保守复核。不要仅mtime+size就永远信任PDF未改，优先正确性。
10. 账本与job一并复用现有 `_lock -> _process_file_lock` 事务load/recheck/save；auto_submit只有enabled && auto_watch才工作。锁内重验active_sources/auto_seen，避免同一manager重复扫描重复入队；不新增文件锁API或跨进程消费者框架。
11. 旧state缺auto_seen时从jobs按submitted_at最新构建；不能用dict最后一条替代max。done且hash相同不重复传；failed/active同hash也记账。保留旧state读写/恢复方式，仅补新增字段兼容，不重构损坏state的整体恢复策略。
12. `_save_state` 剪job不能剪auto_seen。账本允许对**本轮成功完整扫描证实已删的源**清理；扫描有OSError/中断时不清理，避免一次权限问题清空去重证据。无需新数据库。
13. `_worker_loop` queued/parsing/done/failed转换时同事务更新auto_seen（手动job也更新），每源只存最新版本sha。若旧job完成而源已有新版本排队，旧job终态更新自己的jobs，但不能覆盖新sha的auto_seen。A→B→A按版本变化再次eligible；不是永久保存每个历史hash的黑名单。不要因failed.retryable=True自动重提连续未变版本。
14. 明确auto_seen丢失的边界：用户主动删除/损坏全部state且又启用auto_watch，可能重新上传；状态诊断要显示恢复/重建，文档告知不要删state解决问题。不能承诺跨状态全丢失仍exactly-once。
15. 新公共 `auto_submit()` 调 `_auto_pending` → 共用现有submit队列入队逻辑，不新建第二worker；零候选不创建无用worker。活任务去重与手动force维持既有语义。
16. 默认关闭路径零扫描/零hash/零client/零worker。读取status允许报告，但不得借status隐式上传。
17. 限制auto_watch诊断摘要长度，status返回计数+最多5个拒绝样例，不塞全库pending。字段不写到kb_stats.skipped_unsupported（原语义明确排除可摄取格式）。
18. 更新worker与ingest/__init__.py的“仅显式submit、不做watcher”的旧docstring：改成“默认手动，显式授权后可自动”，不能留旧硬性设计相互矛盾。

**测试清单**：
默认关闭零副作用；20MiB±1/0无限；sources混合超限整批不入队；扫描保留too_large诊断；
Agent限额PDFfallback；queued恢复/源变大二次闸门；ignore配置与动态 `.vaultignore`；
符号链接逃逸/排除目录；同hash四种状态skip；changed hash新job；>500历史剪枝免重传；
旧state迁移；手动失败重试/force不越cap；A→B→A契约；旧job终态不覆盖新sha；
同manager重复auto_submit只一新job；读status不启动新自动任务；既有恢复用例继续通过。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ingest_auto.py tests/test_ingest_worker.py tests/test_adversarial_v070.py tests/test_ingest_server.py tests/test_registry.py -q
```

**验收/提交**：所有网络mock；静态failed不自动重试；>500用例必须通过。提交 `feat(ingest): enforce size policy and persist automatic submission dedupe`。明确账本状态落盘与既有state兼容，不更改缓存协议。

### [ ] C58c + C61：原生/poll自动触发与扫描合并

**前置**：C66、C58b。文件：`_indexer/watch.py`、indexer初始化/Facade、`tests/test_watch_integration.py/test_fsnotify.py/test_ingest_auto.py/test_p5_lifecycle.py`。

**设计**：文本refresh与PDF自动扫描不同工作单元。事件线程只能置标志；PDF扫描不可占用唯一文本防抖线程。增加每库一个按需 `vault-ingest-scan` 合并worker，不每事件创建线程。

```text
native event  .md/.txt       -> existing refresh flag -> vault-fs-debounce -> sync
              PDF/Office    -> ingest dirty flag ----> vault-ingest-scan -> auto_submit
poll/start/fallback cadence -+                                  |
                                                         ingest-worker parse
                                                               |
                                          A4 callback -> immediate request_refresh
```

1. indexer增加 `_ingest_hook=None`、ingest短锁/Condition、dirty、worker引用、停止状态与最近scan时间；hook由server注入。无hook时全部自动分支早退，不扫描文档。
2. 新私有 `request_ingest_scan()`：仅做锁/置dirty/notify，按需启动最多一条扫描worker。先保存thread引用再start；同库100事件只一worker。handler中不调scan_pending/auto_submit。
3. 扫描worker在锁外调hook；开始前清dirty，期间新事件置dirty，完成后合并下一轮。失败记诊断、退避复用0.5→5s；不得 tight-loop重复扫描/上传。
4. 原生事件逐条判断text/ingest两类，不能“遇到text就break导致后续PDF漏掉”。缓存目录/默认排除/ignore中的事件两类均排除，**最终上传授权仍由manager重新校验**。
5. 只PDF事件不触发全库文本sync；只有解析完成后A4触发refresh。文本事件不调用ingest扫描，除非同批还有文档事件。目录rename/空路径/`events=None`无法确定类型时请求两类；自动关时仍仅文本路径。
6. `_native_watch_loop` 启动请求一次ingest扫描；每 `watch_fallback_interval>0` 节拍请求扫描，事件丢失有兜底；不把hook放在 `_run_sync_quietly`（否则每次纯文本sync都会扫描PDF）。
7. `_watch_loop` 启动同样请求一次；PDF不在 `_quick_signatures` 的0.25s文本集里，自动扫描另用monotonic节拍。**明确0规则**：`watch_fallback_interval=0`关闭native文本兜底，但开启auto_watch的poll文档扫描仍采用私有30s默认节拍，避免0使功能失效；doctor/hint回显effective_interval。
8. 原生启动失败/运行中死亡回退poll后保持自动扫描；不能因fallback又建第二scan worker。Linux/macOS默认auto→poll也必须有测试模拟覆盖。
9. 复制尚在进行的文档：扫描发现(stat size/mtime)后至少间隔一次debounce采样稳定才哈希入队；仍在变化则延后本源，不阻塞其他源。
10. worker解析前哈希与入队hash不符时标source_changed，等待下一扫描按新hash入队；不引入上传临时副本。
11. stop先set停止、清dirty、notify，再join扫描线程；hook慢于2s则保留引用与_stopping，停止后不再次调hook/入新job。所有测试解除Event后等待资源清理。
12. manager parse任务已开始的云上传不能由关auto_watch撤回；停止scan只禁止新自动入队。不宣传即时取消云端请求，runtime改配置以重启生效。
13. 为上述流在watch/server附近写小ASCII控制图，注释解释“扫描与文本sync分离”的理由，不复制整份计划。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_watch_integration.py tests/test_fsnotify.py tests/test_ingest_auto.py tests/test_p5_lifecycle.py -q
```

**验收**：native、poll、非win回退、watcher死亡、events=None、interval0、启动既有文档、慢hook不阻文本、事件风暴合并、stop后零新上传均通过。提交 `feat(watch): trigger coalesced automatic ingest across native and poll modes`；C61记为同批完成。

### [ ] C58d：server接线、hint、doctor与实时状态

**前置**：C58c。文件：`server.py::_indexer_for/_ingest_manager_for/_kb_init/_kb_init_solo/_kb_ingest/_kb_stats/shutdown`、`doctor.py::check_config/render_md`，`tests/test_ingest_server.py/test_doctor.py/test_ingest_auto.py`。

1. `_indexer_for` 在 `start_watching()`之前挂hook，仅effective=`enabled && auto_watch`时挂；不在hook闭包定义阶段构造manager，不默认创建 .mortis-parsed。
2. hook闭包惰性获取manager后auto_submit；manager注入最新ignore供应器时不反向拿 `_indexers_lock`。锁顺序：禁止持 `_ingest_managers_lock` 再调用 `_indexer_for`，避免反向嵌套死锁；完成回调也只用已存在引用。
3. `enabled=false/auto_watch=false`返回hook None；矛盾键在kb_init/kb_ingest hint点明“自动设置不生效”。不把调用status当许可。
4. 保留 `kb_ingest action=submit/status/pending` 三态，**不把schema写成scan_pending/source**。当前submit参数是 `sources`列表，文档/skill全部使用实际名字。
5. init/solo hint保留 `ingestible_docs`，新增auto effective、size cap、授权风险与建议动作；默认手动给 `pending`→用户确认→submit流程。pending摘要最多5条且避免启动时全量hash；不得重复输出数千路径。
6. `kb_stats` 新增 `ingest_auto` 小对象：configured/effective、max_file_size_mb、watch_method/effective_interval、last_scan_at、last_error、拒绝计数；只读现有manager状态，manager不存在则last_scan=null。不为stats启动扫描/worker。
7. doctor `check_config` 报配置effective/size/cadence与矛盾警告；保持VALID判定不因auto关而失败。STATUS中的“最近自动扫描”只可写已有state的最后记录、标“报告生成时快照”，不能冒充实时更新、不能构造manager/联网。
8. runtime最近scan通过kb_stats/status拿；doctor不循环刷新信任锚，不给STATUS注入全pending列表。
9. `shutdown/_kb_remove` 停scan hook、新refresh，再停止watch；保留已开始任务的不可撤回说明。不要删产物和 `.ingest_state.json`。
10. 新增一条mock端到端集成：enabled+auto true→放PDF→扫描一次→fake parse→md原子落盘→A4刷新→kb_search命中；native与poll各测，负路径自动关明确未调用parse。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ingest_server.py tests/test_ingest_auto.py tests/test_doctor.py tests/test_watch_integration.py -q
```

**验收**：默认零副作用；矛盾键不阻启动；诊断可判定；所有层只授权后上传。提交 `feat(server): wire automatic ingest with explicit status and safe defaults`。

### [ ] C67：compact初筛投影，全路由一致

**前置**：C66。文件：`server.py::_tool_definitions/_fanout_search`、`_server/search_dispatch.py`、`_server/fanout.py`、`_indexer/models.py::Chunk.to_dict`；新建 `tests/test_compact_search.py`，保留preview/budget/oracle测试。

**示例**（单库，顶层归属只写一次）：

```json
{
  "vault": "D:/corpus",
  "vault_name": "小说库",
  "chunks": [
    {
      "source": "vol08/part03.txt",
      "heading": "Chapter 907: Section Title",
      "lines": [697, 720],
      "snippet": "目标实体出现的上下文..."
    }
  ]
}
```

1. schema新增 `compact` bool default false，description写“极简预览，隐含preview，返回source/heading/lines/snippet；按行号+库回读，不返回id”。不新增mode第三值或改full/preview enum。
2. dispatch按现有bool字符串容错解析compact（true/1/yes/on，false/0/no/off）；compact=true强制preview=true，即使mode=full也以compact显式请求优先。compact=false完全保留原preview/mode规则。
3. `Chunk.to_dict` 添加**keyword-only** `compact=False`，原两个位置参数继续可用；不改dataclass字段。compact直接构造四键，不先构造full再删；snippet复用 `_extract_snippet`，lines取metadata物理起止（旧值fallback与现有一致）。
4. 不输出id/score/title/metadata/char_count/source_pdf/content；不改chunk原对象、不改score/metadata、不改缓存或embedding。compact仅投影。
5. 单库顶层vault/vault_name只在compact时加；普通preview/full保持旧键集。单库明确库寻址不依赖可能重复的库名，vault用已解析注册绝对路径。
6. fanout给函数新增默认false关键字compact，Facade `_fanout_search`转发保持旧调用兼容。Scoped/global/默认单库全部接通，不只改单路由。
7. 平铺跨库每chunk加vault，不重复vault_name；groups只在group放库标识/名称。**排序与最高分分组顺序在Chunk/pairs层完成**，投影后不可 `chunk["score"]` 排序（compact无此键）。
8. 投影完成、状态字段/hint构建完成后才预算。cold `status=indexing`统一经过预算路径（C68补），不要返回早退绕过budget。
9. `exact_terms/path_prefix/offset/limit/dedupe/weights/rerank`不变。测试比较compact/full同一查询的source+lines+顺序，不能仅比较结果数。
10. fixture：30个中文标题/中文路径/150字snippet、多库两种形状。量测 `_measure_payload_bytes`，compact对普通preview至少减少冗余开销；选定可复现fixture目标>=30%缩小，不宣称所有真实数据都缩小30%、也不承诺任何宿主绝不dump。
11. 每条compact都做回读集成：单库顶层或平铺条目/group的vault + source + lines → kb_read，正确命中原文；消除id后不能仍要求chunk_id作为唯一阅读方式。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_compact_search.py tests/test_preview_mode.py tests/test_budget_bytes.py tests/test_multivault.py tests/test_scoped_search.py tests/test_search_oracle.py tests/test_facade_freeze.py -q
```

**验收**：普通默认字段不变；四种路由+分组都可读回；排序不依赖投影字段；量测记账。提交 `feat(search): add opt-in compact result projection`。

### [ ] C68：完整chunk预算、最低envelope与分组续页

**前置**：C67。文件：`_server/fanout.py::_measure_payload_bytes/apply_budget/fanout_search`、
`_server/search_dispatch.py`、schema descriptions；
`tests/test_budget_bytes.py/test_compact_search.py/test_multivault.py`。P1，旧测试的“首条二分截断”须改为本卡明确的新行为。

**步骤与精确返回契约**：

1. 保留 `_parse_budget_bytes` 范围500..100000、None/bool/非法值回退语义。budget=None仍不追加任何budget字段，不新加全局默认budget。
2. 保留MCP包装计量 `_measure_payload_bytes`，不要仅count内层JSON/字符串长度；CJK、反斜杠、引号、换行、四字节emoji都算真实UTF-8包装后大小。
3. 所有候选先按最终投影变dict；apply_budget只复制外层/列表，**不修改chunk的任何键/内容**，full与preview/compact一视同仁。
4. 平铺选择最长可容纳前缀；附 `truncated`、`returned`、`next_offset=orig_offset+kept_count`，字段也参与预算。不能跳过首个大chunk继续放后面小chunk，不能clip source/heading/snippet。
5. 全部放得下返回truncated=false、returned=N、next_offset=offset+N，保持既有兼容值；它不是“整个库已经结束”的证明。搜索候选宽度仍受top_k/各route cap约束。
6. 首条放不下但空envelope可满足预算：chunks=[]、truncated=true、returned=0、next_offset不变；加短 `budget_hint="use compact or increase budget_bytes"`，hint计量；不返回不完整chunk，不死循环重试同预算。可以用更短等价hint，但须测试文本有明确动作。
7. 最小envelope指保留已有searched/errors/excluded_solo/indexing/status等控制信息、清空结果列表、附budget计数后的实际字节数。它也放不下则合法返回空结果、`budget_exceeded=true`、`minimum_budget_bytes`、`budget_hint="narrow vaults or increase budget_bytes"`；minimum为**含这些诊断字段的最终响应**所需字节数，迭代到数值稳定（最多3次）。不得返回非法JSON或假称<=budget。
8. overflow是metadata不可压缩的声明，不是另一个工具错误：保留每个库诊断，允许响应超过用户预算且显式标记；用户可缩小vault_paths/重试。该例外必须写schema/升级须知，不能继续宣传无例外硬上限。
9. 固定候选池后先测全量N；不满足才对**正整数前缀1..N-1**二分，使用同一truncated=true/无budget_hint的响应构造函数，每次将计数/游标/groups的字段都填好再真实wrapper计量。大小随更多chunk与数字位数非减；不要把N的truncated=false或0条专用hint形状混进二分判定。正前缀一个也不满足时才构造0条hint/overflow，防0条hint反而比1条大时误判空包络不可满足。N受max_top_k/目标库数限制；复杂度O(logN)次最终计量，不手拼JSON、不缓存不同形状的错误计量。
10. 分组先保持既有每库分页，再按既有组顺序/组内顺序保留整个chunk前缀；空桶删除。保留group vault/vault_name，不改组归属。
11. 分组预算不能用 `offset+全组总returned` 恢复。budget启用且groups返回时新增每组 `next_offset`、`returned`、`truncated`，以及顶层 `group_next_offsets`（vault→下一偏移，**包括本页一条也没放下的候选组**）。
12. 分组顶层 `next_offset=null`。schema新增 `group_offsets:object`，additionalProperties为integer、minimum0；只允许group_by_vault=true（否则具名ValueError），不加required、不改工具数。server严格拒bool/负数/小数，键必须解析到本次已授权搜索entries（未知/未定向solo/未注册键报错），每库最多一值；没传的库回落原offset。group_offsets优先对应库offset，不叠加两者。
    原样把 `group_next_offsets` 传回同一次query/vault_paths/group_by_vault/top_k/use_rerank/filters/dedupe参数，继续fanout同一排序流程。不要转vault_path单库后声称“同一组游标”。budget=None且未传group_offsets保留旧外层形状。
    group_offsets不要求budget_bytes；不带预算时只按各组偏移取页，不新增budget计数/游标字段，调用者按每组本次实际条数推进。带预算时以group_next_offsets为准，文档分别给出这两个组合。
13. 分组top returned=实际所有组返回总数；truncated只表示预算裁掉本页候选，group.truncated表示本组本页裁掉。未放下组cursor=该组本次group_offsets或offset，不能推进；partial/full分别按该组实际returned推进。传给apply_budget新增私有group_orig_offsets映射，不误用统一orig_offset。
14. group_next_offsets与全部诊断本身也要计量；若它使最小envelope超预算同第7步显式overflow，不丢失cursor；分组response原地不覆盖输入。
15. 单库cold status/indexing响应统一走apply_budget（chunks为空），保证指定预算时的控制字段/最低尺寸例外一致。fan-out cold/warm混合响应亦一致。
16. 平铺把per_vault_k提高到 `min(max_top_k, max(top_k, offset+(limit or top_k),20))`，filters分页最后全局做一次。分组为避免续页扩宽候选池导致重新rerank/跨库dedupe改变排序，**固定** `per_vault_k=min(max_top_k,max(top_k,20))`，group_offsets仅切各组相同候选窗口，不每页扩大。每页最多limit或top_k条；达到窗口末尾只说明本次候选耗尽，不宣称整库结束。
    文档明示这不是snapshot cursor：文件变化、provider重排非确定、更改top_k/目标库都会改变序列；无这些变化时mock确定排序与同一固定候选池才要求无漏读。想扩大窗口显式调top_k（至max_top_k）并从头开始；无限深页/稳定opaque cursor另立待办，不改RRF/rerank算法。
17. 更新旧 `test_budget_bytes_first_chunk_exceeds_budget`：full/preview首条巨大→returned0、原游标、hint；新加compact能够放下一整条的对照。保留“无预算零增量字段”和合法JSON断言。

**测试边界**：

| 分支 | 断言 |
|---|---|
| 全部/部分/零放下 | 返回完整dict与原chunk字节内容相等，prefix保序 |
| 边界budget±1 | 计数/游标正确，非overflow响应<=budget |
| UTF-8/JSON逃逸 | 内容含中文、引号、反斜杠、CRLF、emoji，双层可json.loads |
| 不可满足metadata | 保留errors/solo/status，budget_exceeded+最终minimum准确 |
| 空列表/cold/offset超末尾 | 合法响应；不制造可用新chunk/不让游标越过未返回内容 |
| 分组部分/一组零返回 | 每组cursor正确，top next_offset=None，不重复/漏读 |
| 不可变 | apply_budget输入深比较不变，metadata/Chunk对象不改 |
| 兼容 | budget=None/full/preview字段集合与基线一致 |
| 相同查询续页 | group_next_offsets→group_offsets同路由续页，固定语料/候选池/mock确定重排下本页未返项不漏；不承诺事务游标 |
| group_offsets校验 | 未知/solo越授权/负数/bool/浮点/非group模式拒收；缺库键继承offset，预算零返回不推进 |

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_budget_bytes.py tests/test_compact_search.py tests/test_multivault.py tests/test_preview_mode.py tests/test_search_filters.py -q
```

**验收**：不存在二分裁正文/snippet代码；每个零进展响应都提供恢复动作；包络无法满足时显式申报。提交 `fix(search): enforce whole-result budgeting and explicit grouped cursors`。

### [ ] C69a：物理原文读范围、越界诊断与字符上限

**前置**：C66。文件：新私有 `_indexer/reading.py`、indexer::read及新增私有薄委托、`server.py::_kb_read`；
新建 `tests/test_read_ranges.py`，保留txt/wiki/chunk/facade测试。

内部返回值指定为私有 `@dataclass(frozen=True, slots=True) ReadResult`，字段至少为 `source_sha256/content/total_lines/effective_start_line/effective_end_line/content_end_line/truncated/next_start_line/next_start_char`。原请求行号由server回显，不让纯读取helper知道MCP参数字典。新增 `indexer._read_result(source,start_line=None,end_line=None,*,heading=None,start_char=0,max_chars=None,expected_sha256=None)` 私有薄委托；公共 `read()`传max_chars=None只取 `.content`，保留旧API不截字符的行为。server明确传配置cap；`start_char`仅MCP私有路径支持，不扩原Facade签名。

1. 先用fixture固定：10行文件start=11340/end=11450必须工具错误，文本含requested11340、actual10、source与“核对分卷行号或用heading”；不得content空白成功。
2. 新私有纯数据helper置reading.py；只依赖标准库/底层chunking纯函数，不运行时导入Facade。使用上述指定的内部ReadResult，不导出包顶层、不改Chunk字段。
3. 路径权威仍先由 `indexer._safe_path(source)` 检查白名单后缀/resolve在vault内，helper只接已校验Path；不因为未索引而绕沙箱。
4. 一次 `path.read_bytes()` 得原字节+sha256；`utf-8-sig` 解码、splitlines得到物理lines；同一快照算total/heading/span/content，不先计数再另读文件。空文件total=0；BOM不占标题字符，CRLF不制造额外行。
5. Facade `read(source,start_line,end_line)->str`保留参数与返回str，薄委托新helper；范围越界具名ValueError是本版修复，不返回dict。server走新增内部 `_read_result`薄委托拿元数据，避免读取两次。
6. `start_line/end_line` schema minimum1保持；server解析拒显式bool、非整数字符串/浮点、零/负数、end<start（缺省start=1也检查），不能 `_parse_int(True)==1` 默默通过。兼容整数与合法整数字符串。
7. 非空：effective_start=1或请求start；start>total→ValueError；effective_end=min(requested_end或total,total)。end超EOF正常钳制。空文件无显式范围返回content=""；有范围时明确total0越界。
8. MCP成功返回保留source/content/truncated与原请求start_line/end_line回显（缺省仍null，兼容旧调用）；**新增** `total_lines`、`effective_start_line/effective_end_line`。空全文effective两者null，不能谎造第1行。
9. MCP full/range/heading/chunk全部显式传 `indexer.config.index.read_max_chars`，100..1,000,000范围的既有规则不变；公共编程read保持不截字符，不能把server已有上限倒灌成Facade新增breaking。去掉server重复读取配置fallback造成口径分歧，参数已由load_config校验。
10. 字符截断保持既有 `text[:max_chars]` 语义，不改成字节预算，也不因完整行长>cap而无限越限；追加 `content_end_line`、`next_start_line` 与 `next_start_char`（0-based）描述真实已返回范围。
11. 为单行超过cap能续读，schema给kb_read新增可选 `start_char:integer=0`，定义为start_line内0-based Unicode字符偏移，非零只与显式start_line合法组合；chunk_id/heading模式不能显式带此参数（即使0）。未传或full/range显式0不改变起点；无start_line且start_char非0报错。不新增end_char，参数显式bool/浮点拒绝。
12. 正文规范化为 `"\n".join(lines[start-1:end])`，cursor映射使用所选行的前缀字符累计长度，不含原文件末尾换行。**分隔符前**（刚返回行内最后一个字符、尚未返回`\n`）为 `(本行,len(line))`；**分隔符后**为 `(下一行,0)`。不得把两者合称“行边界”。空行长度0同样遵循这两个位置；处于选定end的最后行末尾且正文已尽则next=null。
    例：`abc\ndef`，返回上限3→content=`abc`/next=(1,3)，下一页从该位置返回`\ndef`；上限4→content=`abc\n`/next=(2,0)，下一页返回`def`。两次拼接必须分别复原相同`abc\ndef`，不能丢/重复分隔符。当前read_max_chars下限100，因此测试可用100字符首行配cap100/101做真实集成断言，纯helper可用上述3/4示例。
13. start_char<0/超过该行长度→具名ValueError；==该行长度允许从行末换行继续。不要用start_char跳过first heading/chunk的安全/互斥检查。
14. chunk路径用同原文sha256对比C66stale检查、再合法裁区间。没有两次读导致“检查的是旧bytes、返回的是新bytes”；所有read modes的total来自当前文件，不是索引chunk最大行号。
15. 文件在路径检查后消失/不可读/无法UTF8解码，helper边界捕获具名OSError/UnicodeDecodeError，带相对source、异常类别及修法raise ValueError from exc供MCP工具错误；不能直接str(OSError)把绝对文件名泄露给solo调用方，也不输出坏UTF8附近原文字节。内部cause保留，不吞成空内容。
16. 文档明确start_line/end_line回显与effective/content_end/next区分，不能把整个请求end当实际content末尾。

```json
{
  "source": "volume15_part2.txt",
  "start_line": 3,
  "end_line": 999,
  "effective_start_line": 3,
  "effective_end_line": 10,
  "total_lines": 10,
  "content": "...",
  "truncated": false,
  "content_end_line": 10,
  "next_start_line": null,
  "next_start_char": null
}
```

**测试**：start1/EOF/EOF+1、end省略/超EOF/倒置、空文件两分支、BOM/CRLF/Unicode、只end、非法bool/小数、未索引直连、沙箱/symlink、range超字符cap、截在行中/换行边界、长单行多页拼接严格复原（允许已有join去末尾换行语义）、stale chunk原bytes一致性、丢失/编码错误、isError协议。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_read_ranges.py tests/test_indexer.py tests/test_txt_indexing.py tests/test_wikilink_read.py tests/test_kb_read_chunkid.py tests/test_facade_freeze.py -q
```

**验收**：物理行数准确、越界不再静默成功、长单行可续读、不扩包导出/缓存代际。提交 `fix(read): diagnose bounds and expose accurate continuation positions`。

### [ ] C69b：heading按原文章节定位

**前置**：C69a。文件：`_indexer/reading.py`、server::_kb_read/schema、`tests/test_read_heading.py/test_wikilink_read.py/test_txt_indexing.py`。

1. 不给schema再加一个heading字段，它已存在；补description说明精确标题、子标题包含、重复标题报错、range优先、物理行号。
2. reading helper在已读原文快照上扫描，不用 `all_chunks()` 的heading min/max并集；heading读未索引直连文件也可工作，后台sync仍可继续。
3. 复用 `chunking.frontmatter/_HEADING_RE/_FENCE_START_RE/clean_heading/_is_chapter_heading` 与表格块边界。helper必须跳过frontmatter、代码围栏、表格内部假标题；不用图片注入结果，不删除原始行，不影响物理行号。
4. 支持当前标题语法：ATX层级=len(#)；章节类视level1，按当前判据支持中文/Chapter。无标题/普通TXT传heading→not found；不把文件stem/title fallback当物理heading。
5. scan生成 `(title,level,start_line)` 轻量列表，不构造chunk/embedding。trim用户heading后精确匹配，保留内部空格与大小写；搜索返回heading字符串可直接用于同一文件读取。
6. 零命中ValueError含heading、source、total_lines、最多5个候选标题和起始行，提示核对标题/改行号；列表过多给总数，不整篇回显。
7. 多命中ValueError含最多5个起始行与总命中数，指示用start_line/end_line；不合并两个不同章节，不默选第一。包括不同level同名也计歧义。
8. 单命中start=标题行，end=下一个 `level<=selected_level` 的标题行-1或EOF；深层子标题与其正文包含，空小节仅标题+中间空行合法。
9. 依次保留wikilink归一化：`[[Target|Label]]`、`[[Target#Section]]`、显式heading覆盖anchor、文件名含`#`优先按真实文件现有判断；先正确定位source再扫描标题。
10. `heading+显式range`继续range优先，不因这次增强制造新的互斥breaking；仅 `chunk_id`仍与heading/range/start_char互斥。更新工具说明。
11. 章节结果同C69a字符上限/continuation；next_start_line/start_char可接续用range读取，用户不必反复标题查询重新取第一页。
12. 不改 `_chunk_file` 解析器/缓存代际；如果为读取需要新内部heading helper，只在reading中实现最小扫描并调用现有原语。新增Setext等语法另立待办，不随手改切块。

**固定fixture**：

```markdown
# A
## Target
目标正文
### Child
子节正文
## Sibling
不能混入
# End
```

Target预期lines=[2,5]，Child=[4,5]，A=[1,7]；这是必须锁定的精确断言。

**其他测试**：同名隔远章节；code fence里`## Target`；更长同字符闭围栏；frontmatter假heading；
HTML table假heading；Markdown空标题边界；TXT中文/Chapter+普通正文误判门禁；末尾章节；
BOM/CRLF；未索引文件；正文变更但chunk缓存旧；注入图片/overlap配置不影响物理区间；
wikilink#anchor与`C#教程.md`；range优先；chunk互斥；cap长章节续读；
not found/ambiguous错误均经handle返回isError且候选有限。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_read_heading.py tests/test_read_ranges.py tests/test_wikilink_read.py tests/test_txt_indexing.py tests/test_chunking_seam.py tests/test_facade_freeze.py -q
```

**验收**：无全局chunk并集定位；完整子章节、歧义fail-closed、物理行号与续读精准。提交 `feat(read): resolve heading sections from current source text`。

## 5. 测试矩阵与故障登记

独立QA交接产物：[工程测试清单](C:/Users/芝士雪豹/.gstack/projects/moton16-Mortis-RAG-MCP/feat-v0.8.1-eng-review-test-plan-20261005.md)。它是本轮审查快照，移交其他机器不依赖此个人路径：本节与逐卡测试规格才是仓库中的权威验收要求。

### 5.1 测试设置与执行纪律

- 每卡先写可失败的回归测试，再实现，再跑该卡命令。现有pytest与tmp_path/monkeypatch模式优先，不新引测试框架。
- conftest全局隔离保留；新server用例必须再明确把新旧registry env均指向自己的tmp文件，不能依赖宿主home/默认库。
- app配置静态embedding、reranker关、ingest默认关；测试要开启ingest时仅fake client。subprocess继承UTF-8与缓存env。
- server/watcher测试用try/finally shutdown；Event release、join也在finally，断言失败不能残留线程锁住tmp目录。
- 时间只做宽松死锁看门狗，优先断言前台在释放阻塞Event前返回、调用次数、线程数量、dirty状态；不让网络快慢/机器CPU成为正确性。
- 文件修改用不同长度或显式os.utime配合既有Fast-Stat测试约定；不靠“写两次刚好跨NTFS刻度”。
- sqlite-vec/numpy有无各一组通过CI/指定靶向验证；缺可选依赖允许有理由skip，不能skip基础契约。
- 新用例失败不放宽旧oracle、不删freeze断言、不临时屏蔽autouse夹具。根因、假设、修法三者写技术账。

### 5.2 分支到测试的覆盖图

`EXISTING`只说明已有基础用例，不说明新增行为已覆盖；`NEW`全部是实施时要补的验收规格。

```text
C65 chunk locator                         -> test_kb_read_chunkid.py
  no registry / bad target                -> NEW tool error
  complete: 0 / 1 / multiple hits          -> EXISTING + NEW precise diagnostics
  incomplete: limit / time / cache / cold -> NEW fail-closed + solo privacy
  temp probe close / promote missing      -> NEW resource + stale transition
  vector fallback w load_vectors=False    -> NEW zero vector-load

C66 request_refresh                       -> NEW test_read_stale.py
  missing/empty input: normal read route  -> EXISTING normalization
  cold static/external / warm cache       -> NEW state distinction
  warm ready/locked/slow sync/error       -> NEW Event ordering + old result
  bursts / during-sync dirty / throttle   -> NEW single-flight + follow-up pass
  stop/start/remove/shutdown              -> test_p5_lifecycle.py + NEW
  scoped/global query once/weight/solo    -> EXISTING + NEW state metadata
  stale chunk signature                  -> NEW physical snapshot validation
  parse -> double-argument callback       -> test_ingest_server.py + NEW E2E

C58 config + manager                      -> NEW test_ingest_auto.py
  absent/false / true+disabled            -> NEW no side effects + diagnostics
  cap0 / boundary±1 / invalid             -> NEW all entrances + config fallback
  scan / explicit / force / recovered     -> NEW policy-before-HTTP
  ignore / symlink / changing copy        -> NEW privacy + source_changed
  ledger absent/migrated/>500/pruned       -> NEW persistent dedupe
  repeated auto-submit                    -> NEW same-manager dedupe
  native/poll/start/overflow/fallback/0    -> test_watch_integration.py + NEW

C67 projection                            -> NEW test_compact_search.py
  default/full/preview/compact conflicts  -> NEW exact shape + old compatibility
  single/scoped/global/grouped            -> NEW stable order + read back E2E
  filters/dedupe/weight/exact_terms        -> EXISTING oracle + projection parity

C68 budget                                -> test_budget_bytes.py + NEW
  none / all / partial / first too large  -> NEW whole-prefix, zero-progress hint
  metadata impossible / cold / empty      -> NEW envelope exception truthful
  CJK/escapes/emoji/boundary±1             -> NEW double serialization
  grouped partial / empty group/cursors   -> NEW continuation E2E
  source mutation / deep offset bound     -> NEW immutable input + scoped advice

C69 physical read                         -> NEW test_read_ranges/read_heading.py
  no span / valid / beyond / reversed     -> NEW exact effective lines + errors
  empty / BOM / CRLF / invalid UTF-8      -> NEW nil-vs-empty distinction
  cap / long single line / continuation   -> NEW concatenation round trip
  direct/short/wiki/#filename/chunk stale -> EXISTING + NEW route integration
  heading absent/duplicate/levels/fences  -> NEW bounds exact, limited diagnostics
  tables/frontmatter/TXT chapters         -> NEW shared parsing primitives

Schema/skill/release                      -> test_mcp_stdio/version_sync/facade_*.py
  15 tools / immutable exports / stdio    -> EXISTING + NEW optional fields
  initialize0.8.1 / errors isError         -> NEW protocol smoke
  docs examples -> search/read/auto       -> NEW fixture workflow + manual check
```

### 5.3 Error & Rescue Registry

| 方法/路径 | 失败与异常 | 捕获/处理 | 用户看到/恢复动作 | 必测卡 |
|---|---|---|---|---|
| locator默认库 | 无注册条目，ValueError | tools/call工具错误 | 先kb_init，不是IndexError | C65 |
| locator probes | bin缺失/codec损坏/目录消失，OSError/ValueError/zlib.error | 记未探测，finally关闭临时句柄 | 指定vault_path，不假称唯一 | C65 |
| probe promotion | 目标ID已消失，ValueError | 停止展开 | 重新kb_search | C65 |
| request_refresh | 已停止 | 返回未接受，不启动线程 | stats显示stop/pending口径；显式重启服务 | C66 |
| background sync | OSError/ProviderError/意外异常 | worker边界捕获、失败计数/退避、error状态 | 现有结果+刷新错误；修配置再刷新 | C66 |
| cold search | 无可用索引 | indexing结构化响应 | retry_after后重试/查看stats | C66 |
| query embedding/rerank | ProviderError、超时、429 | 保持现有词法/基础排序降级 | 不改本版provider重试策略 | C66 |
| size gate explicit | 超cap，ValueError | 整批入队前拒绝 | 实际size/limit，增cap或改小文件 | C58b |
| size gate scan | 超cap | skip，pending reason=too_large | 看limit/拒绝计数；不上传 | C58b |
| path validation | 出vault/链接/非支持格式，ValueError | 入队与执行双闸门 | 相对source+原因；不能绕过 | C58b |
| state读写 | OSError/JSONDecodeError/非法结构 | 具名安全回退+诊断；写失败不宣称已入队 | 检查权限/状态，不删状态盲重试 | C58b |
| cloud transient | MineruError(retryable) | failed，保持hash免自动重试 | 手动重试，不自动429风暴 | C58b |
| cloud permanent | MineruError(nonretryable) | 按既有仅PDF可选fallback | parse_quality/fallback说明 | C58b |
| copy changed | hash不符，source_changed | 旧job失败，后续新hash候选 | 文件稳定后自动新任务 | C58c |
| auto hook/scan | worker边界异常 | 记last_error+退避，不杀文本线程 | stats/status诊断，检查配置/权限 | C58c/d |
| apply_budget first | 整条放不下 | 零结果、原cursor、hint | 开compact/增加预算 | C68 |
| apply_budget envelope | 最小控制信息>budget | 显式budget_exceeded+minimum | 缩小目标库或增加预算 | C68 |
| read range | start超EOF/非法span，ValueError | 工具错误 | actual total、核对分卷/用heading | C69a |
| read source | FileNotFoundError/PermissionError/UnicodeDecodeError | 工具错误，不吞为空 | 修source/权限/UTF-8 | C69a |
| read stale chunk | 磁盘hash≠索引签名，ValueError | 拒旧行号展开 | source+heading或重新kb_search | C66/C69a |
| heading | 不存在/重复，ValueError | 有限候选/行号提示 | 改标题或用行区间 | C69b |
| stdio serialization | 孤立Unicode代理、UnicodeEncodeError | 保留现有ensure_ascii降级 | 合法JSON，不stdout日志 | C60 |

catch-all只允许线程/任务最外层的“保服务可用”边界，必须记状态与计数；内部逻辑优先具名异常。diag仍仅原10键白名单，不为丰富诊断泄露内容；用户显式取status才给限长详情。

### 5.4 Failure Modes Registry

| 失败模式 | 风险 | 防护 | 测试/可见性 | 发布阻塞 |
|---|---|---|---|---|
| 部分库未探，却读“唯一”chunk | 错库/隐私歧义 | incomplete拒绝 | C65 / skipped候选 | 是 |
| warm request同步补嵌40s | 交互阻塞 | 前台只置dirty | C66 / Event释放顺序 | 是 |
| 100query起100线程/100扫描 | 额度/CPU | 单调度器+1s节流 | C66 / 次数断言 | 是 |
| query API本身很慢 | 有限外部残余 | 既有provider timeout/降级 | stub/eval、明确不是sync修复范围 | 不新增承诺 |
| failed被500历史剪掉重传 | 付费重复上传 | 独立auto_seen | C58b / >500跨重启 | 是 |
| 多进程同时操作同一摄取队列 | 既有并发边界 | 后续专项，不新增本版owner/lease | 记Execution-plan，不阻塞本版 | 否 |
| ignored/private PDF自动上传 | 隐私不可逆 | matcher+symlink+运行时再校验 | C58b/c / parse调用0 | 是 |
| 大文件scan/force/recovery越cap | 额度 | 双闸门，先size后hash/client | C58b / all entrances | 是 |
| hook拖死文本防抖 | 搜索新内容迟滞 | 独立单扫描worker | C58c / slow hook屏障 | 是 |
| poll/interval0自动静默无效 | 多平台功能缺失 | effective cadence定义 | C58c/d / fallback/0 | 是 |
| 首条巨大但游标推进 | 丢失证据 | whole-prefix，零结果cursor不动 | C68 / zero progress | 是 |
| grouped cursor用总returned | 重复/漏读 | per-vault cursors | C68 / 单库续页 | 是 |
| metadata>预算却声称硬上限 | 宿主响应异常 | budget_exceeded | C68 / wrapper测量 | 是 |
| 同名heading并集跨章节 | 错读正文 | 原文scan+歧义拒绝 | C69b / precise lines | 是 |
| 超EOF静默空内容 | Agent误判文件空 | actual total诊断 | C69a / 1850 vs11340 | 是 |
| 单行被cap截却无续读入口 | 无法获取剩余证据 | start_char continuation | C69a / roundtrip | 是 |
| stale chunk行号读到另一段 | 错证据 | 同bytes签名检查 | C66/C69a | 是 |
| stop后线程复活/引用丢弃 | 句柄/并发写 | retain alive references | C66/C58c / lifecycle | 是 |
| 本地自动测试真联网 | 泄露key/费用/flaky | tmp配置、FakeProvider/client | C54守卫+新卡全程 | 是 |

只验证本版实际改动，不为审查建议另起防御性架构；多进程worker治理、完整检索事务快照与无限历史hash去重均不纳入本版。

## 6. 文档、评测与发布施工

### [ ] C59：Windows升级占用说明

**改动**：`docs/Quick-start_developer.md` §7/§9与 `QUICKSTART_user.md`新增“升级已有部署”，README不塞内部排查细节。纯文档，所有命令需在隔离进程或只读方式核对。

1. 说明现象：Windows运行中的console入口exe被占用，editable安装替换入口可出现WinError5。`git pull`只更新源码，不保证运行进程/已安装入口已重新加载。
2. 正常路径：关闭/停用对应MCP连接器；确认该项目进程退出；再安装；重新启用连接器；initialize版本/kb_list验证。不要说“重启终端”就一定解除IDE持有的锁。
3. 用下面只读进程清单定位具体PID与CommandLine，不按名称全杀python：

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -in @('mortis-rag-mcp.exe', 'vault-mcp.exe') -or $_.CommandLine -like '*mortis_rag_mcp*' } |
    Select-Object ProcessId, Name, ExecutablePath, CommandLine
```

4. 首选在客户端停服务。只有用户已确认确为本项目且客户端无法关闭时，才文档化 `Stop-Process -Id <确认过的PID>`；不是执行本卡时主动杀进程。客户端自动拉起的要先关连接器，不循环kill。
5. 安装命令用本venv python，避免装到另一个环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

6. 推荐后续连接器用该venv `python.exe` + `-m mortis_rag_mcp --serve-mcp-stdio`，减少console exe覆盖冲突；**运行中模块不会自动热更新，仍须重启**。保留旧console连接器兼容，不删entry point。
7. 不推荐“先pip uninstall”规避写锁：锁未解时卸载也会失败。系统重启仅作占用无法解除的最后排障，不作为常规升级步骤。
8. 验证新文档中的命令路径/entrypoints与当前pyproject一致，示例不含真实vault/key。不写新CLI shutdown命令。

提交 `docs: explain Windows MCP process locks during upgrades`。验收：用户知道哪个进程、如何安全停、怎么装、怎么确认新版本，不需要猜命令。

### [ ] C70/T8：文档、使用纪律、历史欠账与离线评测

**前置**：所有代码卡。本卡必须在版本bump前完成行为描述；版本文字在C60统一更新。

#### C70.1 文档逐处同步

| 文件/节 | 执行动作 | 必须包含 |
|---|---|---|
| PROJECT_GUIDE顶部/§一/§二 | 改活的旧包名/旧“不索引非Markdown”描述，不改历史记录 | mortis_rag_mcp、MD/TXT、PDF先转MD、setuptools>=77、真实entrypoint |
| PROJECT_GUIDE §三/§五/§九 | 改“search先同步”时序与锁说明 | read-existing→background refresh、state/cold、条件锁不跨sync、scan worker |
| PROJECT_GUIDE §4.5/§4.8 | 更新Facade/read/search/watch responsibilities | compact投影、whole budget、physical heading/range、A4双参 |
| PROJECT_GUIDE §四 | 新增ingest模块专节（原只有4.1–4.10） | state/auto_seen、单队列、size/ignore、默认授权、fallback |
| PROJECT_GUIDE §六API | 逐工具对齐真实schema | compact/start_char/group_offsets、group游标、pending/sources、list_files分页、chunk跨库solo |
| PROJECT_GUIDE §七 | 新增ingest与已有index/diag参考 | auto_watch=false、20MiB、0、不生效组合、read_max_chars |
| PROJECT_GUIDE §八 | env与状态文件布局 | 新旧CACHE_DIR对、explicit TOML优先、auto_seen字段不是chunk缓存 |
| PROJECT_GUIDE §十/§十一 | 更新实际摄取与测试说明 | 默认关、既有ignore/沙箱、size策略与新增测试文件，不写未实现worker所有权 |
| PROJECT_GUIDE §十五 | 倒序新增0.8.1技术详录 | 实际editor/agent、各卡与测试证据，不伪造model/日期 |
| Quick-start_developer | 重新核对地图/流程/测试命令/Windows | 按实际行数/文件数；不要沿用49文件416用例，UTF-8与靶向本地策略 |
| Docs_Folder-descriptions | 保留5主文档与v0.8.1例外 | 明确PLAN跟踪，不说全部版本目录均获例外 |
| Execution-plan_developer | 已完成打勾，未实现“现状”订正 | FTS探测仍非严格只读、无限深页延后、watcher原生延后、控制正确TODO |
| QUICKSTART_user | 简单可运行例子与升级说明 | 紧凑初筛→原文、heading重名改行号、自动默认关、size、Windows；删无样本依据的preview“70%+”数字 |
| README/README_EN | 面向用户的新能力摘要 | 删除无样本限定的preview“降低70%+”承诺；不承诺所有数据零上传/搜索固定毫秒 |
| config/app.toml.example | 复核新增配置注释 | 自动成本/隐私、初启既有文档、各格式、0与cadence |
| skills/mortis-rag-mcp/SKILL.md | 可判定if-then纪律 | 以下C70.2六条 |
| Changelog_developer | 补已提交C53/C54/C55/C56/C57/C62/C63/C64欠账，记新卡 | 实际commit；C53真实云端验证或待验证原因 |
| CHANGELOG_user | 留到C60按release追加 | 仅用户功能/体验和必要升级变化，不写底层函数/线程细节 |

不对历史CHANGELOG里的旧版本号/旧包名作全局替换；它们是发生过的事实。当前API引用必须真实，主文档的历史章按版本保留。

#### C70.2 Skill调用纪律与样例

1. 大候选初筛：已知库必须定向；`top_k>=5`优先 `compact=true,budget_bytes=3500,use_rerank=false`，不继续沿用“preview必省70%”的无条件量化承诺。
2. compact没有chunk_id：用顶层/条目/group的vault + source + lines回读，不能调不存在的ID。普通preview/full保留id，唯一完整命中可chunk读取。
3. budget返回returned0且truncated：不得同参数无限重试；开compact/增budget/缩库。budget_exceeded按minimum或缩目标，group把group_next_offsets原样作为group_offsets同路由续页，不切单库后沿用旧排序游标。
4. read越界：以actual total核对分卷行号；已知标题改heading；ambiguous heading用候选物理行消歧；truncated用next_start_line+next_start_char而不是旧end_line+1。
5. indexing/cold/stale chunk：看stats与retry_after；无用户指令不kb_rebuild；源已更新可source+heading直接读，不用陈旧chunk行号佐证。
6. 自动摄取默认关：仅解释如何opt-in，用户授权前不修改enabled/auto_watch、不自动手动submit；失败同hash要用户主动重试。pending/status本身不是云上传授权。

复制样例用实际库名占位与实际参数，不能`preview=True`作为JSON布尔：

```json
{"query":"目标实体","vault_path":"小说库","path_prefix":"vol08","top_k":30,"compact":true,"budget_bytes":3500,"use_rerank":false}
```

```json
{"source":"vol08/part03.txt","vault_path":"小说库","start_line":697,"end_line":720}
```

```json
{"source":"vol08/part03.txt","vault_path":"小说库","heading":"Chapter 907: Section Title"}
```

```toml
[ingest]
enabled = true
auto_watch = true
max_file_size_mb = 20
api_key = "${MINERU_API_TOKEN}"
```

最后一个仅供用户主动授权配置；无key时原有通道/fallback语义保留，文档不承诺公共额度或服务端限额永不变化。

#### C70.3 离线质量与payload量测

1. 在临时目录创建配置，embedding.static、reranker=false、cache临时、ingest=false。**脚本直接调用load_config，不能指望pytest的conftest来隔离**；明确传 `--config`，避免宿主配置。
2. 对 `tests/eval/golden_queries.json` 与其仓库fixture跑 `--k 5`。若golden引用外部私人路径，改用临时fixture及本地golden副本，不改用户真实vault、不伪造100%。
3. 本轮代码未改前记录基线Hit@5/MRR@5；全部卡后同配置同语料复测，不低于基线。现有历史100%不能替代这次实际输出。
4. `test_search_oracle/test_golden_v073/test_facade_freeze/test_cache_codec_roundtrip`验证排名、缓存和导出不变量；投影及线程变化不允许破坏这些。
5. 用固定30候选fixture输出full/preview/compact字节、budget3500最终字节/returned；记录Python/OS/可选依赖、语料规模、cold/warm。
6. refresh延迟隔离量测：FakeProvider为sync补嵌设Event暂停，query不联网；测30连续查询的p50/p95/max和indexing标志。报告“无前台sync”，而非推导外网端到端毫秒保证。
7. tools/list schema体积采用 `len(json.dumps(_tool_definitions(),ensure_ascii=False).encode("utf-8"))`，分别对origin/main(v0.8.0)与本轮a8fbb4d基线测量；基线已实测12563/13412 bytes、+6.758%。同时说明是否测整个tools/list包装，不能混比较口径。
8. 目标守住历史≤10%增量门禁；compact/start_char/group_offsets/必要安全描述优先，精简重复description。当前距10%只剩约407 bytes（12563*1.1向下取整-13412），新增三参数及说明极可能超额，实施时先量测。若超限，**记录准确字节/百分比并提交用户例外确认**，不删必要能力、不把工具required伪造、不悄悄换基线冒充达标。
9. 不扩C55 schema属性/required（历史裁定）。注明vault_path handler别名对严格schema客户端仍有限，不把description支持宣传成所有客户端可校验的替代参数。
10. 技能规则变动至少用mock语料实测“compact→read、越界→heading、零预算进展→增预算、自动关→无上传”四流程；不用付费LLM evaluator或真实MinerU做自动测试。

可直接执行的隔离量测配置生成命令，先在项目根目录运行§0.3的UTF-8设置。只创建本轮临时目录，不改变用户配置；显式禁用所有外部服务与诊断输出。使用UTF-8无BOM，兼容Windows PowerShell 5的TOML读取：

```powershell
$qaRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('mortis-v081-qa-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $qaRoot
$qaConfig = Join-Path $qaRoot 'app.toml'
$qaCache = (Join-Path $qaRoot 'cache').Replace('\', '/')
$qaToml = @"
[embedding]
mode = "static"
dimension = 384
[reranker]
enabled = false
[ingest]
enabled = false
[cache]
enabled = true
dir = "$qaCache"
placement = "home"
max_age_days = 0
[vector]
backend = "memory"
[diag]
enabled = false
"@
[System.IO.File]::WriteAllText($qaConfig, $qaToml, [System.Text.UTF8Encoding]::new($false))
.\.venv\Scripts\python.exe scripts/eval_search.py --golden tests/eval/golden_queries.json --config $qaConfig --k 5
.\.venv\Scripts\python.exe -m pytest tests/test_search_oracle.py tests/test_golden_v073.py tests/test_facade_freeze.py tests/test_cache_codec_roundtrip.py -q
git diff --check
```

Golden预期目前仅 `HIT(#1) / Hit@5 1/1 / MRR@5 1.000`；只有1条公共语料，不外推大库。后续payload/时延量测也沿用该隔离配置，但必须另准备卡片指定的30候选/多库/长单行fixture，而非把1条Golden当综合性能基准。

**提交**：文档分两笔可读变更（开发者现状/技术账，用户指南/skill）；C70状态仅在两笔及全部验证通过后打勾。不要把新功能提前写成已发布。

### [ ] C60：0.8.1版本收口与发布交接

**前置**：C65–C70、C58全子卡、C59已完成。不是当前审核轮要执行的git发布命令。

1. 同批更新 `pyproject.toml version`与 `mortis_rag_mcp.__version__` 到0.8.1；SERVER_INFO/diaglog应引用包真源，不重新加硬编码。
2. README中英文badge、Skill标题包版本0.8.0→0.8.1；Skill自己的frontmatter version当前5.2.0，行为纪律增强独立递增为**5.3.0**，不误改成包0.8.1。历史能力标题保留历史首次加入版本。
3. CHANGELOG_user顶部新增0.8.1，实际发布日期以发布当天为准，非自动照抄审核日期；大白话描述修复上传、自动摄取、阅读/初筛/升级体验。
4. 必须列升级变化：20MiB默认影响手动/自动全部入口；0不限需用户显式设；完整chunk预算可能放不下一条；分组预算有独立续页位置；read越界提示实际行数、heading读取完整章节；检索先返回可用结果再后台刷新。用户CHANGELOG用大白话，参数名/响应字段放QUICKSTART与API文档；不改变旧parsing恢复行为。
5. `MORTIS_RAG_CACHE_DIR`说明放用户指南/配置表，不把env实现逻辑写进用户CHANGELOG。C56多库唯一寻址升级可用户可读说明，D5保持。
6. C53真实云端验收写二选一：`实机验证：已通过（日期/通道/样本）`或`待实机验证（原因/由谁后续确认）`；不使用test回环200冒充OSS成功。
7. `tests/test_version_sync.py`增加README badge、Skill包版本、diaglog版本的对应检查，但不要求历史每一版本字符串全相等。现有三项仍保留。
8. 运行靶向发布集与Python语法编译：

```powershell
.\.venv\Scripts\python.exe -m compileall -q mortis_rag_mcp
.\.venv\Scripts\python.exe -m pytest tests/test_version_sync.py tests/test_diaglog.py tests/test_mcp_stdio.py tests/test_facade_freeze.py tests/test_cache_codec_roundtrip.py -q
git diff --check
git diff --stat
git status --short --branch
```

9. stdio smoke：initialize版本0.8.1、15工具列表、新参数可见、ping正常、invalid range与heading歧义返回isError、无stdout日志。测试用临时registry/config，stderr净或只既有允许诊断，不探真实API。
10. 全量回归交CI：现workflow是**一个test job、5个matrix实例**（Ubuntu3.10–3.13+Windows3.12），不是“单job意味着只有一组”、也不是feature分支push自动跑。需PR或workflow_dispatch才触发。
11. 这轮只交接计划；下个开发者未获用户发布授权不要push/创建PR/tag。获授权走 `/ship`，PR正文清楚关联#5/#6与真实云端验收状态；确认GitHub所有矩阵，不以本机targeted绿替代。
12. 发布前检查尚未解决的schema例外/实机风险，披露或等待用户；不能把“计划审阅通过”写成“代码审阅通过”。最终tag/合并由授权发布流程执行。

**完成记账模板**：

```text
卡：Cxx / commit：
操作者：实际GitHub账户，日期，agent；仅确知时写模型名
改动：函数/数据流/兼容性
验证：实际命令、passed/skipped、是否外部网络
量测：语料规模/环境/字节或延迟，不以估计当结果
残余：已验证与未验证的边界
```

### 6.1 真机验收与回滚运行簿

**授权真机验收**只在用户确认样本可上传且可消耗额度后进行。用非私密小样本库，不自动从真实课程/小说/工作库选PDF；不输出key/预签名URL。步骤：

1. 先单独验证MinerU两通道实际上传、完成产物与parse_quality（缺key只能Agent，不冒充v4验收）。
2. 手动submit后status到done，产物存在、A4刷新后search命中；记录源size、输出相对路径、时间，不上传不必要文件。
3. auto_watch关时新放PDF不parse；显式授权开启+重启后仅可用样本自动入队，同hash失败/成功不重提、超cap与ignore不上传。
4. 文本编辑仍可索引；慢parse不能阻止warm查询，search返回状态诚实。
5. 完毕关闭自动、停止样本连接器；样本库/产物是否保留由用户决定，不擅自清理。

```text
异常 -> 是否自动上传风险?
          yes -> 用户将auto_watch=false并停客户端/重启 -> 禁止新入队
          no  -> 保存诊断/状态 -> 对应卡具名修复
              -> 需要回退?
                  -> 停客户端 -> 安装用户指定旧版本 -> 重启 -> 隔离smoke
                  -> 保留源文档/.mortis-parsed/.ingest_state/注册表与缓存
```

回退不做git reset、不删状态来“变干净”。新auto_seen为旧版本可忽略字段；原bin缓存未变；旧版本预算策略/heading并集语义会恢复，用户须知。已提交云端文件不能由本地回滚撤销，绝不承诺撤回隐私泄露。

## 7. 完成门禁与交接

### 7.1 Issue覆盖表

| 需求 | 验收卡 | 可关闭Issue的证明 |
|---|---|---|
| #5.1 上传403 | C53已有+C60真机字段 | 请求头回环测试+真实成功或未验证披露 |
| #5.2 测试隔离 | C54已有+C70 | UTF-8靶向、宿主隔离守卫、不联网 |
| #5.3 路径别名 | C55已有+C70 | handler测试、schema限制诚实描述 |
| #5.4 chunk跨库 | C56/C64已有+C65/C66 | 完整唯一/歧义/incomplete/solo/旧ID |
| #5.5 list分页 | C57/C62已有+C66 | prefix/offset/total/cursor+读优先 |
| #5.6 Windows写锁 | C59 | 正确定位PID/停连接器/安装/验证 |
| #5.7 auto_watch | C58a–d+C61 | opt-in、多平台节拍、cap/ignore/ledger、完成检索 |
| #6.1 compact与budget | C67/C68 | 四路投影、whole chunk、不可满足envelope/游标 |
| #6.2 bounds | C69a | actual总行数、越界工具错误、续读 |
| #6.3 heading | C69b | 物理章节/子章节/重复/保护/未索引 |
| #6.4 stale/read优先 | C66 | 慢sync Event下前台返回、状态、单flight |

### 7.2 本版本发布清单

- [ ] §3所有卡按依赖完成，记录真实commit，无“代码写了但测试没跑绿”卡。
- [ ] 本版新增路径靶向通过，重点完整预算、failed历史剪枝、章节/越界与watch触发；不扩到新owner/lease测试。
- [ ] 已提交功能回归与新功能一起靶向绿，基础可选依赖缺失路径有证据。
- [ ] search排序/权重/dedupe/exact_terms、bin roundtrip、Facade导出冻结通过。
- [ ] CLI/stdio initialize0.8.1、15工具、isError、UTF-8、纯JSON输出通过。
- [ ] 零新运行时依赖；不变注册表/缓存代际；不会全库重嵌。
- [ ] 用户指南/Skill示例与schema逐条对上，没有ghost参数scan_pending/source。
- [ ] schema字节门禁通过或有用户显式例外；没有换比较基线掩盖超限。
- [ ] C53真机字段不空，auto_watch费用/隐私与默认关说明到位。
- [ ] 版本同步/用户升级变化/开发者技术账/主文档现状更新完成。
- [ ] 本地不跑全量的约定保留，PR/手动CI五matrix通过；发布授权独立确认。
- [ ] git diff --check绿；stage仅本卡文件；没有个人路径/key/用户真实配置或生成缓存入仓库。

### 7.3 每卡交接与阻塞处理

每完成一张卡追加一段到本版本Worklog，必须写：当前HEAD/分支、已完成卡、刚执行的测试命令/输出、下一卡及依赖、待验证云端/schema例外。代码与文档不同步时该卡不标完成。

遇到阻塞先用最小单用例复现、记录具体异常与现有相关改动；只能回到该卡指定文件修。若需要改变D1–D5、上传授权、缓存格式或引新依赖，停下向用户提一个具体问题；不偷换方案、不牺牲隐私闸门、不循环重试真实API。用户选择后修本文件契约/测试/记账三处。

下一个开发者的第一件实施工作是**C65的失败用例**，不是从Lane E重来，也不是把旧stash应用进来。

## 8. Autoplan 审核与决策日志

### 8.1 Phase 1：CEO / SELECTIVE EXPANSION

前提挑战：#6描述的“heading不存在”和“字符串裁剪破坏JSON”不成立，已根据源码修正为章节语义与完整chunk契约。#5已完成部分不能重做，未提交历史也不能当当前代码。一次0.8.1与默认opt-in仍按历史裁定，补全#6不改变发布节奏。

1. **架构**：检查Facade/私有子包、watcher与manager边界；复用既有对象，拒绝新增任务框架与持久化索引。新refresh/自动扫描按每库合并，不把耗时操作塞防抖线程。
2. **错误救援**：检查首chunk预算、行号/heading、缓存缺失与无key通道；要求可判定结果、修法与测试。具体异常和用户动作见§5，不以“吞异常不崩”代替验收。
3. **安全**：检查自动上传的授权范围、ignore、symlink、size二次校验与solo歧义。自动路径比手动更严格，失败不会无限重传，部分探测不得假称唯一。
4. **数据流**：缺省与空值分别定义；空库不是冷启动，空文件不是行号越界，同名标题不是单章节。状态分页必须能告诉agent下一步是重试、加预算还是指定库。
5. **代码质量**：检查重复入参解析与单库双路由；只抽必要纯函数和状态调度，保留Facade薄委托。禁止反向导入/Chunk字段重排/缓存代际漂移。
6. **测试**：已有73项证据与新增矩阵分开记账；线程测试用Event屏障，不以sleep猜调度。云端真机验收要写验证或未验证原因，不能冒充本机HTTP回环。
7. **性能**：扫描、补嵌、query embedding、rerank分别归因；目标是不让同步占前台，不承诺整个搜索固定毫秒。大标题扫描只读一份原文，预算计量不重复序列化无界候选池。
8. **可观测性**：STATUS/doctor是快照而非实时服务；实时状态由kb_stats/status返回，diag白名单不加原文/key/路径。冷启动、后台错误、自动拒绝原因必须可区分。
9. **部署**：compact默认关，auto_watch默认关；仍要公开size默认20与预算首条策略的行为变化。Windows先停入口再安装，回滚不删原始文件/解析产物/状态。
10. **长期轨迹**：把复杂一致性、无限游标、全Markdown解析留后续，当前契约可测试、可回滚。施工正文只保留一个有效方案，历史审计在Git中保留。
11. **Design**：无浏览器、页面或可视界面，Phase 2不适用；MCP返回结构与错误交互在Eng/DX审核。

CEO独立声部：首次子代理因执行器reasoning参数报错，重试成功。独立审阅确认缺少#6、预算口径、行号/新鲜度风险；其建议新增 `kb_read.preview/read_budget_bytes` **不采纳**，因为#6初筛是kb_search、不应混淆搜索预算与精读字符上限。

| CEO维度 | 主审 | 独立声部 | 裁决 |
|---|---|---|---|
| 前提 | 纠正3类失效前提 | 原计划缺#6 | 已修正文 |
| 问题 | #5剩余+#6四组 | 读契约不完整 | 搜索与读分别落卡 |
| 范围 | 不加框架/读preview | 提议额外读preview | 按issue原范围，拒绝扩张 |
| 替代 | A/B/C比较 | 缺替代契约 | B指定为施工方案 |
| 外部风险 | 云隐私/额度/宿主大小不固定 | 市场项N/A | 不编造市场数据 |
| 六月后 | 可回滚契约+追踪待办 | stale/预算固化风险 | 测试锁定并记录升级行为 |

注意：这是Codex主审+独立子代理，不声称已运行Claude CLI，不伪造跨模型一致结论。完整新增方案仍待用户确认。

**CEO Completion Summary**：SELECTIVE EXPANSION；接受#5剩余与#6四组增量，拒绝读preview和新持久化框架。既有能力/失效前提在§1，范围与长期差距在§2，error/rescue和failure登记在§5。没有提出改变用户“一次0.8.1”方向的User Challenge；自动上传授权、云端验证和新增Taste契约仍受最终门禁约束。行业规模/商业收入/市场份额没有可靠项目证据，已检查并标N/A，不编造数字。

<details>
<summary>收口前的工程与DX审核记录：仅留档，不参与施工</summary>

### 8.2 Phase 3：Eng / FULL REVIEW

**历史审查记录说明**：本节保留范围收口前的独立意见，不是额外施工清单。用户最新明确要求不扩防御性工程；下文涉及集合/状态新锁、worker独占/租约、保守parsing恢复、上传副本的建议均未采纳。本版实施以§0范围声明及§4当前卡为准，不能把本节旧处置说明重新变成发布前置。

**Scope Challenge**：不能把#6当四个schema字段补丁；读优先会扩大并发窗口，已经实施的跨库寻址也有fail-open，自动摄取则必须保护跨进程消费。核实 `sync_engine.run_sync` 的逐文件文本修改、worker构造中的parsing恢复、registry文件锁失败退化、fanout的固定候选窗，因而把安全/生命周期前置，而不是先做compact再补测试。范围不扩到新任务框架或原子检索后端代际。

**架构与依赖图**：

```text
config.py -> indexer Facade -> private watch / sync_engine / search / reading
     |           |                 |           |
     |           |          short chunks/state locks
     |           +-> request_refresh -> existing scheduler -> sync lock
     +-> ingest manager <--- separate scan hook
                |       source -> frozen upload file -> parse -> A4 refresh
                +-> required state lock + lifetime worker-owner lock

server dispatch -> route -> rank/filter/dedupe -> projection -> whole budget
      |             |                        |             |
      |          single/scoped/global      compact      group_offsets
      +-> source path -> bytes snapshot -> range/heading -> char continuation

C65 -> C66 ------------------> C67 -> C68
          |                      (projection before budgeting)
          +------------------> C69a -> C69b
          +-> C58c <- C58b <- C58a
                 +-> C58d
all code + C59 -> C70 -> C60 -> authorized CI/release
```

1. **Architecture（置信9/10）**：`run_sync` 当前会执行 `owner._chunks[source] = chunks`、`owner._chunks.pop(source,None)`，所以“已有索引”不等于上一轮原子提交的全后端快照。C66改为短锁稳定集合/统一进度状态，公开最终一致性，不无意承诺事务隔离；FTS候选仍按by_id过滤。完整代际构建是独立审阅建议，当前不采用，避免补丁版变成同步引擎迁移；该取舍列Taste和后续待办。
2. **Code Quality（置信9/10）**：优先watch/reading等私有纯函数，Facade薄委托，保留7导出与Chunk结构。公共read原来不截字符，不能把MCP cap倒灌到其API；C69a明确public无限字符、server显式cap。锁错误不能silent-yield继续写摄取账本，新增required/nonblocking选项但默认注册表行为保持，避免借此重构整套注册表。
3. **Tests（置信9/10）**：pytest已存在，新增分支与fixture在§4/§5.2逐条指定；真实两进程与Event屏障必要，线程数/调用次数/次序才是CI正确性，不用“应该很快”的sleep断言。既有122项和1条Golden是基线，新测试还未创建；外置QA清单已保存，不能声称实施覆盖率100%。所有nil/empty、bounds、incomplete、锁失败、预算零进展、长单行/分隔符和stop分支都有拟定验收。
4. **Performance（置信8/10）**：1s普通read节流、native事件合并、PDF扫描离开文本scheduler，阻止请求数线性增加线程/扫描。不把query embedding/rerank时间混作sync，不把32库/2s软probe当硬超时。完整chunk预算对固定候选池做正前缀二分；0条hint和全量形状独立测，避免混入不同包络导致非单调。上传快照增加最多一个源副本的磁盘开销，cap0与磁盘满有明确失败语义；实际大库IO/p95仍需C70量测。

**独立工程声部与处置**：中断前agent恢复查询为not_found，不计成功；恢复后新独立审阅完整给出8项。下表都已写入施工卡，而不是仅留在审核意见中。

| # / 严重度 | 独立意见与代码证据 | 主审处置 | 是否还有施工设计空白 |
|---|---|---|---|
| E1/P1 | run_sync逐文件变异/all_chunks乐观重试；建议完整代际读视图 | 接受一致性缺口，C66短集合快照+诚实最终一致；重后端代际不选 | 无；较强一致性延后/Taste |
| E2/P2 | `_sync_progress`写入与copy无共同锁 | C66独立短状态锁，全写点一起修改 | 无 |
| E3/P1 | vault-startup线程未保存，shutdown后可构造新库 | C66服务stop event/创建双检/startup句柄/移除防复活 | 无 |
| E4/P1 | `_process_file_lock` open或locking失败仍yield | C58b required锁，IO失败拒绝写/入队 | 无 |
| E5/P1 | constructor把活parsing改queued；建议续租owner | 接受；采用更简单OS生命周期独占锁+保守中断失败，无到期抢占 | 无；恢复行为Taste |
| E6/P2 | 单最新sha无法永久记住A→B→A | 明确契约仅连续未改版本免重提，sha实际变化可入队，不无限存旧hash | 无 |
| E7/P1 | grouped换单库路由改变dedupe/rerank；候选窗有限 | C68新增group_offsets同路由，固定候选池，明确非事务/无限游标 | 无；额外输入Taste |
| E8/P2 | newline前后同叫“行边界”会丢/重复换行 | C69a两个坐标与cap100/101拼接测试明确指定 | 无 |

| Eng维度 | 主审 | 独立声部 | Consensus |
|---|---|---|---|
| 范围 | 完整安全增量，拒绝重后端迁移 | 原子代际才能称已提交快照 | DISAGREE，改承诺边界并列Taste |
| 架构 | 复用scheduler与OS独占 | 需要owner/lease | 问题CONFIRMED，具体机制不同 |
| 质量 | strict锁/稳定集合/字符边界 | 指出silent-lock/state copy | CONFIRMED |
| 测试 | 进程/Event/换行/group闭环 | 至少8新增分支缺口 | CONFIRMED，均落到卡 |
| 性能 | 前台无sync，固定池二分 | 固定响应正前缀二分可单调 | CONFIRMED，不承诺所有网络快 |
| 失败恢复 | parsing未知结果手动恢复 | 禁止把活parsing重排 | 安全需求CONFIRMED，保守恢复新增取舍 |

这是主审+独立子代理的意见比较，不是Claude+Codex双CLI验证。5/6维度在问题层面一致，1处一致性方案取舍已列门禁；具体机制不同不能宣传成完全跨模型一致。

**Eng Completion Summary**：FULL_REVIEW；8独立发现全部有确定处置，未留施工设计空白；8项中的较强代际建议降为诚实契约+待办，未假称已修代码。架构/测试图、failure与error登记、外置QA产物、NOT In Scope、既有能力清单均已保存。实施仍有§7所有发布阻塞待验证；方案Reviewed不等于代码Clean或ship Cleared。

### 8.3 Phase 3.5：DX / POLISH

**产品与人物**：本地Python MCP服务+用户Skill；主要persona是熟悉Python/PowerShell/pytest但第一次接手本仓库的贡献者，另需兼顾通过IDE连接器升级的库主人。已有Python3.10+、已clone项目是“热准备”前提；全新系统安装Python/客户端不计入≤5min的热准备目标，必须分开报时。不新建网站/在线playground，不用新增远程服务降低表面步骤。

**Developer Empathy Narrative（第一人称）**：
我第一次接手时，先想确认“哪些已经做完，哪些才是下一步”，不想用过期Worklog猜工作树。看到C65第一个失败测试与当前分支证据，我可以先把最小回归跑起来。作为库主人，我愿意显式开启自动解析，但需要知道它会上传启用时已有PDF、失败为什么不自己重试，以及关掉后不能撤回已上传文件。检索给我0条时，我必须知道是预算放不下、索引还没好，还是文件真的不存在，不能靠再问十次碰运气。

**Developer Journey（九阶段）**：

| 阶段 | 本版可观察成果 | 原摩擦 / 计划动作 | 验证 |
|---|---|---|---|
| 1 Discover | README知道是本地多库MCP，不把PDF原件当直接索引 | 能力摘要与云上传边界分清 | C70中英文 |
| 2 Evaluate | 不填key也可static+公共fixture验证 | 历史100%无语料说明→本轮Golden仅1条明确 | C70.3离线输出 |
| 3 Install | venv editable安装成功 | Windows exe占用→停具体连接器再装 | C59/C60 |
| 4 Hello World | 公共fixture检索命中并拿到source | 默认配置可能宿主external→显式临时配置 | §0.4/C70.3 |
| 5 Integrate | 连接器initialize，kb_init/kb_search/kb_read闭环 | required path与handler别名不等价→说明限制 | C55/C70/stdio |
| 6 Debug | 越界/歧义/预算0/cold能选唯一恢复动作 | 空正文成功/同名并集→具名错误与有限候选 | C65/C68/C69 |
| 7 Scale | 多库compact与分组预算可续 | 旧group游标混总数→group_offsets同路由，固定窗限制 | C67/C68 |
| 8 Upgrade | 0.8.1行为变化/worker恢复/回滚数据保留 | “git pull即升级”/删state建议→运行簿 | C59/C60/§6.1 |
| 9 Contribute | 卡、所有权、靶向集、技术账、CI入口清楚 | 过期未提交代码/无测试记账→逐卡闭环 | §3/§7.3 |

**竞争与参考入口核查（2026-10-05）**：只比较官方文档的onboarding方式，不宣称跑了竞争产品或有真实耗时。选择“简单、本地、无key可试”的参考档，不拿云数据库能力做本仓库补丁版的需求。

| 参考 | 官方入口方式 | 可借用的DX原则 | TTHW事实 |
|---|---|---|---|
| [Filesystem MCP](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) | 允许目录/Roots配置，npm入口 | 先公开可读范围，范围缺失具名错误 | 本轮未安装量测，不填分钟数 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 小型tool示例+Inspector | 第一个可见结果具体，不让用户先读内部架构 | 本轮未量测；不是本版要新增的依赖 |
| [Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant) | uvx+集合/本地或远程Qdrant配置 | 配置选择互斥、默认与读取范围可发现 | 本轮未量测，模型下载/DB环境与本库不可直接比 |

参考取自Exa读取的官方README，未采用star数、市场规模、公开宣传分钟数作为性能证据。

**TTHW评估与目标**：当前热准备路径估计5–15min，目标≤5min；冷准备（Python/客户端/网络安装均无）估计15–30min+，不能承诺≤5min。均为人工估计，非计时结果。C70记录从install开始、首次fixture命中、首次真实MCP检索三时间点；首次MCP还含人工客户端接线，单独报告。三阶段：安装环境、跑隔离Golden、连接器注册/检索；原始命令数不得合并成“仅3条命令”的营销断言。

**八遍检查与Scorecard**：以下是计划完成后的预期质量分，不是现行产品评分、不是实测承诺。

| Pass | 现状估计→计划预期 | 已查内容/处置/剩余限制 |
|---|---|---|
| 1 Getting Started | 6→7 | 显式venv+UTF-8+无BOM临时配置，Golden命中可见；客户端配置仍需人工，冷环境不保证5分钟 |
| 2 API/CLI | 6→8 | compact默认关、source/lines回读、group_offsets/start_char精确定义；新增API需用户确认且schema仍有别名限制 |
| 3 Error Handling | 4→9 | 越界实际行数、heading歧义、budget0/incomplete恢复动作；锁/坏state自动暂停。内部cause保留但不泄露路径/key |
| 4 Docs | 5→8 | 单一权威卡、主文档映射、旧方案Git还原；不强迫新人先通读全部1500行，先§0–3后首卡 |
| 5 Upgrade | 4→8 | Windows停连接器、版本检查、20MiB/whole budget/group/heading/崩溃恢复变化公开；云上传不可撤销 |
| 6 Environment | 7→8 | Python3.10无新依赖、临时pytest隔离、Event/两进程、五矩阵CI；非Win原生watch不新增 |
| 7 Ecosystem | 5→6 | 现有GitHub Issues/开发者记账/官方skill是反馈渠道，不编造社群规模、未验证收费额度不承诺 |
| 8 Measurement | 3→8 | Hit/MRR与schema已有本轮基线，payload/p95/TTHW实施后测；不增加埋点上传或付费评测 |

算术平均计划预期 **7.8/10**；Getting Started和生态仍有真实限制，不包装成8维10/10。

**三个具体错误旅程**：

| 路径 | 现状 | 目标用户看到 | 唯一推荐下一步 |
|---|---|---|---|
| 行号11340，实际10行 | `content=""`成功 | requested/actual/source+范围错误isError | 核对分卷，已知标题改heading；不要把空白当空文件 |
| 首chunk超预算 | 首条正文被切，cursor已推进 | returned0/truncated/原cursor+hint | compact或增budget；不可重复同预算循环 |
| 构造第二manager时已有parsing | 自动转queued，可能重复上传 | 忙/活consumer状态，孤儿明确failed/未知远程结果 | 活任务等status；真正孤儿用户决定是否手動retry |

错误说明/Skill还需指向用户指南相应节，但不把完整URL重复塞每条成功返回；stdio工具错误仍既有text+isError，不在补丁版另造结构化错误SDK。

**DX Implementation Checklist（汇总，不建第二套卡）**：

- [ ] C58/C66：默认关零自动上传、显式冷暖与停止状态；status/pending不授权云操作。
- [ ] C67/C68/C69：compact回读、同路group续页、bounds/heading/newline/长单行实际mock闭环。
- [ ] C59/C60：指定PID/客户端升级指南，恢复行为与不可逆上传说明，版本/stdio同步。
- [ ] C70：public/developer文档分层，所有例子真实参数，临时配置可运行，TTHW计时和基线同口径复测。

**独立DX复核已完成**：覆盖§0–7、开发者Quickstart和关键配置/入口。四项P2建议中，旧enabled严格布尔校验不扩入本版；group_offsets文档组合、用户CHANGELOG避免内部术语、README/Quickstart无依据70%量化分别在C68/C60/C70说明。没有新增防御性开发任务。评分仅是计划预期，未进行新功能实测。

</details>

### 8.4 Cross-Phase Themes / 最终门禁

1. **可恢复而非空成功**：CEO的错误救援、Eng的完整chunk/范围/活任务处置、DX的下一步动作同指一个问题。每个失败都要区分“重试/增预算/指定库/手动恢复”，不靠silent fallback制造成功外观。
2. **已有不等于已完成**：历史Lane D未提交、heading参数已经存在但语义不足、JSON合法但chunk不完整、测试规格不是通过证据。任务卡状态、旧方案还原点、实际122项结果已分别记账。
3. **本版摄取规则**：保留默认自动关、既有ignore/沙箱、用户确认的size cap与同hash去重；不新增上传副本或锁框架。真机验证仍按已有授权流程执行，不扩大本轮工作。
4. **一致性与上下文经济性要公开边界**：compact减字段，预算完整prefix，分组同路但固定候选窗，后台读优先但最终一致；不再用“瞬间最新/硬预算永远达标/无限游标”掩盖不可满足条件。

**文档完成与实施边界**：本轮计划已完成；下一开发者直接按§3与§4实施，不再等待额外架构选型。未实施代码、未跑全量CI或真实云验收，不代表发布已通过。schema≤10%若实施后实测超限，按C70.3记录实际值后处理。

### 8.5 Decision Audit Trail

| # | Phase | 决策 | 分类 | 原则 | 理由 / 不选方案 |
|---|---|---|---|---|---|
| A01 | CEO | 重建一份当前施工正文，历史保留Git+还原点 | Mechanical | P5显式 | 不让互相推翻的三轮报告成为执行指令 |
| A02 | CEO | 保留D1–D5/X3 | User-confirmed | 保留用户方向 | 不复议拆版、size缺省、solo |
| A03 | CEO | 不续接Worklog未提交Lane D | Mechanical | P3实际 | 当前干净分支无该代码 |
| A04 | CEO | #6不是新增heading、不是修非法JSON | Mechanical | P4复用 | 改已存能力的语义边界 |
| A05 | Eng | 探测incomplete不自动展开 | Mechanical | P1完整 | 不能证明唯一就显式选库 |
| A06 | Eng | 只读请求后台refresh而非缩短等锁 | Taste | P5/P3 | 真正去掉前台sync，保留旧同步API |
| A07 | Eng | compact opt-in，不改preview默认 | Taste | P3兼容 | 满足瘦身且不删除旧消费者必用字段 |
| A08 | Eng | 首条超预算返回零chunk、不裁正文 | Taste | P1完整 | 预算与完整性不能靠切字符串兼得；零进展提示 |
| A09 | Eng | envelope超预算fail-visible | Mechanical | P1诚实 | 不删错误/solo诊断，不假称硬上限 |
| A10 | Eng | heading读物理原文，同级/更高级边界 | Taste | P4/P5 | 不复用不完整、可变的chunk并集 |
| A11 | Eng | 重复heading报歧义，不新occurrence参数 | Mechanical | P5 | 用已存在行号消歧 |
| A12 | Eng | 自动失败账本独立于500job历史 | Mechanical | P1 | 单取最新无法抵抗剪枝 |
| A13 | Eng | failed同hash不自动重试，手动可重试 | Mechanical | P1 | 防额度/429重提；不加入矛盾的自动failed退避 |
| A14 | Eng | size cap只管配置策略，不替换channel限额fallback | Mechanical | P3 | 不砍无key用户已有PDF本地兜底 |
| A15 | DX | UTF-8命令对齐CI，历史fixture不当现存bug | Mechanical | P3 | 本轮73项复跑通过 |
| A16 | DX | 有限诊断hint，不把pending全文重复塞init | Mechanical | P5 | 修上下文经济性时不能新制造大payload |
| A17 | DX | 回滚停客户端、配置默认恢复，不删除数据 | Mechanical | P1 | 上传不可逆，保留可恢复产物与状态 |
| A18 | 用户收口 | 不扩防御性架构，立即完成计划 | User-confirmed | 最新用户指示 | 撤销新增state锁框架/worker owner与lease/上传副本/检索事务代际；既有规则保留 |
| A19 | DX | 用户日志写体验，参数放指南；删无依据70% | Mechanical | 文档一致 | 不把内部字段当用户更新摘要 |

## GSTACK REVIEW REPORT

| Review | Runs | Status | Findings |
|---|---|---|---|
| CEO | 1主审+1独立成功 | REVIEWED | 范围/前提/替代已修，#6纳入施工卡 |
| Design | 0 | SKIPPED | 无可视UI |
| Eng | 1主审+1独立成功 | REVIEWED / SCOPE REDUCED | 施工卡与测试已补；额外防御设计按用户指示移出 |
| DX | 1主审+1独立完成 | REVIEWED | 命令/文档/升级核对完成；不再扩审查 |

VERDICT: 文档编写完成，按当前卡片可执行；不代表功能代码或发布已通过。

**UNRESOLVED DECISIONS:**
- 实施后若schema实测超过10%，按C70.3处理例外；当前不阻塞文档交付。
