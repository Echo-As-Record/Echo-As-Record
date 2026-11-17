---
audience: ai
doc-type: reference
based-on: 本轮负责人明确实施授权；docs/proposals/phone-recorder-extraction.md；octopus-flutter 67869770 的 LocalRecordingPhoneAudioInput.swift、PhoneLocalRecordingRuntimeSessionDriver.swift、LocalRecordingPhoneAudioNotifications.swift、LocalRecordingPcmRecoveryEncoder.swift、LocalRecordingActivePartRecovery.swift、LocalRecordingAutoContinuation.swift、LocalRecordingContextWriter.swift、LocalRecordingGzipJsonlRecovery.swift、LocalRecordingPcTransferSegmentUploader.swift；2026-11-12 context_0002.jsonl.gz 真实样本
---

# EarRecorder 实现与验证

## 范围与入口

只新增 `clients/ios/`。不提交、不推送、不切换 `phone/ear-recorder-proposal`，不修改 Python 或 `services/ingest/`。实现时依据负责人直接授权，提案现已采纳。用户入口见[README](../README.md)，需求来源见[提案](../../../docs/proposals/phone-recorder-extraction.md)。此前没有 iOS 模块 README，本次创建。

规范词汇为 `Audio`（音频）、`Context`（上下文流）、`Location`（系统定位/运动）、`Runtime`（采集状态机）、`Store` / `Manifest` / `Segment`（持久状态）、`Upload` / `Transfer`（上传状态）、`Recovery`（音频恢复）、`Diagnostics`（诊断）。没有引入 Flutter 原来的多层 Driver/Coordinator/Projection；新目录没有需要迁移的旧名称。`GzipRecovery` 从原实现裁剪移植，保留有界扫描、SHA256 完整性核对与原子替换。

## 文件清单

相对 `clients/ios/`：

| 文件 | 职责 |
|---|---|
| `README.md` | 用户安装、权限、设置、恢复与真机验收 |
| `ai-docs/implementation.md` | 本工程导航、差异、验证证据 |
| `EarRecorder/EarRecorder.xcodeproj/project.pbxproj` | 手写 app + XCTest 工程、同步文件夹、Swift 5 模式、iOS 17 |
| `EarRecorder/EarRecorder.xcodeproj/xcshareddata/xcschemes/EarRecorder.xcscheme` | 共享运行、测试、归档 scheme |
| `EarRecorder/EarRecorder/App.swift` | SwiftUI 单页、设置、权限、UIApplicationDelegate、两个 App Intent |
| `EarRecorder/EarRecorder/Info.plist` | 中文隐私用途、后台模式、局域网/HTTP 配置 |
| `EarRecorder/EarRecorder/Store.swift` | manifest 原子读写/锁、设置、会话/段/上传状态 |
| `EarRecorder/EarRecorder/Audio.swift` | AVAudioEngine 转换、持续 writer 切换、PCM 备份、流式 AAC 恢复 |
| `EarRecorder/EarRecorder/Context.swift` | 单流 gzip JSONL、差量、summary、GPS/CLVisit/运动、围栏 |
| `EarRecorder/EarRecorder/GzipRecovery.swift` | 截断 gzip 修复，仅保留完整 JSON 行并校验 |
| `EarRecorder/EarRecorder/Runtime.swift` | 启动扫描、持续重试、分段计时、生命周期事件、通知 |
| `EarRecorder/EarRecorder/Upload.swift` | multipart 文件生成、NWPathMonitor、后台 URLSession、重试/恢复队列、音频清理 |
| `EarRecorder/EarRecorder/Diagnostics.swift` | JSONL 轮转与内存采样 |
| `EarRecorder/EarRecorderTests/RecorderTests.swift` | 协议、存储、格式、恢复、时间及注入测试 |

## 移植与行为差异

- `LocalRecordingPhoneAudioInput`：沿用 AVAudioEngine → AVAudioConverter → 16k mono PCM16 → AVAudioFile AAC 的转换及连续 sink 交换。码率固定 32 kbps，不回退到系统默认码率。增加 mixWithOthers、默认扬声器、可选 HFP；没有 Flutter、输入音量可视化或 100Hz IMU。
- 原 writer 使用 `.active.m4a` 工作文件；这里以 `.opening.m4a` 打开 AVAudioFile 后立即重命名正在写入的 inode 为 `.m4a.part`，PCM 备份同步写。正常切段不重启 engine。每次写入只保留本次小块，不异步排队音频缓冲。
- `PhoneLocalRecordingRuntimeSessionDriver` / Notifications：实现为一个串行 Runtime，DispatchSourceTimer 每秒检查切段/重试。中断、engine configuration、媒体服务重置触发封口；所有 routeChange 仅记录原因，不切段，真正停机由配置变化通知和 tick 检测处理。旧 engine 通知仍过滤。无限退避替代原有限尝试；interruption ended 是立即重试信号，不是允许重试的必要条件。
- `LocalRecordingPcmRecoveryEncoder` / ActivePartRecovery：以 64 KiB 块重编码备份，全文件解码验证 AAC。PCM 真实字节数决定时长；若 AAC 只是明显短于备份的可解码前缀，仍重编码。失败保留原文件并标记人工恢复，不制造成功或猜时长。
- `LocalRecordingAutoContinuation`：按最新 manifest 的 `userStopped` 判定，非用户停止的 paused 会话也可续录；先恢复，再建新 UUID 会话，记录 continuedFrom 与 auto_continuation。用户停止先落盘，避免停止期间被杀后误续录。
- Context 样本确认 `elapsedMs` 相对会话开始，字段 `device_model` / `os_version`、`locationAccuracy` / `locationTimestamp` / `locationMeta` 大小写不改。新会话无定位时使用 null，不伪造坐标；只将变化字段写入 delta。新定位触发为 continuous/source core_location_update。CLVisit 的 event 沿用 event/changed/meta 形状，reason 为 visit_arrival/visit_departure。运动分类为 motionActivity 对象（type/confidence），无原始惯性数据。
- 与 Octopus 对齐：本地 manifest.index、文件序号和 UI 段号从 **1** 开始，multipart 的 segment_meta.segmentIndex 与所有 Context 行的 segmentIndex 为 **index - 1**，从 **0** 开始。恢复重写 Context 时也统一为零起索引，避免 baseline 与补写的 summary 不一致。
- 单个 gzopen 流，每行 Z_SYNC_FLUSH，关闭写 CRC/footer。被杀后的 `.part` 先用源算法修复完整行，再流式重写为一个 gzip member；如缺少末行，补明确的 recovery 事件和真实已保留行的 summary，不冒充正常 stop。
- `LocalRecordingPcTransferSegmentUploader`：去掉同用户 PC 服务发现，目标为配置地址 + `/api/v1/record`；用户 ID 可空。保留 name/text/type/source/group_id/segment_meta/segment_start_ms/segment_end_ms/timestamp/file 和 X-Instance-Id、X-Device-Id、X-User-Id、X-Channel-Id、Authorization。源上传的 context 表单字段仅在 recordContext 非空时发送，本实现无该独立快照，不发送这个可选字段，完整 Context 单独上传。segment_meta.source 固定 phone_local_auto。
- 上传 body 每次按 64 KiB 流式生成；同时最多两个系统任务，优先派发较新会话的已封口分段；每分钟有执行机会时检查系统任务，已知总长度且已部分发送的任务连续 5 分钟无进展会取消重试。零字节、总长度未知、请求体已发完时不主动取消，另设 30 分钟资源超时，不加载整段音频。manifest 是唯一持久队列，后台 session 重连时对照 getAllTasks 修复没有系统任务的 uploading 状态。HTTP 2xx 标成功，中途停滞和临时连接错误指数退避至 60 秒，其他失败退避至 1 小时；不跨 HTTP 重定向转发 token。端到端是至少一次投递：服务器成功后本地未记成功的崩溃可能重传，接收端需按 group/type/index 幂等。
- 默认成功上传后保留 m4a 3 天，Context/manifest/恢复失败文件保留；token 存于本机 app sandbox 的 UserDefaults，日志不记录。ATS 配置保留本地网络访问，并为当前校园网接收端配置单地址 HTTP 例外，无额外第三方依赖。

## 线程、故障与注入边界

Runtime 在 `ear.runtime` 串行队列；录音 tap 同步使用 NSLock 保护 sink，转换及写盘有界。Location delegate/运动活动更新在主线程，将小字典交给 Runtime。Upload 使用独立队列，所有 manifest read-modify-write 经 Store 的递归锁与 atomic Data.write，避免录音/上传互相覆盖状态。UI 仅接收主线程 Snapshot，不负责定时和恢复。

`AudioInput`、`LocationInput` 可注入假实现；XCTest 无需麦克风/GPS。恢复测试使用真实系统 AAC 编码器；gzip 测试用 libz 解压，检查真实样本字段名、delta 差量、summary、截断尾部和恢复事件。其余测试覆盖原子读写/并发更新、multipart 两种类型全部字段和头、相对时间、失败重试/成功终态、userStopped 判定、注入录音开始/停止。

## 首轮验证（历史记录）

2026-11-13，使用 `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`，未修改全局 xcode-select。构建产物置于 `/tmp/EarRecorderBuild` 和 `/tmp/EarRecorderDevice`，不进入仓库。

| 检查 | 原文结果摘要 | 本机日志 |
|---|---|---|
| generic/platform=iOS Simulator build | `** BUILD SUCCEEDED **` | `/tmp/ear-simulator-final.log` |
| generic/platform=iOS、CODE_SIGNING_ALLOWED=NO build | `** BUILD SUCCEEDED **` | `/tmp/ear-device-final.log` |
| generic/platform=iOS Simulator build-for-testing | `** TEST BUILD SUCCEEDED **`，9 个 XCTest 方法编译通过 | `/tmp/ear-tests-final.log` |
| simctl list runtimes | `CoreSimulatorService connection became invalid`、`Connection refused`、`Unable to locate device set` | `/tmp/ear-runtimes.log` |
| plist / pbxproj | `OK`；`git diff --check -- clients/ios` 无输出 | 终端输出 |
| 本目录两份 Markdown 元数据与本地链接 | `iOS docs: 2 files, metadata and local links passed` | 调用现有 check_docs.check_file 验证 |
| 仓库 ruff check | `Found 491 errors.`；输出指向范围外 experiments 等 Python 文件 | `/tmp/ear-ruff.log` |
| 仓库 ruff format --check | `43 files would be reformatted, 882 files already formatted` | `/tmp/ear-format.log` |
| 仓库 pyright | `0 errors, 0 warnings, 0 informations` | `/tmp/ear-pyright.log` |
| 仓库 pytest | `81 failed, 1382 passed, 630 skipped, 1 warning, 96 errors in 39.83s` | `/tmp/ear-pytest.log` |
| 仓库 check_docs --base origin/dev | `文档检查未通过（共 707 项）`；本目录两份文件在人工 review 清单，无本目录检查错误 | `/tmp/ear-docs.log` |

Python 检查通过 `uv run --frozen --no-sync` 使用已有环境，ruff 禁用缓存，pytest 禁用缓存与字节码写入；没有修复、格式化或编辑范围外文件。已查看的 pytest 错误包含 `socketserver.py` / API fixture 的 `PermissionError: [Errno 1] Operation not permitted`，不能据此断言所有失败原因相同。文档失败包含大量既有 experiments Markdown 缺少 front matter。根 README、pyproject.toml、uv.lock 和 ingest 的并行变动不是本任务写入内容。

构建期间 Xcode 曾输出 `Could not archive SSU artifacts. Check build log.`（App Intents 自然语言训练阶段），该次仍以成功结束；最终构建没有该条，测试构建仍有系统签名 XCTest framework 不剥离二进制的 warning。不能用编译成功替代快捷指令实际发现/运行测试。

## 复核修改（2026-11-13）

本轮沿用 `Runtime`、`Store`、`Upload`、`Location` 的职责，增加 `MotionState` 保存最后已发出的运动分类；删除全量 `Store.all()` 扫描接口和只进不出的 `foreground()` 接口，由 `unfinished()` / `latest()` 与 `setActive(_:)` 替代。没有新增模块或重复的协调层。

1. **读盘与推送**：Store 初始化时只扫描一次磁盘，缓存未完成 manifest，另保存最新会话快照供自动续录判定；完成后移出工作缓存。`read/update` 命中缓存，原子写成功后再更新缓存。Runtime 缓存 startedAt；elapsed 和每秒 tick 不读 manifest。scenePhase active 才交付 Snapshot，进入 inactive/background 后连已经排队的 UI 推送也会丢弃。pending 只在初始恢复、段创建/封口和上传状态变化时重算，前台每秒刷新不重算。
2. **上传调度**：封段、网络恢复、上传完成和设置保存触发上传；兜底 timer 为 60 秒，pump 和后台任务重连只枚举未完成会话缓存。已完成会话的音频保留期使用独立 URL/上传时间小索引，每小时检查一次到期文件，避免重读历史 manifest；retainDays=0 的新上传会立即允许下一轮清理。清理错误只记日志，不阻塞上传。启动仍需一次历史扫描，显式读取非最新的历史会话也会读盘，不宣称完全零 IO。
3. **索引**：本地 1 起，multipart 与 Context 0 起；更新两类上传、baseline/delta/event/segment_end、恢复补写的断言。
4. **路由**：routeChange（含 categoryChange、新旧设备、override 等）只记诊断；测试验证段数和 stop 调用数不变，另验证 engine 停止与 configuration change 仍封口。
5. **围栏**：无 ear-latest 或离已监测中心严格大于 100 米时才重建 150 米区域。当前中心保存在内存，避免同批多个点早于系统 monitoredRegions 刷新时重复重建；启动可复用系统保存的区域，停止或监测失败清除内存中心。测试覆盖无区域、同点、100 米内、100 米外、移动后的新中心。
6. **运动**：type 改变，或最后已写入状态的 confidence 从 low 升到 medium/high 才写。比较基准是上次已发出的状态，忽略降级和 medium/high 摆动，因此 high→low→high 不会反复触发。新会话重置该状态。测试将实际回调入口接到 gzip Context，断言整串抖动只产生 5 条 delta 和对应 summary。

现有 9 个 XCTest 方法已更新，新增 5 个，共 **14 个**。新增测试还覆盖缓存启动重读/完成剔除/保留期清理、120 次后台 tick 的 manifest 读盘次数与 pending 重算次数不变、前后台推送、上传变化刷新 pending；所有录音/定位行为测试使用假输入或明确注入的数据，未访问真实麦克风。

本轮三项构建结果及模拟器状态见下表（日志在 `/tmp`，不纳入仓库）：

| 检查 | 结果 | 日志 |
|---|---|---|
| Simulator build | `** BUILD SUCCEEDED **` | `/tmp/ear-review-simulator.log` |
| iOS CODE_SIGNING_ALLOWED=NO build | `** BUILD SUCCEEDED **` | `/tmp/ear-review-device.log` |
| Simulator build-for-testing | `** TEST BUILD SUCCEEDED **`（14 个方法编译链接） | `/tmp/ear-review-tests.log` |
| simctl list runtimes | `CoreSimulatorService connection became invalid` / `Connection refused` / `Unable to locate device set` | `/tmp/ear-review-runtimes.log` |

本轮真机构建也输出 App Intents 训练的 `Could not archive SSU artifacts`，最终仍为 BUILD SUCCEEDED；快捷指令实际运行仍待真机验证。

本轮没有重跑无 Swift 关联变更的 Python 全仓检查，上节结果保留为首轮历史，不能算本轮新通过。仍不修改 Python、根文档或其他工作目录。

## 模拟器端到端验证（2026-11-13，Claude）

- 修复 `Recovery.validDuration`：`AVAudioFile.read(into:)` 在 EOF 抛错而不是返回 0 帧，原循环使每个 m4a 都被判无效（正常切段也会失败并暂停）。改为按 `framePosition < length` 读取。修复后 14 项 XCTest 全部通过（iOS 26.5 模拟器，iPhone 15）。
- 端到端：模拟器 app（分段 20 秒）→ 本机 `ear-ingest`（临时 inbox）。结果：音频 16kHz 单声道 AAC 约 32kbps；context 与音频同 index，`segmentIndex` 从 0 起；每次模拟位置变化写一行 GPS `delta`；`kill -9` 后重启，截断段恢复为 9.5 秒有效 m4a、Context 补 `recovery` 事件与 `segment_end`，随后自动续录新会话并上传。
- 旧 Ear 的 `ear.ingest.parsing.enumerate_audio_candidates` 只读解析临时 inbox，识别全部 5 条音频记录。
- 首次启动时引擎初始化因 coreaudio RPC 超时崩溃一次（`AURemoteIO::Initialize`），第二次正常；推测是 Mac 上 Simulator 麦克风授权弹窗未确认，真机上需观察是否复现。

## 真机联调（2026-11-13）

真机容器中同一会话已生成两段 m4a、两个对应的 Context 和 manifest。诊断日志在同一秒记录 engine_stopped 与 interruption_began，下一秒进入 recording_resumed 并打开下一段。日志分辨率为秒；两条通知可能属于同一次中断，具体外部触发及采样级空档未核实。此次启动未见引擎初始化崩溃。短期内存样本不足以估计趋势。

只读检查本机旧 Ear 的 ear/ingest/sensors.py，合成相同经纬度分别放入 context 和 changed，前者得到 location observation，后者返回 None。此前安装的版本在 GPS delta 里只写 changed，因此原始附件有点不等于每点都有位置 Evidence。经负责人确认，客户端现保留 changed，并在同一条位置 delta 的 context 中补全经纬度、时间及可用精度，供旧解析器生成位置观测。普通状态 delta 不重复发位置。已封口的历史文件保留原始内容。语义 Context 解析仍可应用 delta 更新状态。GPS 密集点从手机到①的实测仍待含位置点的样本验证。

GPS 兼容改动已通过模拟器 XCTest（14 项全部通过），签名构建已安装并在真机启动。后台 tick 回归测试现临时使用默认分段，结束恢复原设置；此前继承模拟器的 20 秒分段导致 120 秒测试窗口发生 6 次正常切段。安装前确认上一会话已手动停止、等待上传。

本次长测起点为 12:58 JST。设备报告锁定，活跃会话为 recording，间隔约 9 秒读取同一段，PCM 从 1.4 MB 增至 1.7 MB，m4a 从 205 KB 增至 240 KB。已确认短时锁屏写入，首次 30 分钟切段、跨段空档和 24 小时结果待验。

真机出现路由变化与引擎中断，两次音频启动失败产生了只有 Context 的第 2、3 段，恢复时报 PCM 文件不存在；第 4 段已产生 m4a。中断至恢复约 106 秒。现先激活音频会话，再分配段号和写 Context，激活失败仍按原策略重试。既有会话的失败记录保留。

后一次真机会话在引擎恢复同秒停止，第 4 段留下 0 字节 PCM 和只有头部的 557 字节 M4A；第 1 段完整解码 264.256 秒。现在恢复时区分空备份、缺失备份及非法长度；保留空段原文件，单列文件问题并标注会话时间和段号。

真机长测 13:49 会话在安装队列修复前已封口 5 段，均未派发；旧会话有一个在途任务。16:12 覆盖安装并启动后，第 6 段恢复并自动续录。手机 6 段的音频和 Context 均标记上传成功；Mac 的同一会话收到 6 对附件，全部音频可完整解码，全部 Context 可解压并逐行解析。覆盖是人为中断；此次验证覆盖补传，下一次自然切段的交付延迟和下游 Evidence 另行核实。

2026-11-13 单段真实入库验收：以旧 Ear 的单条 brain-event 接口分别提交同一段约 46 秒音频和配套 Context，均返回 accepted。② 的 ingest_capture 通过 Evidence HTTP API 读取该段，并校验 inbox 音频大小与 SHA-256，写入隔离 PostgreSQL 后端的① HTTP 服务。读回 audio_ref 1、ambient_sound 4、phone_info 1；该段 speech_segment 为空，无转写、局部说话人、声纹或 GPS 点。重建 HTTP 客户端后再次导入得到 AlreadyIngested，七类记录计数均未变化。仅定向提交这一段，临时测试库随夹具销毁。

## 尚未完成的验证

- 已安装真机并生成录音文件。锁屏跨 30 分钟切段、指定中断场景、蓝牙切换、重开续录与系统位置唤醒均待专项验证。
- 24h 内存/耗电/温度/空档验收尚未完成。单段 Evidence → ② → 隔离①已验证，常驻导入待验。
- 后台 URLSession 在真实网络中断/切换下的重传尚需真机验证；无限退避是进程可执行时的策略，不保证系统一定给执行机会。
- 界面为临时版本，待重做。

### 2026-11-13 真机上传延迟

19:18–20:45（日本时间），手机记录了 28 条 `upload_stalled`，每次已发送字节均为 0。原先的 5 分钟检测会主动取消尚未发出请求体的后台任务，取消后又进入通用指数退避。日志无法定位最初未发送的原因。20:49 修复安装前，第 9 段音频和 Context 已补传成功。现仅在已知总长度且部分发送后停滞时主动取消，等待连接和等待服务响应由 URLSession 管理。模拟器回归涵盖零字节、长度未知、已发完、部分发送后停滞，以及临时错误与普通错误的退避上限。

20:55 覆盖安装时旧会话正在录第 10 段。锁屏阻止开发工具启动 app；解锁打开后，20:56:49 真机记录 `auto_continuation` 和 `recording_resumed`，已从内置麦克风续录。旧会话变为 completed，第 10 段标记 recovered，音频和 Context 均已上传。Mac 收到的音频可完整解码（773.888 秒），Context 通过 gzip 完整性检查。此次恢复发生在用户打开 app 后；修复版尚未重复经历同等时长的接收端离线。

21:24:49–21:34:49（日本时间）暂停并恢复 Mac 接收进程，端口仍监听，暂停期间健康请求超时，恢复后健康检查成功。手机锁屏运行，前两段均封口，第 3 段已开始。第 1 段于 21:26:49 封口，21:56:50 音频和 Context 同报系统请求超时；本轮无 upload_stalled 事件。接回 Type-C、保持锁屏后，22:00:28 Mac 收齐两段共四个附件，距服务恢复 25 分 39 秒。两份音频完整解码（1800.128、1801.152 秒），两个 Context 通过 gzip 检查，四个附件与手机副本逐字节一致。接线后观察到 8791 在 en7 的链路本地地址上有连接，无法从该次收件推断纯 Wi-Fi 自动补传耗时。本轮尚未实测 Mac 整机休眠或关闭手机无线网络。

第二轮全程拔线。22:24:51 暂停接收，22:34:51 恢复，随后观察 60 分钟，未收到预期的第 3 段音频或 Context。手机在同一校园 Wi-Fi；Safari 通过 Mac 的 Wi-Fi 数字 IP 访问健康检查成功，使用同一服务的 .local 地址失败。23:11:58 Mac 在 Wi-Fi 地址记录了与该检查时间一致的 TCP 连接。约 23:13 将 App 服务地址改为该 IP，至观察结束仍无目标附件。手机随后报告第 5 段被 App Transport Security 拦截。此前构建同时设置 NSAllowsLocalNetworking 和 NSAllowsArbitraryLoads；iOS 会忽略后者。已为当前 Mac 数字 IP 添加 NSExceptionAllowsInsecureHTTPLoads，签名包已核对，模拟器 20 项 XCTest 通过。覆盖安装后先拔线、再打开 App；Mac 的 Wi-Fi 地址上记录到来自手机的连接。23:43:40 收齐第 6 段，23:46:44–23:46:50 收齐第 3–5 段。四段音频均完整解码，四份 Context 均解压并逐行通过 JSON 解析。此轮验证修复后积压分段可经校园 Wi-Fi 补传；打开 App 参与了恢复。

### 2026-11-14 真机公网回退

手机连 Type-C，Debug 启动时用 EAR_TEST_ISOLATE_DIRECT=1 将首次直连探测指向手机上未监听的回环端口，故障只生效一次。EAR_FALLBACK_URL 写入用户设置的备用 HTTPS 根地址。手机日志 00:02:26 记录 direct 探测失败并自动续录，00:02:28 记录 fallback 探测通过并派发同段两个附件，00:02:29 和 00:02:36 分别收到 Context、音频成功回执。Mac inbox 内音频完整解码 1092.480 秒，37 行 Context 逐行解析通过。实测使用 Cloudflare Quick Tunnel，该临时入口不能作为重启后稳定的域名。单独的 FRP 代理注册成功但公网 TLS 未接通，已撤下测试代理。此轮覆盖应用层直连故障，未切换手机到蜂窝网。已派发系统任务的回退仍取决于 URLSession 返回失败的时间。

### 插线蜂窝上传（2026-11-14）

Type-C 全程连接，用户将手机切到蜂窝，Mac 临时仅在回环地址监听 8791。Mac 局域网地址被拒、公网 HTTPS 健康检查通过，USB 调试继续可用。手机日志显示第 1 段 00:32:26 自然封口，00:32:27 直连探测失败，00:32:29 备用探测成功并派发两份附件；Context 于 00:32:32、音频于 00:42:57 返回成功。iPhone Instruments 连接表中 EarRecorder 连接到隧道域名解析所得的边缘地址，接口 4 映射为蜂窝接口 pdp_ip0。Mac 收到的 7,232,303 字节 AAC 文件可完整解码，时长 1800.128 秒；Context 解压后 116 行均通过 JSON 解析。第 2 段继续录音。手机与 Mac 的时钟存在差异，以上延迟均以手机日志计算。Mac 已恢复原监听，可用 services/ingest/scripts/isolate_direct.py 重复隔离。

### 两条上传路径同时中断（2026-11-14）

Type-C 保持调试连接。以 Mac 时钟计，00:49:43 暂停 ingest，直连拒绝连接、备用入口返回 HTTP 502。手机自然封口第 2 段，直连及备用探测均失败；USB 读取的 manifest 显示音频和 Context 均为 pending，原文件保留，第 3 段正在写入，Mac inbox 无第 2 段附件。01:02:28 接收服务恢复健康，手机自动选中直连并派发。Mac 于 01:09:12 收到 Context、01:09:21 收到音频，距恢复约 6 分 53 秒。手机两项最终均为 uploaded。音频完整解码 1801.152 秒，Context 的 62 行 JSON 全部可解析。此轮模拟接收端整体不可达，手机无线网络保持开启。

### 合盖睡眠期间自动补传（2026-11-14）

Mac 电源日志记录 01:47:28 合盖入睡，02:19:38 开盖完全唤醒，间隔 32 分 10 秒；其中睡眠累计 29 分 55 秒，三次 DarkWake 合计 2 分 15 秒。第 4 段在手机记录的 02:02:29 封口。Mac 在 02:06:49 维护唤醒，同秒收到 Context，02:06:53 收到可完整解码的音频。用户未打开 EarRecorder，收齐时距离开盖还有 12 分 45 秒。随后通过 USB 读取手机日志：两条路径曾连续探测失败，手机时钟 02:07:12 直连恢复，两份附件均经 direct 派发，02:07:13 收到 Context 成功回执，02:07:17 收到音频成功回执。第 4 段两项均为 uploaded，第 5 段仍在录音。两端时钟有偏差。

### 下游延迟基线（2026-11-14，第 5 段）

本轮定向通知同一段的两份附件。Mac 时钟 02:32:07 Context 已落盘，旧 Ear 返回 retry / matching_audio_capture_not_ready；音频 02:32:11 已落盘，测试端 02:32:32.737 确认音频受理，02:32:33.699 重送 Context 成功。02:33:14.342 首次轮询观察到部分 speech_segment；02:36:37.313 适配器确认整段就绪，02:36:37.972 从隔离①读回 30 条转写、6 个局部说话人、5 条声纹、157 条环境音、2 条手机信息、1 个音频引用、0 个 GPS 点。重复导入断言通过。音频受理至部分转写约 42 秒，至新 Ear 整段可查约 4 分 05 秒。重送由测试端执行；本轮未验证常驻投递与乱序自动恢复。

### 真机网络诊断（2026-11-14）

`network_path` 记录默认网络路径变化，含路径序号、状态、使用的接口类型、可用接口名及 IPv4/IPv6/DNS 支持。手动双路探测和自动上传选路都写 `upload_probe`，记录直连/备用、主机名、起止路径序号、耗时、HTTP 状态、身份校验和 NSError 域/代码。通过 probe ID 或分段任务键关联 `network_transaction`，核对系统报告的本地和远端 IP、蜂窝/代理标志以及 DNS、连接、TLS 阶段。新进程重新编号。探测 URL 的查询参数、token 和响应内容均不进入这些事件。日志含网络地址，随已有 diag.jsonl 在本机轮换。

已安装版本在校园 Wi-Fi 的健康探测验证成功，实际上传的两份文件均记录成功。旧 Quick Tunnel 地址在手机上返回 NSURLErrorDomain -1003。系统代理标志只描述 URLSession 观察到的连接，不能识别 Shadowrocket 的节点或规则。蜂窝/Wi-Fi 切换还需对照请求时的实际 transaction，单凭“可用接口”列表不能认定走了蜂窝。

2026-11-14 13:55–13:58 JST，按操作者记录的顺序做真机六组手动健康探测。直连目标 <configured-host> 身份验证结果（毫秒）：校园 Wi-Fi 同 Mac 节点 1486/554 成功，关闭 Shadowrocket 49/27 成功，新加坡节点 557 失败（-1005）。蜂窝同 Mac 节点 1155/524 成功，关闭 128/73 成功，新加坡节点 5013 超时（-1001）。两组新加坡各有一组完整日志；诊断进行中再次点击会忽略。开启时 URLSession 连接本地代理 127.0.0.1，关闭时实际对端为配置的 IP。旧备用地址十组全部失败。13:57:45 手机恢复 Wi-Fi。节点标签来自人工操作，未取得 Shadowrocket 历史规则日志。这是健康探测，不能反推昨晚的节点或长文件传输速度。

### 2026-11-14 接收端配对验证

iOS 26.5 模拟器通过 Bonjour 解析 Mac 的接收端 UUID，在校验邀请中的 TLS 叶证书指纹后完成兑换，Keychain 凭据可读。第二轮用一个独立接收端身份、禁用其 Bonjour 发布并配置回环 HTTPS 备用地址；模拟器完成兑换、设备身份探测与合成附件上传，临时 inbox 中的文件与输入逐字节一致。该轮验证备用路径选择和请求协议；公网转发、真机扫码及锁屏后台配对上传仍待实测。运行集成 XCTest 时可在模拟器 App 数据容器的 tmp/ear-test-invitation 放入私有测试邀请，用单设备、关闭并行的 XCTest，测试结束后删除该邀请。

2026-11-14 模拟器通过实际公网 `relay.example.org:8080` 和独立临时收件目录完成加密配对、健康探测、附件上传及请求回执验证。测试收件与合成输入逐字节一致。真机新版的蜂窝锁屏测试待做。

### 2026-11-14 真机蜂窝中继与延迟

iPhone 15 保持 Type-C 调试连接。关闭 Wi-Fi 后，两份后台上传任务的 URLSession 指标均为 cellular=true、proxy=false，对端为 <relay-host>。设备日志同时确认两份附件都选择 fallback 并验证成功回执。安装合包修复版使当前段提前封口，Mac 收到长度 504.448 秒、大小 2,046,338 字节的 AAC，ffmpeg 完整解码成功，配套 Context 通过 gzip 完整性校验。30 分钟自然切段未在这次覆盖安装测试中完成。

### 电话结束后恢复采集（2026-11-14）

用户确认旧版 20:10、21:04 两次中断对应主动拨号，22:56 对应来电。后者也有约 29 秒采集空档，并已自动恢复。两次外呼对应的采集空档约 54、57 分钟，其中包含电话占用时间。旧日志没有挂断时间。

Runtime 在启动恢复前订阅 CXCallObserver，监听最后一通电话结束，在录音意图仍有效时立即尝试重新激活音频。通话状态只约束重新激活，实际音频中断才封口。路由、位置、前台及定时回调可以在系统给予执行时间时校正通话状态，用户手动停止后不会重新启动录音。诊断 call_changed 记录通话方向、是否接通及结束，不含号码。模拟器 XCTest 验证中断期间重复通知、多通电话状态、缺少音频结束通知和手动停止。新版覆盖安装后，真机日志出现 auto_continuation 和 recording_resumed，内置麦克风恢复，活动 PCM 持续增长。

23:22:06 未接通外呼触发中断，23:22:30 首版收到挂断但两次引擎启动失败，PCM 均为空。23:24:26 由开发工具回前台，系统在同秒补送后两通的 CallKit 事件并恢复采集。第二版在末通结束时申请短时后台执行，给路由 1 秒切换，检查输入就绪后开段；成功、到期、再次通话或手动停止时释放后台任务。模拟器覆盖首次激活失败后的延迟重试，24 项中 1 项跳过，0 失败。第二版的真实挂断延迟仍待测。

### 手填 HTTP 直连修复（2026-11-18）

`Upload.swift` 原先把所有 HTTP 地址都当作加密中继（`Relay.accepts` 只看 scheme），未配对时报“请先扫码配对中继”，手填 `http://<名字>.local:8791` 无法上传。现改为仅在已配对时走 `/api/v1/relay`，未配对时普通直连；完成回调按请求是否带 `X-Ear-Request` 决定是否校验中继回执。验证：模拟器 `build` 与 `build-for-testing` 通过；未在真机复测手填直连。
