---
reviewed-by: aytony
reviewed-at: 2026-10-30
---

# 术语表（中 → 代码标识）

文档用中文，代码、数据库、事件、日志一律用下表的英文标识。**同一个概念只用一个标识。**
代码标识用 `snake_case`；类名用对应的 `PascalCase`（如 `ActionSuggestion`）。

## 模块

模块名同时用于：目录名、⑦ 的调用方模块、日志的 `module` 字段、① 的写权限身份。

| 编号 | 中文 | 模块名 | 目录 |
|---|---|---|---|
| ① | 存储与事件 | `storage` | `services/storage/` |
| ② | 适配器 | `adapter` | `services/adapters/`（含多个适配器） |
| ③ | 归属模块 | `attribution` | `services/attribution/` |
| ④ | 活动提取模块 | `extraction` | `services/extraction/` |
| ⑤ | 依赖分析模块 | `dependency` | `services/dependency/` |
| ⑥ | 客户端与 API | `api` | `services/api/` |
| ⑦ | 大模型入口 | `llm_gateway` | `services/llm_gateway/` |
| ⑧ | 检索索引模块【以后】 | `retrieval_index` | — |

## 数据分类

| 中文 | 标识 | 说明 |
|---|---|---|
| 原始数据 | `raw_record` | 只追加 |
| 语义信息 | `semantic` | 有版本 |
| 提取规则 | `rule` | |
| 系统记录 | `system_record` | 不发事件 |

## 原始数据类型（`data_type`）

| 中文 | 标识 |
|---|---|
| 录音引用 | `audio_ref` |
| 转写段 | `transcript_segment` |
| 局部说话人 | `local_speaker` |
| 声纹 | `voiceprint` |
| 环境声 | `ambient_sound` |
| GPS 点 | `gps_point` |
| 手机信息 | `phone_info` |
| 用户反馈 | `feedback` |

## 语义信息类型（`data_type`）

| 中文 | 标识 |
|---|---|
| 人物 | `person` |
| 未命名人物 | `person`（`name` 为空），显示为 `#N` |
| 地点 | `place` |
| 未知地点 | `place`，固定常量 `UNKNOWN_PLACE_ID` |
| 归属关系 | `attribution` |
| 参考 | `reference`（人物 / 地点的属性，不是独立类型） |
| 活动 | `activity` |
| md 产物 | `artifact` |
| 外部动作建议 | `action_suggestion` |

## 统一输入格式字段（设计文档 3.3）

| 中文 | 字段 |
|---|---|
| id | `id` |
| 类型 | `data_type` |
| 版本号 | `version`（原始数据固定为 1） |
| 事件时间区间 | `event_start`、`event_end`（半开区间；单点时相等） |
| 入库时间 | `ingested_at` |
| 来源：设备、渠道、用户 | `source.device`、`source.channel`、`source.user` |
| 适配器版本 | `adapter_version` |
| 去重键 | `dedup_key` |
| 数据 | `payload` |

## 版本与状态

| 中文 | 标识 |
|---|---|
| 版本号 | `version`（整数，从 1 开始） |
| 已被替代 | `superseded` |
| 新增 / 修改 / 标记为已被替代 | `created` / `updated` / `superseded` |

外部动作建议的状态：

| 中文 | 标识 |
|---|---|
| 待审核 | `pending` |
| 已通过 | `approved` |
| 已拒绝 | `rejected` |
| 已执行 | `executed` |
| 执行失败 | `failed` |
| 已被替代 | `superseded` |

外部动作类型：写入日历 `calendar_event`，写入提醒事项 `reminder`。

## 事件与依赖分析

| 中文 | 标识 |
|---|---|
| 总线 | `bus` |
| 「数据库被修改」 | `DataChanged`，`event_type = "data_changed"` |
| 「请求重新判断」 | `ReevaluationRequested`，`event_type = "reevaluation_requested"` |
| 根源反馈 id | `root_feedback_id`；系统发起时为字面量 `"system"` |
| 依赖分析深度 | `depth` |
| 不处理历史 | `skip_history` |
| 起因 | `cause` |
| 可读的修改描述 | `change_description` |
| 依赖分析 | `dependency_analysis` |
| 依赖登记 | `dependency_record` |
| 传播记录 | `propagation_record` |
| 登记表（数据类型 → 负责模块） | `handler_registry` |
| 重新判断 | `reevaluate` |

## 提取与规则

| 中文 | 标识 |
|---|---|
| 定时提取 / 手动提取 | `scheduled_extraction` / `manual_extraction` |
| 提取记录 | `extraction_run` |
| 适用范围 | `scope` |
| 提取内容 | `instruction` |
| 可选动作 | `actions` |
| 只改这一次的提取 | `one_off`（规则的一种，绑定单个活动） |
| 引用标记 | `[[person:<id>]]`、`[[place:<id>]]` |

## 用户反馈类型（`feedback.kind`，草案，最终由 ⑥ 与 ③④ 约定）

| 中文 | 标识 | 执行模块 |
|---|---|---|
| 纠正归属关系 | `correct_attribution` | ③ |
| 确认归属关系 | `confirm_attribution` | ③ |
| 增减参考 | `edit_references` | ③ |
| 新建地点 | `create_place` | ③ |
| 命名、改名 | `rename` | ③ |
| 按时间范围重新归属 | `reattribute_range` | ③ |
| 同时重新判断其他相关记录 | `reevaluate_related` | ⑤ |
| 手动提取 / 重新提取 | `extract_range` | ④ |
| 规则增删改 | `edit_rule` | ④ |
| 只改这一次的提取 | `one_off_extraction` | ④ |
| 把规则应用到历史活动 | `apply_rule` | ④ |
| 审核外部动作建议 | `review_suggestion` | ④ |
| 外部动作的执行结果 | `action_result` | ④ |

## 待讨论

- 「活动」用 `activity`，避免和总线「事件」`event` 混淆（沿用 10-23 的约定）。
- 原始数据是否也带 `version`：目前事件里原始数据的版本号为 `null`。
