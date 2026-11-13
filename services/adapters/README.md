---
audience: human
doc-type: reference
---

# ② 适配器

把外部数据转成统一输入格式写入 ①，只做确定计算，不做推断。`ear_adapter` 的录音、转写、局部说话人、声纹、环境声、GPS 和手机信息，全部来自旧 Ear 的 Evidence 只读 API：旧 Ear 照常从 Brain inbox 接收文件并跑语音、声纹、环境声模型，② 读取它的结果，不改旧 Ear 的处理逻辑（设计 3.4「封装、不重写」）。

- [职责与验收](../../docs/design/work-breakdown-v1.2.md)（② 适配器一节）
- [数据流、导入规则与验收方法](ai-docs/implementation.md)
- [各类型 payload 字段](ai-docs/payloads.md)，供 ③④ 使用

当前状态：转换、导入规则、远程写入和常驻扫描已实现。`ear-adapter dry-run` 使用内存并模拟重启重扫（兼容 `--dry-run`）；`ear-adapter once` 向①导入一轮；`ear-adapter run --interval 60` 常驻扫描，SIGTERM/SIGINT 在当前录音处理结束后退出。①临时不可用、提交结果未知均留待下轮，配置/授权错误退出非零。每段录音保持一个原子批次，超限不会拆批。

配置只读环境变量：`EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_EAR_TOKEN`、可选 `EAR_ADAPTER_DATA_ROOT`（默认 `~/.octopus/local`）；远程模式另需 `EAR_ADAPTER_STORAGE_URL`、`EAR_ADAPTER_STORAGE_TOKEN`。凭据不进入命令参数和日志，保存在被忽略的 `deploy/.env`。

`ear-adapter` 身份为 `adapter`，写权限由登记表 `writer=adapter` 决定；必须显式授予 `read_types=["audio_ref", "local_speaker", "voiceprint", "transcript_segment", "ambient_sound", "gps_point", "phone_info"]`，不需要订阅。每轮扫描先验证全部七种读权限，无授权不会被当作尚未导入。①当前 `init-identities` 只签发 api 管理员和 manage-worker，**没有 adapter 身份的签发流程**；正式部署由①负责人 / I 决定，本地测试用部署内 `AuthOperator.issue_principal("adapter", read_types=[...])`，不修改①部署脚本。

真实①联调测试已加入 PostgreSQL/NATS 双门控，2026-11-11 本机运行通过；同日真实旧 Ear 数据经 `Runner` 写入测试夹具起的临时①：384 段导入、重扫无重复、4008 个声纹事件的引用全部可读（[实现说明 7.3](ai-docs/implementation.md)）。Compose 临时部署上用命令行跑真实数据、常驻模式的掉线恢复尚未做，也不是生产验收。③④⑤仍未完整业务联调。旧 Ear 的列录音接口历史状态与 2026-11-05 内存试运行结果也保留在实现说明中。

## GPS 逆地理编码

`geo_adapter` 已实现为同一 uv 项目下的独立进程，依据[已采纳提案](../../docs/proposals/gps-reverse-geocoding.md)。读取①的待编码 `gps_point`，写追加式 `gps_geocode`，供④作为原始证据使用；不创建③的 `place`，不访问旧 Ear。

```bash
uv run geo-adapter dry-run
uv run geo-adapter once
uv run geo-adapter run --interval 60
```

- `dry-run` 只读取①并报告待处理数量，不调用地图服务、不写①；仍校验配置和两类读授权。
- `once` 扫描至待处理集合为空或一个点的瞬态重试耗尽；`run` 轮间默认等待60秒（1–86400），SIGTERM/SIGINT 在当前点的有限请求/写入完成后退出。并发持续导入时一轮可能持续处理新点。
- 高德用于本地中国陆地掩膜内坐标，先将 WGS84 换算为 GCJ-02；其他坐标用 Nominatim 兼容 `/reverse`。仅配置一个 provider 时全部使用它。掩膜为粗分辨率，不保证边境、海岸和小岛精度，数据来源及限制见[实现说明](ai-docs/implementation.md#8-gps-逆地理编码实现)。原生 Photon API 不是 Nominatim `/reverse`，不能直接填入。
- 瞬态重试耗尽立即结束本轮，不再请求剩余点；未超过最长等待不写记录，下一轮重试。仅当 UTC 墙钟距点的 `ingested_at` 超过最长等待时写 `failed`。非法坐标不请求地图，直接写 `provider=invalid`、`status=failed`，其余地图字段为 null，继续后续点。明确无结果写 `no_result`；已写的三种状态均结束④的地名等待。配置/鉴权错误退出2，不写失败记录。①临时不可用或提交结果未知留待下轮，`once` 返回1。正常或仅推迟返回0；`once` 写入 failed 返回1。`deferred` 为因瞬态故障结束本轮时仍待处理的总点数（包括未请求的后续页），不表示请求失败次数。

配置仅取环境变量（模板见 [deploy/.env.example](../../deploy/.env.example)）：

| 环境变量 | 含义/默认值 |
|---|---|
| `GEO_ADAPTER_STORAGE_URL`、`GEO_ADAPTER_STORAGE_TOKEN` | 必填，①根地址与 adapter 身份令牌；不回退 EAR_ADAPTER 配置 |
| `GEO_ADAPTER_AMAP_KEY` | 高德 Web 服务 key；与下一项至少配置一个 |
| `GEO_ADAPTER_NOMINATIM_URL` | Nominatim 兼容服务根地址，可带路径前缀，不带 `/reverse`、查询参数或凭据 |
| `GEO_ADAPTER_DATA_ROOT` | 缓存目录，默认 `~/.local/share/ear/geo` |
| `GEO_ADAPTER_DELAY` | HTTP 请求起点最小间隔，默认1秒，范围0–60；包括重试，单进程限速 |
| `GEO_ADAPTER_TIMEOUT` | HTTP 超时，默认10秒，范围0.1–60 |
| `GEO_ADAPTER_ATTEMPTS` | 每点最大尝试次数，默认3，整数1–5 |
| `GEO_ADAPTER_MAX_WAIT_SECONDS` | 从点入库起允许等待的秒数，默认21600（6小时），有限非负数；0表示首次耗尽且入库时间已过去就写 failed |

`geo_adapter` 仍使用 `adapter` 身份，单独令牌至少授予 `read_types=["gps_point", "gps_geocode"]`，不需订阅；若两进程共享令牌，则在旧 Ear 的七类基础上增加 `gps_geocode`，共八类。旧 `ear-adapter` 仍只检查其七类输入。④ extraction 身份须额外读取 `gps_geocode`。已有①部署需由运维显式运行可信 `bootstrap` 更新核心登记表并补授权，普通启动不自动登记；现有 `init-identities` 不负责签发这些身份。

日志仅输出计数、状态和异常类，不输出凭据、坐标或地名。缓存包含地点数据，置于被忽略的数据目录，`geo-cache.sqlite3*` 也全局忽略。运行单个 geo-adapter 进程，按所用地图服务的限速要求配置间隔。
