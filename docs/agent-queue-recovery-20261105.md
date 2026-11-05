# OpenHands 长时间等待执行：原因、修复与验收

## 原因

用户提交简单问题后，聊天显示“等待执行”，没有回答。执行器持续记录 `Agent reconciliation unavailable type=ValidationError`。

只读检查实际容器发现：API 的套餐配置已经包含 `code_runs_monthly`、`code_timeout_seconds`、`code_concurrency`，但 agent-worker 仍使用不认识这些字段的旧镜像。`effective_policy()` 在认领事务中校验失败，事务回滚，任务一直保持 queued；两秒后的下一次调度再次失败。Pod 显示 Running 并不说明任务可执行。

## 修复

1. 先通过 ops/Argo CD 对齐执行器镜像与已上线套餐字段，恢复原来排队的任务。没有直接更新任务数据库状态，也没有重新提交用户请求。
2. 执行器遇到确定性的套餐配置结构校验错误时，保存 failed、`agent_configuration_invalid` 和最终说明，明确“尚未执行、文件未修改”。不输出配置内容、凭据或校验输入。不将数据库临时连接错误当作这类永久错误。
3. 聊天显示“等待编程执行器接单”和“模型正在准备回复”；排队超过 30 秒显示延迟提示。无文件或命令结果时隐藏空的记录折叠框。
4. 在仓库发布要求中加入共享配置的读取方兼容性核验，不能只检查 API 与 Pod 存活状态。

当前最终回答在本轮完成后展示，任务状态每两秒刷新；这次没有实现模型 token 流式输出，也没有更改审批、Kata、配额或文件隔离边界。

## 验收

- 本地编程任务测试：20 passed、1 skipped。跳过项依赖未启动的本地 Postman delivery simulation，未宣称本次重新验证完整部署链路。
- 新增回归用例使用真实配置模型复现未知套餐字段：任务只失败一次，生成最终事件，不访问模型/工具，不阻塞用户创建下一项任务，不记录测试密钥和管理员字段值。
- LibreChat 镜像构建、类型检查和回归测试通过；新用例验证延迟排队提示、切换为模型处理提示及隐藏空记录。
- 原任务自动恢复并完成，开始执行到完成约 16 秒，历史聊天可读取其最终回答。
- 使用指定用户从 LibreChat 聊天界面发送一条仅介绍能力、不操作项目的测试问题：任务卡片约 660 ms 出现，模型准备提示约 3123 ms 出现，完整回答约 6999 ms 出现。事件只有 queued、started、finished，没有文件或命令操作。
- 这些时间是一次局域网实测，不是服务延迟承诺。原始脱敏采样结果在本地忽略目录 `release/agent-feedback-qa-result.json`，截图为 `release/agent-feedback-result.png`。
- Argo CD 两项应用均 Synced / Healthy。新执行器日志不再出现原配置校验循环。

此次根因是本地持久化配置与部署版本不兼容，不是 HTTP 依赖失败，因此使用真实结构校验回归和线上任务验收，没有为它建立不相关的 HTTP 模拟。已读取 Postman API Engineer / API Simulation 指引；本地 CLI 的 `postman status` 返回 unknown command，未使用该命令结果作为接口通过证据。

## 发布版本

- 执行器：`0.1.0-agent-queue-20261105.1`，摘要 `sha256:8550dbf2658594264b8e8157ff95f6981369480754d2b86c2073f7319025b1bf`。
- LibreChat：`0.8.9-starbridge-20261105.9`，摘要 `sha256:d7431c4e57e47d7ecd1fb2dde286a070e686a5316a7ebb19bbe4d022f0ab54c0`。
- 后端代码提交 `187b0b3`，前端代码提交 `82484817b`，部署提交 `a37a181`。恢复服务的前置部署提交为 `80aad21`。
