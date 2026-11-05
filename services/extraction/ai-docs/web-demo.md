---
audience: ai
doc-type: guide
generated-by: Codex
generated-at: 2026-11-05
based-on: 初版MVP实现f3c28db；用户授权在回退后的MVP上新增网页模拟演示
---

# 网页模拟演示

## 启动与打开

在服务器的仓库目录运行：

```bash
cd ~/Echo-As-Record
uv sync --locked
uv run ear-extraction-web-demo --port 8765
```

VS Code Remote SSH 的「端口 / Ports」面板中点击「转发端口」，填写 `8765`，再打开转发后的本地地址（通常是 `http://localhost:8765`）。如果本地端口已占用，以 VS Code 显示的本地地址为准。服务仅监听服务器 `127.0.0.1`，无需开放公网端口。

不用 VS Code 时，也可以在本机终端执行 `ssh -L 8765:127.0.0.1:8765 用户名@服务器地址`，保持连接后用本地浏览器打开上述地址。Ctrl+C 停止服务；重启会清空临时数据。

## 展示顺序

1. 左侧展示合成转写、说话人甲和提取规则，点击「生成活动与待办」。
2. 中间出现活动与 Markdown 待办；右侧出现待审核提醒。
3. 点击「纠正为乙」：待办负责人更新、产物升为版本 2；活动不重划，未审核旧建议被替代。中间显示更新前后对比。
4. 点击「审核通过」，再点击「模拟执行提醒」：出现模拟提醒卡片，执行回执更新建议状态。
5. 展开下方任务状态及原始数据，查看活动/产物/建议与任务记录。
6. 点击「重置演示」重新开始。也可演示拒绝建议，或先审核再纠正，观察已审核建议保持不变。

每个浏览器 cookie 会话独立；同一浏览器多个标签页共用场景。刷新保留状态。会话无操作两小时回收，同时最多保留 32 个会话。

## 实现与验证边界

- `web_demo.py` 为独立演示入口，不修改④核心提取逻辑。使用真实 ExtractionService、共享 MemoryStorage 和固定 DemoGateway 回复。
- 用户反馈由 api 身份入库，纠正由模拟 attribution 身份写入；模拟 dependency 通过依赖查询发送重判请求。模拟外部执行使用内存记录和④回执流程，不访问手机、日历或模型服务。
- HTTP 接口只有局部演示作用，不作为⑥正式 API；仅支持固定合成场景，不支持上传录音、编辑任意规则或真实数据处理。
- 初版产物仍存人物 ID 引用，页面展示时替换成名字。没有恢复 v1.2 迁移。
- 页面用原生 HTML/CSS/JavaScript，无 CDN。源文件在包内 `web/`，Python 直接提供这些资源。
- 前端构建检查：进入 `services/extraction/src/ear_extraction/web`，执行 `npm install`、`npm run build`；校验 JS 与资源引用，并复制静态资源到被忽略的 `dist/`，无需打包器。
- 自动回归：`uv run pytest services/extraction/tests/test_web_demo.py -q`。另使用真实浏览器验证完整流程、刷新、重置、拒绝、会话隔离及窄屏显示。
