---
audience: human
doc-type: reference
based-on: c5c31f8 的②实现与文档；工作区未提交的②改动（selection.py、source.py、runtime.py、__main__.py 与 test_selection.py、test_locator_safety.py、test_source.py 等，作者读 git diff 与源码核对，只做 pytest --collect-only，未运行测试）；④任务记录 /tmp/ear-mvp-extraction-followup.json 的历史观察（非仓库文件）；任务交接称协调者正在修复 locator fd 与并发路径检查（2026-11-11 晚；2026-11-12 据仓库外日志 /tmp/ear-mvp-adapter-{current,fixed}.log、/tmp/ear-mvp-unified.log、/tmp/ear-mvp-acceptance.log 与对 services/adapters/tests 的复跑，该修复已完成并通过，见“验证状态”）；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核；未填写或改动任何 reviewed 字段
---

# ② 适配器

把外部数据转成统一输入格式写入 ①，只做确定计算，不做推断。MVP 的录音、转写、局部说话人、声纹、环境声、GPS 和手机信息，全部来自旧 Ear 的 Evidence 只读 API：旧 Ear 照常从 Brain inbox 接收文件并跑语音、声纹、环境声模型，② 读取它的结果，不改旧 Ear 的处理逻辑（设计 3.4「封装、不重写」）。

- [职责与验收](../../docs/design/work-breakdown-v1.2.md)（② 适配器一节）
- [数据流、导入规则与验收方法](ai-docs/implementation.md)
- [各类型 payload 字段](ai-docs/payloads.md)，供 ③④ 使用

当前状态：转换、导入规则、远程写入和常驻扫描已实现。`ear-adapter dry-run` 使用内存并模拟重启重扫（兼容 `--dry-run`）；`ear-adapter once` 向①导入一轮；`ear-adapter run --interval 60` 常驻扫描，SIGTERM/SIGINT 在当前录音处理结束后退出。①临时不可用、提交结果未知均留待下轮，配置/授权错误退出非零。每段录音保持一个原子批次，超限不会拆批。

**工作区里未提交的增补（不是 c5c31f8 的内容；2026-11-11 晚仍在修复，2026-11-12 已修复并通过，见“验证状态”）**

- **按已知 ID 显式导入**：`ear-adapter dry-run`、`once`、`run` 可重复传 `--capture-id <id>`（对应 `Runner.run_once(capture_ids=…)`）。显式选择时不调用旧 Ear 的列表接口，只对所选 ID 读 Evidence；不传则仍是原来的列表扫描。ID 必须是非空字符串，不含空白、控制字符、`/`、`\`，也不能是 `.` 或 `..`；先校验、去重、冻结，再做任何读写，非法输入以退出码 2 结束，提示固定、不回显输入，显式的空选择会被拒绝而不是当成“全部”。显式选择只是**选择条件，不是授权**：读权限预检、readiness、降级与缺声纹引用的规则都照旧，Pending 只在所选 ID 内重试；请求的 capture id 必须与响应里外层和内层 capture 记录一致，否则按协议错误处理。命令行参数会出现在进程列表和 shell 历史里，真实 capture ID 清单属于不入 Git、PR、日志的私有信息，使用前先评估，不要写进脚本或文档。
- **音频定位加固**：`InboxAudioLocator` 只在固定深度 `inbox/brain/<实例>/<手机日期>/<collection>/<时间>__<组 id>/` 下找文件；用 POSIX `dir_fd` 加 `O_NOFOLLOW` 逐级打开，根路径各级也不允许符号链接或 `..`，系统不支持时失败关闭；拒绝符号链接、硬链接、目录、FIFO、套接字等非常规文件；媒体类型须在白名单内且扩展名对应；大小与整文件 SHA-256 须同时匹配，读取过程中文件变化即拒绝；不打开同目录的转写、JSON 等其他文件。部署必须是可信的只读视图：它不抵抗挂载替换；对目录被并发搬出根只做尽力检测（每级目录打开后核对身份、失败路径关闭 fd，并在列目录与读哈希前后重新核对祖先目录链），不是文件系统锁，不保证没有竞争窗口，返回的是路径引用，不是永久文件句柄。检查失败（`AudioAccessError`）或旧 Ear 协议错误（`SourceProtocolError`）让命令以退出码 2 结束，不记录路径、文件名或响应正文。`HttpEarSource` 不使用环境代理、不跟随任何重定向，HTTP 错误不带 URL、头或正文。
- **验证状态（2026-11-11 晚记录，保留为历史）**：据任务交接，协调者正在修复 locator 的 fd 处理与并发路径检查，并把 ⑤ 接入根配置。作者对 `services/adapters/tests` 只做了 `--collect-only`（197 项），没有运行任何测试；④ 的任务记录曾在 2026-11-11T20:05Z 观察到 `test_source.py` 有 26 项失败，当时 ② 正被并行修改，那是历史观察，不是当前结果。**当时**修复与统一检查完成之前，不要把中间输出当作 ② 全绿，也不要把这些增补当作已验证。显式 ID 导入没有对现场旧 Ear 验证；Compose 上命令行真跑与常驻模式的掉线恢复仍没做。汇总见[跑通计划第 2.1 节](../../ai-docs/plans/mvp-runnable.md)。
- **验证状态（2026-11-12 更新）**：`test_source.py` 的 26 项失败已不再出现。仓库外日志：23:22Z `/tmp/ear-mvp-adapter-current.log` 为 189 passed、5 failed、3 skipped，5 项失败全在 `test_locator_safety.py`（目录 fstat 失败时 fd 未关闭 1 项、已打开的目录被搬出根仍被读取 4 项）；23:43Z `/tmp/ear-mvp-adapter-fixed.log` 为 194 passed、3 skipped，退出码 0；23:49Z 无开关的全仓运行 `/tmp/ear-mvp-unified.log` 为 1842 passed、651 skipped，退出码 0；四开关整体验收（退出码 0，2026-11-12T00:12:56Z 结束，2485 passed、11 skipped）也包含本模块，其间 `services/adapters` 没有被修改。本次对 `services/adapters/tests` 复跑同为 194 passed、3 skipped，跳过的 3 项是需要 `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS` 的 `test_storage_integration.py`。输入合成｜模型不适用｜外部不适用｜平台替身或测试夹具起的临时容器（本次整体验收与复核都只用合成数据；上面 2026-11-11 的真实旧 Ear 数据试跑是另一件事，不是这里的证据）。**没变的限制**：没有记录显示显式 ID 导入与加固后的 locator 在现场旧 Ear 或真实音频上跑过；Compose 上命令行真跑、常驻掉线恢复、adapter 正式身份仍没做；仍是工作区未提交、未提 PR、未部署。汇总见[跑通计划第 2.2 节](../../ai-docs/plans/mvp-runnable.md)。

配置只读环境变量：`EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_EAR_TOKEN`、可选 `EAR_ADAPTER_DATA_ROOT`（默认 `~/.octopus/local`）；远程模式另需 `EAR_ADAPTER_STORAGE_URL`、`EAR_ADAPTER_STORAGE_TOKEN`。凭据不进入命令参数和日志，保存在被忽略的 `deploy/.env`。

②身份为 `adapter`，写权限由登记表 `writer=adapter` 决定；必须显式授予 `read_types=["audio_ref", "local_speaker", "voiceprint", "transcript_segment", "ambient_sound", "gps_point", "phone_info"]`，不需要订阅。每轮扫描先验证全部七种读权限，无授权不会被当作尚未导入。①当前 `init-identities` 只签发 api 管理员和 manage-worker，**没有 adapter 身份的签发流程**；正式部署由①负责人 / I 决定，本地测试用部署内 `AuthOperator.issue_principal("adapter", read_types=[...])`，不修改①部署脚本。

真实①联调测试已加入 PostgreSQL/NATS 双门控，2026-11-11 本机运行通过；同日真实旧 Ear 数据经 `Runner` 写入测试夹具起的临时①：384 段导入、重扫无重复、4008 个声纹事件的引用全部可读（[实现说明 7.3](ai-docs/implementation.md)）。Compose 临时部署上用命令行跑真实数据、常驻模式的掉线恢复尚未做，也不是生产验收。③④⑤ 的首片在工作区各自实现且未提交；② 的 `Runner`（显式 capture id、合成 Evidence）已被 `services/storage/tests/test_attribution_runtime.py`（测试夹具起的临时 PostgreSQL/NATS/①）与 `tests/e2e/test_core_memory_loop.py`（`MemoryStorage`）用于合成入库联调，不是完整业务联调，也不是真实数据。旧 Ear 的列录音接口历史状态与 2026-11-05 内存试运行结果也保留在实现说明中。
