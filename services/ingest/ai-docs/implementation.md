---
audience: ai
doc-type: reference
based-on: 用户 2026-11-13 services/ingest 实现任务；docs/proposals/phone-recorder-extraction.md 3.5；docs/design/design-v1.2.md 3.4；services/adapters 包结构；本机 brain channel 与旧 Ear 只读源码及 2026-11-12 会话样本
---

# Ingest 实现与兼容依据

功能、配置、差异和故障边界见[模块 README](../README.md)。这里记录已实现代码，不将提案整体标记为 accepted，不导入旧服务代码，不复制真实录音、GPS 或用户内容到仓库。

## 代码导航

| 文件 | 职责 |
|---|---|
| `src/ear_ingest/record.py` | multipart 字段解析、安全路径组件、旧 phone 文件命名 |
| `src/ear_ingest/delivery.py` | SQLite 记录未完成发布、单条投递、退避与旧 Ear 的受理回执 |
| `src/ear_ingest/store.py` | 实例锁、查找重传、分块复制、原子替换、旧附件清理 |
| `src/ear_ingest/app.py` | 鉴权、两种 URL、health、可选定向投递 worker 与历史 reconcile 通知 |
| `src/ear_ingest/__main__.py` | `ear-ingest serve` 启动 |
| `tests/test_ingest.py` | 合成输入、旧解析最小断言、重传、失败与并发测试 |

沿用单一模块名 `ingest`，数据称 `record`，落盘边界称 `store`；未引入并行同义类型，也没有更名或删除现有模块。旧 URL 的兼容路由留在 HTTP 边界；待手机全部迁移新 URL 后可删除，当前无删除日期。POSIX 文件锁在 worker thread 内执行，不阻塞 ASGI 事件循环；不同实例可并发，跨进程也遵循相同锁文件。重传查找仅扫描当前设备下 group_id 匹配的会话目录。定向投递以 Delivery 命名。publications 表暂存发布计划（两次 rename、旧路径清理），先记录计划再发布，在同一 SQLite 事务中登记 job 并移除计划。启动仅恢复未完成的计划。jobs 每条 record 保留最新 revision；revision 绑定附件字节、record 内容及目标路径。后台进程通过 flock 选出唯一发送者，并按 revision 检查回包。每次 accepted 追加 receipts，按 seq 读取可定位对应 capture。

## 只读证据

Brain 源码根：`~/.octopus/local/servicehub/euxcet/octopus-service-brain-channel/`。

- `api/records.py`：multipart 创建入口及成功响应。此服务按任务仅返回 ok、record_id、path，不返回原服务附加的 collection、record。
- `api/shared.py::parse_record_request_input`、`storage_v3.py::normalize_time_values`：JSON 字符串对象化，顶层 offset 字符串保持不变，timestamp 规范为 ISO8601；本服务保留相关行为，不实现任意 JSON/data 包装入口。
- `api/handlers/registry.py`：`source=local` 的 richVoice / phoneContext / phoneIMU 走 PhoneRecordHandler；phoneNotes 未列入，该扩展见 README。
- `api/handlers/phone.py`：目录为会话本机时间加 group_id；record 名携带类型，附件时间键移除类型；segmentIndex 原值（从 0 开始），附件序号从 01 开始。
- `api/handlers/base.py`、`api/record_store.py`、`storage_v3.py::build_attachment_stored_name`：id、files、附件清单字段和文件名后缀。原服务用 `shutil.copyfileobj` 直接写目标附件；此服务使用固定 1 MiB 块写临时文件，再 replace。
- `api/record_query.py::attach_record_attachments`：生成 Octopus 下载 URL，旧 Ear 不依赖此字段；本服务不伪造不可用下载地址。
- `api/record_ingest.py`：每实例串行；本服务改用文件锁，可覆盖多个本地服务进程。原服务通过确定路径覆盖重传，但未清理改名后的历史附件，也会新建 UUID；本服务保留旧 id 并清理。
- `api/shared.py::save_json_file`：UTF-8、ensure_ascii=False、indent=2、临时文件 + replace，无结尾换行。

只读命名样本：`~/.octopus/local/inbox/brain/<device>/2026-11-12/phone/20-21-29__<group>/`。测试只复用会话时间与命名，内容为合成小字节串、实例为 ios-test。`2026-11-12T11:21:29.157Z` 在 Asia/Tokyo 是 20:21:29；index=1、offset=1800000..3600000 得到样本的 20:51:29..21:21:29，原附件名 segment_0002.m4a。

原 Octopus iOS 的 `LocalRecordingPcTransferSidecarUploads.swift` 将会话级 marks.json 作为 phoneNotes 发送，index、起止偏移均为 0。接收端仅对此情况放宽正时长要求并归档。

旧 Ear 源码根：`~/.octopus/local/servicehub/<owner>/octopus-service-ear/`。

- `ear/ingest/parsing.py`：`inbox_root.glob("*/*/*/*/*.json")`，首层目录即 instance；忽略隐藏 JSON；record 类型合法且 processing_status 缺省 ready 才枚举。`resolve_audio_path` 优先用 `record_path.parent / files[0].stored_name`。
- `ear/models/ingest.py::BrainRecord`：id/group_id/type/source/timestamp/两个 offset 必需，end > start；segment_meta 的 segmentIndex 可转换；files 要求 field_name、original_name、stored_name、content_type、size；attachments 缺省空列表。本服务总提供 content_type（缺失时 application/octet-stream）。
- `ear/ingest/brain.py`：起止时间由 timestamp 加 offset 计算；capture id 使用 instance、record id、checksum；保留重传 id 避免同内容重传产生不同 capture。更换文件内容可能产生新 capture，不承诺修改旧处理结果。
- `ear/ingest/context.py`：独立枚举 phoneContext，按同会话、时间与 segment index 关联音频；通过 files 找 sidecar。
- `ear/ingest/sensors.py`：识别 `__context_` / `__imu_`、jsonl 或 jsonl.gz，提取 `segment_<index>`；保留手机原文件名 context_0002.jsonl.gz / imu_0002.jsonl.gz 后自然符合该规则。任意自定义附件名不保证传感器分类成功。
- `ear/api/routes_ingest.py`、`ear/api/assemble.py`：reconcile POST 可直接调用，路由未加身份依赖；没有证明任意部署的外层 Octopus 代理免鉴权。通知仅面向部署者确认可直连的地址。

## 验证

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pyright -p services/ingest
uv run pytest services/ingest
uv run pytest
uv run python tools/check_docs.py
uv run python tools/check_docs.py --base origin/dev
```

根 pyright 已包含 ingest；根 pytest 的默认 testpaths 仍需显式追加本模块测试。根 README 和部署模板已同步接收端配置。

测试使用 tmp_path，包含样本命名、类型/字段转换、401、400、路径穿越、软链接逃逸、跨重启重传/改名清理、同实例并发、大文件 spool/固定分块、复制失败保持旧 record、旧 Ear 最小解析和通知失败。无真实旧 Ear 导入或生产 inbox 写入；合成音频不宣称能通过解码/ASR。崩溃时多文件整体一致性、真实手机上传和完整 Evidence 链路尚未验收。

## 定向投递回归

合成测试覆盖 Context 先到、旧 Ear 受理后唤醒、IMU / Notes 归档、进程重启继续投递、同版重传、内容替换、两次 rename 间中断的恢复、服务不可用、协议异常、暂停与拒绝。查询和部署配置见[模块 README](../README.md)。

2026-11-14 单模块上传/投递测试 55 项通过，包含原发送端全 0 时间的 marks.json。独立 inbox 的一次真实回放在 receipts 中产生一条 richVoice、一条 phoneContext，两条受理回执关联同一 capture。

## 初版验证记录

- 模块 pytest：46 passed；模块 Ruff lint/format、单独模块 pyright、根 pyright 均通过。测试含 HTTP 请求取消时等待复制线程完成，避免 form 退出提前关闭上传文件。
- 全仓库 Ruff：491 个问题；format：43 个待格式化文件；均来自既有 `experiments/codex_baselines/local-data/`。显式排除该目录后，两项通过（没有修改那些文件）。
- 根 pytest：1382 passed、630 skipped、81 failed、96 errors；其他模块出现 socket PermissionError、journal_unavailable 等，未逐一诊断或修改，不宣称全仓库通过。根默认 testpaths 不包含 ingest，所以这与模块 46 项是两次独立结果。
- check_docs 与 `--base origin/dev`：仅上述 local-data 文档错误；本模块两份文档无错误，复核列表包含它们。未改变任何 reviewed 元数据。
- 默认 uv 缓存不可写、网络不可用；使用 `/tmp/ear-ingest-uv-cache` 和现有只读缓存，新增 python-multipart 的锁定元数据由独立临时项目离线解析，再合入原 lock，保留其他锁定版本。`uv sync --locked --offline` 与 `uv lock --check --offline` 均通过。
- CLI 服务初始化成功，但 TCP 127.0.0.1:8791 绑定被执行沙箱以 Operation not permitted 拒绝；Unix socket 备用路径同样被拒绝。**未完成真实 curl 上传**。替代的 TestClient multipart 上传到 `/tmp/ear-ingest-smoke/inbox` 返回 200，确认生成 ios-smoke/2026-11-12/phone/20-21-29__synthetic-session 的 JSON 和 m4a。可在允许监听的终端用 README 的 curl 示例补验。

- 2026-11-13 本机回环 TCP 压测：独立临时 inbox、4 路并发、48 个半小时段，每段一个 7,200,000 字节合成音频负载和一个 16 KiB Context 负载，加 4 次重传。100 次请求成功，1.1 秒；落盘 96 条唯一 record、48 对附件，音频大小正确，重传保持 record ID。该结果仅覆盖接收端；真实 Wi-Fi、手机后台传输和音频解析待验。
