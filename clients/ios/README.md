---
audience: human
doc-type: guide
based-on: 本轮负责人对 iOS 原生录音 app 的明确实施授权；docs/proposals/phone-recorder-extraction.md；octopus-flutter 67869770
---

# Ear Recorder（iOS 17+）

原生 SwiftUI 手机采集端。连续录制 16 kHz 单声道、32 kbps AAC，每 30 分钟封口 m4a；同 index 保存 GPS、系统电量、充电状态与系统运动分类 Context。不包含 Flutter、第三方包或高频 IMU。功能依据见[提案](../../docs/proposals/phone-recorder-extraction.md)（已采纳）。

## 从零开始：在自己的 iPhone 上跑起来

适合第一次接触本项目的团队成员。照着做完，你的 iPhone 会持续录音，并把录音和 GPS 上传到你自己的 Mac。这里只到“Mac 收到文件”为止；转写和入库是另外的服务，见 [② 适配器](../../services/adapters/README.md)。

### 0. 准备

- 一台 Mac，安装 Xcode 26.6 或更新版本（App Store 安装），首次打开时按提示装好 iOS 平台组件。
- 一台 iPhone，iOS 17 或更新版本，和一根能传数据的数据线。
- 一个 Apple ID。免费账号就能装到自己手机上，但签名约 7 天过期，过期后重新 Run 一次即可。
- Mac 上安装 [uv](https://docs.astral.sh/uv/)（Python 环境管理），用来运行接收服务。
- 第一次联调时，手机和 Mac 连同一个 Wi-Fi。

### 1. 取代码

```sh
git clone https://github.com/Echo-As-Record/Echo-As-Record.git
cd Echo-As-Record
uv sync
```

手机代码合入 `dev` 之前，先切到对应分支：`git switch phone/ios`。之后的命令都在仓库根目录执行。

### 2. 在 Mac 上启动接收服务

先生成一个随机口令。手机上传时要用它，自己记好，不要提交到仓库：

```sh
export EAR_INGEST_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "$EAR_INGEST_TOKEN"
uv run ear-ingest serve --host 0.0.0.0 --port 8791
```

这个终端窗口保持开着。录音默认存到 `~/.local/share/ear/inbox/brain`，要换目录就在启动前设置 `EAR_INGEST_INBOX_ROOT`。再查一下 Mac 在局域网里的名字：

```sh
scutil --get LocalHostName
```

假设输出是 `my-mac`，手机要填的地址就是 `http://my-mac.local:8791`。可以先用手机 Safari 打开 `http://my-mac.local:8791/health`，看到 `{"ok":true}` 说明网络是通的。

### 3. 用 Xcode 装到手机

1. 打开 Xcode → **Settings → Accounts**，点左下角 **+**，登录你的 Apple ID。
2. 在 Finder 里双击打开 `clients/ios/EarRecorder/EarRecorder.xcodeproj`。
3. 按 `⌘1` 打开左侧导航，点最上面蓝色的 **EarRecorder** 工程 → 中间 **TARGETS** 选 **EarRecorder** → 顶部 **Signing & Capabilities**：
   - 勾选 **Automatically manage signing**；
   - **Team** 选你的账号，个人账号显示为“你的名字 (Personal Team)”；
   - 如果提示 Bundle Identifier 已被占用，把 `com.prts.ear.recorder` 改成你自己的，例如 `com.<你的名字>.ear.recorder`。这个改动只留在本地，不要提交；
   - 要跑测试的话，对 **EarRecorderTests** 也做同样设置。
4. 用数据线连上 iPhone，手机上点“信任此电脑”。
5. 打开手机的开发者模式：**设置 → 隐私与安全性 → 开发者模式**，打开后按提示重启，解锁后再确认一次。菜单里没有这一项时，先做下一步，让 Xcode 连一次手机，它就会出现。
6. 在 Xcode 顶部工具栏，点 `EarRecorder` 右边的设备名，选你的 iPhone，然后点左上角 ▶︎（Run）。
   - 弹出“codesign 想要访问钥匙串”时，输入的是 **Mac 的登录密码**，点“始终允许”。
   - 第一次装好后，手机上可能提示“不受信任的开发者”：到 **设置 → 通用 → VPN 与设备管理**，信任你的开发者证书，再回 Xcode 点一次 Run。

### 4. 第一次打开 App

1. 点 **“申请麦克风、定位 Always、运动、通知权限”**，逐个允许。定位要选 **“始终”** 并打开精确位置；如果只出现“使用 App 期间”，先允许，再到系统设置里改成“始终”（页面上有“打开系统设置”按钮）。
2. 弹出“查找并连接本地网络上的设备”时选 **允许**，否则连不上 Mac。
3. 在 **设置** 区填写：
   - 服务地址：第 2 步的 `http://my-mac.local:8791`，不要加 `/api/v1/record`；
   - Bearer token：第 2 步生成的口令；
   - 用户 ID 可以空着。

   点 **“保存设置”**，看到“已保存”即可。
4. 点 **“测试已保存的直连和备用地址”**，确认直连可用。

想换成扫码配对（走 HTTPS，换网络后也能找到这台 Mac），见下面的[扫码配对接收端](#扫码配对接收端)。

### 5. 确认 Mac 收到录音

为了不用等 30 分钟，先把分段时长（默认 `1800000` 毫秒）改成 `60000`（1 分钟）并保存，再点 **“开始录音”**。分段时长要到下一次切段才生效，所以第一段仍是 30 分钟：可以停止录音再重新开始。等一两分钟后在 Mac 上查看：

```sh
find ~/.local/share/ear/inbox/brain -type f -name '*.m4a'
```

能看到 `.m4a` 和对应的 `richVoice` / `phoneContext` 记录就说明跑通了。测试完把分段时长改回 `1800000`。

### 6. 常见问题

| 现象 | 处理 |
|---|---|
| Safari 打开 `/health` 失败 | 确认手机和 Mac 在同一 Wi-Fi、接收服务还在运行。校园网等会隔离设备的网络不允许设备互访，换个人热点再试 |
| App 报 “App Transport Security” 错误 | 地址必须是 `http://<名字>.local:端口` 或局域网 IP；公网地址要用 HTTPS |
| 一直显示待上传 | 检查“本地网络”权限（设置 → 隐私与安全性 → 本地网络），以及 token 是否和 Mac 上的一致 |
| 几天后 App 打不开 | 免费账号签名过期，连上数据线在 Xcode 里重新 Run |
| 锁屏后好像停了 | 定位必须是“始终”；强制划掉 App 后要手动重新打开。详见[真机验收边界](#真机验收边界) |

## 打开、签名、安装

1. 用 Xcode 26.6 打开 `EarRecorder/EarRecorder.xcodeproj`，选择 `EarRecorder` scheme。
2. Xcode Settings → Accounts 登录自己的 Apple ID。在 app 和 test target 的 Signing & Capabilities 中选择自己的 Team；仓库中的 `DEVELOPMENT_TEAM` 刻意留空。Bundle ID 为 `com.prts.ear.recorder`，个人签名如遇标识占用需改为自己的唯一标识。
3. USB 连接 iPhone，信任电脑，在手机设置中启用开发者模式。选择真机并 Run；若系统要求，进入设备管理信任开发者。免费个人签名的有效期和能力以 Xcode 实际提示为准。
4. 首次启动后点击权限按钮，允许麦克风、通知、运动与健身。定位先允许使用期间，再允许 Always（始终）；若系统没有再次弹窗，进入系统设置手动选“始终”，并允许精确位置。允许局域网访问。首次安装/重启手机后至少解锁并打开 app 一次。

## 使用与设置

填写服务根地址（例如 `http://my-mac.local:8791`）、Bearer token 和可选用户 ID，点“保存设置”。按钮旁显示“已保存”表示设置已写入本机，继续编辑时提示消失。上传结果另看传输状态。app 自动追加 `/api/v1/record`，不要在服务地址里再填该路径。服务的访问口令需与接收端一致；同一局域网可在 Mac 运行 `scutil --get LocalHostName`，使用该名称加 `.local`；也可使用可达的 HTTPS 地址。默认没有公网 HTTP 的 ATS 例外。公网优先使用 HTTPS；自建加密 HTTP 中继需要开发者在本地 Info.plist 为自己的域名增加 ATS 例外并重新编译。每段封口即可上传，保持录音时也会派发。离线时保留数据，联网后重试；最多两个任务同时在途，仅当已发送部分请求、仍有数据待发且连续 5 分钟无进展时，才主动取消重试。等待连接或已发完等待响应交给系统处理。临时网络错误的重试退避最长 60 秒，实际派发取决于系统调度。

可选的「备用 HTTPS 地址」填写指向同一 Mac 接收端的隧道根地址。配置后，待传文件先探测直连，再探测备用；每次探测都用随机 challenge 和原 token 验证接收端，探测请求不携带 token。选定路径可复用 60 秒。两条都失败时文件保留，等待下一次调度。已经派发的系统上传可能先等到网络失败或资源超时，之后再选择路径。

用蓝牙耳机听音乐时，可关闭「蓝牙麦克风输入」。开关自动保存，录音中切换会封存当前段并立即重新启动采集，优先使用手机麦克风，并允许蓝牙立体声播放（A2DP）。开启此选项时，支持通话的耳机可能使用 HFP，播放音质随之改变。停止录音会释放音频会话，常规切段无需重新激活。

默认分段 `1800000` 毫秒，GPS 距离过滤 `25` 米，蓝牙输入开启。分段时长在下一次分段后使用，录音页显示系统报告的实际输入与输出端口类型，GPS 设置在下次开始会话时使用。上传设置保存后供下一上传任务使用，已交给系统的任务保留原请求。

点“开始录音”，页面显示状态、段 index、会话经过秒数、当前进程本次会话 GPS 点数、待上传文件数和最后错误。界面仅在前台每秒刷新；后台不推送 UI，待上传数由状态变化更新。本地段号/文件名从 1 开始，上传和 Context 内的 segmentIndex 从 0 开始，与 Octopus 一致。停止会先持久记录 `userStopped=true`，之后不会因位置唤醒自动续录。待上传数按音频和 Context 分开计数，含活动段和需要人工恢复的文件；暂停期间会话经过时间仍增加，文件时长以实际 PCM 帧数为准。

默认成功上传的 m4a 在上传成功 3 天后清理；设置 `-1` 永久保留，`0` 成功上传后清理。清理在 app 有网络且没有在途上传时使用独立索引每小时检查，可能晚于期限；已完成会话不再参与上传扫描。Context、manifest 和无法恢复的原文件不自动删除。删除 app 会删除其本地未上传数据。

“快捷指令”中可添加“开始 Ear 录音”和“停止 Ear 录音”，用于个人自动化。Intent 会打开 app，录音是否成功以页面和诊断日志为准。

位置更新的 delta 保留 changed，并在同一行的 context 内补齐当前经纬度、采样时间和可用精度，供旧 Ear 的位置观测解析器读取。

「录音文件问题」显示最近有文件错误的会话时间、段号和原因；停止或恢复时若 PCM 为零字节且 AAC 无法解码，保留该段文件并标记没有可恢复的录音。

## 数据与恢复

手机 app 容器内：

```text
Library/Application Support/EarRecorder/
  diag.jsonl
  diag.previous.jsonl
  sessions/<groupId>/manifest.json
  sessions/<groupId>/segment_0001.m4a
  sessions/<groupId>/context_0001.jsonl.gz
  uploads/<uuid>.multipart
```

活动音频为 `segment_0001.m4a.part`，同时保留 `segment_0001.pcm.part`（16 kHz、mono、PCM16 little endian）。活动 Context 为 `context_0001.jsonl.gz.part`。封口和恢复均先校验，再标为可上传。都无法恢复的段标记 `manual_recovery_required`，保留原文件；不要当作成功上传。manifest 用原子替换写入。

启动先扫描恢复，再根据最新会话的 `userStopped` 决定是否新建会话；新会话记录 `auto_continuation`。录音被电话中断时封口当前段，CallKit 显示仍有电话进行时等待，最后一通结束时申请最多 12 秒的后台执行时间，等待约 1 秒让系统释放音频路由，再尝试开启下一段；实际执行时间由 iOS 决定。已停止录音的会话保持停止。系统允许 App 执行时，位置和音频路由回调也可触发到期重试。激活失败后按 1、2、5、10、30、60 秒持续重试，随后每 60 秒重试，回前台立即再试。诊断日志记录恢复、位置唤醒、上传结果和每 5 分钟的可用内存/进程占用；超过 5 MiB 后轮转，保留上一份。

在 Xcode Devices and Simulators 中下载 app 容器，可取回 manifest、录音和日志。日志不记录 token、完整服务 URL 或音频正文。

## 真机验收边界

iOS 决定后台调度和位置唤醒，DispatchSourceTimer 只在进程获得执行时间时运行；这不是“永远不会停”的保证。用户强制划掉 app 后通常需要手动重新打开。静止、权限不足、重启后未解锁、系统不允许后台音频激活时，位置触发与重试不保证及时发生。连续定位、显著位置变化、150 米离开围栏和到访监测均已接入；围栏仅在没有区域或离原中心超过 100 米时重建。路由变化本身不切段，引擎实际停止才恢复。运动只在类型变化或首次 low 升到 medium/high 时记录，避免置信度抖动刷行；实际触发和耗电必须真机测量。

至少做一次 24 小时测试：锁屏、来电、其他 app 播放、耳机插拔/蓝牙切换、断网再联网、服务重启、进程终止后重新启动、静止/步行/骑车、后台定位唤醒。比对 PCM/音频时长、Context 点密度、段边界、通知次数、录音空档、内存曲线、每小时电量和温度；确认接收服务与旧 Ear 能读出 Evidence。Xcode 停止调试与系统 jetsam 不能视为同一唤醒场景。

## 构建与测试

在仓库根执行，不需要改全局 xcode-select：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project clients/ios/EarRecorder/EarRecorder.xcodeproj -scheme EarRecorder -destination 'generic/platform=iOS Simulator' build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project clients/ios/EarRecorder/EarRecorder.xcodeproj -scheme EarRecorder -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project clients/ios/EarRecorder/EarRecorder.xcodeproj -scheme EarRecorder -destination 'generic/platform=iOS Simulator' build-for-testing
```

有可用 iOS Simulator runtime 后，在 Xcode 创建 iPhone 15 模拟器并执行 `xcodebuild test`，使用同一 project/scheme，destination 改为 `platform=iOS Simulator,id=<模拟器 UUID>`。沙箱中可增加 `-derivedDataPath /tmp/EarRecorderBuild`。实现、完整文件清单、验证记录和差异见[实现说明](ai-docs/implementation.md)。

网络异常时可点「测试已保存的直连和备用地址」，同时检查两个已保存的入口。测试仅请求健康接口并验证接收端身份，结果写入本机诊断日志。对照 Shadowrocket 时保持 Wi-Fi 和地址相同，只改变一个开关或节点，记下时间，再用蜂窝重复。具体命中规则以 Shadowrocket 自己的连接记录为准。

### 扫码配对接收端

在 Mac 上运行[接收端配对命令](../../services/ingest/README.md#设备配对与公网入口)，使用 iPhone 系统相机扫码，核对接收端 UUID 后点「配对」，并允许访问本地网络。已配对时设置页显示 Mac 的 UUID。手机解析该 UUID 的 Bonjour 服务，直连不可用时尝试二维码中的中继；HTTPS 路径校验同一台 Mac 的证书。显式配对的 HTTP 备用入口使用二维码中的配对秘密加密请求和回执。之前手填地址的用户可继续沿用原设置。重新配对会把待传和今后录音的目的地切换到确认的 Mac。
