---
audience: human
doc-type: reference
---

# ② 适配器

把外部数据转成统一输入格式写入 ①，只做确定计算，不做推断。MVP 的录音、转写、局部说话人、声纹、环境声、GPS 和手机信息，全部来自旧 Ear 的 Evidence 只读 API：旧 Ear 照常从 Brain inbox 接收文件并跑语音、声纹、环境声模型，② 读取它的结果，不改旧 Ear 的处理逻辑（设计 3.4「封装、不重写」）。

- [职责与验收](../../docs/design/work-breakdown-v1.2.md)（② 适配器一节）
- [数据流、导入规则与验收方法](ai-docs/implementation.md)
- [各类型 payload 字段](ai-docs/payloads.md)，供 ③④ 使用

当前状态：转换、导入规则和测试已完成。① 的持久化服务已实现并随 PR8 合入 `dev`（376294d；远程 CI 通过，隔离部署切片非生产，尚无生产启动，见[① 存储](../storage/README.md)），但 **② 的命令行仍只写内存存储**（`MemoryStorage`），未接入 ① 远程服务，没有对真实 ① 服务的落库联调；③④⑤ 也未完整业务联调。旧 Ear 的「列出录音」接口在旧 Ear 的合并请求里（未合并），本机已用它跑过一次真实数据试运行，结果见实现说明 5.2。
