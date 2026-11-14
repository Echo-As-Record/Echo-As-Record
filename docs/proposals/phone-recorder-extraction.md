---
audience: human
doc-type: proposal
status: accepted
generated-by: Claude (AI)，应 YijieYing 要求
generated-at: 2026-11-13
based-on: octopus-flutter 67869770（ios/Runner 本地录音、PC transfer、Context writer；docs/features/local-recording.md）；servicehub euxcet/octopus-service-brain-channel（api/records.py、record_ingest.py）；<owner>/octopus-service-ear（ear/ingest/brain.py、context.py）；~/.octopus/local/inbox/brain/<device>/2026-11-10～13 的真实上传；docs/design/design-v1.2.md 3.4 节；local-notes 2026-11-13 夜间口述第 1 件
---

# 提案：把手机录音链路从 Octopus 抽成 Ear 自己的基础设施

> 2026-11-13 YijieYing 在 Claude Code 对话中同意：新写原生 app；iOS 先做，Android（APK）随后、不急；新增 `clients/` 目录并改 README；去掉 100Hz IMU；码率降到 32kbps。本文件据此标为 accepted，未经逐字人工复审。

## 1. 目标

把 octopus-flutter 里和 Ear 有关的部分（手机录音、分段、GPS/上下文、传到电脑）最小化地抽出来，作为 Ear 的数据入口。要解决三件事：

1. 录音会自己断。要做到 24 小时不断，并尽量省电。
2. 提高 GPS 采样频率，**不改音频切分**（仍然 30 分钟一段）。
3. 接到当前 Ear 架构，下游（旧 Ear 处理、② 适配器）尽量不改。

## 2. 现状

### 2.1 数据怎么从手机到 Ear

```text
iPhone（octopus-flutter，phone_local）
  AVAudioEngine → 16kHz 单声道 → AAC .m4a，每 30 分钟封口一段
  同 index 的 Context（GPS 等，jsonl.gz）、IMU（jsonl.gz）
  ↓ 局域网发现同用户 PC 服务，multipart POST
  /api/v1/instances/brain___<设备ID>/service/api/v1/record
PC：Octopus 的 brain channel（servicehub euxcet/octopus-service-brain-channel）
  落盘到 ~/.octopus/local/inbox/brain/<设备ID>/<日期>/phone/<开始时间>__<groupId>/
  发 brain.record.received 事件
  ↓
旧 Ear（octopus-service-ear）：接收端逐条发送持久化的 brain 事件 → ASR / 声纹 / 环境声 → Evidence API
  ↓
② 适配器读 Evidence API → ①
```

上传字段：`name`、`text`、`type`（`richVoice` / `phoneIMU` / `phoneContext` / `phoneNotes`）、`source=local`、`context`、`group_id`、`segment_meta`、`segment_start_ms`、`segment_end_ms`、`timestamp`、`file`；头部 `X-Channel-Id: brain`、`X-Device-Id`、`X-User-Id`、`Authorization`。

### 2.2 录音为什么会断（来自 11-10～11-13 的真实上传）

| 会话开始 | 持续 | 结束方式 | 下一段开始 |
|---|---|---|---|
| 11-10 10:52 | 3.5 h | 没有 stop，直接中断 | 3 秒后 |
| 11-10 14:19 | 1.9 h | 同上 | 5 秒后 |
| 11-10 16:15 | 2.5 h | 同上 | 约 1.5 h 后 |
| 11-11 10:12 | 2.3 h | 同上 | 9 秒后 |
| 11-11 12:30 | 0.9 h | 同上 | **约 28 h 后** |
| 11-12 17:07 | 3.2 h | 同上 | 3 秒后 |
| 11-12 20:21 | 2.3 h | 同上 | **约 2 h 后** |

（时间为手机本地时间。）

判断：

- **进程被系统杀掉**，不是用户停止：每次都没有正常的 stop，最后一段是截断后由恢复逻辑补封口的。
- **紧接着几秒后新开的会话，是被「显著位置变化」唤醒的**：新会话第一行 Context 的定位来源是 `significant_change`。也就是说，app 被杀后 iOS 因位置变化把它在后台拉起，自动续录成功开始了新会话。这证明「被杀后在后台重新开始录音」在当前 iOS（26.6）上是可行的。
- **长空档是因为没有被唤醒**：人不动时没有显著位置变化，app 就一直不回来，直到用户自己打开。
- 存活时间稳定在 1～3.5 小时，更像**内存逐渐增长后被 jetsam 杀掉**，而不是偶发崩溃。这一点还要用手机上的 `JetsamEvent-*.ips` 确认（设置 → 隐私与安全性 → 分析与改进 → 分析数据）。
- 其他会断的点（代码里看到的）：
  - 音频会话没有 `.mixWithOthers`，别的 app 一放声音就会打断录音；在后台恢复只重试约 20 秒（5 次），失败后停在暂停，等用户回前台。
  - 没有处理 `AVAudioEngineConfigurationChange` 和 `mediaServicesWereReset`；引擎因路由、采样率变化或媒体服务重启而停掉时，不会自动重建。
- 额外负担：手机 IMU 加速度和陀螺仪按约 100Hz 写 gzip JSONL（每小时各约 36 万条），还有一个整点切分的独立 `sensor_phone` IMU 流。这是不小的 CPU、写盘和内存开销，而 Ear 目前用不上这么高的频率。

### 2.3 GPS 现状

只在这些时候取一次位置：开始录音、权限恢复、网络路径变化、每 30 分钟的周期采样；两次之间至少隔 10 分钟，IMU 判断静止时还会抑制。所以 GPS 很稀，这就是代码结构笔记里「② GPS 密度（手机侧）」的缺口。

## 3. 提议

### 3.1 手机端：新写一个只做 Ear 的原生 iOS app（推荐）

不再依赖 Flutter 和 Octopus 全家桶，用 SwiftUI + 原生代码写一个小 app，只保留：

| 保留 | 来源（octopus-flutter iOS） | 改动 |
|---|---|---|
| 录音：AVAudioEngine → 16kHz 单声道 → AAC m4a，30 分钟分段 | `LocalRecordingPhoneAudioInput.swift`、`ContinuousLocalRecordingM4ASegmentWriter` | 切分不变；加 `.mixWithOthers`；处理引擎配置变化和媒体服务重启 |
| 中断后自动续录、被杀后恢复 `.part` 并重新开始 | `PhoneLocalRecordingRuntimeSessionDriver`、`LocalRecordingActivePartRecovery`、`LocalRecordingAutoContinuation` | 精简；后台重试不再 20 秒就放弃 |
| Context（GPS 等）按音频 index 分段写 jsonl.gz | `LocalRecordingContextWriter` | 格式不变，GPS 变密（见 3.3） |
| 上传：同样的 multipart 协议 | `LocalRecordingPcTransferSegmentUploader` | 配对后按接收端 UUID 发现局域网地址，必要时使用配对的公网中继；兼容手动填写地址 |

去掉：戒指、IM、PC Chat、记录列表、Octopus 登录、100Hz IMU。IMU 改用系统的 `CMMotionActivityManager`（步行 / 骑车 / 乘车 / 静止，几乎不耗电），夜间口述里「骑车时听到的 A」正好需要这个。

为什么不直接改 octopus-flutter：那是同学（石伟男）维护的完整产品，Flutter 引擎本身常驻就占不少内存，又带着很多和 Ear 无关的运行时；我们要的恰好是「后台长时间活着」，内存越小越不容易被杀。原生小 app 也便于我们自己快速改。

### 3.2 24 小时不断

按优先级：

1. **减少被杀**：原生小 app 的常驻内存远低于 Flutter 版；去掉 100Hz IMU；长期运行前后对比内存曲线，排查增长点。
2. **少被打断**：`.mixWithOthers`；处理引擎配置变化、媒体服务重启；后台恢复改成持续退避重试（例如最长每 1 分钟一次），不放弃。
3. **被杀后自己回来**：保留显著位置变化唤醒；另加**围绕当前位置的地理围栏**（离开约 100～200 米就唤醒）和**到访监测（CLVisit）**，人静止时也有更多唤醒机会。被唤醒后先修复上一段，再自动开新会话（已在当前数据里证明可行）。
4. **回不来时告诉你**：恢复失败就发本地通知「录音已中断，点一下继续」；可选再用 iOS 的 App Intent，让快捷指令自动化（例如每天定时、接上充电器时）一键重新开始。
5. **用户手动划掉 app** 是 iOS 明确不允许后台自动拉起的情况，只能靠第 4 条提醒。

### 3.3 省电

- 音频：16kHz 单声道 AAC，码率从 64kbps 降到 32kbps，对语音足够，写盘减半。
- 去掉 100Hz IMU 写入（3.1）。
- 上传：已封口分段照常上传；网络不通时排队，不反复空转重试。
- GPS：见下条，静止时自动暂停。
- 实际耗电要在真机上测（每小时掉电百分比、温度），这里不给数字。是否需要在一天中段充一次电，测完再定。

### 3.4 提高 GPS 频率，不改音频切分

- 录音期间开启连续定位：精度约 10～100 米、`distanceFilter` 约 25 米、`pausesLocationUpdatesAutomatically` 打开（静止自动停，有移动再恢复），再配合到访监测记录「在某处停留」。
- 新的位置点**照旧写进同 index 的 Context 文件**，用现有的 `delta` 行格式（`latitude`、`longitude`、`locationAccuracy`、`locationTimestamp`、`locationMeta.trigger` 标为新来源）。音频仍按 30 分钟封口，Context 跟着同一个 index，切分完全不变。
- 这样旧 Ear 的 Context 解析和 ② 不用改，只是点变多。需要核实：旧 Ear 的 context 规则在点很密时是否会去重或限流。

### 3.5 接入 Ear

分两步，每一步都让下游不改：

原计划先接现有 brain channel，但实测经 Octopus 管理器转发的 brain 接口要求 Octopus 登录凭据（未带凭据返回 401），新 app 不做 Octopus 登录，所以直接做第二步：

- **Ear 自己的小接收服务 `services/ingest`**：FastAPI，接受同一套 multipart 字段（兼容旧 URL 路径），用 Ear 自己的 token 鉴权，写**与 brain channel 相同的 inbox 目录布局**（`inbox/brain/<设备ID>/<日期>/phone/<开始时间>__<groupId>/`）。配置定向事件后，接收服务负责重试至识别端接受。Context 可以先来，音频到达后再关联；音频也可先处理，迟到的 GPS 和手机状态由②补入。旧 Ear 的整库补扫需要显式触发。兼容识别端可以独立于 Octopus Manager 部署。按设计 3.4「封装、不重写」，旧 Ear 的处理逻辑不动。

## 4. 放在哪里

新增顶层目录 `clients/`：`clients/ios/` 为 iOS app，之后的 Android app 放 `clients/android/`。接收服务为 `services/ingest/`。README 第 2 节已同步。

## 5. 已决定（2026-11-13）

1. 新写原生小 app，不在 octopus-flutter 上改。
2. iOS 先做；Android 也要做，不急。
3. 目录按第 4 节。
4. 码率降到 32kbps，去掉 100Hz IMU。
5. 2026-11-14 追加：手机绑定具体 Mac；局域网发现与公网备用共享该接收端身份。公网入口由部署方提供。

## 6. 验收

- 真机连续 24 小时：所有中断都有记录；被杀后在设定时间内恢复，或有通知提醒；空档总时长统计。
- 每小时掉电和发热记录。
- GPS 点密度：移动时每 25 米左右一个点，静止时不刷点；音频分段时长与现在一致。
- 上传后旧 Ear 正常出 Evidence，② 能读到更密的 GPS。

## 7. 联调条件（2026-11-13 本机检查）

- 本机有 Xcode 26.6，但 `xcode-select` 指向 Command Line Tools，**没有安装 iOS 模拟器运行时**，也没有连接 iPhone。
- 模拟器只能验证编译、界面、上传协议和模拟定位；**后台被杀、耗电、真实 GPS、来电打断都必须在真机上测**。真机安装需要 Apple 开发者签名（免费 Apple ID 也可，7 天过期）。
