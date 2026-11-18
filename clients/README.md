---
audience: human
doc-type: guide
based-on: docs/proposals/phone-recorder-extraction.md（accepted：iOS 先做，Android 随后）；docs/modules/phone-client-protocol.md；clients/ios 与 services/ingest 的当前实现；YijieYing 2026-11-18 要求整理文档与双端同步守则、由 ay-tony 实现 Android 版
---

# 手机采集端

Ear 的手机端只负责**录音、GPS 与手机状态的采集和上传**。转写、声纹、入库、MCP 都在电脑端，手机端不参与。

| 目录 | 平台 | 状态 |
|---|---|---|
| [`ios/`](ios/README.md) | iOS 17+，Swift / SwiftUI 原生 | 可用，真机联调过 |
| `android/` | Android，Kotlin 原生 | 未开始 |

两端都是原生实现，不共享代码，**共享的是协议**：[手机客户端协议](../docs/modules/phone-client-protocol.md)。电脑端的接收服务是 [`services/ingest`](../services/ingest/README.md)，两端连的是同一个接收服务。

## 双端同步守则

两套原生代码长期并存，靠下面这几条保持一致。

1. **协议只有一个出处。** 上传字段、分段规则、Context 格式、设备 ID、配对二维码、加密中继，以 [手机客户端协议](../docs/modules/phone-client-protocol.md) 为准。代码和协议文档不一致时，先报告，由负责人决定改哪边，不按某一端的现状悄悄对齐。
2. **改协议的 PR 必须同时改：** 协议文档、`services/ingest` 的实现、`services/ingest/tests/vectors/` 的测试样例。只改了一端客户端的话，在 PR 里写明另一端的跟进 issue，并标出接收端如何兼容旧客户端。
3. **接收端向后兼容。** 新字段默认可选；删除或改义一个字段之前，两端客户端都要先发布不再依赖它的版本。
4. **两端都必须通过一致性检查**：用 `services/ingest/tests/vectors/` 的测试样例自测加密、签名、二维码解析，并对本机接收服务跑一次合成数据的端到端上传（见协议文档的“一致性检查”）。没通过的版本不发给大家。
5. **功能分三类，PR 里写明属于哪一类：**
   - **协议功能**（例如新的上传类型、新的 Context 字段）：两端都要做，按第 2 条走。
   - **平台功能**（例如 iOS 的 ATS、安卓的前台服务和厂商保活）：只在一端，写进该端自己的 README，不进协议。
   - **实验功能**（例如实时转写预览）：先在一端做、验证过再进协议，进协议之前另一端不必跟。
6. **功能对照表放在本文件**（下表）。任何一端新增或删掉用户可见的功能，同一个 PR 里更新对照表。
7. **各端的 README 都要有“从零开始”**，让没参与过的人能装到自己手机上并确认电脑收到文件。iOS 的见 [iOS README](ios/README.md#从零开始在自己的-iphone-上跑起来)。
8. **不进仓库**：签名证书与密钥库、个人签名 Team、真实设备 ID、真实录音与 GPS、个人中继域名和凭据。

## 功能对照

| 功能 | 类别 | iOS | Android |
|---|---|---|---|
| 连续录音，30 分钟分段，16 kHz 单声道 32 kbps AAC | 协议 | ✅ | — |
| GPS、电量、充电、运动状态 Context | 协议 | ✅ | — |
| 局域网直连上传与重传 | 协议 | ✅ | — |
| 断网保留、联网补传 | 协议 | ✅ | — |
| 扫码配对（HTTPS，证书指纹校验） | 协议 | ✅ | — |
| 加密中继（跨网络上传） | 协议 | ✅ | — |
| 带 challenge 的健康探测与路径选择 | 协议 | ✅ | — |
| 电话 / 系统音频中断后续录 | 平台 | ✅ CallKit / 系统结束通知；缺通知时可能持续等待，见 [恢复边界](ios/README.md#数据与恢复) | — |
| 蓝牙麦克风输入开关 | 平台 | ✅ | — |
| 快捷指令开始/停止录音 | 平台 | ✅ | — |
| 实时转写预览（PCM 流） | 实验 | 本地分支，未提交 | — |

## 给 Android 实现者

**先读：** [手机客户端协议](../docs/modules/phone-client-protocol.md) → [接收服务 README](../services/ingest/README.md) → [iOS README](ios/README.md)（看行为和边界，不必读 Swift）。

**建议的顺序**（每一步都能单独验收）：

1. 设备 ID、分段录音、Context 写文件，本机能导出可播放的 m4a 和 jsonl.gz。
2. 局域网直连上传到本机接收服务，用协议文档的端到端验收确认落盘。
3. 断网保留与补传。
4. 扫码配对与 HTTPS 证书指纹校验。
5. 加密中继：先过测试样例，再对真实接收服务跑通。
6. 长时间后台录音与保活，按下面的平台差异逐项真机测试。

**和 iOS 不同、需要自己处理的地方：**

- **后台录音**：要用前台服务（`foregroundServiceType="microphone"`）和常驻通知；Android 14 起只能在 app 可见时启动前台服务。
- **厂商省电策略**：小米、华为、OPPO、vivo 等会杀后台，需要引导用户关闭电池优化、允许自启动。这是安卓最大的不确定性，至少在两个品牌上做 24 小时测试。
- **定位**：后台定位要单独申请（`ACCESS_BACKGROUND_LOCATION`），通常要用户到设置里选“始终允许”。很多国内手机没有 Google 服务，不能只依赖 Fused Location，要能退回系统 `LocationManager`。坐标统一用 WGS84。
- **电话与音频焦点**：通话期间其他 app 拿不到麦克风；要监听音频焦点和通话状态，结束后续录。
- **局域网与明文**：默认禁止明文 HTTP，局域网直连需要在网络安全配置里只放开局域网；没有 iOS 的“本地网络”权限，Bonjour 用 `NsdManager`。
- **后台上传**：用 WorkManager，上传状态以接收端回执为准。
- **凭据保存**：配对得到的设备凭据存 Android Keystore。
- **中继判断**：是否走加密中继看是否已配对，不要按地址是不是 http 判断（iOS 曾因此出错，见协议文档“已知不一致”第 1 条）。
- **分发**：直接发 APK 即可，不需要 TestFlight；签名密钥库不进仓库。

**可参考的现成代码：** octopus-flutter 的安卓原生部分（`android/app/src/main/kotlin/.../localrecording/` 下的前台录音服务与 Context 采集）已经处理过前台服务和后台定位，可以借鉴写法，但上传协议以本仓库的协议文档为准，不沿用 Octopus 的接口。
