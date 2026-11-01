# 权限、隔离与配额

## 身份与角色

Authentik 注册/登录；控制面只接受服务端配置的 issuer、audience、JWKS。当前算法固定 RS256，要求 exp/iat/iss/sub/aud 和 `platform-api` scope。必须在 Authentik OAuth provider 选择 RSA signing key，并给访问令牌配置 groups 与 API scope。TLS 证书和 JWT 签名密钥的用途不同。

平台管理员由签名 token 的 `platform-admins` 组映射，只有 Authentik 管理员能授予该组；任何请求头或请求体自报的角色无效。租户成员角色存储在控制面 DB，每个请求重新读取，撤销成员立即影响后续请求。平台管理员访问普通项目接口仍需租户成员关系，跨租户操作仅开放明确的 `/v1/admin/*` 管理接口。

| 操作 | owner | admin | developer | viewer | 平台管理员专用 |
| --- | --- | --- | --- | --- | --- |
| 查看所属租户项目/资源/额度 | 是 | 是 | 是 | 是 | 不能隐式绕过成员关系 |
| 创建项目、资源申请、取消未开通资源 | 是 | 是 | 是 | 否 | 同上 |
| 查看成员和成功操作审计 | 是 | 是 | 否 | 否 | 同上 |
| 添加/修改/移除成员 | 是 | 否 | 否 | 否 | 暂无跨租户代管成员接口 |
| 修改 owner | 否 | 否 | 否 | 否 | 后续独立转移流程 |
| 列出所有租户、修改租户总额度 | 否 | 否 | 否 | 否 | 是，并写审计 |

成员按租户共享项目，这是当前明确的产品规则。需要同一租户中互不知情的个人项目时，先实现项目级成员关系，不能将本规则直接用于该场景。

跨租户资源查询返回 404，防止泄露其是否存在；已知所属租户但角色不足返回 403。API 不接受客户端指定的 tenant/project 归属、namespace、serviceAccount、PVC 名、数据库 role、任意域名、hostPath、Pod 安全上下文或 provider 凭据。

## 运行隔离

目标为 tenant namespace + 项目独立卷/凭据 + Kata + restricted Pod security + 无 SA token + 默认拒绝网络。Kata 失败不得自动退回 runc；普通用户代码不能通过 Kubernetes API 创建新的工作负载，也不能挂载同 namespace 下其他项目的卷或 Secret。

NetworkPolicy 必须实测 CNI 实际执行。默认仅 DNS 放行；模型、包下载、数据库、对象存储和入口代理分别增加最小范围规则。不能为了安装依赖直接放开整个内网、云 metadata 地址或 Kubernetes API；后续应增加受控出口代理。

命名空间内仍需项目标签匹配网络规则，所有标签由 worker 注入，不信任用户自报 OTel 标签。动态工作负载和平台自身有不同凭据与管理者。现有共享 OpenHands 验证实例保持独立，不能把其 API key 下发给平台用户。

## 额度语义

当前账本统一预留 CPU milli-core、内存 MiB、持久存储 GiB、资源数量与项目数量。Workspace 和 deployment 各自预留运行容量；Agent 绑定已有 workspace，最多一个，不额外创建不计额度的运行 Pod。bucket 的容量预留也记入 storage_gib。

数据库当前只预留资源数量，不承诺按 GiB 强制硬配额。PostgreSQL 没有直接对应 ResourceQuota 的通用每 database 磁盘硬上限；需要连接数/语句限制、容量监控、阈值处理或独立实例方案。数据库实际使用、MinIO 实际使用和 PVC 实际使用必须分别展示。

PostgreSQL 上按租户 `SELECT FOR UPDATE` 串行化申请、取消和额度调整，Resource、Operation 与 Audit 同事务提交。申请使用租户范围内唯一的 Idempotency-Key 和规范化请求 hash；同 key 同请求复用原资源，不同请求 409。两个并发 worker 使用同一锁顺序避免与取消流程死锁。

SQLite 只用于单进程演示与单元测试，不能验证 PostgreSQL 行锁行为或多 API 进程下的配额正确性。已在独立本地 PostgreSQL 16 容器通过 20 请求并发额度、并发幂等和 worker/取消竞态测试；集群 PostgreSQL 17 和多副本部署仍待验收。数据库使用版本化 Alembic 迁移，API 不执行 create_all。

数据库使用复合外键约束资源与项目、任务与资源的租户一致性，作为 API 权限校验外的防线。真实 Kata apply adapter 还需要将 RuntimeClass 的 CPU/内存 overhead 纳入预留；当前计划额度未标记为实际运行用量。

资源未 apply 前允许取消并释放预留。真实资源失败、删除中或尚未确认已销毁时必须继续占用预留；不能用删除数据库记录来掩盖基础设施泄漏。当前 worker 只做规划，因而没有基础设施失败后的清理和重试闭环。

## 审计与凭据

审计只保存主体、动作、目标、租户和时间，不保存密码、完整 token、源码、prompt 和连接串。当前是控制面成功写操作审计；拒绝日志、读操作审计、不可变归档和数据库最小权限仍需部署阶段补齐。

业务 DB 凭据、MinIO 凭据和模型凭据通过受控 Secret 引用传递。开通器管理凭据与运行凭据必须分离；平台 API 和桌面 Sidecar 不应持有共享 PostgreSQL superuser 或 MinIO root 凭据。

单节点意味着 CPU、磁盘、内核漏洞和节点故障仍是共同边界。内部可信用户范围不能未经重新评审直接扩展为匿名公开运行服务。
