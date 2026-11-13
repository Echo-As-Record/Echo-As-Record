---
audience: ai
doc-type: guide
based-on: docs/design/design-v1.2.md 3.3–3.4；docs/design/work-breakdown-v1.2.md ②；contracts/records/raw-record.schema.json；旧 Ear doc/0929_evidence_read_api.md、doc/audio-ingestion.md；负责人 2026-11-05 确认：音频走旧 Ear Evidence API、补列录音接口、每版本只导入一次、确定性 id；旧 Ear ear/evidence/readiness.py（availability 规则）、ear/runtime/model_lifecycle.py；2026-11-05 旧 Ear 卡住根因排查（日志 + 数据库，只读）；2026-11-10 用户授权②接入①远程服务、常驻扫描、CommitUnknown 重放及门控联调测试
---

# ② 适配器：实现说明

## 1. 数据流

```text
手机 ─► Brain ─► ~/.octopus/local/inbox/brain/{实例}/{日期}/{collection}/{HH-MM-SS}__{组}/
                         │
                         ▼
                   旧 Ear（不改处理逻辑）：MOSS 转写、ECAPA 声纹、PANNs 环境声、传感与手机上下文
                         │  Evidence 只读 API（bearer token + Evidence-read 授权）
                         ▼
   ② Runner.run_once()
     0. query(七种原始类型, limit=1)         显式验证 read_types
     1. list_captures()                      列出录音
     2. read_capture(id) → readiness()       三个音频阶段都 available/empty 才导入
     3. ① get(audio_ref 的确定性 id)          已存在 → 已导入，跳过
     4. InboxAudioLocator.locate()           按组 id（不限日期目录）+ 大小 + SHA-256 找到原始音频
     5. read_embedding()                     取每条声纹向量
     6. capture_records()（纯函数）           → 一批 NewRawRecord
     7. ① write([AppendRaw…], Context.system(), request_id=…)   一段录音一批，原子写入
                         │
                         ▼
                ①「数据库被修改」──► ③（声纹、GPS 归属）④ ⑤ ⑥
```

代码：`src/ear_adapter/`

| 文件 | 内容 | 副作用 |
|---|---|---|
| `ear_api.py` | 旧 Ear 响应的 pydantic 模型（只取用到的字段，未知字段忽略） | 无 |
| `convert.py` | `readiness`、`capture_records`、`audio_ref_id` | 无 |
| `ids.py` | 确定性 UUIDv7 | 无 |
| `source.py` | `HttpEarSource`（urllib）、`InboxAudioLocator` | 网络、文件 |
| `runtime.py` | `ingest_capture`、`Runner`、`Report`、结构化日志 | 写 ① |
| `__main__.py` | `ear-adapter dry-run / once / run`（保留 `--dry-run` 别名） | 全部 |
| `testing.py` | 合成的旧 Ear 响应、`FakeSource`、`FakeLocator` | 无 |

## 2. 导入规则

1. **只导入完整结果。** `analyzable` 为真、录音已结束，且 `speech_segment`、`audio_event`、`speaker_resolution` 都是 `available` 或 `empty`。没有语音时旧 Ear 把 `speaker_resolution` 标成 `missing`（`speech_segment` 为 `empty`），也算完整。其余 → `Pending`（下一轮重试）。传感和手机上下文有多少导多少，不阻塞。
   - **降级结果不导入。** 旧 Ear 某个阶段的任务没成功时，run 仍会以 `degraded` 跑完、`analyzable` 仍为真，只有 availability 显示 `failed`（该阶段没有结果）或 `partial`（部分结果）。这两种 → `Failed`，原因写明「降级结果」。因为同一版本只导入一次，导入降级结果以后就换不回来。`Failed` 不进 `Runner.done`，每轮都重读，旧 Ear 修好并重新处理后自动导入。
   - 已知实例（2026-10-20～10-27）：旧 Ear 的声纹模型目录改名，加载失败一次后被 `ear/runtime/model_lifecycle.py` 永久锁住，此后一周所有 run 都是 `degraded`、声纹阶段 `failed`，直到改 config 重建才恢复。
   - **看不到声纹引用也不导入。** 旧 Ear 对没有 `embedding_readers` 授权的服务隐藏 `embedding_ref`。availability 显示 `speaker_embedding` 为 `available`、却没有任何说话人结果带引用时 → `Failed`（原因写明缺授权），否则会导入一批没有声纹的数据且换不回来。
   - 看不到的降级：阶段任务成功、但提供方内部退回了备用输出（例如二次分离退回一次分离）时，Evidence API 不暴露，② 无法识别。
2. **每个 adapter_version 每段录音只导入一次。** 旧 Ear 重新处理后 Evidence id 全变，不跟着重导，避免同一时间出现两份转写。要换成新结果，升级 `config.ADAPTER_VERSION`，重新导入一批；旧批次保留（设计 3.3）。
3. **确定性 id。** `ids.deterministic_id(事件时间, data_type, adapter_version, dedup_key)`：前 48 位为事件时间毫秒，随机位取自 SHA-256。重扫时同一条数据 id 不变，批内引用不会断。
4. **一段录音一批。** 批内顺序 audio_ref → local_speaker → voiceprint → transcript_segment → ambient_sound → gps_point → phone_info。audio_ref 存在即说明整批已导入（原子写入）。
5. **去重键**（同一 adapter_version 内唯一）：

| 类型 | dedup_key |
|---|---|
| audio_ref | `{实例}:{上游 record_id}:{音频 sha256}`（下称录音键，不随处理批次变化） |
| local_speaker | `{录音键}:speaker:{作用域}:{标签}` |
| voiceprint | `{录音键}:voiceprint:{上游 resolution_id}` |
| transcript_segment | `{录音键}:transcript:{上游 speech_segment_id}` |
| ambient_sound | `{录音键}:ambient:{上游 audio_event_id}` |
| gps_point | `{实例}:gps:{UTC 时间}:{纬度}:{经度}`（跨录音去重） |
| phone_info | `{实例}:phone_info:snapshot|event:{UTC 时间}:…:{内容摘要}`（跨录音去重） |

6. **不导入**：旧 Ear 里的用户纠正（用原始文本和标签）、置信度、推断出的手机事件、IMU。

## 3. 旧 Ear 需要补的接口

旧 Ear 现有的 Evidence API 只能按 id 读一段录音，没有列表。`/api/mvp/recordings` 是主人界面的接口，不对其他服务开放。需要在旧 Ear 仓库补一个只读接口，认证与授权和其他 Evidence 接口相同。已在旧 Ear 分支 `dev-capture-list` 实现，合并请求 herryying30/octopus-service-ear!15（指向 main，未合并）；本机运行中的旧 Ear 目前就是这个分支：

```http
GET /api/captures?offset=0&limit=100
Authorization: Bearer <token>   （需要 Evidence-read 授权）

200 {"total": 123, "offset": 0, "limit": 100,
     "items": [{"capture_id": "...", "updated_at": "2026-10-26T12:00:00+09:00"}, ...]}
```

按 `capture_id` 稳定排序；同一上游录音被重新处理过时只列最新一版；`limit` 为 1–500；`updated_at` 可选，旧 Ear 目前返回 null（以后用于增量）。`HttpEarSource.list_captures` 已按这个形状实现，2026-11-05 已对真实旧 Ear 跑通（见 5.2）。

## 4. 运行可靠性

旧 Ear 有时需要手动重新触发或重新挂载。2026-11-05 按日志和数据库排查（只读）的结论：

- **最近一次卡住（10-20～10-27）的根因在旧 Ear 内部**：声纹模型加载失败一次后被永久锁住，run 都以 `degraded` 跑完，直到改 config 重建才恢复。不是 Octopus 断线重连。② 的对策见第 2 节「降级结果不导入」。
- **旧 Ear 的定时补漏扫描已关闭**，只能手动 `POST /api/ingest/reconcile`；Octopus automator 投递失败不重试，旧 Ear 暂停接收时还返回 200。事件可能丢（9 月丢过 14 条）。
- **Octopus service 子进程不会自动恢复**：channel 子进程在 `d3953614`（2026-08-24）补了「连接失败时重启」，service 子进程（旧 Ear 属于这类）没有，子进程死掉后会一直 502，直到重新挂载。代码缺口存在，但约 6 周日志里没造成过卡住。
- 以上修复建议（旧 Ear 的模型锁冷却重试、恢复低频补漏扫描；Octopus 的 service 重启与投递重试）尚未实施，属于旧 Ear / Octopus 仓库的工作。

② 的对策：

- `HttpEarSource` 直接连旧 Ear 的回环地址（`EAR_ADAPTER_EAR_URL`），不经过 Octopus 代理。
- 连不上或 5xx → `SourceUnavailable` → 本轮记为 `Pending` / `source_error`，下一轮自动重试，不当成数据错误。
- 不依赖 Brain 事件：每轮全量列出录音，靠 ① 里的 audio_ref 跳过已导入的。
- `Report.summary()` 列出所有 `Pending` 和 `Failed` 的录音及原因，旧 Ear 卡住的录音不会被静默漏掉。

- 降级结果记为 `Failed` 并每轮重读，旧 Ear 恢复并重新处理后自动补上；长期停在 `Failed` 的录音说明旧 Ear 没有重新处理，需要人去旧 Ear 触发。

## 5. 验收

### 5.1 自动测试（CI，合成数据）

`uv run pytest services/adapters`。对应工作分解 ② 的验收：

| 验收 | 测试 |
|---|---|
| 数据都能入库，格式符合契约 | `test_ingest_writes_one_atomic_batch`（逐条用 raw-record Schema 校验） |
| 不重复：重启重扫不产生新数据 | `test_rescan_after_restart_writes_nothing`、`test_same_runner_skips_done_captures` |
| 不重复：旧 Ear 重新处理不重导；升级版本产生新批次 | `test_reprocessed_capture_is_not_imported_again`、`test_new_adapter_version_imports_a_new_batch` |
| 不重复：相邻录音共享的 GPS / 手机状态 | `test_gps_shared_by_adjacent_captures_is_deduplicated` |
| 不导入降级结果；旧 Ear 重新处理成功后下一轮导入；没人说话的录音能导入 | `test_degraded_run_is_not_imported_until_reprocessed`、`test_readiness` |
| 不漏：未完成、失败、找不到文件、旧 Ear 掉线都会报告并重试 | `test_pending_and_failed_are_reported`、`test_missing_audio_file_is_pending`、`test_ear_unavailable_is_retried_later`、`test_conflict_marks_capture_failed` |
| 时间换算成 UTC | `test_times_are_utc` |
| ③ 收到声纹事件时引用都可读；重复投递无害 | `test_voiceprint_event_arrives_after_its_local_speaker` |
| 只写 ② 负责的数据 | `test_only_adapter_may_write_raw_records` |
| 远程结果未知使用原 request_id/ops/context 重放；错误分类 | `test_remote.py`（不需 Docker） |
| 命令兼容、退出码、掉线恢复、SIGTERM/SIGINT 与间隔边界 | `test_cli.py`、`test_stop_finishes_current_capture` |
| 真实①条数/Schema、事件引用、重启去重、重放、无读授权拒绝 | `test_storage_integration.py`（双门控；本环境未运行） |
| 30分钟数量级合成批次容量 | `test_batch_size.py`（实际 wire 序列化） |

### 5.2 真实数据试运行（人工，数据不进仓库）

前提：

- 旧 Ear 已挂载第 3 节的接口。Octopus 启动时只给每个服务 20 秒健康检查，旧 Ear 冷启动超过 20 秒会被杀掉；用 `OCTOPUS_SERVICEHUB_HEALTH_ATTEMPTS=1200 bash octopus/scripts/start.sh` 启动（等待 120 秒）。
- 旧 Ear 运行目录的 `secrets/gateway.json`（`~/.octopus/local/service/herryying30/octopus-service-ear/secrets/`，文件权限 0600）给 ② 登记 token，并同时授予 `evidence_readers` 和 `embedding_readers`。**缺 `embedding_readers` 时旧 Ear 会隐藏声纹引用**，② 现在会把这种录音拦成 `Failed`（原因写明缺授权），不会导入没有声纹的一批。旧 Ear 每次请求重新读取这个文件，不用重启。
- 同一个 token 和旧 Ear 地址写进 `deploy/.env`（被 git 忽略）：`EAR_ADAPTER_EAR_URL=http://127.0.0.1:<旧 Ear 端口>/internal`、`EAR_ADAPTER_EAR_TOKEN=…`。端口每次挂载都会变，从 Octopus 日志的 `Spawned subprocess service 'herryying30/octopus-service-ear' on …` 读取。

```bash
set -a && . deploy/.env && set +a && uv run ear-adapter dry-run
```

输出 `first`（首轮）和 `rescan`（模拟重启后重扫）。通过条件：

1. `first.captures` 里 `Ingested + Pending + Failed` 等于旧 Ear 里的录音总数；每个 `Pending` / `Failed` 都有明确原因，且能对应到旧 Ear 的真实状态。
2. `first.written` 每类条数与旧 Ear 一致：transcript_segment 等于旧 Ear 的转写段数，ambient_sound 等于环境声事件数，voiceprint 等于生成了向量的说话人结果数。
3. `first` 里已导入的录音在 `rescan` 里全部为 `AlreadyIngested`。旧 Ear 在两轮之间刚处理完的录音会在 `rescan` 里首次 `Ingested`，这不算重复；除此之外 `rescan.written` 为空。
4. 抽查几条转写：`event_start` 与录音里听到的时刻对得上。允许误差待团队确定（工作分解未定数值）。
5. 天数待定：工作分解要求「真实数据所有文件可入库」，跑几天由团队决定。

#### 2026-11-05 试运行结果

旧 Ear 分支 `dev-capture-list`，共 428 段录音（只记条数，不记内容）。

| 条件 | 结果 |
|---|---|
| 1. 总数 | 374 导入 + 32 等待 + 22 失败 = 428。等待都是「感知尚未开始或录音尚未结束」（旧 Ear 正在补处理积压，两轮之间又完成了 2 段）；失败都是降级结果：speaker_resolution 失败 16、部分失败 5，audio_event 部分失败 1 |
| 2. 条数 | 对首轮已导入的 358 段，按旧 Ear 数据库（副本）独立统计当前 run 的音频块：转写 73043、环境声 27050、局部说话人 4126、带向量的说话人 3692，与 ② 写入的 transcript_segment / ambient_sound / local_speaker / voiceprint 完全一致 |
| 3. 重扫 | 374 段 `AlreadyIngested`；新写入只来自两轮之间旧 Ear 刚处理完的 2 段 |
| 4. 时间 | 随机 37 段录音的 2785 条数据都是 UTC；转写和环境声都落在录音时间内；7 条局部说话人和 4 条声纹的结束时间比录音结束晚 0.01–0.13 秒（旧 Ear 音频块解码时长与录音元数据的舍入差）。听录音核对时刻还没做 |

试运行中发现并已修复：

- 一开始 token 只有 `evidence_readers`，声纹全部为 0：旧 Ear 隐藏了声纹引用，② 照常导入了没有声纹的数据。已改为拦下（见前提）。
- 16 段录音一开始找不到音频文件：inbox 的日期目录是手机当地日期（那几天手机在 UTC+8），② 只按录音的 UTC 日期找。已改为按组 id 查找，不限日期。

### 5.3 尚未覆盖

- 远程写入、重启重扫、事件引用可读、CommitUnknown 重放和缺读授权已有双门控真实①测试，2026-11-11 本机运行通过（见 7.1）。真实旧 Ear 数据经 `Runner` 写入本机临时①（测试夹具起的 PostgreSQL/NATS + ① HTTP 服务）也已跑通，结果见 7.3。
- 仍未验证：按[①部署指南](../../storage/ai-docs/deployment.md)起的 Compose 临时部署上用 `ear-adapter once` / `run` 命令行跑真实数据（7.2）、常驻模式下的掉线恢复与 SIGTERM、并发进程、真实断电恢复。不能用单元测试或故障注入宣称已覆盖。
- 时间抽查只验证了落在录音范围内，还没有听录音核对具体时刻；允许误差和要跑几天仍待团队决定。
- GPS 密度不足（见 payloads.md），属于手机侧问题。

## 6. 运行方式、权限与远程恢复

```bash
set -a && . deploy/.env && set +a
uv run ear-adapter once
uv run ear-adapter run --interval 60
```

配置仅来自环境变量（间隔是运行参数）：旧 Ear 的 `EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_EAR_TOKEN`；音频根目录 `EAR_ADAPTER_DATA_ROOT` 默认 `~/.octopus/local`；①根地址 `EAR_ADAPTER_STORAGE_URL`（不带 `/v1`）和①签发的 `EAR_ADAPTER_STORAGE_TOKEN`。模板见 [deploy/.env.example](../../../deploy/.env.example)。不自动加载 `.env`，不把 token 放进命令参数、异常正文或日志。

- `dry-run`：内存首轮 + 新 Runner 重扫，输出 first/rescan，保留旧 `--dry-run` 别名，退出 0（配置错误 2）。
- `once`：一轮后打印 JSON 摘要；source_error、storage_error 或 Failed 返回 1，配置/权限/协议错误返回 2，否则 0。Pending 单独出现不表示命令失败。
- `run`：每轮结束等待，默认 60 秒，可设 1–86400 秒（有限数）。stdout 每轮一行 JSON 摘要，stderr 为逐录音 JSON 日志，均带 module/event_id/root_feedback_id/depth。SIGTERM/SIGINT 设置停止标志，当前录音的读取、转换及有限重放完成后停止，不开始下一段；等待可立即打断。列表读取仍受网络超时约束，不保证瞬间退出。
- 旧 Ear 掉线：列表失败为 source_error，单段失败为 Pending；① ServiceUnavailable：预检失败为 storage_error，单段读/写失败为 Pending。选择继续扫描、下轮重试，以保留每段结果，不把瞬时故障归为数据错误。启动时① identity 请求不可用也按轮重试。
- CommitUnknown：一次计算并保存 ops/context，显式生成 request_id；每次使用异常的 request_id 和原对象重放。总计最多三次，间隔 0.25、0.5 秒。仍未知返回 Pending、不加入 done，下轮查确定性 audio_ref；已提交则 AlreadyIngested，否则确定性 id + dedup_key 兜底。基础 StorageClient 协议没有 request_id 参数，内存仍走两参数 write，远程客户端（含其子类）使用扩展签名；没有修改 SDK。
- ResourceLimit（包括请求体超限）为 Failed，报告操作数与序列化正文 UTF-8 字节数（不计 request_id 包装），绝不拆批。服务端仍负责最终容量判定；拆批会破坏声纹事件到达时局部说话人/audio_ref 已可读的保证。
- AuthenticationError / PermissionDenied 直接退出非零；ProtocolError 同样停止，避免把接口不兼容当正常数据。每轮先 query 七种类型（limit=1），因为按 id 读无法可靠区分无权限与不存在。授权清单、正式签发缺口见 [模块 README](../README.md)。测试仅用 AuthOperator.issue_principal，不由②业务代码签发身份。

容量样本见 `tests/test_batch_size.py`：565 条（1 audio_ref、20 local_speaker、20 voiceprint、400 transcript_segment、120 ambient_sound、2 GPS、2 phone_info），192 维浮点声纹内联、每条声纹引用20段转写、每段转写120个合成汉字。按 SDK encode_request + canonical_json（含 request_id）实测 **579496 字节，0.553 MiB**，约占 8 MiB 的 6.9%。这个数量级够用，但不是最长录音上界；①还有每批2000操作、当前图投影上限，长录音/密集分段可能先触发其他 ResourceLimit。音频仅存引用，不把音频内容算入批次。

## 7. 本机真实①联调步骤

### 7.1 合成数据门控验收

先安装锁定依赖，Docker 必须可用，并预拉取 `postgres:17-alpine`、`nats:2.11-alpine`。在仓库根运行：

```bash
EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1 uv run pytest services/adapters/tests/test_storage_integration.py -v
uv run pytest services/adapters/tests/test_batch_size.py -s
```

两个开关缺任何一个都 skip（含只开 NATS 的情况）。2026-11-11 在 macOS 本机 Docker 上运行：门控集成测试与 ① 的门控测试合计 51 passed。测试直接导入 `services/storage/tests/conftest.py` 的 postgres_server/pg_dsn/nats_server 与 `test_http_postgres.py` 的 live_server，最小复用、不复制容器夹具、不修改①代码。每测试独立数据库和流，随机回环端口；session 结束销毁专属容器。禁止外部 DSN。测试验证与内存条数一致、每条 raw Schema、订阅声纹时引用可读、重启无记录/事件新增、真实提交后注入 CommitUnknown 的同请求重放、缺少读授权明确拒绝。它只验证存储边界，不运行③归属业务。

### 7.2 真实旧 Ear → 本机临时 Compose ①（尚未执行的步骤草案）

1. 按 [①隔离部署指南](../../storage/ai-docs/deployment.md) 第2节选择全新的临时 Compose 项目名和独立卷，核对与已有部署无关。仅在该临时项目显式 migrate/bootstrap/init-identities 后启动 storage/relay；真实数据不进仓库，所有输出与凭据仅本机私密保存。不要把本草案指向生产①。
2. `init-identities` 不会产生 adapter 身份。由①负责人 / I 在该临时项目的部署内 Python 环境，用其受控 DSN 执行 `AuthOperator(dsn).issue_principal("adapter", read_types=list(READ_TYPES))`，READ_TYPES 使用 README 列出的七种类型；不要在部署环境依赖安装②包。明文只返回一次，写到该临时部署的私有 0600 文件，不打印、不通过命令行传递；将所需值安全填入被忽略的 `deploy/.env`，保留已有配置。该步骤是本地操作草案，不是新增正式签发 CLI。
3. 依照5.2准备旧 Ear evidence_readers + embedding_readers，设置五个 EAR_ADAPTER 环境变量；①地址是临时 storage 的回环端口，数据根目录只读指向真实音频所在目录。确认 `/health/ready` 成功，不打印解析后的 Compose 环境或 token。
4. 加载环境后执行 `uv run ear-adapter once`，保存摘要到仓库外的私有位置，记录七类型条数、Pending/Failed原因和退出码。再次执行 once（新进程）应为 AlreadyIngested、written为空；期间旧 Ear 新完成的录音需单独核对，不能误判为重复。
5. 执行 `uv run ear-adapter run --interval 60`，验证掉线后下轮恢复，并在处理中发 SIGTERM 或 Ctrl-C，确认当前录音整批提交或 Pending、没有开始下一段。可选由①负责人签发只读/订阅 voiceprint、local_speaker、audio_ref 的 attribution 测试身份，按门控测试的 handler 核对引用；不要借用 api 管理员充当②。
6. 回填本机执行日期、环境、计数与失败原因，禁止回填录音、GPS、转写或凭据。停止临时项目；清理按①指南第6节人工核对专属项目及三套卷，真实数据与身份按私密数据管理。以上 Compose 步骤尚未执行；7.3 用的是测试夹具，不是这套步骤。

### 7.3 真实旧 Ear → 测试夹具临时①（2026-11-11 本机结果）

做法：仓库外的一次性 pytest 脚本，复用 7.1 的夹具起临时 PostgreSQL/NATS 和 ① HTTP 服务，用 `AuthOperator.issue_principal` 签发 adapter（读七种原始类型）和 attribution（读并订阅 voiceprint、local_speaker、audio_ref）两个身份；`Runner` 经 `RemoteStorageClient` 从本机旧 Ear 导入，再新建 `Runner` 重扫，最后跑 Relay 投递、attribution 用 `run_until_idle` 消费。没有经过 `ear-adapter` 命令行入口。脚本、数据、凭据都不进仓库，结束后容器已销毁。

| 项目 | 结果 |
|---|---|
| 首轮 | 384 段 Ingested；27 段 Pending（感知尚未开始或录音尚未结束）；22 段 Failed（旧 Ear speaker_resolution 失败 16、部分失败 5，audio_event 部分失败 1）；source_error、storage_error 均为空 |
| 条数 | audio_ref 384、transcript_segment 79112、local_speaker 4457、voiceprint 4008、ambient_sound 29074、gps_point 214、phone_info 346，共 117595 |
| 重扫（新 Runner） | 384 段 AlreadyIngested，新写入 0，七类条数不变 |
| 耗时 | 首轮 279.7 s，重扫 29.9 s |
| 批次 | 384 批（每段录音一批），最大 1479 条操作、1.445 MiB，低于 8 MiB 和每批 2000 操作 |
| 事件 | Relay 投递 117595 条；attribution 收到 4008 个新增声纹事件，handler 里读声纹引用的 local_speaker 和 audio_ref 全部可读（缺失 0） |

过程中暴露的环境问题（不是 ② 的缺陷，供 ① 和 ③ 参考）：

- ① 测试夹具的容量按合成数据设定：PostgreSQL tmpfs 384 MiB 写满；NATS `max_file_store: 48MB` 小于脚本里流的 `max_bytes`（512 MiB），JetStream 流建不起来，订阅注册一直返回 `ServiceUnavailable`（consumer initialization pending）且没有更具体的原因。本次只在仓库外副本里放大了容量（PostgreSQL 内存 3 GiB / tmpfs 2 GiB，NATS 内存 1.5 GiB / tmpfs 1 GiB / max_file_store 900 MB），没有改 ① 的夹具。
- ① 的流默认 `max_bytes` 256 MiB、`DiscardPolicy.NEW`；这批真实数据约 11.8 万条事件，正式部署要按真实数据量估算流容量。
- 远程消费速度约每秒 5 个事件（每个事件一次确认往返，handler 内每个声纹还要再读 3 次）。8849 个订阅事件用了约 25 分钟；③ 从头补消费时这会是瓶颈。

## 8. GPS 逆地理编码实现

依据[已采纳提案](../../../docs/proposals/gps-reverse-geocoding.md)。命令、授权与环境变量以[模块入口](../README.md#gps-逆地理编码)为准；旧 Ear 的七种读权限仅属于 `ear_adapter`，不包含独立 `geo_adapter`。

- `geo_adapter/providers.py`：`Provider` 协议及 `AmapProvider`、`NominatimProvider`，归一化 payload。协议依据[高德逆地理编码](https://lbs.amap.com/api/webservice/guide/api/georegeo)、[Nominatim reverse](https://nominatim.org/release-docs/latest/api/Reverse/)。高德 `extensions=all`，按距离取最近有名称 POI；Nominatim 不保证返回 POI，未给出的距离留 null。通用 User-Agent 为 `EchoAsRecord-GeoAdapter/0.1`。不跟随重定向。
- `china.json`：从 [Natural Earth 官方仓库的 1:110m countries](https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_admin_0_countries.geojson) 提取中国 geometry，数据为[公有领域](https://www.naturalearthdata.com/about/terms-of-use/)。仅作服务路由/坐标转换的陆地掩膜，不表示领土主张；包括主陆地和海南。离岸小岛、边境和海岸有分辨率误差，不用于精确边界判定。射线法避免传统矩形误包含日本、韩国、蒙古等地区，境外坐标保持 WGS84。
- `geo_adapter/runtime.py`：`Runner` 每次取待处理集合第一页100点，写后重查第一页，不用偏移游标扫描正在缩小的集合。① memory/PostgreSQL 的 `unreferenced_by` 新增 raw 引用方支持，有版本引用方仍只看未替代当前版。引用字段是 payload 顶层字符串，与 attribution.subject_id 的查询方式一致，不需要额外 registry 字段，也不把 gps_point_id 设为全局唯一（允许未来不同 provider）。
- `Cache` 使用 data root 下 SQLite，文件0600。WGS84 四位小数取整后转换、请求并保存；缓存键含 provider、服务端地址、语言和实际查询坐标，payload 中 query_coordinate 保留真实请求值。`ok`/`no_result` 持久缓存，`failed` 不缓存；无 TTL。每点重新附加自身 gps_point_id 和时间。改变服务端可隔离缓存，但已写①的点不会自动重查；换 provider 的历史补写需另行显式操作，本扫描器只处理无任何结果的点。
- 地图超时/传输错误、HTTP 429/5xx、无效 JSON/结构与高德瞬态错误按次数上限重试，退避1、2、4、8秒。耗尽后停止整轮请求；`Runner.resolve` 在尚未超时返回 None，不写①，下轮重扫；只有注入的 `wall_clock`（默认 UTC 墙钟）减 `point.ingested_at` 严格大于 `max_wait`（环境变量 `GEO_ADAPTER_MAX_WAIT_SECONDS`，默认21600秒）时返回 failed，写后仍停止本轮。`clock` 单调时钟仅用于限速，不能与入库时间比较。`deferred` 用① count 统计停止时剩余全部待处理点（含未请求的后续页）。非法、非有限或越界坐标直接返回 provider=invalid、status=failed，其余地图字段全 null；不选 provider、不查缓存、不请求地图，写后继续。HTTP 其他非成功状态、无效 key/权限/配置停止，不记录响应体。Nominatim 明确 `Unable to geocode`（含404）及高德成功空结果为 no_result。未识别的高德错误保守作为配置错误，不伪装成功或无结果。
- 原始记录使用 `ear_adapter.ids.deterministic_id`，来源继承 GPS 点，时间保持相同，版本 `geo/0.1.0`。①去重键为 `{gps_point_id}:geocode:{provider}`。写入 CommitUnknown 使用同一 ops/context/request_id 最多重放三次；仍未知/①不可用交给下轮扫描，绝不转换为地图 failed。进程重启靠①引用查询与确定性 id/去重键恢复，不靠缓存判断完成。
- CLI 复用 `ear_adapter.__main__` 的间隔校验与脱敏 JSON 输出；不改其行为。扫描前显式检查两类读权限；信号只设置停止标志，在当前点处理结束后停。超时是 HTTP 各阶段上限；限速是单进程，多个进程不共享速率预算。
- 测试 `tests/test_geo.py`、`tests/test_geo_cli.py` 用 MockTransport/替身，不请求地图服务；`test_storage_integration.py` 的 GPS 用例沿用 PostgreSQL/NATS 双门控。默认检查不代表真实地图、真实凭据、持久①或真实进程信号联调已完成。
