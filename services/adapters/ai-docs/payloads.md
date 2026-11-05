---
audience: ai
doc-type: reference
based-on: services/adapters/src/ear_adapter/convert.py；contracts/records/raw-record.schema.json；旧 Ear ear/models/evidence_read.py
---

# ② 写入的原始数据：payload 字段

统一字段（id、时间、来源、适配器版本、去重键）见 `contracts/records/raw-record.schema.json`。本文只列 payload。字段名是 ② 的草案，③④ 有不同需要时提出，修改后同步本文和 `convert.py`。

## 共同约定

- 时间一律 UTC。一段录音的数据在**同一批**写入：收到任何一条的事件时，它引用的其他数据都已经能读到。
- `*_id` 字段都是 Ear 原始数据的 id，可以直接 `get`。
- `upstream` 只用于排查，记下旧 Ear 里的 id；不要依赖它做业务判断。
- `source.device` 是 Brain 实例 id（手机），`source.channel = "octopus"`，`source.user = "default"`。
- 不写置信度：旧 Ear 的转写置信度、环境声置信度、推断出的手机事件都不导入（设计：不使用置信度；原始数据只来自确定计算）。

## audio_ref：一段录音

时间为整段录音。

| 字段 | 说明 |
|---|---|
| `file_ref` | `{path, sha256, size}`，path 相对数据根目录（默认 `~/.octopus/local`），指向 Brain inbox 里的原始音频 |
| `media_type` | 例如 `audio/mp4` |
| `upstream` | `service`、`capture_id`、`record_id`、`group_id`、`pipeline_run_id` |

## local_speaker：一个音频块里的一个说话人

时间为这个说话人在该块里所有转写段的范围。**只在一个分离作用域（旧 Ear 的一个音频块）内有意义**：两个块里的 `S1` 是两条不同的数据，是否同一人由 ③ 通过声纹判断。

| 字段 | 说明 |
|---|---|
| `audio_ref_id` | 所在录音 |
| `label` | 旧 Ear 的局部标签，例如 `S1` |
| `upstream.diarization_scope_id` | 分离作用域 |

## voiceprint：一条声纹（③ 归属到人物）

每个生成了声纹向量的局部说话人一条。语音太少或质量不合格时旧 Ear 不生成，这个说话人就没有声纹。时间为生成它所用的转写段范围。

| 字段 | 说明 |
|---|---|
| `local_speaker_id` | 对应的局部说话人 |
| `audio_ref_id` | 所在录音 |
| `embedding` | `{values, shape, dtype, provenance}`：向量直接放在 payload 里（ECAPA 为 192 维，约 2 KB，不算大文件）；`provenance` 含归一化方式等，可能为 null |
| `sample_quality` | 旧 Ear 计算的样本质量，可能为 null |
| `transcript_segment_ids` | 生成向量所用的转写段（不一定是这个说话人的全部发言） |
| `model_version` | 声纹模型版本 |

## transcript_segment：一段转写

| 字段 | 说明 |
|---|---|
| `text` | 模型原始输出。旧 Ear 里用户纠正过的文本**不**导入，纠正在新系统里是用户反馈 |
| `language` | 可能为 null |
| `local_speaker_id` | 说话人 |
| `voiceprint_id` | 该说话人的声纹；没有声纹时为 null。④ 通过它查归属关系得到人物（与 ④ 的草案一致） |
| `overlap` | 是否与他人重叠 |
| `audio_ref_id`、`model_version` | |

## ambient_sound：一个环境声事件

| 字段 | 说明 |
|---|---|
| `label` | 模型原始类别（不导入旧 Ear 里的纠正） |
| `taxonomy` | 分类体系，例如 `audioset` |
| `audio_ref_id`、`model_version` | |

## gps_point：一个定位点（③ 归属到地点）

单点数据，时间为定位时间。同一设备、同一时间、同一坐标只存一条（相邻录音文件的首尾快照常重复）。

| 字段 | 说明 |
|---|---|
| `latitude`、`longitude` | WGS84（按手机原样） |
| `accuracy_m` | 可能为 null |

**密度**：手机目前每个录音文件（约 30 分钟）只上传开始和结束两次上下文，所以 GPS 大约每 30 分钟一两个点，远低于设计 3.4 期望的「一到几分钟一个点」。这需要手机或 Brain 提高上报频率，② 无法补出来。

## phone_info：手机状态

单点数据。`kind` 区分两种：

| kind | 字段 |
|---|---|
| `snapshot` | `state`：电量、充电、锁屏、耳机、媒体播放、前台应用、Wi-Fi 等，字段按旧 Ear 的 `DeviceContextState`，缺失表示手机没报 |
| `event` | `event_type`、`details`：一次状态变化（例如解锁），只导入手机直接报告的，不导入推断的 |

IMU 不在 MVP 的原始数据类型里，不导入，原始数据留在 Brain inbox。
