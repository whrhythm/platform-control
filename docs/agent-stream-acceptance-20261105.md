# OpenHands 流式回答与命令实时进度

## 用户操作

刷新 LibreChat，选择 OpenHands 后正常发送消息即可。模型生成公开回答时，文字会在原助手回复中逐步增长，不需要打开执行记录。

执行命令时，聊天会显示准备执行环境、运行中和结果整理状态，运行中的输出默认展开，也可手动折叠。任务完成后保留最终回答、退出码和可展开的历史记录。需要审批的操作继续在聊天里确认；没有审批的已授权操作沿用现有设置。

短暂断网或刷新不会重新提交任务，恢复后接回原任务的当前内容。失败或停止后保留已生成的部分回复，并标明未完成。模型在开始生成公开文字之前，仍可能有准备或推理等待时间；流式输出不等于立即出现首字。

## 实现

详细接口语义见 [聊天任务契约](agent-chat-contract.md)。主要路径是 SDK token 回调 → 加密任务进度快照 → 所有者鉴权 SSE → 原聊天回复更新。

当前 SDK 使用 `token_callbacks`；最终回答若位于 `finish.message` 工具参数中，也逐步解码其公开 message 字段。其它工具参数和 reasoning_content 不进入回答。参考 [OpenHands 官方流式示例](https://github.com/OpenHands/software-agent-sdk/blob/main/examples/01_standalone_sdk/29_llm_streaming.py)，实现以集群固定镜像内的实际 SDK 源码为准。

SSE 以最新快照替换视图，不要求用户浏览器永久在线。普通查询作为回退，不覆盖更新的流式状态。读取和重连均无任务执行副作用。

命令输出来自归属核验后的真实 Pod 日志，约两秒观察一次；无法读取时保留上次结果，不自动重跑命令。Python 使用非缓冲输出；其它程序自己的输出缓冲仍可能延迟日志出现。

## 验收证据

- 真实固定 SDK + Postman 合成 OpenAI HTTP 流：在 `conversation.run()` 结束前收到 13 次公开文本更新，首次增量早于结束至少一秒，最终文字一致，没有内部推理字段。
- SDK 原有文件读取与所选 MCP 工具回归通过；未注册本地任意执行工具。
- 定向后端测试与 Postman Kubernetes HTTP 模拟通过：包括未知结果的命令创建只发生一次、观察同一 Job、最终清理，没有重复写入。
- 加密进度覆盖写、跨用户拒绝、命令日志归属、完整和尾部不完整凭据遮蔽、日志超时保留上次输出测试通过。
- 后端完整回归先得到 245 passed、28 skipped，另 7 项因本地 rpds 搜索路径拼写错误失败；修正环境后仅重跑这 7 项，全部通过。合计 252 项通过；跳过项依赖额外外部模拟或 PostgreSQL 环境，未伪报通过。
- LibreChat 构建门禁 209 项测试通过，包含新增 SSE 所有者限定、断开取消、增量先于完成及旧查询不能覆盖新快照；各受影响工作区类型检查通过。
- 指定用户真实浏览器：较长回答在任务结束前出现长度 1 → 102 → 159 的连续增量；首次可见文字约 19.6 秒，完整回答约 27.9 秒、1457 字符。此为单次长回答实测，不是时延承诺。
- 在回答进行中模拟断网约 1.8 秒并刷新，恢复到同一任务，最终只显示一个完整回答，没有新增重复任务。
- 通过聊天请求运行仅打印三段测试标记、间隔六秒的 Python 命令；第一、第二段均在第三段出现和命令完成前可见，最后收到完成回答。未修改用户代码或发布业务。
- 亮色截图已人工检查。原有主题色令牌沿用；这次未声称单独完成暗色全流程验收。
- Argo CD 两项应用均 Synced / Healthy。临时 Postman 模拟、WSL 桥接和 SSH 隧道已关闭，监听端口已确认清理。

浏览器原始数据位于本地忽略目录 `release/agent-stream-ui-result.json`；截图为 `agent-stream-partial.png`、`agent-stream-complete.png`、`agent-command-live-first.png`、`agent-command-live-second.png`、`agent-command-completed.png`。

`npm run lighthouse` 已尝试，但本地缺少 rimraf，在准备阶段终止，没有 Lighthouse 分数。Postman CLI 的 `status` 子命令仍不可用；HTTP 模拟、实际 SDK 和浏览器检查独立完成。

## 发布

- LibreChat `0.8.9-starbridge-20261105.10`，摘要 `sha256:830ebd69694d040e0c99a03ab2302ada77a342fecbac8a49b25279a7717bacd0`，源码提交 `3dcdc2614`。
- 执行器 `0.1.0-agent-stream-20261105.1`，摘要 `sha256:0e8fb85090c3f2fa64146c282619e8106a04ce23c6a5e86d41da3507798d8a13`。
- ops 部署提交 `aa1f6a3`。本次没有数据库迁移。
