---
audience: ai
doc-type: reference
based-on: dev e9590ae；本轮授权的本地原生录音接入、适配器任务及故障恢复要求
---

# 原生录音接入：本地 spool 与 worker

实现入口：[recording_intake.py](../src/ear_adapter/recording_intake.py)、[recording_worker.py](../src/ear_adapter/recording_worker.py)。与旧 Ear 服务无关。Web、iOS HTTP 与 Octopus 入口独立提交作业；本页说明共用 spool 与原生 worker。

## 上游接口

- `RecordingIntake(root: Path)`：所有生产者与同一 worker 使用同一个根。在可信且路径稳定的本地 POSIX 卷上，以同一 UID 运行；现有父目录路径不得有软链接，根和 `blobs/` 为该 UID 所有且 `0700`。根缺失时只创建末级。部署不得清理在用的锁、数据库、附件，或移动、替换、挂载覆盖该路径；非分布式锁或任意同 UID 程序的隔离机制。
- `accept(stream, *, source, source_id, started_at, media_type, ended_at=None, kind="audio")` 从当前位置读取但不关闭调用者的流。`source` 是受信入口的命名空间，不由用户配置文件名决定；`source_id=None` 不绑定源身份。`phone_source_id(device, group, segment_index, kind)` 归一设备为小写，保留组大小写，校验段号并区分 audio/Context；同一手机来源经不同通路进入时使用 `source="phone"` 和同一助手函数。调用方先校验来源、段偏移、附件与清单一致性。
- 元数据先于读流检查。RFC 3339 带偏移时间归一到 UTC（至多 6 位小数，不接受闰秒）；同内容的开始时间必须一致。结束仅接收可信段元数据；未提供时由识别准备阶段读实际时长。不因以后同内容重传改变已有边界，即使传来不同结束时间。源身份或开始时间冲突抛 `IntakeConflict`；入参、音频容器头、Context 格式、单次读取超限抛 `IntakeInvalid`；本地安全/持久化故障抛 `IntakeStorageError`，不是接收成功。
- 音频 `audio/wav`（兼容 `audio/x-wav`）或 `audio/mp4`，512 MiB；这里只检测容器标记，完整解码、实际时长与声道判定归 worker。`kind="context"` 仅接收 gzip（`application/gzip` 或 `application/x-gzip`）内非空 UTF-8 JSONL 对象行（压缩与解压各 16 MiB，每行 256 KiB），只归档，不判定字段业务语义，不提交 GPS 或手机信息，不排识别作业。生产者不能传入服务器路径。
- `Receipt(id, duplicate, state)` 用同一 UUIDv7 标识同内容同开始时间的上传。`get(id)` 只返回 `id/state/attempts/error` 或 `None`；路由仍须做身份验证。不可透出私有 worker 对象、结果、内部路径和原出错正文。
- 临时文件先完整写入并 `fsync`。SQLite `BEGIN IMMEDIATE` 序列化源 ID 和内容去重；SHA-256 文件名的只读 blob 先原子命名及目录 `fsync`，再提交作业和绑定，成功后才回执。SQLite 使用回滚日志与 `synchronous=EXTRA`。中途故障可能留下 `.upload-*` 或未引用 blob，不会自动删除；没有聚合容量限额、清理策略或上游节流。

## Worker 接入与恢复

持有 `with intake.worker() as worker:`，单一调用循环，拒绝另一个持锁 worker。可用 `worker.run_one(prepare, publish)`（无任务返回 `False`），或使用 `claim()`、`open_audio(job)`、`save_result(job, result)`、`get_result(id)`、`begin_commit(job)`、`finish(job, state=...)`。`prepare(job, audio)` 收到已校验的借用只读流，其结果必须是 16 MiB 以内的 JSON 对象；不能执行任何①写入。`publish(job, result)` 收到已持久保存的结果，只在确认①成功时返回 `None`，仅在明确拒绝时抛 `PublicationRejected`。需要长音频时更换 prepare 或 `RecognitionProvider`（并使用相应 profile），不需要改队列；本切片不指定分块或 FFmpeg/模型启动方式。

`NativeRecordingPreparation` 与 `NativeRecordingPublication(storage)` 实现这两个回调。确定性音频描述使用 intake job ID 和固定的 intake 设备标记（各渠道绑定保留在 spool，不把其中任一上游 ID 当成跨渠道原始批次标识）。没有可信结束时间时，`normalized_duration(job, audio)` 可在识别范围内返回已归一化样本的时长 `timedelta`，供识别复用；仍兼容返回 `timedelta` 的独立 `duration` 回调。确保 `config.channel` 和 `device_id` 配置稳定，并让下游按同一根解析记录中的相对文件引用。

重启可重新 claim 处于 `processing` 的作业，并优先使用已经保存的结果；`ProcessingRetry` 最多三次尝试（退避 2、4 秒）。**必须在调用任何权威写入前记录 `committing`。** 不要把 `ingest_native()` 当作 `prepare`（它会提交，并且沿用旧方式对相同请求在进程内重试并将未知压入 `Pending`）；应使用无写入的 `prepare_native_records()` 或上面的回调，再由 `NativeRecordingPublication` 仅提交一次。已进入 `committing` 后遇到未知、进程退出或无可验证回执，改为 `blocked`；不会重跑模型、创建新请求或重放。结果保留，以便人工核对；没有自动解封或重放流程。即使实际上尚未发出，也可保守阻塞，不能凭缺少 marker 推定之前永远不会提交。


## 已打包的 Linux 启动入口

```sh
uv run ear-native-worker --config /absolute/private/worker.json --once
uv run ear-native-worker --config /absolute/private/worker.json --watch
```

默认等同 `--once`：最多认领一个就绪作业，既不扫描原目录也不等待。仅显式 `--watch` 常驻；持有同一 spool 的独占 worker 锁，每轮至多认领 16 个作业、轮间等待 1 秒。SIGTERM/SIGINT 不打断当前作业、不会再主动认领下一条；空闲时最多等待本轮休眠。watch 遇到启动鉴权、本地队列等进程级故障即退出，不在进程内无限重连；交由运维监督。退出码 0 的 `processed` 只表示处理过一个 claim，**不是**入库成功回执；`idle` 表示无就绪作业，`stopped` 表示接到停机请求。退出码 1 是 worker 忙或进程级故障，2 是参数或配置无效。查上传状态 API 或私有 spool 回执确认结果；worker 日志不含 job ID、私有路径、令牌或模型内容。

在仓库外以 `umask 077` 创建 JSON。要求绝对路径、普通文件、仅一个硬链接、运行 UID 拥有且权限恰为 `0600`；不跟随配置文件或目录软链接。只接受下表字段，均必填，不从环境变量补全，不接受 HTTP 客户端设置命令、路径、模型、profile 或目标 URL。

| 字段 | 值 |
| --- | --- |
| `spool_root` | 已存在、当前 UID 拥有的 `0700` 绝对目录；必须与已启用生产者的 spool 相同 |
| `scratch_dir` | 已存在、当前 UID 拥有的 `0700` 绝对目录；使用可信本地磁盘，给单个 worker 至少预留 128 MiB 源副本及 57 MiB PCM 空间 |
| `adapter_version` | `ear-native/0.1.0+<profile>`；给规范化构建、启动器、模型/配置、分窗策略绑定固定 profile |
| `offline_isolated` | 必须是布尔 `true`；仅表示运维已确保隔离，不提供隔离机制 |
| `launcher_argv` | 可信本机一次性识别启动器及固定参数；首项为绝对可执行文件路径，不放令牌、采集路径或采集时间，不手工指定 `--started-at`/`--ended-at` |
| `ffmpeg_path` / `ffprobe_path` | 可信本机绝对可执行文件路径；M4A 需要支持 `fd:` 的版本 |
| `storage_url` | 固定①根地址；HTTPS 或字面回环 IP 的 HTTP，无路径前缀、查询参数、认证信息；禁用代理、重定向和重试 |
| `storage_token` | ①单独签发给此 worker 的 `adapter` 身份令牌，非旧服务、API 用户或模型令牌；启动时经①验证模块 |

运行期间不得替换 spool 或程序资产。不要把同一 spool 改指其他①实例、身份、profile 或模型后直接重启：待处理/已准备批次的解释可能改变；配置升级和旧 spool 迁移需单独核对，worker 不会重跑已完成或 `blocked` 作业。FFmpeg、ffprobe 和启动器均由运维预置；必须另外实施无网络和本地服务访问、最小权限、只读资产、无凭据、资源限制和进程树清理。worker 本身仅通过官方存储 SDK 访问①；没有旧 Ear、远程识别或回退路径。默认总识别期限 7200 秒、单次启动器 245 秒、探测加解码 60 秒；SDK 默认 HTTP 超时 35 秒（各 I/O 阶段，非总墙钟期限）。为优雅退出预留正在执行作业的时间；若监督器必须强制终止，spool 仍按上面的 `processing` / `committing` 规则恢复。系统 I/O 卡住或进程逃离组的硬上限由运维限制。

支持归一化 PCM WAV 和受限的单音轨 AAC-LC/ALAC M4A，归一化时长 1 秒至 31 分钟，长解码输入上限 128 MiB（与 HTTP 接收上限不同）。Web 无结束时间时，实际完整归一化后的 16 kHz 帧数换算为微秒，从开始时间导出结束时间；已验证 PCM 在同一作业的连续窗口复用，**不二次解码**，AAC 仅按验证过的包尾填充截去。手机结束时间不改写，必须与媒体时长在既有的 1 ms 容差内。不推测时长、不接受通过媒体校验失败的前缀。分窗为本机一次性识别，不跨窗合并声纹身份；识别失败、部分结果或超限不会发布部分批次。
