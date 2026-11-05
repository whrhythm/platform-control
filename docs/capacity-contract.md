# 套餐、逻辑用量与测试升级协议

2026-11-05 起的统一配置、数据库分项、应用合计算力、OpenHands 与构建权益，见[统一套餐与配额管理](unified-quota-management.md)。管理员入口已整理为“套餐与配额管理”；用户权益以已激活快照为准。

普通用户从 LibreChat“我的空间 → 套餐与用量”查看模型余额、真实 Token 用量、生效权益和测试套餐。独立 Python 服务拥有订单、权益与额度任务；浏览器不提供价格、身份或存储对象标识。初始赠送与付费额度分开记账，注册赠送 1 美元不重复发放。后续充值规则见[模型与套餐协议](chat-and-membership-contract.md)。

## 接口与状态

接口位于服务认证的 `/internal/v1/workspace/capacity`；LibreChat 以同源 `/api/user/workspace/capacity` 转发已验证的用户身份。

- GET `/capacity`：当前套餐、分项计量、应用数、可选套餐、进行中订单与近期订单。
- POST `/capacity/orders`：`{plan_id, plan_version}`，必需 Idempotency-Key。plan_version 是服务端根据完整价格/权益生成的报价指纹；管理员只改价格而忘记改目录标签也会使旧报价失效。价格/权益使用服务端目录快照；相同键同内容复用订单，不同内容返回 409。每用户最多一个待处理订单。
- POST `/capacity/orders/{id}/confirm-test`：仅显式启用测试付款时可用，无真实扣款；待付款转 applying，支付确认与开通结果分别记录。重复确认不重复开通或发放模型额度。
- POST `/capacity/orders/{id}/cancel`：只取消未付款订单。
- POST `/capacity/orders/{id}/retry`：失败订单显式重试，复用原价格与权益快照，不重复付款；身份/对象不一致需要管理员检查。

状态：pending_payment → applying → ready / failed。ready 才更新生效权益。失败保留原生效权益和用户资料；不自动删除卷、不缩容、不因重试重新创建磁盘。订单保留审计和观测证据。测试权益没有自动续费、真实支付或到期清除；不作为正式账单。

2026-11-03 增加模型额度：报价包含人民币额度、结算汇率快照和美元微单位金额。支付确认后 `model-billing-controller` 先持久化原预算与目标预算，观察 LiteLLM 用户及 key 两级增加后标记 `model_credit_status=applied`，容量控制器才继续文件权益开通。重复购买原套餐允许补充模型预算，较小档位保持已经取得的较高文件和应用权益。额度到账与资源生效分别展示；写入超时复用绝对目标恢复，外部预算变化转人工核验。真实支付和退款尚未接入。

测试入口开关控制新订单及用户确认操作；已经确认的扩容由控制器继续处理。进行中或失败的扩容保留对该配额/PVC 的协调权，普通创建流程不会把中间容量误判成损坏并关闭文件服务。

第一版目录提供可配置的 4/8 GiB 文件盘测试升级及应用数提升；现有自定义较大权益保留，只允许容量和应用数均不降低的选项。价格只在测试确认页显示，不实际扣款。生产支付、周期结算、退款与降级尚未接入。

## 观测与计量

统一账本按个人文件、数据库、对象、镜像、额外应用持久卷记录来源、时间和完整性。个人文件及依赖/Agent 缓存通过个人卷扫描；托管数据库通过 pg_database_size 汇总；对象按受管私有桶内完成的对象版本大小汇总；镜像读取已核验 Harbor 项目的原生 quota.used.storage，避免重复累加共享层。目前应用不另建 PVC，完整核验仅有原个人卷时，额外应用盘记为零；未知额外 PVC 不能记为零。详见[计量协议](storage-accounting-contract.md)。

数据库与对象存储各有 256 MiB 测试预算，保留已有文件盘额度。数据库预算用于申请预留和超量提示，不是硬字节写入限制；MinIO 使用原生桶配额。Harbor 的实际原生额度可能为无限，不伪造有限分项额度。自 2026-11-05 起，storage.budget_bytes 固定取当前生效套餐的 storage_gib × 1024³，budget_complete=true 表示套餐总额度已知；分项预算保留在 components 中，不能相加成购买的总容量。例如 4 GiB 套餐加两个 256 MiB 分项限制仍显示 4 GiB，不显示 4.5 GiB。known_used_bytes 继续累加各分项新鲜实测用量；complete 独立表示计量是否完整。套餐总量展示并不代表已经具备跨服务统一硬写入限制。

只有五类都获得新鲜样本才 complete=true；其余情况下 known_used_bytes 是新鲜分项小计，不能展示成完整总量。扫描失败保留上次值并标 stale，无样本为 unknown；超过有效期也标过期。已生效存储预算、待开通增量预留和实际使用分开。

当前测试升级改变文件盘和应用数权益，不改变数据库、对象和镜像额度。对象/镜像付费扩容、额外应用盘创建及统一跨服务硬限额尚未接入。相同个人盘内的工程和缓存只扫描一次。

扩容控制器先校验已记录 Namespace/PVC/PV/Longhorn 标识及保留策略，再按 UID/resourceVersion 条件增加 ResourceQuota 和原 PVC 请求。只读 StorageClass 确认可扩容。观察 PVC/PV 容量、文件系统待扩容条件和个人服务实际可见文件系统大小后才生效。文件系统有元数据开销，可见总量与块设备额度不完全相同；容差由服务端配置。超时明确失败，允许用户重试观测同一任务，不缩回已扩大的卷。

扩容期间不自动终止编程进程或删除用户文件；需要运行环境重启等人工处理时显示等待/失败状态。普通上传、读取和下载继续使用原来的个人服务。

## 验收

本地验证：订单并发幂等、快照、重复确认/重试/取消、越权拒绝、未知计量、应用数权益、扩容条件写、超时与进程重启恢复；数据库并发在一次性本地 PostgreSQL 验证。Kubernetes HTTP 模拟不冒充真实扩容。

真实界面：只使用指定用户，从 UI 创建并确认测试订单；观察原卷扩容及同一 UID，确认原文件可预览、已发布服务仍可访问、磁盘与应用数显示新权益，再重复请求验证无重复订单。失败和未执行检查写入独立验收记录。

依据：[Kubernetes PVC 扩容](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#expanding-persistent-volumes-claims)、[Longhorn 扩容](https://longhorn.io/docs/1.13.0/nodes-and-volumes/volumes/expansion/)。具体 Kata 环境能否在线完成以实际观测为准。
