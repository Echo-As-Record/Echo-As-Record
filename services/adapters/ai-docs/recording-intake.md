---
audience: ai
doc-type: reference
based-on: dev e9590ae；本轮授权的本地原生录音接入、适配器任务及故障恢复要求
---

# 原生录音接入：本地 spool 与 worker

实现入口：[recording_intake.py](../src/ear_adapter/recording_intake.py)、[recording_worker.py](../src/ear_adapter/recording_worker.py)。与旧 Ear 服务无关。此切片不包含 Web、iOS HTTP 路由、Octopus 文件发现或 worker 启动器。

## 上游接口

- `RecordingIntake(root: Path)`：所有生产者与同一 worker 使用同一个根。在可信且路径稳定的本地 POSIX 卷上，以同一 UID 运行；现有父目录路径不得有软链接，根和 `blobs/` 为该 UID 所有且 `0700`。根缺失时只创建末级。部署不得清理在用的锁、数据库、附件，或移动、替换、挂载覆盖该路径；非分布式锁或任意同 UID 程序的隔离机制。
- `accept(stream, *, source, source_id, started_at, media_type, ended_at=None, kind="audio")` 从当前位置读取但不关闭调用者的流。`source` 是受信入口的命名空间，不由用户配置文件名决定；`source_id=None` 不绑定源身份。`phone_source_id(device, group, segment_index, kind)` 归一设备为小写，保留组大小写，校验段号并区分 audio/Context；同一手机来源经不同通路进入时使用 `source="phone"` 和同一助手函数。调用方先校验来源、段偏移、附件与清单一致性。
- 元数据先于读流检查。RFC 3339 带偏移时间归一到 UTC（至多 6 位小数，不接受闰秒）；同内容的开始时间必须一致。结束仅接收可信段元数据；未提供时由识别准备阶段读实际时长。不因以后同内容重传改变已有边界，即使传来不同结束时间。源身份或开始时间冲突抛 `IntakeConflict`；入参、音频容器头、Context 格式、单次读取超限抛 `IntakeInvalid`；本地安全/持久化故障抛 `IntakeStorageError`，不是接收成功。
- 音频 `audio/wav`（兼容 `audio/x-wav`）或 `audio/mp4`，512 MiB；这里只检测容器标记，完整解码、实际时长与声道判定归 worker。`kind="context"` 仅接收 gzip（`application/gzip` 或 `application/x-gzip`）内非空 UTF-8 JSONL 对象行（压缩与解压各 16 MiB，每行 256 KiB），只归档，不判定字段业务语义，不提交 GPS 或手机信息，不排识别作业。生产者不能传入服务器路径。
- `Receipt(id, duplicate, state)` 用同一 UUIDv7 标识同内容同开始时间的上传。`get(id)` 只返回 `id/state/attempts/error` 或 `None`；路由仍须做身份验证。不可透出私有 worker 对象、结果、内部路径和原出错正文。
- 临时文件先完整写入并 `fsync`。SQLite `BEGIN IMMEDIATE` 序列化源 ID 和内容去重；SHA-256 文件名的只读 blob 先原子命名及目录 `fsync`，再提交作业和绑定，成功后才回执。SQLite 使用回滚日志与 `synchronous=EXTRA`。中途故障可能留下 `.upload-*` 或未引用 blob，不会自动删除；没有聚合容量限额、清理策略或上游节流。

## Worker 接入与恢复

持有 `with intake.worker() as worker:`，单一调用循环，拒绝另一个持锁 worker。可用 `worker.run_one(prepare, publish)`（无任务返回 `False`），或使用 `claim()`、`open_audio(job)`、`save_result(job, result)`、`get_result(id)`、`begin_commit(job)`、`finish(job, state=...)`。`prepare(job, audio)` 收到已校验的借用只读流，其结果必须是 16 MiB 以内的 JSON 对象；不能执行任何①写入。`publish(job, result)` 收到已持久保存的结果，只在确认①成功时返回 `None`，仅在明确拒绝时抛 `PublicationRejected`。需要长音频时更换 prepare 或 `RecognitionProvider`（并使用相应 profile），不需要改队列；本切片不指定分块或 FFmpeg/模型启动方式。

`NativeRecordingPreparation(provider, config, duration=...)` 与 `NativeRecordingPublication(storage)` 是现成的插件接口实现，确定性音频描述使用 intake job ID 和固定的 intake 设备标记（各渠道绑定保留在 spool，不把其中任一上游 ID 当成跨渠道原始批次标识）。`duration(job, audio)` 需要在没有手机等可信结束时间时提供，并返回完整文件实际时长 `timedelta`。确保 `config.channel` 和 `device_id` 配置稳定，并让下游按同一根解析记录中的相对文件引用。

重启可重新 claim 处于 `processing` 的作业，并优先使用已经保存的结果；`ProcessingRetry` 最多三次尝试（退避 2、4 秒）。**必须在调用任何权威写入前记录 `committing`。** 不要把 `ingest_native()` 当作 `prepare`（它会提交，并且沿用旧方式对相同请求在进程内重试并将未知压入 `Pending`）；应使用无写入的 `prepare_native_records()` 或上面的回调，再由 `NativeRecordingPublication` 仅提交一次。已进入 `committing` 后遇到未知、进程退出或无可验证回执，改为 `blocked`；不会重跑模型、创建新请求或重放。结果保留，以便人工核对；没有自动解封或重放流程。即使实际上尚未发出，也可保守阻塞，不能凭缺少 marker 推定之前永远不会提交。
