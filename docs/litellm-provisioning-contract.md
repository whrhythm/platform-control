# LiteLLM 自动开通：内部接口契约

`POST /internal/v1/litellm/key` 仅供可信 LibreChat 服务端调用。请求使用 Authentik 的用户 access token（`Authorization: Bearer …`）和独立服务凭据（`X-CodeChat-Service-Token`），无请求体。控制面验证 token 的签名、issuer、audience、scope 和 subject；调用方不能指定用户身份。成功响应包含该用户的 virtual key，`Cache-Control: no-store`；响应只能经内部 HTTPS 传给服务端，禁止写日志、返回浏览器或暴露给用户工作空间。

初次开通先在数据库持久化加密密钥并提交，再使用同一密钥调用 LiteLLM。每个 `(issuer, subject)` 只有一条记录；并发请求由数据库唯一约束与行锁串行化。状态为 pending 的开通可以安全重试；ready 的密钥如果在 LiteLLM 不存在，拒绝自动重建，防止管理员撤销后重新登录恢复。LiteLLM 用户首次创建时预算 1 美元且不设置周期，后续调用不重置用户预算。

401/403 表示用户身份或服务凭据错误；503 表示配置、加密或外部服务不可用；已经撤销的密钥不能自动恢复。所有错误均不得包含凭据或提供商响应体。管理能力、密钥轮换与完整删除流程需另行实现，不能通过手工直接删除数据库记录来重发赠额。数据库备份必须与加密密钥备份和恢复演练配套。
