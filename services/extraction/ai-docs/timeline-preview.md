---
audience: ai
doc-type: guide
based-on: 设计 v1.2 §7.1、已确认决策 C07；本分支 timeline_preview.py、reviewed_timeline_handoff.py 与合成回归
---

# 离线时间线边界预览

`ear_extraction.timeline_preview` 不连接 Storage、旧 Ear 或网关。一个评估范围可以放在同一条时间线上，按有界重叠窗生成复核材料，随后用人工标注或已有的合成审阅回复组装结果。离线 CLI **不包含模型调用器、联机预览端点或写入器**，不是部署或重跑指令。宿主可通过[另行配置](automatic-timeline.md)复用相同规划和逐窗校验。

## 输入与命令

准备锁定的开发环境。在权限受限的私有目录中使用已授权投影。不要把转写、请求、回复或真实标注写入 Git、公共目录或日志。

```bash
umask 077
UV_OFFLINE=1 uv run --no-sync python -m ear_extraction.timeline_preview plan < timeline.json > plan.private.json
UV_OFFLINE=1 uv run --no-sync python -m ear_extraction.timeline_preview assemble < completed.private.json > preview.private.json
```

- `timeline.json` 是对象：`scope` 为非空 UTC `[start,end)`，`coverage` 为与源录音对应、已选定且不重叠的有序区间，`transcripts` 为有序的 `{start,end,text}`；起止相同的行不支持。`withheld` 是明确隔离的区间列表（无隔离填 `[]`）。已有未核清调用的全部覆盖应从输入文字排除，**不可**通过换窗或扩大范围重发；此工具不会自动查询运行审计或替调用方证明只读范围。缺失覆盖也不能用推测填回。
- 规划最多接受 32 小时、20,000 行、256 个视图（CLI 输入最多 32 MiB）。每窗最多 96 个核心行，前后各带最多 24 行；同一转写跨窗沿用同一序号，带 `clipped` 的摘录只展示前缀。投影仅含 UTC 时间、局部序号、摘录与完整性标记，不带真实 ID、说话人、设备信息、声纹、GPS 或文件路径。摘录仍可能含可识别文字；创建规划不授权给任何模型或服务发送数据。
- `plan.private.json` 给出 `windows`、`instructions` 和 `reply_schema`。单窗复核须带该窗口的 `digest`；指纹只绑定可见输入，不证明隐藏的剪裁后缀或真实世界正确性。最终输入 `completed.private.json` 是 `{ "timeline": <原 timeline>, "reviews": [<每窗一份复核>] }`。默认缺回复、重复或错用指纹都拒绝。人工抽查可显式设 `"allow_partial": true`，直接省略未审窗；输出单列未审核心区间、未审核心行数及其中未被已审重叠窗覆盖的行数，后者不计入已审未分类行数。明确审阅后无可判定片段才用 `spans: []`，不能用该值冒充缺失/发送结果未知。

## 候选与证据边界

- 录音或计算接缝默认不是换场。合并候选需要同一窗的连续片段及其跨边两侧原话锚点。边缘行若只是语气词，可显式用 `continuity[].after_row` 指定边，在同一连续片段内分别引用边的左右 24 行、各三分钟以内的可见原话（未指定则仍要求引用紧邻两行）；内容主题一致、同一角色标签或时间相近不够。换场提案也需要双侧原话。意见冲突、证据不足、转写时间重叠均不能认定换场。只校验引文在对应可见行中，不替人证明其解释正确。
- 中间的已导入转写可被共享窗口补入；没有可定位证据的行仍未分类。没有选中的源覆盖、整窗无可用转写、超过五分钟的无文本区间、被隔离的时间都单独展示，不把这些区间视为静默、换场或延续。时间只来自纳入的转写。
- `engagement` 独立于主题：参与、观看／收听、背景播放、未确认。非未知类型必须有相关的原话引文；听起来像讲座、剧情对白或设备在播不证明用户参与或在看。冲突不投票猜测；无线程、设备或音频证据时，宁可保留 `unknown`。预览候选包含非参与片段，它们不是新活动。
- 组装返回候选范围及行号、跨窗接续、换场提案、证据不足、覆盖问题、角色冲突和重复时间提示；不会回显转写、引用句或输出 Markdown。本结果不符合业务重提取草稿形状，不能直接用于替代旧活动。

合成回归覆盖跨文件/计算窗同一任务、接缝附近漏段、剧情与技术内容换场、参与/播放分类与冲突、无文本区间、隔离、错配和重叠；亦验证假数据全天规模的窗口数量和唯一计数。它验证程序如何处理**给定的复核意见**，不估计模型或真实分割准确率。真实边界质量及实际数据验收不在本说明范围内。

## 稀疏重提取的私有交接

仅适用 [④ 宿主门控](../README.md#私有交接复用完整复核的稀疏时间线)。`ear_extraction.reviewed_timeline_handoff` 是离线 Python helper，不查询 Storage、不发模型请求、不写文件：

1. 准备原允许窗、对应全部 `RawRecord` 转写和[可信预检输入](reextract-preflight.md)。预检输入须包含完整采集边界、隔离记录和当前旧活动/产物/规则选择元数据，不得根据审核草稿倒推旧版本。`project_timeline(windows, raw, preflight)` 生成预览输入；使用完全相同的投影完成所有预览窗的复核。
2. 人工从组装结果选定边界可靠的候选和本地标题/描述。给 `build_reviewed_timeline_draft(windows, raw, preflight, completed, spans)` 的 `spans` 是有序 `ReviewedTimelineSpan`（和原课堂草稿一样的 `first`、`last`、准确 UTC `start`、`end`、`title_md`、`description_md`，可选 `kind`）。helper 重新调用 `assemble_timeline`，拒绝部分复核、非原候选、未知或背景播放、冲突或结构不明的边界；只选证据充分的参与或观看／收听候选。未分类邻行若与已选活动时间重叠，同样阻止交接。不能安全交接的旧家族不允许因为删去候选而消失。
3. 只将返回对象的 `model_dump(mode="json")` 置于宿主原有的 `0600` 私有配置的 `transcript_course_reextract_timeline`，不要放进反馈、Git 或日志。范围取原允许窗首尾；反馈仍使用公开预览的完整 `versions`，其中没有私有规则选择版本。私有配置不应包含原始转写、摘录或复核回复。

`input_sha256` 按 `_window_rows` 的唯一行顺序绑定每窗 UTC 起止、每窗和全批行数，以及每行 `(id, start, 原始 end, text)`，包括未选行；不使用预览摘录或被选窗截短的 end。`draft_sha256` 绑定该输入指纹、全部选中 spans（含标题描述）和完整的预检版本钉。二者用版本化前缀和规范 JSON；应由 helper 生成，而非手工计算。它们在只允许宿主写入的配置边界内检测变化，不是对预检来源或人工审批的密码学证明。
