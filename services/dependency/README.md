---
audience: human
doc-type: reference
based-on: 设计 v1.2 §8；信号补充 §3.4；工作分解 v1.2 ⑤节；origin/dev 8750a83（本目录尚无代码）
---

# ⑤ 依赖分析模块

输入变化后找出确实过时的结果，等相关上游完成，再请求所属模块重新判断；全程记录传播，供追溯、展示和论文实验统计。⑤是“请求重新判断”事件的唯一发出方，只写传播记录，不代写任何业务结果或插件登记。

- 目标行为：[设计 v1.2 第 8 节](../../docs/design/design-v1.2.md)、[信号补充 §3.4 与 §4](../../docs/design/signals-supplement.md)
- 职责与验收：[工作分解 v1.2 ⑤节](../../docs/design/work-breakdown-v1.2.md)
- 对外接口提案（传播记录、按钮反馈、①权限，**proposed**）：[⑤接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)
- 实现设计与任务拆分（**草案**）：[实现设计](ai-docs/design.md)
- 用到的①接口：[① SDK 指南](../storage/ai-docs/interface.md)

## 状态

**未实现。** 本目录只有文档，没有代码。①侧基础能力已在 dev 上（PR #8）；③④尚未接入完成协议，⑤先用合成所属方和测试插件开发与验收。部署还没有⑤的身份，插件类型的读权限也待①确认，见[提案第 4 节](../../docs/proposals/dependency-scheduler-interfaces.md)。

## 两个入口

| 入口 | 行为 |
|---|---|
| 已有数据被修改或整体替代（经历史门控筛选） | 找依赖它的当前有效、确实过时的结果，按所属方和是否支持重判发请求 |
| “重新判断其他相关记录”反馈（⑥按钮或 MCP） | 引用纠正反馈 id 时先等纠正生效；按纠正前后对象找过时归属并请③重判。无效引用明确拒绝，重复请求不会重复重判 |

新原始数据、规则编辑本身、被历史门控拦截的参考变化都不触发重判。

## 等待③确定的字段

按钮入口要读被纠正数据的当前归属，用到③归属 payload 的字段名（`subject_id`、`target_id` 等），这些在③那边仍标“待确认”。在③确定前，⑤按①草案的字段名实现，并标注“待③确认”。

检查③是否已经确定：

```bash
uv run python services/dependency/scripts/check_attribution_fields.py
```

它会 `git fetch origin`，然后只读地检查 `origin/dev` 和③分支上的几个信号：登记表注释、归属 Schema、③代码合入、③开发文档的 P1 状态。退出码 0 表示有进展需要核对，1 表示仍在等待，2 表示检查出错。加 `--json` 输出机器可读结果。
