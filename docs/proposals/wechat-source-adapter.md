---
audience: human
doc-type: proposal
status: proposed
generated-by: Claude (AI)，应 YijieYing 要求起草
generated-at: 2026-11-28
based-on: docs/proposals/phone-recorder-extraction.md（accepted，"封装、不重写"与新输入渠道按同一输入约定接入的先例）；services/adapters/ai-docs/librechat-source.md 与 services/adapters/src/librechat_adapter/（1c153b9，可选聊天适配器的现成形态与 chat_session/chat_message/chat_attachment 契约）；contracts/records/raw-record.schema.json 与 contracts/registry/data-types.json（e9590ae 时的定义）；Echo-As-Record/wechat-lens e68266c 的 README、docs/API.md 与 wxlens.record.v1 / wxlens.moment.v1 导出格式（本机合成数据实跑：65 项 pytest、self-test、demo 导出通过；真实微信密钥受本机 SIP 限制未取到）
---

# 提案：微信来源适配器（wechat_adapter，可选）

> **状态：proposed，未采纳。** 本文提议新增一个**可选**的 ② 适配器，把一个人的微信聊天、朋友圈经本机的 [wechat-lens](https://github.com/Echo-As-Record/wechat-lens) 导入 Ear，作为录音之外的又一个输入渠道。沿用[手机录音拆分提案](phone-recorder-extraction.md)已确立的"新输入渠道按同一输入约定接入"。其中第 5 节的 D1～D4 会改动契约（消息 `role`、朋友圈类型、媒体字节、导入范围），需要负责人决定；新增/扩展数据类型属于契约增补，采纳后仍按[工作分解](../design/work-breakdown-v1.2.md)由 ① 负责人与 I 审核。本提案只用合成数据验证，不含真实微信数据、不含部署或验收声明。

## 1. 目标与边界

把微信变成 Ear 的一个输入渠道，让录音和微信消息落在同一条时间线上，供 ④ 一起划分活动。

**在范围内：**

- 从本机 wechat-lens 的只读接口增量读取：会话、消息、朋友圈。
- 转成 ① 的原始数据，用 `adapter` 身份、确定性 id 经 `AppendRaw` 写入；不新增数据库、队列或常驻依赖。
- 可选、显式开启；不开启时对现有部署零影响。

**不在范围内：**

- **不碰密钥、不解密、不读微信数据库。** 这些全部由 wechat-lens 负责；适配器只经 loopback HTTP 读它已经规范化好的数据。
- 不发微信消息、不改微信、不做人物归属（归属仍归 ③）。
- 不把微信数据上传到任何合作仓库或远端；数据只留在用户个人部署里。

**与 LibreChat 适配器的关系：** 两者形态一致（可选、只读、`chat_*` 类型、修订+去重），可以共用 `ear-storage bootstrap-chat-source` 登记的三类类型和读取投影。区别只在来源侧：LibreChat 读 MongoDB 导出，微信读 wechat-lens 的 HTTP/JSONL。

## 2. 数据流

```text
本机微信（加密库，SIP 保护）
   │  wechat-lens 负责：取密钥、SQLCipher 解密、增量同步、规范化
   ▼
wechat-lens（127.0.0.1:8790，无鉴权，仅本机）
   wxlens.record.v1 / wxlens.moment.v1  +  稳定 id
   │  GET /api/export/messages?since=&format=json
   │  GET /api/moments?since=&offset=
   │  （只读；游标用 ts，since 含、until 不含）
   ▼
wechat-adapter once|run        ← 本提案新增，在 services/adapters/
   ├─ 媒体字节（见 D3）────────► Ear 私有文件卷 wechat/blobs/aa/bb/<sha256>
   └─ chat_session / chat_message / chat_attachment / （朋友圈见 D2）──► ① AppendRaw
```

- 适配器与 wechat-lens 同机、同用户；wechat-lens 的密钥（`~/.wxlens/keys.json`，0600）不经过适配器，下游只拿到 HTTP/JSONL。
- wechat-lens 的记录带稳定 `id`，适配器据此做确定性 `dedup_key` 和 UUIDv7 `id`，重复导入幂等。

## 3. 映射到 ① 的原始数据

公共字段按 [raw-record 契约](../../contracts/records/raw-record.schema.json)：

- `source = {device: <实例名，默认 "wechat">, channel: "wechat", user: "default"}`。`channel` 是新值，`snake_case`，契约本来就不枚举，无需改 schema。
- `adapter_version = "wechat-chat/0.1.0"`。
- 时间：消息、朋友圈都是点事件，`event_start == event_end` 取其创建时间；UTC `Z`。
- `dedup_key`：`wechat:<类型>:<wechat-lens 稳定 id>:r<修订号>`。
- 三类都带 `revision`（从 1）、`content_sha256`，修订语义照搬 LibreChat（r1、r2… 探测前沿，内容指纹相同不写）。

| wechat-lens | → ① 类型 | 说明 |
|---|---|---|
| 会话（conversation） | `chat_session` | `conversation_id`、`title`、`kind`（direct/group）；群聊 `kind=group` |
| 消息（record.v1） | `chat_message` | `text`、`conversation_id`、`message_id`；**`role` 见 D1**；扩展 `sender{id,name,is_self}`、`msg_kind`（text/image/voice/…）、`source_system="wechat"` |
| 消息媒体（图/文件/视频/语音） | `chat_attachment` | 仅在本机有缓存时；`file_ref` 指向 Ear 私有文件卷；**是否存字节见 D3** |
| 朋友圈（moment.v1） | **见 D2** | 无现成对应类型 |

## 4. 隐私与配置（这是和 LibreChat 最大的不同）

微信数据量和敏感度都远超 LibreChat：单账号实测可达 400 万条消息、11 年跨度。因此：

- **默认不全量导入。** 必须用户显式选择范围：按时间窗（`since`）、或按会话白名单。全量 400 万条默认灌进 ① 既不合理也危险。范围的表达方式是 D4。
- 适配器只读 wechat-lens 的 loopback 接口；wechat-lens 本身无鉴权，仅限本机，不得暴露到局域网/公网（见其 README 7.6）。
- ① 的 `adapter` 读取权限按用途显式批准，不自动授予 extraction、Gateway、dependency 任何身份。启用本渠道不自动扩权。

## 5. 待负责人决定

### D1：微信消息的 `role`

现有 `chat_message.role` 枚举只有 `user` / `assistant`，是为"人 ↔ 助手"设计的。微信是"人 ↔ 人"，发信人可能是本人、某个联系人、或群成员。

| 选项 | 做法 | 代价 |
|---|---|---|
| A 扩展枚举 | `role` 增加一个人际值（如 `contact`），本人消息用现有 `user` | 改契约；LibreChat 消费方要忽略未知 role |
| B 复用 user/assistant | 本人=`user`，对方=`assistant` | 语义扭曲；assistant 的"陈述不证明已完成"注释对人不成立 |
| C 新建类型 | 另立 `im_message` 等 | 多一套类型，和 LibreChat 不共用投影 |

倾向 A：消息身份本来就靠扩展字段 `sender{is_self}` 承载，`role` 只需能区分"本人/非本人"。但改枚举影响已有消费方，须负责人定。

### D2：朋友圈放哪种类型

朋友圈是社交动态（作者、正文、媒体、点赞、评论），没有聊天语义。选项：A 新建原始类型 `social_post`（点赞/评论作为其 payload 或各自成条）；B 塞进 `chat_message` 的一个伪会话（语义不清，不建议）。倾向 A。点赞、评论是否各自成条，关系到 ④ 怎么用，需一起定。

### D3：媒体字节是否归档

微信媒体只有本机缓存过才有实体（原图、完整视频、文件常常没有）。语音是 SILK，可转 24 kHz WAV。选项：A v1 只存文本/元数据，不存字节（最简、最安全）；B 存已缓存的字节到私有文件卷（同 LibreChat）；C 把语音转成 WAV 走 `audio_ref` 接现有录音识别链路（最有价值但最复杂，牵涉 ① 音频管线）。倾向 A 起步，C 列为以后。

### D4：导入范围怎么表达

按时间窗、会话白名单，还是两者组合？默认值是什么（建议默认空，必须显式指定）？这决定适配器的配置形态，也是隐私的主控制点。

## 6. 实施步骤（采纳后）

每步派 Codex 用 `dispatch --worktree` 在独立分支上做，Claude 审查；代码默认交 Codex，前端/文档除外。

0. **先定 D1～D4。** 契约不定，适配器写出来就要返工。
1. 若 D1/D2 改契约：先提契约 PR（① 负责人 + I 审核），更新 `raw-record.schema.json`、`data-types.json`、`bootstrap-chat-source`。
2. `services/adapters/src/wechat_adapter/`：读 wechat-lens 导出 → 映射 → `AppendRaw`；游标、确定性 id、修订探测、合成测试向量（全部合成值，不含真实聊天/密钥/设备）。
3. 文档：`services/adapters/README.md` 增一节、`services/adapters/ai-docs/` 加来源说明（仿 `librechat-source.md`）。
4. 真实数据验收由用户在个人部署里做，不进仓库。

## 7. 现状备注（不属于契约）

- wechat-lens 在本机用**合成数据**验证通过：65 项 pytest、self-test、demo 导出为 `wxlens.record.v1`。
- 取真实密钥受本机 SIP 限制未成功（内存扫描与 Frida 都要读微信内存，被系统挡下）。这不影响本适配器设计——适配器本就不碰密钥，只读 wechat-lens 的输出。真实接入的前提是用户机器上 wechat-lens 能出数据。
- 为在本机把 wechat-lens 跑到取密钥这一步，发现并在本地修了它两处"按当前用户 uid 过滤微信进程"的缺陷（sudo/管理员子进程下会误报"微信未运行"）。改动未推送，待整理给 wechat-lens 维护者。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
