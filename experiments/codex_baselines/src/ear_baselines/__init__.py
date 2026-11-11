"""Codex 基线实验：把录音变成三种可以交给 Codex 的输入，并用同一个目标运行 Codex。

- A1a：原始音频交给能听音频的模型（OpenAI gpt-audio），得到「听写记录」，再交给 Codex。
- A1b：原始音频文件直接放进 Codex 的工作区，让它自己想办法。
- A2：旧 Ear Evidence（转写、局部说话人、环境声、GPS、手机信息）渲染成 Markdown，交给 Codex。

真实录音和输出都放在 experiments/codex_baselines/local-data/（git 忽略），不进仓库。
"""
