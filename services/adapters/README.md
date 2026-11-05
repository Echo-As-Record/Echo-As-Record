---
audience: human
doc-type: reference
---

# ② 适配器

把外部数据转成统一输入格式写入 ①，只做确定计算，不做推断。MVP 的录音、转写、局部说话人、声纹、环境声、GPS 和手机信息，全部来自旧 Ear 的 Evidence 只读 API：旧 Ear 照常从 Brain inbox 接收文件并跑语音、声纹、环境声模型，② 读取它的结果，不改旧 Ear 的处理逻辑（设计 3.4「封装、不重写」）。

- [职责与验收](../../docs/design/work-breakdown-v1.2.md)（② 适配器一节）
- [数据流、导入规则与验收方法](ai-docs/implementation.md)
- [各类型 payload 字段](ai-docs/payloads.md)，供 ③④ 使用

当前状态：转换、导入规则和测试已完成；① 的持久化服务尚未实现，命令行只能试运行（写进内存存储）。旧 Ear 的「列出录音」接口在旧 Ear 的合并请求里（未合并），本机已用它跑过一次真实数据试运行，结果见实现说明 5.2。
