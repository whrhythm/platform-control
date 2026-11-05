# 个人空间、用户管理与代码运行验收（2026-11-05）

## 七项交付

| 请求 | 实现与实际验收 |
| --- | --- |
| 数据库占用比对齐 | 数据库、CPU、内存使用一致的指标排版；浏览器测量三条进度条的左边缘与宽度差异小于 1 px。 |
| 文件预览和编辑合并 | Monaco 同一窗口切换预览与编辑，显示行号、语法高亮；上传 Python 文件、修改、预览、保存、重新打开、未保存关闭提醒全部通过。 |
| 我的空间默认宽度 | 沿用左侧展开栏宽度；实际浏览器测得左右均为 360 px。仍支持手动调整。 |
| 编程设置移除执行记录 | 实际页面确认设置中不再显示执行记录；任务进度和审批结果继续显示在聊天。 |
| 独立代码运行服务 | Argo CD 部署 ClickHouse/code-interpreter；指定用户点击聊天代码块的「运行代码」，实际接口返回 `stdout:\n42`。 |
| 管理员管理用户 | akadmin 在「用户与资源」中创建临时普通账号、修改姓名并禁用，保存成功且平台访问限制可见。 |
| 资源详情浮窗 | 点击「查看详情」显示模态窗口，Escape 关闭通过。 |

普通用户验收仅使用指定的 `1321670187@qq.com`。临时文件通过界面上传和删除，没有删除真实项目文件。管理员验证使用 akadmin；临时账号 `qa-account-20261105` 留在禁用状态，未以该账号进行普通用户验收。

亮色与暗色主题均检查了编辑器。浏览器状态、凭据、截图和原始响应仅保存在 Git 忽略的 `release/` 中，不随源码提交。主要证据文件：

- `editor-light-preview.png`、`editor-dark-preview.png`
- `programming-settings-no-history.png`
- `application-metrics-aligned.png`
- `codeapi-ui-executed.png`、`codeapi-ui-result.json`
- `accounts-management-verified.png`、`accounts-resource-modal.png`

## 自动化验证

- 控制面 pytest：242 通过、27 跳过；独立 PostgreSQL 测试：13 通过。
- Postman 本地 HTTP 模拟：真实身份客户端调用模拟身份目录，覆盖正常创建与启用、远端已写入但返回 503 的不明确结果；确认不自动重复写入、维持访问限制并隐藏服务诊断信息。
- 管理前端构建通过；LibreChat 镜像构建包含相关工作区类型检查、ESLint 与 178 项测试，全部通过。
- Helm 渲染、服务器端 dry-run 通过；Argo CD 的 `codeapi`、`librechat`、`platform-control` 均为 Synced / Healthy；代码执行服务 8 个 Pod Ready。
- Lighthouse 已尝试，但本地缺少 `rimraf`，在测量前退出；没有取得性能评分，不能据此声称性能验收通过。
- 本地 Postman CLI 1.74 不支持 `postman status`；模拟验证结果来自实际执行记录，未发布到云端工作区。

## 部署版本

- 控制面 API / 迁移：`sha256:9bd8546b57a98529d2927e963380b472f00ec0544bfbb99de02aa556dfe0c557`
- Agent worker：`sha256:7656fafdf6befb3e43e7a32ff70e8c8744902c1cc8d0f21061a89f476c2fb157`
- LibreChat：`sha256:7f9df16bd0aac8af7f0fe46ee6f9a183e7409a6eefc91badfea6e09a89953c16`
- Code Interpreter 固定上游提交：`9a3f5dbd6fedf42462239d135c2c25ec8e737628`；组件镜像摘要见 ops 中的 `apps/codeapi/values.yaml`。
- 数据库迁移：`0017_account_access`。

执行器通过专用设备插件请求 KVM，不为用户代码容器开启特权模式。LibreChat 使用绑定用户的短期 JWT 调用 CodeAPI；该上游版本拒绝旧 API Key 模式，因此采用 `LIBRECHAT_CODE_BASEURL` 与 JWT 私钥配置，而非填写无效的 `LIBRECHAT_CODE_API_KEY`。

## 边界与后续

「运行代码」使用临时计算空间，不挂载个人项目盘。产物存入独立 MinIO bucket，服务总容量上限 2 GiB、一天生命周期异步清理；尚未纳入各用户套餐的独立代码执行计费。OpenHands 继续使用个人持久空间。

本次真实代码执行验收为 Python `print(6*7)`，未声称已覆盖全部语言、大文件和高并发。禁用账号后既有文件和已发布应用保留；已经发出的单次外部操作不承诺立即撤回。

操作和运维说明见[使用手册](space-editor-and-accounts-guide.md)。
