# 代码运行额度与文件保存验收（2026-11-05）

## 已上线行为

代码运行统一经过 Platform Control，按已激活套餐检查月度次数、单次时限和并发。免费、基础、专业的默认月额度分别为 30 / 300 / 1500 次。管理员「平台管理 → 模型与套餐」可配置后续订单的运行次数、最长时间、并发数。

用户在代码结果区域点击「保存到我的空间」，可选文件和目录、修改文件名。保存调用已有的个人空间上传接口，检查容量、禁止同名覆盖，保存后计入个人磁盘。入口同时覆盖手动运行代码的结果和智能体附件组。

## 实际用户界面验证

仅使用指定普通用户 `1321670187@qq.com`：

1. 在普通聊天请求生成 Python 代码，点击「运行代码」生成 31 字节文本文件。
2. 同一操作前后，月度用量从 1 次变为 2 次；基础版限制显示 300 次/月、15 秒/次、并发 1。
3. 点击「保存到我的空间」，修改目标文件名并保存成功；再次保存同名文件显示冲突提示。
4. 从个人文件树打开保存后的文件，预览内容与生成内容一致，验证其已持久保存。
5. 通过界面删除仅用于验收的保存副本；未修改其他用户文件或项目。
6. 亮色和暗色的保存窗口均检查，验收结束恢复亮色。
7. 从聊天点击运行一个等待 60 秒的 Python 程序，15.427 秒后返回 `Time limit exceeded`；未执行其 60 秒后的完成输出。该次运行正常计入额度。

管理员仅使用获准的 akadmin 验证配置：额度字段正确读取，保持原值保存成功，没有调整用户现有套餐。

第一次文件生成已成功，但独立只读验收客户端未继承浏览器证书信任，导致验收中断；改用已验证证书的浏览器上下文读取状态后完成全流程。因此本次 UI 验收共消耗指定账号 3 次运行额度，不手动回写或抹除计量记录。超时验收脚本最初遗漏上游的 `Time limit exceeded` 文案，修正断言后对已记录结果完成核对，没有重复执行程序。

截图、浏览器状态及原始结果保存在 Git 忽略的 `release/`：`code-quota-save-success.png`、`code-quota-save-conflict.png`、`code-quota-saved-preview.png`、`code-quota-save-dark.png`、`code-quota-membership.png`、`code-quota-admin-settings.png`、`code-quota-ui-evidence.json`、`code-quota-timeout-result.json`。凭据和客户数据不进入源码仓库。

## 自动化验证

- 控制面完整测试：246 通过、28 跳过；最终套餐资格调整后相关测试 15 通过。
- 一次性本地 PostgreSQL 16：14 通过，包含 16 个并行提交在并发上限 1 时只接受一个，未使用业务数据库做并发测试。
- Postman 本地 HTTP 模拟通过：真实 JWT 认证及网关、强制套餐时限、持久计量、不明确结果不重放、并发占用阻止继续提交。
- LibreChat 构建：相关工作区 TypeScript 检查及 203 项测试通过；新增测试覆盖身份签名/缓存变化、保存目录、同名冲突、空间不足、不自动重试。相关源码 ESLint 与 import 排序通过。
- 管理界面安装依赖、类型检查及生产构建通过。
- Helm 渲染与实际命名空间的 server dry-run 通过；四个 Argo CD Application 为 Synced / Healthy。
- Lighthouse 在准备阶段因本地缺少 `rimraf` 失败，没有取得性能评分。Postman CLI 1.74 的 `status` 命令不受支持，模拟结果来自实际 HTTP 执行。

## 部署记录

- ops：`43a72fb`。
- LibreChat：`harbor.k3s.local/internal/codechat-librechat@sha256:0789ce7998da9b90287fff6eb89fb43af00cf0bbf42fa777a595371891dfef63`。
- 平台 API / 迁移：`harbor.k3s.local/internal/platform-control@sha256:d745a4140e682c629b9d1b015bf00c85a5eb9ceb904f4b66e7fb66421c228c6e`。
- 工作空间控制器：`harbor.k3s.local/internal/platform-control@sha256:c0fa681ceea92f02ed21cc69c250a88f9eee78ce45d793f7d5416ff6948375b1`。
- 迁移 `0018_code_execution` 增加持久预留和上传计量；API 获得该表必要的读取、新增、更新权限。
- CodeAPI 入口网络策略仅允许平台 API；LibreChat 使用内部计量网关地址。单次程序内存 256 MiB，签名清单限制产物总量 16 MiB、最多 10 个文件。

完整计量和失败规则见[代码运行协议](code-execution-contract.md)，操作说明见[使用手册](space-editor-and-accounts-guide.md)。本次没有接入真实支付，也没有把运行次数标注成实际 CPU 秒数或 tokens。
