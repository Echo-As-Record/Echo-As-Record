---
audience: human
doc-type: guide
based-on: docs/design/design-v1.2.md；用户授权本轮开始迁移v1.2并单列契约缺口
---

# ④ 活动提取

根据入库证据划分活动、选择规则、生成 Markdown 和待审核动作建议，消费用户反馈及⑤的重判请求。外部动作由⑥执行。

- 当前目标：[设计 v1.2](../../docs/design/design-v1.2.md#7-活动提取模块④)、[模块间信号](../../docs/design/signals-supplement.md)。
- 运行和接入：[实现说明](ai-docs/implementation.md)。
- 已完成迁移、验证范围与公共契约缺口：[迁移记录](ai-docs/v12-migration.md)。

当前为部分迁移，①使用共享内存实现；mock 联调通过不代表真实模块联调或 v1.2 全部完成。
