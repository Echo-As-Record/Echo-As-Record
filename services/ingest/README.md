---
audience: human
doc-type: reference
---

# Ingest：手机录音接收服务

接收手机 multipart 上传。默认写入旧 Ear 已有的 Brain inbox 布局，可按单条事件通知旧 Ear 转写和处理上下文；另可显式切换到②的原生录音接收队列。本服务不写①，不修改旧 Ear，不承担音频处理或 Octopus 服务发现。

本次实现依据负责人直接授权的接收服务任务；[抽取提案](../../docs/proposals/phone-recorder-extraction.md)已采纳。这里的旧格式、覆盖重传和本机时间目录属于外部 inbox 兼容边界，不改变①的只追加及 UTC 数据契约。

跨平台实现请先读[手机客户端协议与固定测试向量](../../docs/modules/phone-client-protocol.md)；其中单列当前 iOS 与接收端的不一致和待定项。

## 启动

在仓库根执行；环境变量由部署环境注入，凭据保存在被忽略的 `deploy/.env`，服务不会自动读取 dotenv：

```sh
uv sync
uv run ear-ingest serve --host 0.0.0.0 --port 8791
```

| 环境变量 | 含义 |
|---|---|
| `EAR_INGEST_TOKEN` | 必需的共享 bearer token；未配置或全空白时启动失败 |
| `EAR_INGEST_INBOX_ROOT` | 仅旧模式使用，默认 `~/.local/share/ear/inbox/brain`；旧部署可显式设为 `~/.octopus/local/inbox/brain`；测试务必使用临时目录 |
| `EAR_INGEST_EVENT_URL` | 推荐配置。旧 Ear 可直连的单条事件完整 URL，例如 `http://127.0.0.1:8794/internal/api/ingest/brain-event` |
| `EAR_INGEST_NOTIFY_URL` | 兼容旧部署，可选的 `/api/ingest/reconcile` 完整 URL。与 EVENT_URL 互斥 |
| `EAR_INGEST_NATIVE_ROOT` | 显式启用原生模式，设为②原生队列的私有绝对路径。与两种旧通知 URL 均互斥；不读写旧 inbox |
| `EAR_INGEST_NATIVE_MAX_BYTES` | 原生上传的请求正文上限（含 multipart 开销），默认 134217728 字节，允许 1–536870912；②仍可按媒体类型施加更小的附件上限 |

`GET /health` 无需鉴权，返回 `{"ok": true}`；不检测旧 Ear、磁盘空间或通知连接。CLI 默认绑定 `127.0.0.1:8791`，可用 `--host`、`--port` 修改。文件锁采用 POSIX `flock`，支持本机 macOS/Linux；部署文件系统须支持该锁与原子 rename。

## 原生自动接入（可选）

默认仍为旧模式。为同一接收地址处理上传的所有进程（普通、配对、加密中继）配置同一个 `EAR_INGEST_NATIVE_ROOT`，同时去掉两种旧通知 URL；混用的单个进程会拒绝启动。路径及其父级需符合②原生队列的安全要求，现有父目录不可经软链接，队列为服务所有用户的私有目录。配置旧 `EAR_INGEST_INBOX_ROOT` 不会让原生模式回退或双写。不用旧 Ear、旧 inbox 或旧交付数据库，也不扫描或迁移历史。

- 地址、两条上传路由、Bearer 鉴权、配对、`/health` HMAC 和加密中继协议不变；HTTPS 回退仍需把同一请求转给该接收端。原生上传先鉴权，再校验声明长度和流式实收长度、完整 multipart 结束边界与非空附件。中继在解密过程中限制录音正文和帧开销，验证结束帧后才转入接收端。外层超限返回 413，无成功回执；这些限额不应用到旧模式、配对和实时转写。
- `timestamp` 必须是 `YYYY-MM-DDTHH:MM:SS[.小数]Z` 或带明确 `±HH:MM` 的会话起点。先规整到 UTC，再加相对毫秒；同一设备（统一小写）、组、段号和类型构成稳定键。不把客户端的 `source`、`X-User-Id`、文件名、请求路由或中继 ticket 当作队列身份。
- `richVoice` 必须是②支持的音频，原生队列只对音频安排识别。`phoneContext` 独立归档，不等音频，不表示已提取 GPS 或手机状态。接收端将 `phoneIMU`、`phoneNotes` 分别交给②的 `imu`、`notes` 归档类型；**所用 `RecordingIntake` 必须支持相应类型才会成功**。不支持的类型和坏文件返回非 2xx，不回退到旧 Ear，也不伪装成音频或 Context。
- 仅在共享 intake 确认持久化后返回 `{"ok":true,"record_id":"…","path":"…"}`。原生 `record_id` 是 intake ID；`path` 仍为绝对诊断路径，但指向真实队列目录，不是 Brain JSON。重试相同内容可得到原回执；共享 intake 判定冲突返回 409，不覆盖已受理数据。成功只确认队列或归档，不代表识别成功、数据已写入①或下游可用。

这一步是**同主机、同一受信任本地持久卷**的自动交接：ingest 生产，②消费，二者须由部署者明确配成看到同一队列。分主机不假设有挂载，生产使用前需要经过鉴权的加密 store-and-forward、可靠重试和持久确认；现有手机中继不会替代该服务间交接。

### Octopus 手机上传和独立落地

- 原 Octopus iOS `phone_local` 的 multipart 字段与旧 Brain 路由 `/api/v1/instances/brain___<device>/service/api/v1/record` 兼容；仅当请求已实际送到本接收端、Bearer 由本接收端认可、`timestamp` 遵循以上会话起点约定时，HTTP 原生路径才适用。没有接管 Octopus Manager、PC 发现或登录。查阅的 Octopus iOS `6786977` 源码在部分分段上发送段起始的绝对时间而非会话起点，并可能回退到会话起点；路由和字段无法可靠区分两种来源。未修改该客户端，也不宣称它直接改指后各段时间都正确；需要发送方对齐，或使用下述已知真值的 finalized 事件。
- 对仍落在其它 Octopus 接收端的文件，`ear-ingest octopus-finalized` 仅提供一个**本机可信生产者**的单次入口。上游必须在真正封口后主动调用并提供最终字节、原始段时间和事件时间；不要把此命令直接暴露给网络或不受信任的调用者。它不接受文件路径，不搜索旧 inbox、目录或历史清单。
- 额外显式配置 `EAR_INGEST_OCTOPUS_DEVICE_ID`（唯一允许设备，大小写无关）和 `EAR_INGEST_OCTOPUS_FINALIZED_AFTER`（固定启用时间，带时区）。输入为一行 UTF-8 JSON（含 LF 最多 8192 字节），后接该条附件的原始字节并立即 EOF。对象字段必须恰好如下，字段顺序任意：

  ```text
  event="record.finalized.v1", device, group_id, segment_index, type, media_type,
  started_at, ended_at, finalized_at, size, sha256
  ```

  起止时间为**绝对时间**；无结束时间只允许第 0 段会话 Notes（`ended_at: null`）。结束（无结束用起点）与可信封口时间均不能早于启用时间；封口时间不能超前或超过 24 小时。`size` 为正整数字节数且受原生上限约束，`sha256` 为 64 位小写十六进制。独立校验长度和摘要后才入队；退出 0 且输出 JSON 代表已持久确认，非零不确认。生产者负责保留附件、在允许窗口内重试以及观察非零退出。与 HTTP 路径使用同样的身份，不应同时配置两路投递，尤其是两路时间基准未对齐时。

- `ear-ingest octopus-bridge` 可由操作者单次运行，从 Octopus 拥有的专用原始封口导出读取有界批次，校验后通过同一 finalized 入口入队；也可以只输出一条已校验的 wire frame，交给异机接收端。不安装 Octopus 钩子，不读取旧 Ear，不调度或补扫历史。前置条件、导出格式和重试见 [Octopus 封口导出接入](octopus-bridge.md)。若只有旧 Ear 拥有的挂载，或上游无法提供确切的物理段时间及封口事件，则不能激活。

## macOS 登录后常驻

本机使用用户 LaunchAgent `~/Library/LaunchAgents/com.prts.ear-ingest.plist`，标签为 `com.prts.ear-ingest`。登录后 launchd 自动启动服务，退出后重启。它从仓库的 `deploy/.env` 单独读取一行无引号的 `EAR_INGEST_TOKEN=值`，再从仓库根运行 `uv run --locked --no-sync ear-ingest serve --host 0.0.0.0 --port 8791`。需要已同步的工作环境。重启 Mac 后须先登录，睡眠期间无法接收。

```sh
launchctl print "gui/$(id -u)/com.prts.ear-ingest"
curl --noproxy '*' --fail http://127.0.0.1:8791/health
# 修改口令后重启
launchctl kickstart -k "gui/$(id -u)/com.prts.ear-ingest"
```

手机与 Mac 网络互通时，在手机填 `http://<LocalHostName>.local:8791` 并保存口令。Mac 上用 `scutil --get LocalHostName` 查看名称。Mac 上的健康检查只覆盖本机路径。可在手机 Safari 打开相同地址的 `/health` 验证网络，再以真实 inbox 中新增的音频和 Context 两条记录确认上传。

### 插线测试公网回退

在 Mac 保持 USB 调试连接，手机关闭 Wi-Fi 并开启蜂窝数据。先确认备用 HTTPS 入口能访问本机接收端。以下命令使本机接收端在指定时间内仅监听回环地址，USB 和 Wi-Fi 客户端都无法直连 8791；本机隧道仍可转发到接收端。命令会短暂重启接收进程，请在测试分段封口前运行。

```sh
python3 services/ingest/scripts/isolate_direct.py --seconds 600
```

脚本适用于上面记录的 macOS 用户 LaunchAgent，使用临时配置，计时结束或按 Ctrl-C 后重新加载原配置并验证监听。隔离期间检查 Mac 的局域网 IP 请求失败、备用入口健康，然后用手机的 `upload_probe`、`upload_dispatch` 和真实 inbox 验收。该方法隔离的是接收端直连路径；单凭这三个结果无法识别手机公网请求使用的无线接口。

## 上传协议

以下详细的旧格式、覆盖重传、无总量限制和文件路径指默认旧模式；原生模式的差异见上文。

- `POST /api/v1/record`；兼容 `POST /api/v1/instances/{instance}/service/api/v1/record`，其中 instance 必须与 `brain___<X-Device-Id>` 大小写无关地一致。
- `Authorization: Bearer <token>` 必需，常量时间比较，失败 401；在解析上传体前鉴权。
- `X-Device-Id` 必需，满足 `^[A-Za-z0-9._-]{1,128}$` 且不能为 `.` 或 `..`，落盘统一小写；缺失或非法返回 400。
- `X-User-Id` 可选，以 `user_id` 写入 record；它只是元数据，不是另一套用户授权。
- multipart 字段：`name`、`text`（可为空）、`type`（`richVoice` / `phoneContext` / `phoneIMU` / `phoneNotes`）、`source`、`group_id`、`segment_meta`、`segment_start_ms`、`segment_end_ms`、`timestamp`、一个 `file`；`context` 可选。
- `segment_meta` 是 JSON 对象字符串，`segmentIndex` 必须为非负整数；若包含 `segmentStartMs` / `segmentEndMs`，须与顶层毫秒字段一致。`source` 等额外 meta 内容原样保留。
- 时间偏移为非负整数毫秒，end 必须大于 start；会话级 `phoneNotes` 可按原 Octopus 格式使用 segmentIndex/start/end 全 0；`timestamp` 为会话开始 ISO8601。推荐明确时区；无时区按服务本机时区解释，与 brain 一致。
- `group_id` 使用与 device 相同的安全字符及长度限制，保留大小写。路径型附件名、重复/未知字段和不合法数据返回 400。

成功返回 `{"ok": true, "record_id": "…", "path": "绝对 record JSON 路径"}`。请求体由 Starlette multipart 解析器流式接收，较大文件暂存磁盘，再按 1 MiB 块复制到 inbox 临时文件；不会整读上传到内存。文本字段限制为每字段 1 MiB，不设置录音总大小上限；部署需给临时目录与 inbox 预留空间。

## 文件与重传

```text
<inbox>/<device>/<会话开始的本机日期>/phone/<HH-MM-SS>__<group_id>/
  segment_1_richVoice_from_20-51-29_to_21-21-29.json
  segment_1_from_20-51-29_to_21-21-29__01__segment_0002.m4a
```

record 保留上传字段，增加 `id` 和 `files` 清单，JSON 使用 UTF-8、两空格缩进、不转义中文、无结尾换行。目录和文件名按会话起始时间加相对偏移转换为本机时间；record 的 timestamp 保留输入时区并规范化为 ISO8601。

同一 `(device, group_id, type, segmentIndex)` 保留首次 record id，重传替换文件和 record；附件名、偏移甚至会话 timestamp 改变时，也删除旧版本文件。按 device 用隐藏 `.ingest.lock` 串行，锁文件保留，不影响旧 Ear 的 glob。附件先原子替换，record 后原子替换，再清理旧文件；这保证每个文件不会暴露半写内容，**不是多个文件的事务**。定向投递模式会记录尚未完成的替换计划，重启时先修复；单纯落盘模式依靠同条重传清理中断的替换。无 fsync 断电持久性保证。inbox 应由服务管理，禁止不受信任进程并发更换目录/软链接。

## 旧 Ear 通知与兼容差异

配置 `EAR_INGEST_EVENT_URL` 后，接收端独立保存每个附件，再异步 POST 一条 `brain.record.received` 到指定的旧 Ear。两端须看到相同绝对路径的 inbox，通常都在 Mac 上。上传响应只确认文件和投递状态已保存。旧 Ear 暂停、离线或重启期间，手机仍可持续上传。接收端重启后继续待办，不扫描其他来源的记录，也不追发启用前就存在的文件。

| 类型 | 定向投递行为 |
|---|---|
| `richVoice` | 单独投递，受理后保存 capture ID 和可选 pipeline run ID。音频可先于任何附件受理 |
| `phoneContext` | 单独投递。若对应音频尚未受理，保留待办。相同 device、group、segmentIndex 的音频受理后立即唤醒重试，旧 Ear 负责验证实际匹配 |
| `phoneIMU` | 独立保存并标记 archived。旧 Ear 的音频扫描能识别部分同段 IMU sidecar，当前没有确认晚到 IMU 的单条补挂接口 |
| `phoneNotes` | 独立保存并标记 archived。旧 Ear 没有已确认的消费接口 |

状态保存在 `<inbox>/.ear-ingest/delivery.sqlite3`。查看队列和已受理事件可执行以下只读查询。每个消费者保存自己的 seq 游标；receipt 表示旧 Ear 受理，不表示 ASR 或适配器已完成。

```sql
SELECT kind, state, reason, count(*) FROM jobs GROUP BY kind, state, reason;
SELECT seq, instance, record_id, kind, capture_id, pipeline_run_id, accepted_at
FROM receipts WHERE seq > 0 ORDER BY seq;
```

网络错误、无效响应及旧 Ear 的 `retry` 会退避 1 秒至 5 分钟，再次投递同一条。旧 Ear 明确返回 `rejected` 才终止该版本。相同内容的重传保留受理状态，修改后的 record 或附件会重新投递。远端接受与本地写回之间发生崩溃可导致重复调用，依赖旧 Ear 的 capture / Context 幂等规则。上传 token 不发给旧 Ear。切换独立部署时要同时选用该部署的 inbox 和队列。

历史部署可单独配置 `EAR_INGEST_NOTIFY_URL`。它在上传后后台 POST 一次全 inbox reconcile（5 秒超时），失败只记录日志。此旧接口可绕过自动摄取暂停开关；定向事件遵守暂停状态。两个变量均未设置时只写 inbox。当前旧 Ear 已取消周期补扫。

与 brain channel 的已知差异：

- 只实现上述手机 multipart 接口；不提供列表、附件下载、远端同步和 Octopus 事件。
- 不写 `attachments` 中指向 Octopus 下载接口的 URL，避免记录无效地址；旧 Ear 直接读取 `files[].stored_name`，`attachments` 默认空列表可解析。
- 首次 id 同样为 UUID4 hex；重传保留 id，且按四元组清理旧路径，比 brain 按文件名覆盖更严格。
- 对路径、meta、时间和重复字段严格拒绝；brain 对部分非法输入会宽容转换或回退。
- 四种类型都走 phone 布局。当前 brain registry 对 `source=local` 的前三种走 phone，而未收录 `phoneNotes`；这里是任务明确要求的扩展。旧 Ear 尚无已确认的 Notes 消费链，保存成功不代表 Notes 已被处理。
- 同名附件若已归另一条 record，返回 400，避免不同类型覆盖彼此的内容。

实现依据、边界及验证命令见[实现说明](ai-docs/implementation.md)。

## 临时 inbox 的 curl 验收

在允许本地监听端口的终端执行（以下 token 仅用于合成测试）：

```sh
INGEST_SMOKE_DIR=$(mktemp -d)
export EAR_INGEST_TOKEN=synthetic-smoke-token
export EAR_INGEST_INBOX_ROOT="$INGEST_SMOKE_DIR/inbox"
printf 'synthetic-audio' > "$INGEST_SMOKE_DIR/segment_0002.m4a"
uv run ear-ingest serve --host 127.0.0.1 --port 8791 &
INGEST_SMOKE_PID=$!
trap 'kill "$INGEST_SMOKE_PID" 2>/dev/null || true' EXIT
# 等待 /health 成功后，再上传；失败则停止，不向其他服务提交。
INGEST_SMOKE_READY=0
for attempt in 1 2 3 4 5; do
  if curl --fail --silent http://127.0.0.1:8791/health; then
    INGEST_SMOKE_READY=1
    break
  fi
  sleep 1
done
if [ "$INGEST_SMOKE_READY" = 1 ] && kill -0 "$INGEST_SMOKE_PID" 2>/dev/null; then
  curl --fail-with-body http://127.0.0.1:8791/api/v1/record \
    -H "Authorization: Bearer $EAR_INGEST_TOKEN" \
    -H 'X-Device-Id: IOS-SMOKE' \
    -F 'name=PhoneVoice' -F 'text=' -F 'type=richVoice' -F 'source=local' \
    -F 'group_id=synthetic-session' \
    -F 'timestamp=2026-11-12T11:21:29.157Z' \
    -F 'segment_meta={"segmentIndex":1,"segmentStartMs":1800000,"segmentEndMs":3600000,"source":"phone_local_auto"}' \
    -F 'segment_start_ms=1800000' -F 'segment_end_ms=3600000' \
    -F "file=@$INGEST_SMOKE_DIR/segment_0002.m4a;type=audio/mp4"
  find "$EAR_INGEST_INBOX_ROOT" -type f
fi
kill "$INGEST_SMOKE_PID" 2>/dev/null || true
wait "$INGEST_SMOKE_PID" 2>/dev/null || true
trap - EXIT
```

此字节串只验证传输与落盘，不是可解码的录音。


### 手机的备用 HTTPS 入口

直连和备用入口须转发到同一接收端，保留请求路径、Authorization 和 multipart 正文。普通 `GET /health` 仍返回 `{"ok":true}`。手机探测时调用 `GET /health?challenge=<32位小写十六进制随机数>`；返回的 `proof` 是以接收 token 为密钥，对 UTF-8 字符串 `ear-health-v1:<challenge>` 计算的 HMAC-SHA256 小写十六进制值。使用随机高熵 token。更新接收服务后重启进程使新接口生效。接收服务和 Mac 都需要在线，手机离线时保留文件。临时隧道适合联调，换域名后需在手机重新保存备用地址。

### 实时转写

将 `EAR_TRANSCRIPTION_URL` 设为 `http://127.0.0.1:8793` 并重启 ingest，可在原接收地址访问已鉴权的 `/api/v1/transcription/` 接口。该变量仅接受带端口的 HTTP 回环 IP。Mac 模型安装、PCM 协议和读回示例见后续的 `services/transcription` 服务。

固定公网备用入口可使用 Mac 已登录的 Tailscale Funnel。首次启用时由 tailnet 管理员确认 CLI 提供的授权页。在 Mac 上运行：

```sh
tailscale funnel status
tailscale funnel --bg --https=443 http://127.0.0.1:8791
tailscale funnel status
```

先确认 443 根路径可供本接收服务使用。将输出中的完整 HTTPS 根地址保存到手机的「备用 HTTPS 地址」，保留原直连地址与 token。Funnel 由 Mac 端提供，手机无需连接 Tailscale。保留此 Mac 的 Tailscale 设备名与 tailnet DNS 名。公网验收应从 tailnet 之外请求带随机 challenge 的健康接口并校验 proof，再在直连隔离时核实手机实际上传的附件。若 Mac 睡眠，手机继续本地排队。

### 设备配对与公网入口

Mac 上保留兼容端口 8791，另一个终端用相同的接收目录启动配对服务。在同一个 macOS 用户下生成邀请。如果配置了异步投递或转写，两个服务使用相同的环境配置。

```sh
uv run ear-ingest serve --paired --host 0.0.0.0 --port 8792
# 在另一个终端运行，iPhone 系统相机扫描终端中的二维码
uv run ear-ingest pair
```

Mac 用 Bonjour 发布自己的接收端 UUID。邀请五分钟有效，首台兑换的手机可在有效期内重试。身份、设备凭据和 TLS 私钥位于 `~/Library/Application Support/EarIngest/pairing`（可用 `EAR_PAIRING_HOME` 指定）。删除该目录后需要重新配对。手机用二维码内的指纹验证 Mac，凭据保存在手机钥匙串中。

跨网部署时，由运营方配置泛域名（例如 `*.ear.example.org`）指向公网中继，并提供按 TLS SNI 转发的入口。例如在 FRP 的 HTTPS 虚拟主机端口启用 443，Mac 的 frpc 使用 `type = "https"`、`localIP = "127.0.0.1"`、`localPort = 8792`，将 `customDomains` 设为这一台 Mac 对应的完整主机名。中继须将原始 TLS 字节转发到 Mac，手机会校验 Mac 的证书。运营方须限制每台 Mac 能注册的主机名。域名、DNS 和中继需单独部署。

```sh
uv run ear-ingest pair --relay 'https://{receiver}.ear.example.org'
```

命令将占位符替换为本机 UUID。没有配置公网入口时，二维码仅提供局域网配对。Mac 睡眠期间上传依靠手机队列等待恢复。

本机使用 Octopus 的 HTTP 入口。frpc 连接 `relay.example.org:7000`，以 `type = "http"` 注册 `subdomain = "ear-example"`，目标为 `127.0.0.1:18873`。在 Mac 上使用同一个配对目录、接收目录及接收服务环境变量运行：

```sh
uv run ear-ingest relay --port 18873
# 在另一个终端生成五分钟有效的二维码
uv run ear-ingest pair --relay http://ear.example.org:8080
```

FRP 只连接专用加密入口。手机与 Mac 以邀请中的随机秘密完成加密配对，再用每台手机的独立凭据派生请求、响应和回执密钥。每个请求使用随机标识，录音和 GPS 分块用 AES-256-GCM 加密；Mac 验证完整结束帧后提交给本地接口。手机校验与该请求绑定的回执后才将附件标为已上传。中继能看到设备标识、传输时间和大小。此部署使用 `com.prts.ear-ingest-relay` 和 `com.prts.ear-paired-relay` 两个用户 LaunchAgent。其他 Mac 使用各自分配的子域名。

本机用于手机的 8791、8792 和 FRP 后端 18873 共用 `~/Library/Application Support/EarRecognition/data/local/inbox/brain`，三个服务的 `EAR_INGEST_EVENT_URL` 均指向 `http://127.0.0.1:8794/internal/api/ingest/brain-event`。接收回执只确认附件与事件队列已持久化。完整转写需等识别与②入库，参见[适配器说明](../adapters/README.md)。

中继地址通过 `EAR_INGEST_RELAY_URL` 或 `pair --relay` 配置。HTTP 加密中继还须设置 `EAR_INGEST_HTTP_RELAY` 为完全相同的根地址；默认不允许 HTTP 中继。手机仅使用配对得到的地址。
