# 聊天能力入口与空间分区验收

日期：2026-11-03。状态：功能验收通过，已通过 ArgoCD 上线；独立性能检查尚未完成，见下文。

## 交付范围

- 聊天输入框上方提供 Chat、Coding；Writing、Music 明确未开放。
- 同一会话切换能力，继续沿用已有个人磁盘、身份和模型计费凭据。
- CodeChat 入口隐藏模型选择菜单；兼容历史 OpenHands 会话。
- 我的空间提供 Files、My applications 两页；浏览工程后切换应用页可选择同一工程，聊天保持在主界面。
- 本阶段未实现真实支付、磁盘升级、数据库计量或 Dify。

## 验收覆盖

1. 后端能力别名映射、错误配置拒绝、旧路由兼容，以及实际 HTTP 路径上的历史文本和凭据转发。
2. 前端类型检查、局部规范检查、切换保留会话字段、执行中禁用、未来能力禁用和文件/应用分区测试。
3. 用已指定账号从 LibreChat 普通聊天说明工程需求，切到 Coding 创建实际文件并测试；刷新恢复能力；回到普通聊天再切 Coding 修改同一文件并再次测试。
4. 文件页不显示应用卡片，应用页不显示文件操作；工程选择保留；原来已发布服务仍可访问。

## 真实用户界面结果

仅使用指定账号 `1321670187@qq.com`，全部编程指令从 LibreChat 聊天输入框发出；没有用后台写文件脚本代替产品功能。

1. 空白聊天直接显示 Chat、Coding，不要求先选择模型；Writing、Music 显示 Coming soon 且不可点击。
2. 在 Chat 说明工程目录与计算/测试需求，切到 Coding 后只要求按上文实现。实际生成 `codechat-capability-mw0237cd/main.py`、`test_main.py`，运行 unittest 通过，返回值为 13。
3. 刷新后保留同一聊天与 Coding 选择；从文件页预览真实代码。切应用页沿用该工程路径，提示该纯代码工程还缺少 Dockerfile；文件按钮与应用卡片分别位于两个页签。
4. 切回 Chat 后能正确说出前面的工程名称；刷新后恢复 Chat。再切 Coding，要求将原结果增加 1，修改同一目录中的文件和测试，实际 unittest 通过，结果为 14。
5. 重新登录并打开同一会话，Coding 仍恢复；通过文件预览核对测试实际包含 14。聊天 ID 为 `07f89216-dda6-5c2f-a382-95b46c5f75fb`，没有因能力切换生成新聊天或磁盘。
6. 从应用页取得原服务地址并实际访问，返回 `Hello CodeChat v3`。本轮没有重新发布该服务；构建、推送、部署、失败清理与旧版本恢复的独立验收见 [上一阶段](space-management-acceptance-20261102.md)。
7. 在 My applications 中选择已有 Dockerfile 的 `codechat-demo-20261102`，实际显示 `Publish this project` 按钮。这里只检查入口，没有触发额外发布。

个人磁盘仍为原 `workspace-data`，2 GiB、Bound，UID `19e17f16-8d3b-43ff-9480-9baa3d0c8faa`。本阶段保留原文件和既有应用。

## 部署记录

- LibreChat 源码：`5c1340556`，分支 dev；镜像标签 `0.8.9-capabilities-20261103.6`，摘要 `sha256:d3cc4617cf7fbcbf55499f320219c6f3cc1b801a9feebc117eb15922e6bd75f7`。
- 平台后端源码：`eb0252f`；镜像标签 `0.1.0-capabilities-20261103.1`，摘要 `sha256:8cbe7ebd5c8226f9810b655f40cbe13d7c6a06122d73f4111242e563f1da5e95`。
- GitOps 先部署兼容旧路由的后端，再启用前端能力入口；最终应用清单版本 `b2545b0`。platform-control 与 librechat 均观察为 Synced / Healthy，新镜像的 Deployment generation 已被控制器观察，实例可用。
- 用户文件服务和轻量 SDK 仍沿用上一阶段的镜像及个人卷，本次没有要求重建空间或重新发放模型预算。

## 已完成的构建与回归

- Python 全量回归：140 通过、14 跳过；跳过项依赖单独的 PostgreSQL/Postman 测试环境，本阶段没有新增数据库结构。
- 网关专项目标：12 通过、1 跳过，包含 Chat/Coding 与旧 OpenHands 路径上的真实请求对象、顺序历史、凭据、目标地址和错误配置拒绝。HTTP 单元测试使用本地 MockTransport，不能替代真实模型调用验收。
- LibreChat：data-provider、packages/client、packages/api、client 的独立类型检查通过；本次 12 个相关源文件的局部 ESLint 通过。模型凭据 2 项、身份/文件接口 10 项、共享对话框 2 项、CodeChat 用户界面 22 项通过。
- 额外覆盖：新会话沿用空白 Agents 默认配置时，无需先选技术端点；已有其他端点会话切到 Coding 后保留原会话 ID 与消息引用。
- 管理端在独立目录执行 npm ci、类型检查和生产构建通过。
- npm run lighthouse 已执行，前端构建通过；性能测试启动时测试容器缺少 openssl，未得到 LCP 指标。该项未完成，需要在具备测试依赖的独立环境重跑；原始日志保留在忽略的 release 目录。

## 上线前发现并修复的问题

首次实际浏览器验收发现空白聊天继承 Agents，能力入口被端点条件隐藏。最终修复按显式启用的 CodeChat 配置显示入口，并仅对空白新会话初始化聊天目标；已有会话由用户选择能力，不自动重建或改写历史。只通过组件测试或 Deployment Ready 均不足以宣布产品流程完成。
