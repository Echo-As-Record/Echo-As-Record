---
audience: ai
doc-type: guide
based-on: docs/design/design-v1.2.md 3.3–3.4；docs/design/work-breakdown-v1.2.md ②；contracts/records/raw-record.schema.json；旧 Ear doc/0929_evidence_read_api.md、doc/audio-ingestion.md；负责人 2026-11-05 确认：音频走旧 Ear Evidence API、补列录音接口、每版本只导入一次、确定性 id；旧 Ear ear/evidence/readiness.py（availability 规则）、ear/runtime/model_lifecycle.py；2026-11-05 旧 Ear 卡住根因排查（日志 + 数据库，只读）；2026-11 文档同步：更正 5.3 中 ① 持久化服务“未实现”的过期描述（① 已随 PR8 合入 dev 376294d；② 命令行仍写内存，已核对 __main__.py）
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
     1. list_captures()                      列出录音
     2. read_capture(id) → readiness()       三个音频阶段都 available/empty 才导入
     3. ① get(audio_ref 的确定性 id)          已存在 → 已导入，跳过
     4. InboxAudioLocator.locate()           按组 id（不限日期目录）+ 大小 + SHA-256 找到原始音频
     5. read_embedding()                     取每条声纹向量
     6. capture_records()（纯函数）           → 一批 NewRawRecord
     7. ① write([AppendRaw…], Context.system())   一段录音一批，原子写入
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
| `__main__.py` | `ear-adapter --dry-run` | 全部 |
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

### 5.2 真实数据试运行（人工，数据不进仓库）

前提：

- 旧 Ear 已挂载第 3 节的接口。Octopus 启动时只给每个服务 20 秒健康检查，旧 Ear 冷启动超过 20 秒会被杀掉；用 `OCTOPUS_SERVICEHUB_HEALTH_ATTEMPTS=1200 bash octopus/scripts/start.sh` 启动（等待 120 秒）。
- 旧 Ear 运行目录的 `secrets/gateway.json`（`~/.octopus/local/service/herryying30/octopus-service-ear/secrets/`，文件权限 0600）给 ② 登记 token，并同时授予 `evidence_readers` 和 `embedding_readers`。**缺 `embedding_readers` 时旧 Ear 会隐藏声纹引用**，② 现在会把这种录音拦成 `Failed`（原因写明缺授权），不会导入没有声纹的一批。旧 Ear 每次请求重新读取这个文件，不用重启。
- 同一个 token 和旧 Ear 地址写进 `deploy/.env`（被 git 忽略）：`EAR_ADAPTER_EAR_URL=http://127.0.0.1:<旧 Ear 端口>/internal`、`EAR_ADAPTER_EAR_TOKEN=…`。端口每次挂载都会变，从 Octopus 日志的 `Spawned subprocess service 'herryying30/octopus-service-ear' on …` 读取。

```bash
set -a && . deploy/.env && set +a && uv run ear-adapter --dry-run
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

- ② 命令行 `ear-adapter` 仍写 `MemoryStorage`（`__main__.py`），**没有对真实 ① 服务的落库联调**：② → ① 的远程写入、并发与断电恢复都未验证。① 的持久化服务已实现并随 PR8 合入 dev 376294d（隔离部署切片非生产，见[① 存储](../../storage/README.md)），但它自己的测试不能代替 ② 的落库验证。
- 时间抽查只验证了落在录音范围内，还没有听录音核对具体时刻；允许误差和要跑几天仍待团队决定。
- GPS 密度不足（见 payloads.md），属于手机侧问题。
