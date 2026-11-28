---
audience: ai
doc-type: reference
based-on: 11771ed 的窗口化代码、固定旧 Ear eef64ce 的本地 provider、2026-11-27 的隔离合成语音验收
---

# 原生录音：离线识别验收与排查

## 已验证路径

输入是 FFmpeg Flite 合成的重复英文短句，不是用户录音。Linux 上的实际 `FFmpegDecoder` 读取 AAC/M4A，经 `ffprobe` 检查并用 `fd:` 解码，得到 112000 帧、16 kHz、单声道 PCM16LE。规范 WAV 的 SHA-256 为 `5442738629b3a578d4645d51b8c7ebd049c91803d6fbade6737f7fd7120bbf52`。将这份非静音 WAV 作为 Mac 独立 worker 的标准输入，经进程退出码和 JSON 检查后，把真实回执给 Linux 的 `_result` 和 `ingest_native`；再次解码并逐字节核对是同一份 PCM。复验时由 Linux `RecognitionProcess` 启动一次性、无监听的私有文件交接程序；核对它收到的 WAV 哈希，再返回同次 Mac 真模型运行的完整 JSON，实际经过进程传输、解码、协议校验和内存写入。私有 SSH 只负责测试文件交接，模型进程没有网络。此跨机验收**不代表** Mac 可以直接运行 Linux 的 `RecognitionProcess` / `FFmpegDecoder`，也不代表端到端单机验收已通过。

| 项 | 隔离验收值 |
| --- | --- |
| 基础源码 | `eef64ce933fc06e9a5e441a546cb2265f1394ef7` 的 `ear/` Git archive |
| MOSS | 缓存快照 `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`，MOSS 辅助包来自已缓存 0.1.0 wheel |
| ECAPA | 缓存快照 `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` |
| PaNNs | 缓存 `Cnn14_DecisionLevelMax.pth` 和 527 类标签 CSV |
| Mac 运行时 | arm64 Python 3.12.13、torch/torchaudio 2.11.0、transformers 5.14.1、speechbrain 1.1.0、panns-inference 0.1.1 |
| 实测 | CPU 2 个 intra-op 线程、1 个 inter-op 线程，MOSS FP32；带阶段探针 109.8 秒，无诊断钩子复验 127.1 秒、抽样峰值 RSS 约 2.32 GiB；两次结果 JSON 的 SHA-256 完全相同 |
| 结果 | 1 个有时间戳的真实转写段、1 个局部说话人和 1 个有限且 L2 归一化的 192 维 ECAPA 向量；PaNNs 执行完成，输出 0 个事件；4 条记录经标准校验进入同一个 `MemoryStorage` 批次 |

语句并非逐字正确；一次合成测试不能评价中文、重叠、真实录音、长录音吞吐或 PaNNs 正类召回。30 分钟音频的跨窗口实现此前经替代进程覆盖，不能当成 30 分钟真实模型验收。

## 启动设置和约束

独立打包受信任的旧源码、Python/依赖与精确模型资产，保留逐文件哈希、依赖锁和脚本哈希；模型 profile 应随资产、数值精度、解码器或关键设置变化。OS sandbox 先启用，再用隔离 Python `-I -B` 启动 [worker](../../../tools/ear_legacy_recognition_worker.py)。确认允许读取包、禁止访问包外测试哨兵，并实际验证无回环或外部网络访问、包不可写、仅私有 scratch 可写。模型进程不继承生产环境变量，不访问真实录音、旧 Ear HTTP、存储服务或凭据。在 Mac 的验证包上，应用沙箱限制文件读取、擦除环境和网络；外部 watchdog 限制墙钟并以 RSS 抽样止损，后者**不是**内核级内存上限。Mac 验证器不属于现有部署。Linux 上继续使用网络命名空间、只读资产规则和内核资源上限；不要只传 `--offline-isolated` 就视为隔离。

```text
<isolated-python> -I -B tools/ear_legacy_recognition_worker.py \
  --offline-isolated --device cpu --cpu-threads 2 --moss-cpu-fp32 \
  --model-profile <exact-bundle-profile> --source <pinned-source-export> \
  --moss <moss-snapshot> --ecapa <ecapa-snapshot> \
  --panns-checkpoint <checkpoint> --panns-labels <labels> \
  --started-at <UTC-start> --ended-at <UTC-end>
```

只有退出码 0 且完整通过 JSON、时标、引用校验才消费结果；空音轨不是验收。CPU 配置用 CLI 传入，外部 shell 的线程环境会被 worker 擦掉。FP32 仅影响 MOSS，增加内存占用，非默认，不应直接用于共享 VM。复验只开一个请求，正常调度，外部 235 秒硬截止（worker 自身 240 秒；Linux 传输 245 秒），Mac watchdog 在 RSS 抽样超过 4700000 KiB 时终止。一次 nice +10 的无诊断运行到 170 秒被看门狗终止，不将 127 秒当成保证。Scratch 必须包含 Hugging Face 动态代码目录、TorchInductor、Numba、Matplotlib；Torch 2.11 的 TorchInductor 在导入时会查默认缓存目录，Mac 沙箱不能查询用户目录时会抛 `getpwuid` 的 `KeyError`。显式指定私有 cache 解决该导入错误，不是放宽沙箱。首次运行可以包含动态模块准备和 JIT；不要让它写原缓存。

## 超时的证据

原尝试 95 秒和 180 秒均以 124 退出；第二次过了 bridge 初始化，该日志不能单独确定卡在哪一条算子。调整线程/绑定后的 Linux BF16 探针约 2.6 秒进入 ASR、约 7.7 秒完成 MOSS 加载；到 220 秒有界终止仍未返回结果，scope 统计约 208 CPU 秒、787 MiB 内存峰值。该 KVM 客体未暴露 AVX/AVX2/硬件 BF16；不是已证明的整机 CPU 仿真。Mac 上 BF16 的栈采样定位到 Whisper encoder 的 SDPA prefill，而不是 shell 或模型下载；FP32 才完成这次合成样本。不能把调线程、加时限或常驻化单独当成修复。现场 P104-100 正有负载，未占用它；Mac Metal 未作为本次验收的独立 GPU 使用。

## 持久 worker（建议，未实现/未采纳）

先用短样本分别测冷启动、加载、prefill、解码、ECAPA 和 PaNNs；再测同包热跑、内存峰值和连续窗口。若冷启动占比高，考虑让同一个受限进程缓存只读模型，而不是为每个窗口重新导入。用有长度上限的继承管道，不监听网络；单请求在途、有界队列。保留现有 PCM/UTC 校验、每窗独立说话人作用域、完整覆盖与全录音原子发布。每个请求用独立 scratch 和 artifact 引用；不跨请求缓存音频、转写或声纹。超时由父进程停止并回收整个进程组，无部分输出就无写入；内存增长或固定最大任务数触发进程轮换。验收至少覆盖两次同输入冷/热一致性、超时后新进程不串数据、异常退出无残留，以及短/长样本阶段耗时。若主要成本是 Whisper prefill 或解码，常驻不能解决，先评估合适的独立算力和精度。
