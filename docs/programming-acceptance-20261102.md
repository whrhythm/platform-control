# LibreChat 编程阶段验收记录（2026-11-02）

本文记录当时的模型菜单入口。2026-11-03 的能力标签和空间分区验收见 [后续记录](capabilities-acceptance-20261103.md)，当前操作见 [用户手册](user-guide.md)。

## 已交付的用户路径

全部真实用户验收使用 `1321670187@qq.com`，没有为这轮验收创建其他用户。

1. 登录 LibreChat，展开“我的空间”。现有 2 GiB 磁盘和先前的验证文件仍在。
2. 主聊天顶部“选择模型” → LiteLLM → OpenHands。
3. 在主聊天输入创建工程的要求。OpenHands 实际创建 `codechat-demo-20261102` 目录及 `app.py`、`test_app.py`、`README.md`、`Dockerfile`，文件树自动刷新。
4. OpenHands 执行 `python -m unittest test_app.py -v`，两项测试通过；实际启动 HTTPServer 并请求 `/`，返回 `Hello CodeChat v1`，随后停止进程。
5. 在工程目录内，通过侧栏上传 `change-request.txt`，再点击文件下载，内容一致。
6. 在同一聊天要求读取上传的需求文件并修改工程。OpenHands 实际把问候语改为 `Hello CodeChat v2`，更新两处断言，再执行两项测试通过。
7. 从侧栏下载 `app.py`、`test_app.py`，自动断言均包含 v2，源码中不再包含 v1。

聊天记录和工程文件保留在该账号，用户可以继续查看和修改。没有执行销毁或新建第二块用户磁盘。

## 部署与验证证据

- Argo CD 的 platform-control、librechat、litellm 均为 Synced / Healthy。
- 个人工作 Pod 两个容器就绪，使用 Kata；文件服务和 SDK 执行进程挂载同一 `workspace-data`。
- PVC UID 升级前后均为 `19e17f16-8d3b-43ff-9480-9baa3d0c8faa`。
- 后端全量回归：120 通过，10 跳过；最终针对性回归：8 通过，1 个需要 Postman 的用例跳过。
- Windows 本地 Postman 模拟：编程模块 6 项通过，含真实 HTTP key 哈希查询和个人空间归属核验。模拟日志确认七次服务交互；该测试没有使用真实账号或真实模型密钥。
- 安装版本 OpenHands SDK 1.58.1 的独立兼容性探针，使用本机回环模拟模型，实际执行终端工具并写出临时文件；临时目录退出时清理。
- LibreChat 发布镜像中的 API/client 类型检查与相关测试通过；本地文件面板测试 2 项通过，client 类型检查通过。
- 已执行仓库要求的 `npm run lighthouse`；前端编译通过，性能检查启动阶段因测试容器缺少 `openssl` 失败，没有形成可用的 LCP 性能报告。

截图位于忽略提交的 `release/programming-created.png`、`release/programming-edited.png`。这些是该账号实际界面，不是产品原型图。

## 尚未交付

本阶段最初证明“聊天编程 → 个人磁盘 → 文件上传/下载 → 连续修改 → 实际测试”成立。之后已经完成真实镜像构建、私有仓库、发布访问及启停/日志/用量验收，见 [交付验收](application-delivery-acceptance-20261102.md)；不能把 Dockerfile 的存在视作已发布。

后续修复将执行切换为 SDK `arun()` 和 `interrupt()`：停止或 180 秒超时请求取消当前异步执行并结束聊天流；工具清理仍可能需要短暂时间。发布回执会直接结束 Agent 本轮，不让模型继续自行轮询或修改源码重试。实际安装的 SDK 探针已证明发布回执后只有一次模型请求。同一聊天任务的重复发布请求复用原记录，包括已失败构建。
