# 聊天编程与应用发布验收（2026-11-02）

## 真实用户流程

使用既有用户 `1321670187@qq.com`，从 LibreChat 界面发起；未使用其他真人测试账号。开发诊断使用集群命令，用户编程与发布操作由产品界面发起。

1. 已有工程 `codechat-demo-20261102` 位于个人持久磁盘，文件上传、下载、创建与修改的早期验收见 [编程记录](programming-acceptance-20261102.md)。
2. 通过主聊天修复服务启动配置，保留 HTTPServer 业务，执行测试并提交一次发布；聊天在 28 秒结束。后台 Kata BuildKit Job 成功构建并推送到该用户私有 Harbor project。
3. 发现应用可用但入口缺少 `ingressClassName: apisix`，浏览器返回 404；修复控制器自动更新已有路由，并等待当前 generation 的 Accepted=True，未用手工路由替代产品实现。
4. 在应用卡片打开实际地址，浏览器响应严格等于 `Hello CodeChat v2`。
5. 同一聊天要求将业务和测试改为 v3，测试通过后提交一次发布；聊天在 21 秒结束。后台生成新镜像，同一地址实际返回 `Hello CodeChat v3`。
6. 从侧栏下载 `app.py`，确认真实源码包含 v3。
7. 应用卡片实际显示近期 HTTP 日志、CPU/内存值及百分比；并非固定示例数据。
8. 点击 Stop，观察到 Stopped；再点击 Start，实际地址再次返回 v3。源码和原个人 PVC 保留，未新增第二块用户磁盘。

## 保留的可复核结果

- 应用 ID：`0dc75d4e-20cb-4a6a-ae7f-b17fc0b1041c`，最终状态 running。
- 地址：`http://app-0dc75d4e20cb4a6aae7fb17f.192.168.10.30.sslip.io:30097/`，仅用于当前局域网。
- Harbor 私有 project：`u-fe92d11178999899f6fca762`。
- 实际运行镜像 digest：`sha256:b505aa71695d97c96c37b2de854c95726029acf33e2033c2c0439d0cdd0be750`。
- v3 构建 Job：`build-74cc6601933246ec990e1c4f`。
- 个人 PVC：2 GiB、Bound；UID `19e17f16-8d3b-43ff-9480-9baa3d0c8faa`，与编程阶段相同。
- 一次观测：CPU 0.26m / 1000m（0.03%），内存约 29.7 MiB / 512 MiB（5.79%）；这些数值会随时间变化。
- Argo CD platform-control 与 librechat 均 Synced / Healthy。
- 本机忽略提交的截图：`release/published-service-v2.png`、`release/published-service-v3.png`、`release/published-app-panel.png`；结果摘要 `release/delivery-ui-results.json`。

## 六分钟持续运行问题

此前发布工具同步等待构建，Agent 在平台失败后反复重试，并错误地把 Dockerfile 改为只打印 ok；镜像虽曾构建成功，容器立即退出，不能算发布成功。

修复后发布工具立即返回后台任务，执行器收到回执直接结束当前轮；同一任务 ID 重复发布复用原记录，失败也不重建。编程轮次上限 180 秒，超时或用户停止请求 SDK interrupt；已生成文件保留。没有通过放宽成功判定掩盖错误。

## 自动验证及限制

- 控制平台全量 pytest：128 通过、12 跳过；路由修复后的编程/交付针对性验证：13 通过、2 跳过。跳过项需独立 PostgreSQL/Postman 环境。
- 安装版本 OpenHands SDK 的回环模拟模型探针：工具返回发布回执后，只有一次模型请求，当前轮结束。
- 同一聊天任务连续三次提交已失败发布的 API 用例：不新增版本、不产生新发布审计；新的用户任务才生成新版本。
- LibreChat 已部署镜像的 API/client 类型检查和相关测试已通过，代码已推送 dev 分支。
- 本次管理员 web 的 npm ci 重跑遇到外部包下载 ECONNRESET，未完成这次依赖重装与构建检查；本次没有修改该前端源码。早期 Lighthouse 因测试容器缺少 openssl 未完成，不能声称性能验收通过。

本记录不宣称所有产品能力完工。真实支付、套餐与持久存储统一计费、项目数据库/MinIO 自动开通、Harbor 网页统一身份、删除应用的完整 UI 验收、故障恢复和生产隔离仍待后续阶段验证。当前文件与应用共用侧栏的位置也需要独立改进。
