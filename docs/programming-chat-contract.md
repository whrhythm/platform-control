# LibreChat 编程对话接入

普通用户继续在 LibreChat 中聊天。启用能力入口后，在输入框上方选择 `Chat` 或 `Coding`，同一输入框分别用于讨论需求或驱动真实编程工具。文件树读取已分配的个人 PVC；切换能力、聊天和重新登录不创建第二块磁盘。写作和音乐显示为未开放，尚未接入 Dify。

## 边界

- `POST /agent/v1/chat/completions`：OpenAI chat completions 格式。Authorization 使用自动分配的 LiteLLM virtual key；服务端向 LiteLLM 查询该 key 的哈希，确认关联身份和平台记录。客户端不能指定其他用户的空间。
- 启用时，`model=codechat-coding` 发送到该用户 Kata 环境中的轻量 SDK 执行进程，工作目录 `/workspace/files`。这是执行进程，不启动每用户完整 OpenHands Web 服务。`model=codechat-chat` 由服务端映射到管理员允许的真实 LiteLLM 模型；浏览器不能配置目标 URL 或绕开用户 virtual key。旧的 `OpenHands` 及允许的真实模型名继续兼容已有会话。
- `X-CodeChat-Conversation`：LibreChat 会话 ID。会话 ID 只参与任务去重，不能授权磁盘访问。请求体最多 512 KiB；仅接受文本聊天，上传的工程文件通过“我的空间”读取。
- 执行进程 `POST /v1/run`：仅集群内部、TLS、个人服务 token。请求包含经过验证的模型配置和对话文本。返回 OpenAI SSE；先返回正在执行提示，再返回工具进展和最终回答。每个空间同一时刻执行一个任务，忙时返回 409。
- 任务 ID 由会话 ID 和完整请求内容生成。同一任务重试读取已完成结果；状态不明的任务拒绝自动重跑，防止重复修改。用户发送新消息可建立新任务。
- SDK 事件和输出留在个人磁盘的内部状态目录，平台日志不记录源码、用户提示词或 key。编程子进程退出时清理临时 SDK 会话；用户文件持续保留。
- 用户停止生成或连接断开时，网关关闭执行流，执行环境调用 SDK interrupt 取消异步 arun；每轮最多 180 秒，超时同样请求中断，工具清理可能短暂延后。已写入的文件继续保留。

## 能力切换与上下文

能力切换只修改下一轮的执行目标；保留 conversationId、消息父子关系、聊天项目和工作区字段，不执行“新建聊天”。执行中禁止切换，避免当前任务与下一轮目标混淆。普通聊天没有文件执行器；需要读取实际文件、测试或发布时选择 Coding。

当前两个能力与两个稳定路由别名一一对应。复用已有会话的 model 字段保存别名，每次发送后写回会话；重新打开或刷新根据已保存别名恢复。只点击标签但尚未发送属于当前页面选择，不承诺跨刷新保存。新增能力或改变映射必须保持旧别名兼容；后续 Dify 工作流版本和独立子会话绑定另行设计，不能直接覆盖旧映射。

两种能力复用同一消息分支。LibreChat 仍按上下文窗口截断；能力选择将上下文窗口设为配置值，当前 32000 tokens。网关接受的编程请求上限为 512 KiB、200 条文本消息，超限明确报错。这不等于无限历史记忆，也不会自动读取用户整个磁盘。Coding 实际按需读取个人工程文件；普通聊天只知道发送的对话文本。

配置：librechat.yaml 的 `codechat.capabilities` 包含 enabled（默认 false）、endpoint、chatRoute、codingRoute、maxContextTokens。Python 网关使用 `PLATFORM_CAPABILITIES_ENABLED`、`PLATFORM_CAPABILITY_CHAT_ROUTE`、`PLATFORM_CAPABILITY_CODING_ROUTE` 和 `PLATFORM_CAPABILITY_CHAT_MODEL`。两边路由名必须一致；真实聊天模型必须在 `PLATFORM_LITELLM_MODELS` 中，别名冲突或缺少映射会拒绝请求而非误路由。

“我的空间”分为 Files 和 My applications。文件浏览路径由当前用户的功能状态保存，切换到应用页时沿用该路径，或从目录下拉框选工程；有 Dockerfile 才提供发布。两页切换不改变聊天路由，不删除文件。应用失败通知在空间折叠时仍继续观察，点击后打开 My applications。

## 本次验收

使用账号 `1321670187@qq.com`，从 LibreChat 发消息创建一个独立示例工程，生成文件、执行测试，再发消息修改并重新测试。通过文件树查看真实文件。构建、推送和发布另设执行记录，只有实际观察到服务可访问才算发布成功。

## 后续发布边界

发布使用独立 Kata 构建任务、Harbor 项目 robot、应用实例记录和 Kubernetes 控制器。编程工具提交发布后立即结束本轮，执行器识别回执后不再让模型自动轮询/重试；同一任务 ID 重复提交不生成新构建。应用列表、日志、CPU/内存占用已通过真实界面验收，见 [交付记录](application-delivery-acceptance-20261102.md)。不能用元数据计划或已提交状态冒充已部署应用。
