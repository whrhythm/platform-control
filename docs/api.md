# API 契约

FastAPI 的类型化路由为源，`scripts/export_openapi.py` 导出 `contracts/openapi.json`。接口改变时同时更新导出文件。`/docs` 是交互文档；生产部署应限制文档访问范围。

除 `/healthz`、`/v1/config`、框架文档和静态页面外，所有业务接口必须携带 `Authorization: Bearer <access_token>`。不接收 X-User/X-Tenant/X-Role 作为身份。无有效 OIDC 配置返回 503；没有 token 或验签/claims/API scope 不通过返回 401。

| 接口 | 行为 |
| --- | --- |
| GET `/v1/config` | 非敏感登录配置与 provisioning_mode=plan |
| GET `/v1/me` | 当前 issuer/subject、平台管理员标识 |
| POST/GET `/v1/tenants` | 创建唯一个人租户/列出所属租户 |
| GET/PUT `/v1/tenants/{t}/members` | 查看成员/owner 分配成员角色 |
| DELETE `/v1/tenants/{t}/members/{subject}` | owner 撤销成员，禁止移除 owner |
| POST/GET `/v1/tenants/{t}/projects` | 创建/列出项目 |
| GET `/v1/tenants/{t}/projects/{p}` | 校验归属后的项目详情 |
| POST/GET `/v1/tenants/{t}/projects/{p}/resources` | 申请/列出资源 |
| POST/GET `/v1/tenants/{t}/projects/{p}/environment` | 申请/查看当前用户专属运行环境（仅计划） |
| DELETE `/v1/tenants/{t}/resources/{r}` | 取消从未 apply 的资源申请 |
| GET `/v1/tenants/{t}/quota` | 总额度与容量预留（不是实际计费） |
| GET `/v1/tenants/{t}/operations` | 最近任务，limit 1..200，默认 50 |
| GET `/v1/tenants/{t}/audit` | owner/admin 查看最近成功操作审计 |
| GET `/v1/admin/tenants` | 平台管理员列出全部租户 |
| PUT `/v1/admin/tenants/{t}/quota` | 平台管理员调整总额度，不得低于已预留 |

资源申请必须提供 `Idempotency-Key`（8..128 字符）。相同租户、相同 key、相同规范化请求返回同一资源；不同请求复用 key 返回 409。成员权限仍会在每次重试时校验。当前资源申请首次与重放都返回 201。

已取消的申请仍保留原 key；重放会返回原来的 cancelled 记录，不会重新预留额度。重新申请需使用新 key。

```http
POST /v1/tenants/<t>/projects/<p>/resources
Authorization: Bearer <access_token>
Idempotency-Key: <生成后保留供重试的随机值>
Content-Type: application/json

{"kind":"workspace","name":"dev","cpu_millicores":500,"memory_mib":1024,"storage_gib":5}
```

其他 kind 的字段以 OpenAPI 的 discriminator/schema 为准。未知字段全部拒绝。deployment 必须为允许仓库中的 `image@sha256:<64hex>`，agent 必须引用同租户、同项目的有效 workspace。不能提交任意 YAML、Secret、数据库管理密码或域名。

资源返回 `requested`；计划 worker 处理后为 `planned`，操作中的 `plan.executable` 固定为 false，`blockers` 列出还未实现的开通环节。计划不是上线成功证明；API 当前不返回可用终端、数据库密码或已经分配的访问地址。

404 用于不存在或不可见的租户/资源；403 用于已有成员角色不足；409 用于额度、重复名称、幂等冲突、Agent 独占或删除依赖；422 用于字段校验失败。未来真实 provider 超时后的重试必须结合幂等键和外部查询结果，不采用无条件重复创建。
# 资源计划批量取消

`POST /v1/tenants/{tenant_id}/projects/{project_id}/cancel-plans` 仅允许租户 owner/admin 调用；锁定租户后原子取消该项目内尚未应用的所有 requested/planned 资源和对应操作，释放预留额度。重复调用返回零个新取消项；响应中的 `infrastructure_deleted` 始终为 false。如果存在已开通或未知状态资源、或操作记录缺失，整批拒绝并保留额度。此接口不删除项目、文件、Pod、数据库或存储桶；实际资源销毁尚未实现。

`POST /v1/me/personal-tenant` 使用经过验证的 OIDC 身份查询或创建个人租户；重复请求返回同一租户，并发创建通过数据库唯一约束仲裁。该接口不接受客户端提供的用户身份。LibreChat 尚未调用此接口，注册时也不会自动调用。

# 用户运行环境（仅计划）

`POST /v1/tenants/{tenant_id}/projects/{project_id}/environment` 查询或创建**请求者本人**的专属运行环境记录：身份只取经过验证的 OIDC `(issuer, subject)`，请求体中的任何字段（issuer/subject/storage/namespace）全部忽略。每用户至多一条记录，由数据库唯一约束仲裁并发首请求；重复请求返回同一条记录（始终 200），审计只记录一次 `environment.create`。项目只作为“首次从哪里请求”的上下文（`first_tenant_id`/`first_project_id`）；同一用户的其他项目再次请求得到同一条记录。

响应中的 `mode` 固定为 `plan`、`executable` 固定为 `false`；`plan` 列出计划对象（命名空间、ResourceQuota、单块 Longhorn PVC、default-deny NetworkPolicy），`blockers` 列出未实现的开通环节。**该接口不创建任何沙箱、Pod、卷或凭据**，`status` 在 apply adapter 落地前停留在 `requested`（生命周期 `requested → provisioning → ready`，另有 `suspended`/`deleted`）。每用户存储默认 2 GiB（免费额度），由 `PLATFORM_DEFAULT_ENVIRONMENT_STORAGE_GIB` 调整（1..40）。角色要求与项目一致：owner/admin/developer 可申请，viewer 403；环境属于用户而不是租户，租户 A 的成员在租户 B 的项目里请求，得到的仍是该成员自己的环境。

