---
audience: human
doc-type: proposal
status: accepted
generated-by: Claude (AI)，应 YijieYing 要求
generated-at: 2026-11-13
based-on: docs/design/design-v1.2.md 3.4、6、7.1 节；docs/decisions/mvp-confirmed-decisions.md C07；contracts/registry/data-types.json；services/adapters/ai-docs/payloads.md；services/extraction/src/ear_extraction/segmentation.py（c5c31f8）
---

# 提案：GPS 逆地理编码（`gps_geocode`）

> 2026-11-13 YijieYing 在 Claude Code 对话中同意：采用本方案；逆地理编码结果按原始数据处理，④划分活动时可以读取；可以把坐标发给第三方地图服务；实现放在 `services/adapters` 下。本文件据此标为 accepted，未经逐字人工复审。新增数据类型属于契约增补，仍按[工作分解](../design/work-breakdown-v1.2.md)第 11 条由①负责人与 I 审核。

## 1. 目标

每个 GPS 点在第一次交给大模型（④划分活动、写名称描述）之前，已经有对应的真实地名（地址、最近楼宇名）。

## 2. 决定

1. **新增原始数据类型 `gps_geocode`**（`kind: raw_record`，`writer: adapter`）。一条记录对应一个 `gps_point`，时间与该点相同。`gps_point` 本身不变。
2. **由②下的新进程 `geo_adapter` 写入**，和 `ear_adapter` 并排、独立运行。它用 `Query("gps_point", unreferenced_by=("gps_geocode", "gps_point_id"))` 找出还没有地名的点，定时扫描，不需要订阅；历史点自然补上。`ear_adapter`、手机 app、旧 Ear 都不改。
3. **地图服务可替换**：国内坐标用高德逆地理编码（先把 WGS84 本地换算成 GCJ-02）；国外坐标用 Nominatim 兼容接口（可自建 Nominatim/Photon）。坐标取整后本地缓存、限速。凭据放 `deploy/.env`，用环境变量读取。
4. **地名按原始数据处理**：它只由坐标和地图服务的一次查询决定，不是③推断的用户地点，所以不违反 C07“活动划分/名称描述只用原始数据”。④不得把它当作③的 `place`。
5. **④前置检查**：时间范围内仍有没有 `gps_geocode` 的 `gps_point` 时，④等待（`Waiting("geocode_pending")`），与现有“归属未完成”检查同样处理；`gps_geocode` 并入④交给模型的原始证据。
6. **失败不无限阻塞**（2026-11-13 按负责人本轮容错修复要求同步）：瞬态重试耗尽后停止本轮，不再请求后续点；距该点 `ingested_at` 尚未超过可配置最长等待时不写记录，下轮重试。超过最长等待（`GEO_ADAPTER_MAX_WAIT_SECONDS`，默认6小时）才写 `status=failed`，④照常提取，只用坐标。以 UTC 墙钟判断等待时长。非法坐标不请求地图，直接写 `provider=invalid`、`status=failed`，除关联点 id、provider、status 外的 payload 字段全 null，并继续处理后续点。

## 3. payload（草案，以契约为准）

| 字段 | 说明 |
|---|---|
| `gps_point_id` | 对应的 `gps_point` |
| `status` | `ok` / `no_result` / `failed` |
| `provider`、`language` | 例如 `amap`、`zh-CN`；非法坐标为 `invalid`、null |
| `formatted_address` | 完整地址，可为 null |
| `country`、`province`、`city`、`district`、`township`、`street` | 可为 null |
| `building_name`、`building_type`、`building_distance_m` | 最近楼宇的名字、原始类型和距离（米），可为 null |
| `query_coordinate` | 实际发给地图服务的坐标及坐标系（例如 GCJ-02）；非法坐标为 null |

楼宇规则（2026-11-13 按负责人要求修订，PR #17 未合并，直接替换原地点字段）：

- 高德使用 `extensions=all`，不加 `poitype`。候选为 `aois` 中 `type` 以 `12` 开头的项，以及 `pois` 中 `type` 以 `商务住宅` 开头或 `typecode` 以 `12` 开头的项，包含产业园区、楼宇和住宅区。仅取有名称、距离有效且不超过200米的最近候选；AOI 距离0表示点在其中。`building_type` 保留原始 `type`；无合格候选时三个字段均为 null。
- Nominatim 请求 `zoom=18`；名称优先取 `address.building`，否则仅在 `category == "building"` 时取 `name`。有楼宇时类型取原始 `type`，距离始终为 null；无楼宇时三个字段均为 null。有地址但无楼宇仍可为 ok。

去重键：`{gps_point_id}:geocode:{provider}`。以后换地图服务时写新记录，不覆盖旧记录。

## 4. 不做

- 不在手机端做地理编码（以后可以作为辅助提示另提）。
- 不自动建③地点；用地名给用户新建地点提供默认名字属于以后的功能。
