# LibreChat 与 LiteLLM 自动开通运维手册

## 用户使用

通过 LibreChat 的 Authentik 入口注册或登录。首次完成登录时，平台自动建立个人空间和 LiteLLM 用户，发放一把专属虚拟密钥及一次性的 1 美元模型预算。选择 LiteLLM 下的模型即可对话；密钥由平台管理。已有 LibreChat 会话在升级后需退出并重新通过 Authentik 登录，才会执行开通。

如果登录阶段显示服务不可用，请稍后重试；不要自行设置 LiteLLM 密钥。调用额度耗尽时停止模型调用，并联系管理员。这里的美元是测试预算单位，并非实际充值余额。模型价格尚需核对上游账单，不能用于财务结算。

## 管理员检查

```sh
kubectl -n argocd get applications platform-prereqs platform-control librechat litellm
kubectl -n platform-system get pods
kubectl -n librechat get pods
kubectl -n litellm get pods
```

控制面的 `model_credentials` 表只保存加密后的密钥和状态；LiteLLM 保存模型用户、密钥摘要及预算/用量。控制面在开通成功时写入 `model_credential.created` 审计事件，不记录密钥。日志、终端和工单不得包含虚拟密钥或管理密钥。

## 密钥、备份与故障处理

`platform-system/platform-model-provisioning` 保存 LiteLLM 管理凭据、密钥加密材料和服务间认证凭据，`librechat/librechat-codechat-provisioning` 保存对应的服务间认证凭据。它们是集群 Secret，**不存入 Git**。部署前可通过 `scripts/bootstrap_model_credentials.py` 创建；脚本不覆盖既有 Secret，发现两侧凭据不一致时停止。

数据库备份必须与上述 Secret 及 LibreChat 自身加密 Secret 的加密备份配套，定期演练恢复。不要随意删除、重新创建或分别修改这些 Secret：丢失加密材料会使已保存的密钥无法解密。当前尚无自动轮换；轮换前需安排维护、恢复和逐用户重新发放方案。

重复登录不会重置 1 美元预算。已完成开通的密钥或 LiteLLM 用户若被删除，后续登录不会自动重新创建；需要管理员排查。开通中途失败的记录可在下一次登录时使用同一密钥重试。LiteLLM 预算的并发超限误差和真实单价尚需进一步验证，不能将它当成严格的财务结算系统。

## 当前边界

管理界面中的预算调整、用户停用/撤销、密钥轮换、账号注销后的完整销毁和账单页面仍未实现；在实现并验证前，勿声称已经支持完整运营闭环。OpenHands 的用户级密钥注入尚未实现。注册邮箱验证和公网反滥用也尚未完成，现阶段仅适合内网可信用户。
