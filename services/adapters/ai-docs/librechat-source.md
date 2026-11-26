---
audience: ai
doc-type: guide
based-on: a252033 的协议、适配器和合成测试；本分支的可选登记、导出器及 API 证据投影
---

# LibreChat 源适配器

可选的 ② 适配器（不是分析插件）：把**一个个人 LibreChat 账号**的会话、消息和附件真实字节导入 Ear。代码在 `services/adapters/src/librechat_adapter/`（`librechat-adapter` 命令），来源侧只读导出器在 `tools/librechat_export.mjs`。它写入 ① 的方式与其他适配器相同：`AppendRaw`、`adapter` 身份、确定性 id；不新增数据库、队列或常驻服务。

## 1. 数据流

```text
LibreChat 源实例（显式配置）
 MongoDB ──只读用户，仅 3 个账号绑定视图──┐
 uploads/images 卷 ──:ro 挂载───┤  librechat_export_docker.sh
                                 └─ 一次性容器（同镜像，只读根、无权限、小内存）
                                        │ stdout: ear-librechat-export/1 (NDJSON)
                                        │ 受保护的传输（由宿主配置）
                           librechat-adapter once|run
                              ├─ 文件字节 ──► Ear 私有文件卷 librechat/blobs/aa/bb/<sha256>
                              └─ chat_session / chat_message / chat_attachment ──► ① AppendRaw
```

导出流由导出器按 LibreChat 版本翻译成稳定格式；适配器永远看不到 LibreChat 内部字段。一次完整成功的导出才推进水位。空全量保守地不推进，不查询其他账号来推断是否选错用户。

## 2. 与提取/界面侧的契约

登记类型（`raw_record`，writer=`adapter`）：`chat_session`、`chat_message`、`chat_attachment`。不进入默认登记表；在迁移且已初始化的个人库上显式运行 `ear-storage bootstrap-chat-source`，只登记这三类，不改现有定义。`librechat-adapter registry` 可核对定义。按用途显式批准专用 adapter 身份及可选 API 证据绑定的读取权限；不自动授予任何身份，包括 extraction、Gateway 和 dependency。提取接入另行审查。严格调度模式的覆盖条件仍独立核对，启用本渠道不自动扩权。

时间：消息与附件 `event_start == event_end`，取**消息创建时间**（附件随消息；库内未关联文件取自身存储时间并标为 `unlinked`）；会话取会话创建时间（点事件）。全部 UTC `Z`。`source = {device: <实例名，默认 librechat>, channel: "librechat", user: "default"}`，`adapter_version = "librechat-chat/0.1.0"`。

| 类型 | payload |
|---|---|
| `chat_message` | `text`、`role`（user/assistant）、`conversation_id`、`message_id`、可选 `parent_message_id`（根消息省略）；扩展：`sender`、`model`、`endpoint`、`finish_reason`、`attachment_ids`（源 file_id）、`reasoning`（think 文本，≤64k 字符）、`tool_calls[{name,args,output,truncated}]`（每项 ≤2k 字符）、`part_types`、`error`、`unfinished`、`is_temporary`、`text_chars`、`text_sha256`、`text_truncated`、`text_file_ref`（正文超过 200k 字符时全文存卷，payload 只留前缀） |
| `chat_attachment` | `text`（LibreChat 提取文本 > 文本类文件前缀 > 安全元数据行，≤20k 字符）、`text_kind`（extracted/decoded/metadata）、`text_truncated`、`conversation_id`、`message_id`、`file_id`、`filename`、`media_type`、`size`、`origin`（upload/generated/inline）、`source_context`、`available`；可用时 `file_ref{path,sha256,size}`，不可用时 `unavailable_reason` |
| `chat_session` | `conversation_id`、`title`、`tags`、`is_temporary`、`is_archived`、`endpoint`、`model`、`agent_id`、`source_created_at` |

三类都带：`revision`（从 1 起）、`content_sha256`（内容指纹）、`source_updated_at`、`source_system="librechat"`。`file_ref.path` 相对 **Ear 私有文件卷根**，仅经授权的归档维护进程可解析；路径内容寻址，不含任何来源路径、用户名或 URL。附件的 `payload` 永不含 LibreChat 的 `filepath`。

**修订（编辑/重放）**：同一对象（会话、消息、消息的某个附件）的第 n 次修订去重键为 `<device>:<类型>:<ids>:r<n>`，id 由 (事件时间, 类型, 版本, 去重键) 确定。写入前沿 r1、r2… 向 ① 探测已有修订并比较 `content_sha256`：相同不写；不同写 r(n+1)。因此重放不新增、编辑追加新修订（旧修订保留）、A→B→A 是第三次修订、仅 `updatedAt` 变化（点赞等）不新增、本地状态丢失后全量重导同样幂等。**读取方对同一 (conversation_id, message_id) 取 `revision` 最大的一条**；附件用 (message_id, file_id)，会话用 conversation_id。覆盖同名代码产物（LibreChat 复用 file_id）会产生附件新修订并保留旧字节。

**批次顺序**：会话 → 附件 → 消息；附件总先于引用它的消息提交。

**限制**：`reasoning`/`tool_calls` 只保留有界片段，不是完整工具输入输出；`image_url` 里的内联 PNG/JPEG/GIF/WebP 可归档原字节，普通远程 URL 不抓取；他人共享给本账号的内容只在 `user` 等于本账号时才会导出。

## 3. 兼容性与验证边界

版本白名单当前为 LibreChat `0.8.9`，导出器与适配器分别复核。MongoDB 视图以固定账号筛选，导出器使用有时限的 `find`、小批量及批间停顿；不做统计聚合。需由部署方独立验证自己的镜像、存储结构、挂载边界和资源开销。此交付只使用合成数据，不包含现场核查、部署信息或验收声明。文件根及其祖先须由部署方信任并保持稳定；文件校验不提供面对恶意并发重命名或换挂载的隔离保证。

## 4. 来源侧最小权限

- **库**：创建只读用户（需 MongoDB 管理员，导出器不创建凭据）：`node tools/librechat_export.mjs --print-setup --user-id <account-id>` 打印 mongosh 片段。三个视图用固定账号匹配和字段白名单，只授予这些视图的 `find`，没有底层集合、其他账号、统计或写权限。
- **账号**：每次查询都带 `user` 等值条件；`EAR_EXPORT_USER_ID` 必须是 24 位十六进制，没有“全部用户”模式。
- **文件**：只读 `/uploads/<账号>/<名>`、`/images/<账号>/<名>`（去掉 query/hash），`O_NOFOLLOW` 打开，拒绝符号链接、硬链接、目录、他人账号目录、超大文件（默认 64 MiB）；**不抓取任何 URL**，非 `local` 来源、s3/http/data 路径一律记为 `unsupported_source`/`bad_path`。字节读完后复核大小与 mtime，适配器再重算 SHA-256，不信导出器声明。
- **传输**：只使用宿主配置的受信命令，导出只读；不得在参数、日志或仓库中存放凭据。
- **版本**：导出器读取应用目录的 `package.json`，不在 `SUPPORTED_LIBRECHAT_VERSIONS` 就在任何数据之前以退出码 3 拒绝；适配器收到 header 后再核对一次，`--accept-version X` 才放行未核对的版本。

## 5. 运行

环境变量（或 `--config` 指向 0600 的私有 JSON，键相同；令牌可用 `_FILE`）：

| 变量 | 含义 |
|---|---|
| `EAR_LIBRECHAT_FILE_ROOT` | Ear 私有文件卷根（绝对路径，真实目录，不对其他用户可写）；`once`/`run` 必填 |
| `EAR_LIBRECHAT_STORAGE_URL`、`…_STORAGE_TOKEN(_FILE)` | ① 根地址与 adapter 身份令牌 |
| `EAR_LIBRECHAT_EXPORT_ARGV` | 受信任的导出命令（JSON 数组）；命令收到请求 JSON，返回 NDJSON |
| `EAR_LIBRECHAT_DEVICE` | `source.device`，默认 `librechat` |
| `EAR_LIBRECHAT_FULL_EVERY_HOURS` | 周期性全量重同步，默认 168，0 关闭 |
| `EAR_LIBRECHAT_FILE_MODE` | 卷内文件权限（八进制，默认 640；目录自动加 x） |
| `EAR_LIBRECHAT_ACCEPT_VERSIONS`、`…_EXPORT_TIMEOUT` | 额外放行版本、导出超时秒数 |

```bash
uv run librechat-adapter dry-run --from-file export.ndjson      # 内存 ① + 临时卷，证明重放无新增
uv run librechat-adapter once   [--full]                         # 一轮
uv run librechat-adapter run --interval 300                      # 常驻；SIGTERM 在当前一轮结束后退出
```

退出码：0 完整成功；1 暂时失败（导出命令/① 不可用、流不完整、有可重试的不可用附件）或有被拒绝的条目；2 配置、权限、登记、协议或文件卷问题；3 版本/格式不在核对范围。日志只有计数和状态，不含正文、文件名或路径。

水位和锁在 `<卷>/librechat/state/<device>.{json,lock}`：导出器给出 `watermark = 开始时间 − 120 s`，只在收到完整结束帧且没有可重试的附件失败后保存（已拒绝的个别格式错误条目仍返回非零；全量重扫可重访）；丢失或损坏退回全量（对 ① 幂等）。暂时性不可用（`read_error`、`changed_during_read`、完整性失败）会写入“不可用”修订但**不推进水位**，下轮重导成功后追加可用的新修订；确定性的原因（`missing_on_disk`、`too_large`、`unsupported_source`…）不阻塞。校验不过的条目计入 `rejected_frames`，不回显内容。

## 6. 删除

LibreChat 是硬删除且无墓碑，无法可靠区分“删除”与“暂时查不到”。因此**源端删除不会删除或修改 Ear 里的任何记录和文件字节**（原始数据只追加），也不发任何删除事件；摘要里固定输出 `"deletions": "deferred"`。如以后要表达删除，需要另行设计（例如全量清单对比后的显式事件），不在本适配器范围。

## 7. 验证状态

- `services/adapters/tests/test_librechat_*.py`（合成数据，含 Node 导出器夹具端到端）和 `tools/tests/librechat_export.test.mjs`（`node --test`）。覆盖：账号隔离与隐藏字段不外泄、版本核对、符号链接/他人目录/URL/超大文件、字节完整性与回滚、重放幂等、编辑/改回/仅元数据变化的修订、附件字节被覆盖、批次顺序、超长文本转存、孤立代理项与 NUL 清洗、协议错误与截断流、水位与锁、命令来源的环境隔离与超时、CLI 退出码。
- 隔离 PostgreSQL/NATS 联调覆盖真实 HTTP 入库、可选登记、无隐式授权、附件字节回读、重放幂等、Outbox/JetStream 事件无正文；这些集成测试须显式开启。本次不验证生产源、真实模型、长期常驻或真实容器包装。
- CI：仓库 CI 已装 Node 24，pytest 会自动运行导出器端到端测试；`node --test tools/tests/librechat_export.test.mjs` 已纳入 `.github/workflows/ci.yml`。

## 8. 运行边界

- 没有默认启动或部署接线。容器包装要求显式指定应用容器、网络和私有环境文件。MongoDB 视图初始化命令只打印 mongosh 脚本，不执行；本轮不要对任何实例执行它。
- 导出文件、内容寻址卷、容器只读挂载和运行状态都属于私有数据。每个固定账号必须分配独立的设备命名空间和私有卷；不能把原有设备/卷重定向到其他账号，也不允许把卷共享给 API 或后续提取进程。
- API 若显式开启仅回已有记录的有界文本，不读卷、不给下载。见 [⑥ 证据回读说明](../../api/README.md#可选-librechat-证据)。提取与 viewer 对接不是本归档分支的一部分。
