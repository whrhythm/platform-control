# 实施、部署和验收

## 当前状态

| 模块 | 本次状态 | 证据/限制 |
| --- | --- | --- |
| Python API | 已实现 | 身份校验、租户、成员、项目、配额、资源申请、操作与审计 |
| 计划 worker | 已实现 | 数据库事务队列；只生成计划，绝不标记 ready |
| PostgreSQL/MinIO/Kubernetes 计划 | 已实现 | 无凭据、不可直接视为完整上线清单，返回 blockers |
| React 浏览器界面 | 已构建并部署 | 实际双用户 PKCE 登录、项目与申请流程已验收 |
| Python 本地 Sidecar | 已实现并打包 Linux 二进制 | 已验证实际启动、认证和父管道关闭退出；其他系统待验收 |
| Tauri 生命周期 | 已实现源码 | 拉起、退避重启、关闭回收；完整打包验收待做 |
| 实际开通 worker | 待实现 | 不需要也不读取当前集群的管理员 kubeconfig |
| 真正的 Agent 编码/测试 | 待集成 | 不能用共享 Canvas 代替项目隔离 |
| 发布与独立域名 | 待集成 | 需 DNS/入口证书/APISIX 与构建闭环 |
| 用量归集/费用 | 待集成 | 当前只有容量预留 |
| Alembic 迁移 | 已实现 | 初始 revision、升级/降级/重新升级及模型一致性测试 |
| PostgreSQL 并发 | 本地 16 已验证 | 20 请求配额、20 请求同键幂等、10 轮 worker/取消竞态；集群 17 待验收 |

平台已部署于 `platform-system`，API/worker 均为独立 Deployment；迁移通过 Argo CD Sync hook 完成。集群 PostgreSQL 17 已验证证书校验、不同运行账号、跨数据库拒绝、审计修改拒绝及 Pod 替换后元数据保留；17 的并发压力与备份恢复仍待验收。当前不是完整生产验收。

使用入口、域名、CA、用户和管理员操作见 [使用手册](user-guide.md)。GitOps 清单与操作说明位于同工作区 `ops/apps/platform-control/`。本次使用本地测试后的源码/前端小包在目标主机重建镜像并推送 Harbor，原因是大镜像经 Tailscale DERP 上传持续超时；原本地归档未传完。

开放真实工作空间之前必须先处理已观察到的 k3s PodCIDR 10.42 与 Calico 172.16 不一致，并验证 NetworkPolicy 实际隔离。

## 阶段一：身份与控制面

建立专用 Authentik application/provider，公共浏览器客户端使用 Authorization Code + PKCE；redirect URI 使用精确的 `https://platform.<域>/`，本地开发精确注册 `http://localhost:5173/`。客户端不能持有 client secret。配置 RSA 签名、API scope 和 groups claim，确认普通用户不能编辑平台管理员组。

issuer/JWKS/audience 必须从实际 provider 配置取得，不能依据猜测地址上线；后台信任内部 CA。不要将浏览器 ID token 直接当作平台 API access token。使用短效 token；退出登录、令牌到期与成员移除的行为分别验收。

控制面使用共享 PostgreSQL 中的独立 DB 和最小权限 role。已有 Alembic 初始迁移；首次部署需通过独立迁移 Job 执行，再启动 API/worker，并完成数据库备份和还原演练。已在本地 PostgreSQL 16 验证：同租户 20 个并行申请，成功总容量不超过额度；相同幂等键返回同一资源；worker 与取消竞态不恢复已取消资源。上线仍需在实际 PostgreSQL 17 和多副本部署中复验。

API 服务无 Kubernetes 资源写权限；计划 worker 同样无权限。只有后续 apply worker 获得所需的 Kubernetes/数据库/对象存储操作凭据。目标 API/worker 部署到 `platform-system`，不使用桌面 Sidecar 来承载公共控制面。

## 阶段二：真实工作空间与 Agent

实现 apply adapter，从服务端模板创建 namespace、quota、default-deny、PVC、Kata workload。添加租约、指数退避、恢复对账和外部资源引用。多个 API/worker 使用 PostgreSQL；Kubernetes apply 按稳定资源 ID 幂等。

开通 namespace 的权限范围很大，需用独立 bootstrap 组件或 admission policy 限定为 `tn-*` 与带平台标记资源；RBAC 自身不能仅按名称前缀限制 create namespace。不得为了方便直接给公共 API cluster-admin。

先验证一个用户的持久工作空间：写文件、运行测试、重启恢复、卷容量上限、无 SA token、guest 内核、跨项目和跨租户网络拒绝。再连接工作空间专属 OpenHands，并由访问代理按用户身份授权 API/WebSocket。Kata 下访问需经过受控代理，而非假设直接 port-forward 始终可用。

先做受限工具集，再接 LLM。模型网关持有上游凭据，使用租户/项目的服务端身份执行额度与模型许可。必须验证越权的模型、workspace id 与 Agent 会话被拒绝。

## 阶段三：数据库与对象存储

数据库 provisioner 创建项目/环境独立 role/database，先处理共享 PostgreSQL 默认 PUBLIC 权限，再验证 A 凭据不能连接/读写 B 的库。凭据轮换与重试不能重复创建孤立数据库。

MinIO provisioner 创建独立 bucket、policy、身份和 quota；拒绝匿名访问与跨 bucket 访问。对真实版本测试 quota、生效延迟与 multipart 行为，不能用计划 JSON 证明服务端策略已生效。

## 阶段四：构建与发布

受控代码快照 → 独立 Kata 构建任务 → 测试 → Harbor digest → 部署 → 探针 → APISIX/DNS/TLS → 访问验证。所有临时构建资源计入额度，完成后执行清理。用户只能指定平台支持的构建方式与受控参数，不能提交任意宿主机挂载或管理员凭据。

确定可用基础域、DNS 管理方式和访问范围后配置域名；不自动暴露到公网。生产数据迁移必须独立于代码回滚，保存发布历史和上一个稳定版本，支持回滚镜像但不盲目回滚数据库。

## 阶段五：监控与桌面

现有 OTel agent 已收集容器日志，但 Kata guest 的容器日志可见性、卷指标和 runtime 标识仍需实测。为平台服务和用户任务增加可信 tenant/project/resource 标签；使用 usage worker 生成平台用量汇总，并与 SigNoz 运维视图核对。

桌面复用 React UI，Rust 仅负责 GUI 与进程生命周期。Sidecar 打包为原生系统可执行文件，动态回环端口、随机本地 token、origin/host 检查、TLS 信任、启动握手和崩溃重启。不能在 Rust 中实现租户权限、资源开通、数据库访问等业务。

桌面身份登录后续采用系统浏览器 PKCE + 随机 state + 一次性 loopback callback，拒绝任意回调 URL。远程终端 WebSocket 需短时一次性授权、重连重新校验和撤销成员后的断连。当前 HTTP bridge 尚不实现这些功能。

## GitOps 与发布约定

应用源码保持在 `platform-control/`，当前还不是独立 Git 仓库；已有 `ops/` 仓库只管理部署清单。不要把业务源码、密钥或 kubeconfig 混进公开 ops 仓库。

构建 API/web/worker 镜像，推送 Harbor 后，以镜像 digest 更新 ops 中的 Application/values。平台自身由 Argo CD 部署；动态租户资源归 worker，Argo CD 不递归接管这些 namespace。没有完成身份与数据库配置前，不创建一个对局域网开放的空认证平台。

备份包括控制面 DB、项目 DB、MinIO、工作空间 PVC，以及不进入 Git 的 Secret 恢复策略。单节点 Longhorn 只有一份副本；必须有集群外备份，验收恢复到新节点。
