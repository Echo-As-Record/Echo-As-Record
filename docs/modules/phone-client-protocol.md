---
audience: human
doc-type: reference
based-on: clients/ios-recorder 278a205c2593ecae670c56bc64c4713e66a00bd8；services/ingest/src/ear_ingest/{app,record,store,pairing,relay,__main__}.py；clients/ios/EarRecorder/EarRecorder/{Audio,Context,Runtime,Store,Upload,Pairing,Relay}.swift；本次获授权的现状整理
---

# 手机客户端协议：当前实现参考

本文供 Android/Kotlin 及其他客户端独立实现兼容采集和上传。基线是上列提交的实际代码，不把现状提升为新的已采纳产品决策。“必须”指互操作或安全确认所需条件，“建议”指客户端策略；接收端未校验的约定会明确说明。全文示例与向量均为合成数据。

实时 PCM 预览不在本规范内（未测、未提交的客户端工作不作为依据），以后另补。

## 1. 录音、分段和标识

源码：[Audio.swift](../../clients/ios/EarRecorder/EarRecorder/Audio.swift)、[Runtime.swift](../../clients/ios/EarRecorder/EarRecorder/Runtime.swift)、[Store.swift](../../clients/ios/EarRecorder/EarRecorder/Store.swift)、[record.py](../../services/ingest/src/ear_ingest/record.py)。

| 项目 | 兼容客户端行为 |
|---|---|
| 音频 | 16,000 Hz，单声道，MPEG-4 AAC，目标码率 32,000 bit/s，M4A 容器；上传 MIME `audio/mp4`。编码输出不是逐字节确定的；接收端不解码验证 |
| PCM | iOS 采集转换为 PCM16 little endian、16 kHz、mono，作为本地恢复备份；不是上传附件 |
| 分段 | 默认 1,800,000 ms；iOS 按执行中的定时回调检查切段，设置最小值 1,000 ms。不是固定字节数，也不保证后台回调准时 |
| 段边界 | 切换写入文件、封口旧段；停止或中断提前封口，恢复后新开段。正常切段保持音频引擎。只有完整封口且可解码的音频才进入上传队列 |
| 段时间 | start 是会话开始后的实际墙钟经过毫秒；end = start + 实际 PCM 帧数对应的整数毫秒（PCM 字节数 / 32，截断）。恢复可能按解码帧数重算。暂停空档不能伪装成连续音频 |
| 段号 | 本地 index 从 1 递增；上传 `segmentIndex` 与 Context 行内 index 均为本地 index 减 1，从 0 开始 |
| 文件名 | `segment_0001.m4a`、`context_0001.jsonl.gz`；至少四位十进制补零，不限制最大四位。每段两次独立上传 |
| 会话 | 开始会话生成小写带连字符 UUIDv4 `group_id`，同会话各段/各附件/重试必须保持不变；进程重启自动续录在 iOS 创建新会话，不复用旧 group |

接收端仅要求 group 满足 `[A-Za-z0-9._-]{1,128}` 且非 `.`、`..`，保留大小写；没有强制 UUID 版本。手机边界的 UUIDv4 和本机时间 inbox 是现有兼容行为，不等于仓库核心 UUIDv7/UTC 契约。

设备 ID 与会话 ID 必须分开。iOS 首次生成 `ear-ios-<小写带连字符 UUIDv4>` 并持久保存，不能每段、每次重启或重试重建。接收端接受同一安全字符集合，转小写用于上传和直接配对；健康与中继查凭据不转小写，所以客户端必须始终发送同一小写值。Android 前缀尚无冻结约定，可选满足上述字符要求的稳定随机标识，不能假装必须使用 iOS 前缀。ID 不是 token；与凭据一起保留，重装或丢失后重新配对。

## 2. Context：gzip 中的 UTF-8 JSON Lines

源码：[Context.swift](../../clients/ios/EarRecorder/EarRecorder/Context.swift)、[Runtime.swift](../../clients/ios/EarRecorder/EarRecorder/Runtime.swift)、[Upload.swift](../../clients/ios/EarRecorder/EarRecorder/Upload.swift)。

一个 Context 文件对应同 group、同 `segmentIndex` 的音频。解 gzip 后每行一个 JSON 对象，以 LF（`0a`）结束；不写 JSON 数组。正常封口为完整 gzip 流。JSON 键顺序不是协议，iOS 常规行按键排序；gzip 压缩字节也不要求一致。接收端将其作为附件保存，不校验行 schema。

所有行都有 `type`（字符串）、`segmentIndex`（0 起整数）、`elapsedMs`（非负整数，会话开始后的毫秒，**不是段内偏移**）、`timestamp`（写行时刻 UTC ISO8601，iOS 秒精度、`Z` 结尾）。GPS 自身采样时间独立于写行时间。

| type | 附加字段与含义 |
|---|---|
| `baseline` | 每段首行；`context` 为当时完整快照，可以含 null 未知值 |
| `delta` | `changed` 为与上次快照不同的字段；没有改变则不写行。位置四字段任一改变且经纬度为数值时，另带 `context`，补齐当前 `latitude`、`longitude`、`locationTimestamp` 和可用 `locationAccuracy`，即使坐标未变也补齐 |
| `event` | `event` 字符串、`changed` 对象（可以 `{}`）、`meta: {"reason": "…"}`；不自动并入 delta 快照 |
| `segment_end` | 最后一行；`summary: {"deltaCount": 整数, "eventCount": 整数, "arrivalCount": 整数}`，统计本文件 delta/event 数及 reason 为 `visit_arrival` 的事件数 |

快照/changed 字段：

| 字段 | 类型、单位和当前取值 |
|---|---|
| `platform` / `device_model` / `os_version` | 字符串；iOS baseline 写 `ios`、系统型号、系统版本。新客户端填写自身信息，不能复制 iOS 型号 |
| `battery` | 整数百分比 0–100，未知 null；iOS 每约 15 秒获得执行机会时采样，仅变化落行 |
| `isCharging` | 布尔；iOS charging 或 full 都为 true，其余 false |
| `latitude` / `longitude` | WGS84 度数，未知 null；不能传 GCJ-02/BD-09 当作原坐标 |
| `locationAccuracy` | 水平精度，米，未知 null；连续定位只接受精度非负样本 |
| `locationTimestamp` | GPS 样本 UTC Unix epoch 整数毫秒，未知 null |
| `locationMeta` | 未知 null，或对象：`trigger`、`source`、`provider` 字符串，`isFromLastKnown`、`isMock` 布尔，`ageMs` 非负整数毫秒。当前连续定位为 `continuous`、`core_location_update`、`core_location`、false；ageMs 为写入时距样本时间，负值截成 0。Android 应如实标识来源，具体字符串枚举未冻结 |
| `motionActivity` | 取得后才出现，形如 `{"type":"walking","confidence":"high"}`。type 为 walking/running/cycling/automotive/stationary/unknown；confidence 为 low/medium/high |

运动只在类型改变，或上次已发 low 首次提升到 medium/high 时写 delta。默认 GPS 距离过滤 25 米是采样策略，不是服务端要求。首次 baseline 可以只有未知位置；不得捏造坐标补齐。

现有事件有 `location_arrived` / `visit_arrival`、`location_departed` / `visit_departure`（changed 含位置、精度、采样时间）、`lifecycle` / `region_exit` 或 `location_resumed`、`location_error` / 错误说明、`auto_continuation` / `process_relaunch`。恢复未完整结束的 Context 会补 `recovery` / `process_interrupted` 和 summary；不得把恢复当成原时刻采样。event/reason 并无服务端枚举校验。

合成行示例：

```json
{"type":"baseline","segmentIndex":0,"elapsedMs":0,"timestamp":"2026-01-02T03:04:05Z","context":{"platform":"android","device_model":"synthetic","os_version":"synthetic","battery":80,"isCharging":false,"latitude":null,"longitude":null,"locationAccuracy":null,"locationTimestamp":null,"locationMeta":null}}
{"type":"delta","segmentIndex":0,"elapsedMs":500,"timestamp":"2026-01-02T03:04:05Z","changed":{"latitude":0.0,"longitude":0.0,"locationAccuracy":10.0,"locationTimestamp":1767323045500},"context":{"latitude":0.0,"longitude":0.0,"locationAccuracy":10.0,"locationTimestamp":1767323045500}}
{"type":"segment_end","segmentIndex":0,"elapsedMs":1000,"timestamp":"2026-01-02T03:04:06Z","summary":{"deltaCount":1,"eventCount":0,"arrivalCount":0}}
```

## 3. 上传接口与重传

源码：[app.py](../../services/ingest/src/ear_ingest/app.py)、[record.py](../../services/ingest/src/ear_ingest/record.py)、[store.py](../../services/ingest/src/ear_ingest/store.py)、[Upload.swift](../../clients/ios/EarRecorder/EarRecorder/Upload.swift)。

`POST /api/v1/record`，HTTP body 为标准 multipart/form-data。每个 part 以 CRLF 分隔，文件为原始字节。`Content-Type` 必须带实际 boundary；边界不可出现在内容中。Content-Length 由 HTTP 客户端正确计算。兼容旧路径 `/api/v1/instances/{instance}/service/api/v1/record` 只在 instance 与 `brain___<device>` 大小写无关一致时可用；新客户端使用短路径。

| HTTP 头 | 要求 |
|---|---|
| `Authorization` | `Bearer <secret>`；secret 是手工配置共享口令，或配对签发的独立 token。服务在解析 multipart 前鉴权；Bearer 大小写不敏感，secret 精确比较 |
| `X-Device-Id` | 必需，见 §1；配对 token 必须匹配该设备 |
| `X-User-Id` | 可选；非空值原样保存为 `user_id`，只是元数据，不授予权限 |
| `Content-Type` | `multipart/form-data; boundary=<boundary>` |
| `X-Channel-Id` / `X-Instance-Id` | iOS 发 `brain` / device；当前接收端忽略，非必需 |

字段不能重复，不能加未知字段；只有 `file` 是文件，其余是文本 part。所有下列字段除 context 外都必需：

| 字段 | 值与校验 |
|---|---|
| `name` | 字符串，iOS 为 `PhoneVoice`（Context 也一样） |
| `text` | 字符串，可空，iOS `""` |
| `type` | `richVoice`（音频）、`phoneContext`（Context）、`phoneIMU`、`phoneNotes`；iOS 当前只生成前两种 |
| `source` | 非空字符串，iOS `local` |
| `group_id` | 会话标识，见 §1 |
| `segment_meta` | JSON 对象字符串；必须含 `segmentIndex` 整数，范围 0..2147483647，不能是布尔。iOS 另发 `segmentStartMs`、`segmentEndMs` 整数及 `source: "phone_local_auto"`。两个时间键若出现，必须等于对应顶层偏移；其他键保留 |
| `segment_start_ms` / `segment_end_ms` | 文本十进制非负整数，1–15 位，只接受数字字符；一般 end > start。唯一例外：phoneNotes 的 index/start/end 全 0，表示会话级 notes |
| `timestamp` | 会话起点，不是上传或段起点。客户端应传 UTC ISO8601 `Z`；iOS 秒精度。服务接受 Python fromisoformat，可含时区和小数秒；缺时区按服务器本地时区解释，输出规范化 `+00:00` 等偏移 |
| `context` | 可选文本元数据，不等于 Context 文件。iOS 不发。与 text 一样，以 `{`/`[` 开头且 JSON 有效则存为对象/数组；否则保留字符串 |
| `file` | 恰好一个文件；音频用 `segment_0001.m4a` + `audio/mp4`，Context 用 `context_0001.jsonl.gz` + `application/gzip` |

附件名拒绝 `/`、反斜线、NUL、`.`、`..`；其他危险标点/控制字符替换为 `_`、折叠空白、去首尾点和空白，空名变 `file`，清洗后 UTF-8 超过 150 字节拒绝。建议只发上述 ASCII 文件名。文本 part 默认上限 1 MiB，解析器最多 1 文件、12 文本字段；无业务层音频总大小上限。

成功 HTTP 200：`{"ok":true,"record_id":"32位UUID4小写hex","path":"接收端绝对JSON路径"}`。path 只供诊断，不能作为客户端文件路径。错误通常为 `{"detail":"说明"}`：401 token 无效（带 `WWW-Authenticate: Bearer`），400 非法设备/字段/时间/文件名/附件冲突；multipart 解析错误也为 400。磁盘、运行时或代理故障可能为 5xx 或无响应，不假设所有错误都返回 JSON。保存成功不表示转写、②入库或 Notes 消费完成；无 fsync 断电保证，细节见 [ingest 文件与重传](../../services/ingest/README.md#文件与重传)。

重传键为 `(小写 device, group_id, type, segmentIndex)`。重传保留首次 record_id，但会替换 record 与附件；即便 timestamp、文件名或偏移改变，也清理旧路径。因此必须保存并重用原元数据和文件，网络超时后重发同条，不生成新 group/index。音频与 Context 可任意先后独立上传；同名附件已属于另一 record 时返回 400。phoneIMU/phoneNotes 保存为 archived，无已确认的 Notes 消费接口。

客户端必须只在确认成功后标 uploaded；直连建议同时检查成功 JSON。当前 iOS 只检查无传输错误和 2xx，中继额外验回执，不读取上传响应正文，见 §8。失败保留附件。建议网络失败/5xx 指数退避并加随机抖动；401/403 提示检查凭据，400 提示检查协议，不忙循环。iOS 现状是失败次数 n 从 1 起，延迟 `min(2^min(n,12), cap)` 秒：临时网络错误 cap=60，其余 cap=3600，**包括 HTTP 4xx/5xx**；没有抖动。此策略不是服务端强制协议。

## 4. 健康探测与接收端身份

源码：[app.py](../../services/ingest/src/ear_ingest/app.py)、[relay.py](../../services/ingest/src/ear_ingest/relay.py)、[Upload.swift](../../clients/ios/EarRecorder/EarRecorder/Upload.swift)。

直连接收服务 `GET /health` 无鉴权返回 `{"ok":true}`，仅说明此进程可达。验证接收端时必须生成新的 32 位小写十六进制 challenge（iOS 用随机 UUID 去连字符），调用 `/health?challenge=<challenge>`；配对模式加 `&device=<小写设备ID>`。不发 Authorization、不把 token 放 URL。

精确算法：`proof = lowercase_hex(HMAC-SHA256(key=UTF8(secret), message=UTF8("ear-health-v1:" + challenge)))`。secret 即共享口令或该 device 的 token，**即使看起来是 hex，也必须用 UTF-8 字符串，不能 hex 解码**。客户端要求 HTTP 200 且 proof 精确匹配；建议常量时间比较。

直连返回 `{"ok":true,"proof":"…"}`；challenge 非空而不满足 `[0-9a-f]{32}` 返回 400，未知 device 返回 403。空 challenge 按普通健康检查，不做身份验证。加密中继的 `/health` 必须同时给 device/challenge，仅返回 `{"proof":"…"}`（无 ok），无效值 403，缺查询参数由框架返回 422。新客户端不要依赖中继有 ok。探测不验证磁盘、转写或通知队列。

## 5. 二维码、发现、证书和凭据

源码：[pairing.py](../../services/ingest/src/ear_ingest/pairing.py)、[__main__.py](../../services/ingest/src/ear_ingest/__main__.py)、[Pairing.swift](../../clients/ios/EarRecorder/EarRecorder/Pairing.swift)。

二维码完整语法：`earrecorder://pair?receiver=<UUID>&pin=<64小写hex>&code=<64小写hex>&relay=<URL编码根地址或空>`。四字段各恰好一个，relay 即使空也必须存在；未知额外 query 在 iOS 被忽略。receiver 能解析为 UUID，连接时规范为小写；pin/code 必须 `[0-9a-f]{64}`。可逐字节测试的完整字符串见 [pairing.json](../../services/ingest/tests/vectors/pairing.json)。生成端采用 URL query 编码，不能把整个 relay URL 直接拼接进查询串。

relay 非空必须是 HTTPS 根地址，或由接收端显式 `EAR_INGEST_HTTP_RELAY` 允许的 HTTP 根地址：有主机，无 user/password，无 query/fragment，path 只能空或 `/`。`pair --relay` 支持先将 `{receiver}` 替换为 receiver UUID；线上二维码里没有该占位符。HTTP 的含义是 §6 的端到端加密入口，不能直接把 bearer 上传发给它。

Bonjour 服务实例名就是小写 receiver UUID，类型 `_ear-ingest._tcp.`，域 `local.`；解析 SRV 的目标主机与端口，组成 `https://<host>:<port>`。iOS 等待解析最多 4 秒，去掉 hostname 末尾点并缓存地址；不能将服务实例名当 hostname，也不能硬编码 DHCP 地址。先尝试解析出的直连，再尝试邀请 relay，每次配对请求 iOS 超时 6 秒。

HTTPS 配对和后续已配对通信必须校验 **叶证书 DER 字节的 SHA-256 小写 hex** 等于 pin；不是公钥/SPKI hash，也不是 PEM 文本 hash。当前 iOS 接受匹配的自签证书，不另依赖系统 CA/hostname 信任结论；不跟随重定向。HTTPS 备用必须透传到同一证书，TLS 在第三方终止会导致 pin 不匹配。HTTP 加密配对通过邀请秘密认证，不使用 TLS pin。

直接兑换：`POST /api/v1/pair`，头 `X-Device-Id`、`X-Ear-Invitation: <code>`，无 body、无 bearer。成功 200 `{"receiver":"小写UUID","token":"64小写hex"}`；必须核对 receiver 与邀请一致并安全保存 token。非法 device 400，过期、他人已兑换或凭据已被替换的邀请 403。

邀请 code 是随机 32 字节的 hex 字符串，从生成起 300 秒有效；截止时间不在二维码中，客户端不能从扫码时间延长它。服务端 `expires >= now` 仍有效。第一次成功绑定一台设备，同设备有效期内可重试，其他设备不能兑换。签发公式为 `lowercase_hex(HMAC-SHA256(UTF8(code), UTF8("ear-pair-v1:" + device)))`；返回 token 才是以后请求的 secret，不能继续用 code。新的邀请给同 device 配对会替换旧 token；旧邀请不能把它改回。

必须安全持久保存 receiver、pin、device、独立 token、备用地址，避免日志和备份暴露秘密；保存成功后才切换配对状态。iOS 用 Keychain 保存 token，设置只存索引；Android 可使用其系统安全存储，不必照抄 Keychain。重新配对会改变未上传队列的目的地，产品是否允许迁移须明确告知用户。二维码测试 pin 为合成值，不是可连接的证书。

## 6. 加密 HTTP 中继：逐字节定义

源码：[relay.py](../../services/ingest/src/ear_ingest/relay.py)、[Relay.swift](../../clients/ios/EarRecorder/EarRecorder/Relay.swift)、[Pairing.swift](../../clients/ios/EarRecorder/EarRecorder/Pairing.swift)。

外层 `POST /api/v1/relay`，`Content-Type: application/octet-stream`，头 `X-Device-Id`、`X-Ear-Request: <ticket>`；不发送 bearer。ticket 是每次新请求生成的 32 位小写 hex 随机标识（iOS UUIDv4 去连字符）。邀请兑换另发 `X-Ear-Invite-Id = lowercase_hex(SHA256(UTF8(code)))`，服务从仍有效邀请中查找秘密。上传不发此头，按 device 查 token。

### 6.1 密钥

secret：邀请阶段用 code，配对后用独立 token。ticket 和 secret 都是 UTF-8 字符串，不 hex 解码。每方向得到 32 原始字节 AES/HMAC key：

```text
K_request  = HMAC-SHA256(UTF8(secret), UTF8("ear-relay-v1/request/"  + ticket))
K_response = HMAC-SHA256(UTF8(secret), UTF8("ear-relay-v1/response/" + ticket))
K_receipt  = HMAC-SHA256(UTF8(secret), UTF8("ear-relay-v1/receipt/"  + ticket))
```

不是 HKDF，无 salt，无额外截断。AES 使用 256-bit GCM，tag 为 16 字节。

### 6.2 请求明文与帧

拼接后的明文为 `UTF8(JSON元数据) || LF || 原始内层HTTP body`。元数据对象必需 `method`、`path`、`device`；可选键精确为 `Content-Type`、`X-User-Id`、`X-Ear-Final`。最后一个为现有转发键，本规范上传不使用。device 必须等于外层头；path 不含主机，包含原始 percent-encoded path 与 query。上传固定 POST `/api/v1/record`；邀请固定 POST `/api/v1/pair`，正文为空。中继会自行补内层 bearer 或邀请头，客户端不能在 metadata 中提供 Authorization 覆盖它。

元数据 JSON 不要求排序或空白风格，但字节测试必须使用向量的 `metadata_utf8`，含末尾 LF。服务只读取首行最多 8192 字节，客户端必须让整行（含 LF）在此范围内。后续字节就是原 multipart body；不能 base64 或先解读附件再重新拼接。

请求按次序拆帧，iOS 单独发元数据一帧，body 每最多 65536 字节一帧，最后发零长度结束帧。所有帧（包括最后一帧）格式：

```text
uint32_be(明文长度 L) || AES_GCM_ciphertext[L] || tag[16]
nonce = uint96_be(帧序号 i)，i 从 0 起递增
key = K_request
AAD = 空字节串
```

nonce 不在帧内发送；iOS 实现等价于 4 个零字节 + uint64_be(i)。请求 nonce **不是随机**，禁止每帧自行随机化。结束帧 L=0，仍须有正确的 GCM tag，总长 20 字节；省略它、标签错误、结束后还有数据、L>65536、流被截断都拒绝。网络 chunk 边界与帧边界无关。接收端只有解密并验证完整结束帧后才调用内层接口。

必须为每次新加密操作生成新随机 ticket，不可在同一 secret/ticket 下以相同计数器 nonce 加密不同内容。现实现无 ticket 防重放表；重发同密文可能再次执行内层接口，上传依靠重传键幂等，不能将它视为通用 exactly-once 通道。

### 6.3 响应与回执

解密/元数据/路由校验失败直接返回未加密错误：非法 ticket 为 400，未知秘密为 403，非法 envelope 为 400 `{"detail":"Invalid envelope"}`。这些没有有效回执，不能作为上传成功。内层接口执行后，无论其成功还是业务错误，外层 HTTP 状态等于内层状态 S，并产生：

```text
statusBytes = UTF8(十进制HTTP状态码)，例如 "200"（无换行）
responseBody = nonce[12] || AES-256-GCM(K_response, nonce, innerResponseBytes, AAD=statusBytes)
X-Ear-Receipt = lowercase_hex(HMAC-SHA256(K_receipt, statusBytes))
Cache-Control: no-store
```

响应的 AES 返回 ciphertext || tag[16]，没有长度前缀、没有结束帧。nonce 用随机 12 字节，每次响应重新生成。测试向量固定 response nonce 只为复现，生产实现必须随机。不得用请求计数器 nonce 代替响应 nonce。

客户端依据自己保存的 secret/ticket 和收到的 HTTP 状态计算回执并校验；需要读取正文时再分离 nonce、用状态作为 AAD 解密并验证 tag。回执只 MAC 状态，**不 MAC 响应正文**；正文完整性由 GCM 保证。只有成功状态且有效回执才能确认中继上传；配对还必须成功解密并验证返回 receiver/token。iOS 上传仅验状态回执、丢弃响应正文；新客户端可以同时验证解密后的成功 JSON。不能把中继返回任意 200 当成功。

## 7. 路径选择与本地队列

源码：[Upload.swift](../../clients/ios/EarRecorder/EarRecorder/Upload.swift)、[Store.swift](../../clients/ios/EarRecorder/EarRecorder/Store.swift)、[Pairing.swift](../../clients/ios/EarRecorder/EarRecorder/Pairing.swift)。

当前配置只有 primary 和一个 fallback，并非直连、HTTPS、中继三条同时竞速。配对模式 primary 来自 Bonjour 缓存的 HTTPS 地址；fallback 来自邀请，可为 HTTPS 透传或 HTTP 加密中继。未配对 primary 为手填地址，fallback 仅允许 HTTPS。

一般先探测 primary，再探测 fallback；若上次选中的 primary 被记录为失败，下轮先 fallback 后 primary。均使用 §4 proof；选中路径在地址/凭据不变时复用 60 秒。都失败后至少 15 秒才再探测；网络变化和设置更新可触发重新选择。未配对且无 fallback 时直接用 primary，不自动做 proof 探测。iOS 的探测 request timeout 为 5 秒、resource timeout 为 6 秒；在途上传不因换网立刻搬迁。不能将 60 秒理解为重传间隔。

每个音频/Context 独立持久保存 pending/uploading/retry/uploaded 状态、次数和下次时间。进程重启须把没有对应在途任务的 uploading 恢复为 retry。必须保留断网或未确认的原始文件；封口失败/无法恢复时保留原件供人工恢复，不标 uploaded。所有文件确认后会话才能 completed。临时 multipart 可以重建，原文件不得因此删除。

iOS 最多两项在途；已开始发送但尚有剩余字节且连续 5 分钟无进展才取消重试，等连接或等响应交系统超时（resource timeout 1800 秒）。这些是策略参考。上传成功的音频默认 3 天后清理，可设永久保留或成功后清理；Context、manifest、无法恢复原件不自动删。Android 可选择不同保留期，但不能在确认前清理。

## 8. 已知不一致 / 待定

依据为上文逐节链接的源码，并对照 [iOS README](../../clients/ios/README.md) 与 [ingest README](../../services/ingest/README.md)；本次不修改业务行为，也不自行裁决以下差异。

1. **手填 HTTP 直连（已修复）。** 基线提交里 iOS 把所有 HTTP 地址都当成加密中继，未配对时报“请先扫码配对中继”，与 README 描述的手填 HTTP 直连不符。现已改为：仅在已配对时把 HTTP 地址封装为 `/api/v1/relay`；未配对时按普通 multipart 直连上传；响应按请求是否带 `X-Ear-Request` 判断是否校验中继回执。Android 应采用同样的判断，不按 URL scheme 推断中继。
2. **状态和正文确认不同。** 接收端明确返回成功 JSON，iOS 直连只看 2xx；中继上传只验回执不验响应 GCM，配对才验二者。本文区分实际最小行为与更完整的客户端校验建议。
3. **回执与防重放范围。** 回执绑定 ticket 派生密钥和状态，不覆盖正文；服务端没有 ticket 去重/时效校验，安全依赖新随机 ticket 与内层幂等。是否升级协议待定，不自行添加 AAD 或改帧格式。
4. **时间与段边界。** iOS 会话 timestamp 秒精度，但 offset 基于毫秒墙钟；可能有小于一秒的绝对时间偏差。Context 结束 elapsed 是封口时墙钟，音频 end 来自帧数，两者可能不同；暂停段不保证首尾连续。服务接受无时区 timestamp 且 inbox 用本地时区，与核心 UTC 约定不同，Android 应明确发送 UTC。
5. **校验宽度不同。** receiver 接受非 UUID group/device、额外 meta 键、任意附件内容；iOS 使用 UUIDv4，固定格式和行字段。AAC profile/精确码率模式、Android device 前缀和 locationMeta 枚举并未在接收端冻结。不能从接收成功推断音频/Context 可消费。
6. **健康响应不同。** 直连有 ok，中继 proof-only；错误码/必填查询参数亦不同，见 §4。
7. **HTTPS 与 HTTP fallback 不是同一种中继。** HTTPS 配对路径要求 Mac 叶证书透传；HTTP 路径才走 §6。当前失败路径清除逻辑比对 `/api/v1/record`，已封装的 HTTP `/api/v1/relay` 不匹配，可能继续复用选路直到缓存过期。不要许诺故障立即切换。
8. **Notes/IMU 只有接收边界。** 接收端支持四类，iOS 只发两类；Notes 文件正文 schema 与后续消费未确认，不能凭本规范自行定义新的 Notes 产品语义。

## 9. 不属于跨平台协议的 iOS 细节

源码：[Audio.swift](../../clients/ios/EarRecorder/EarRecorder/Audio.swift)、[Runtime.swift](../../clients/ios/EarRecorder/EarRecorder/Runtime.swift)、[Pairing.swift](../../clients/ios/EarRecorder/EarRecorder/Pairing.swift)、[Info.plist](../../clients/ios/EarRecorder/EarRecorder/Info.plist)。

ATS 域名例外、后台音频会话类别、AVAudioEngine、蓝牙 HFP/A2DP 选项、CallKit、来电后后台执行时间、Core Location 权限/围栏/到访回调、App Intents、URLSession 后台标识、UserDefaults/Keychain API、签名与安装都不是线上协议。Android 要用自身系统机制实现持续采集和持久队列，不复制 Apple 配置。`.part`、PCM 备份、manifest 文件结构与保留任务也是本地实现方案；跨平台必须保持的是文件封口、同段标识、可靠保存和确认语义。

## 10. 一致性检查

源码/测试：[test_protocol_vectors.py](../../services/ingest/tests/test_protocol_vectors.py)、[test_relay.py](../../services/ingest/tests/test_relay.py)、[test_ingest.py](../../services/ingest/tests/test_ingest.py)。固定输入与期望值不在测试时重新生成，也不含真实凭据、设备、坐标或录音：

| 向量 | 自测内容 |
|---|---|
| [health.json](../../services/ingest/tests/vectors/health.json) | secret/challenge/device → proof |
| [relay-keys.json](../../services/ingest/tests/vectors/relay-keys.json) | secret/ticket → request、response、receipt 三个 32 字节 key |
| [relay-request.json](../../services/ingest/tests/vectors/relay-request.json) | 元数据原文、完整合成 multipart 数据、每帧明文/nonce/密文、结束帧、完整 wire、固定 nonce 响应与回执 |
| [pairing.json](../../services/ingest/tests/vectors/pairing.json) | 完整二维码字符串、解析字段、邀请 hash、签发 token、生成/到期秒数 |
| [upload.json](../../services/ingest/tests/vectors/upload.json) | 头、全部文本字段、附件字节、完整 multipart body、UTC 下 record/附件相对路径 |

Kotlin 自测时按 UTF-8 编码 secret 和所有标签，以 hex 解码 `*_hex` 字段；逐字节核对 key、每帧和拼接体。请求 nonce 由计数器固定，响应 nonce 按向量固定仅用于单元测试。用 `metadata_utf8` 原字节验证，不让 JSON 序列化器的键顺序改变预期密文。生产无需复制向量的 JSON 排序或 multipart boundary。

再验证：能解密响应、不同 HTTP 状态导致 GCM/回执不匹配、请求截断/改 tag/结束后加字节都被拒绝；邀请首次兑换、同设备重试成功、另一设备及到期后失败。接收端测试直接调用真实 derive、健康、邀请、解密和上传实现。精确响应字节测试用固定内层响应替代含临时绝对路径的输出；另一个测试经真实 ingest 完整上传及中继重传，核对保存字节、元数据、相对路径和 record_id。向量中 `/synthetic/inbox` 是纯示例根目录，真实服务路径随临时目录变化。

```sh
uv run pytest services/ingest/tests/test_protocol_vectors.py -q
```

本机端到端验收可复用 [ingest 的 curl 验收](../../services/ingest/README.md#临时-inbox-的-curl-验收)；下面直接发送向量中的完整 body。先使用空闲回环端口，在专用终端启动，无外部通知/转写：

```sh
export EAR_VECTOR_DIR=$(mktemp -d)
export EAR_INGEST_INBOX_ROOT="$EAR_VECTOR_DIR/inbox"
export EAR_INGEST_TOKEN=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
unset EAR_INGEST_EVENT_URL EAR_INGEST_NOTIFY_URL EAR_TRANSCRIPTION_URL
export TZ=UTC
uv run ear-ingest serve --host 127.0.0.1 --port 18791
```

等待该进程明确报告启动成功；若端口占用则停止，换空闲端口，不向已有服务发送测试。另开终端在仓库根执行：

```sh
uv run python - <<'PY'
import json
from pathlib import Path
import httpx
v = json.loads(Path('services/ingest/tests/vectors/upload.json').read_text())
h = json.loads(Path('services/ingest/tests/vectors/health.json').read_text())
with httpx.Client(base_url='http://127.0.0.1:18791', trust_env=False) as client:
    health = client.get('/health', params={'challenge': h['challenge']})
    assert health.status_code == 200 and health.json()['proof'] == h['proof']
    first = None
    for _ in range(2):
        result = client.post('/api/v1/record', headers=v['headers'], content=bytes.fromhex(v['body_hex']))
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['ok'] is True
        if first is not None:
            assert data['record_id'] == first
        first = data['record_id']
        path = Path(data['path'])
        assert path.as_posix().endswith('/' + v['record_path'])
        record = json.loads(path.read_text())
        attachment = path.parent / record['files'][0]['stored_name']
        assert attachment.as_posix().endswith('/' + v['attachment_path'])
        assert attachment.read_bytes().hex() == v['file']['hex']
    print('PASS: health proof, upload, bytes, UTC layout, retry identity')
PY
```

完成后 Ctrl-C 停止专用服务，按需清理该临时目录。此验收只证明接收、保存和重传；合成附件不是可解码 M4A，不验证 AAC 编码、真机录音、TLS/Bonjour、真实网络中继或下游转写。手机联调还须用自行生成的合成音频和 Context 上传，分别核对两条同段记录；不要把真实录音放入测试向量。
