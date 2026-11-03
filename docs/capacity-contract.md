# 套餐、逻辑用量与测试升级协议

普通用户从 LibreChat“我的空间 → Storage & plan”查看已生效权益、已知用量及测试套餐。独立 Python 服务拥有订单、权益和扩容状态；浏览器不提供价格、身份或存储对象标识。模型额度与磁盘套餐分开，不因升级再次发放初始模型预算。

## 接口与状态

接口位于服务认证的 `/internal/v1/workspace/capacity`；LibreChat 以同源 `/api/user/workspace/capacity` 转发已验证的用户身份。

- GET `/capacity`：当前套餐、分项计量、应用数、可选套餐、进行中订单与近期订单。
- POST `/capacity/orders`：`{plan_id, plan_version}`，必需 Idempotency-Key。plan_version 是服务端根据完整价格/权益生成的报价指纹；管理员只改价格而忘记改目录标签也会使旧报价失效。价格/权益使用服务端目录快照；相同键同内容复用订单，不同内容返回 409。每用户最多一个待处理订单。
- POST `/capacity/orders/{id}/confirm-test`：仅显式启用测试付款时可用，无真实扣款；待付款转 applying，支付确认与开通结果分别记录。重复确认不重复开通或发放模型额度。
- POST `/capacity/orders/{id}/cancel`：只取消未付款订单。
- POST `/capacity/orders/{id}/retry`：失败订单显式重试，复用原价格与权益快照，不重复付款；身份/对象不一致需要管理员检查。

状态：pending_payment → applying → ready / failed。ready 才更新生效权益。失败保留原生效权益和用户资料；不自动删除卷、不缩容、不因重试重新创建磁盘。订单保留审计和观测证据。测试权益没有自动续费、真实支付或到期清除；不作为正式账单。

第一版目录提供可配置的 4/8 GiB 文件盘测试升级及应用数提升；现有自定义较大权益保留，只允许容量和应用数均不降低的选项。价格只在测试确认页显示，不实际扣款。生产支付、周期结算、退款与降级尚未接入。

## 观测与计量

统一账本按个人文件、数据库、对象、镜像、额外应用持久卷记录来源、时间和完整性。本阶段只接入个人文件及依赖/Agent 缓存的扫描；其他分项 used_bytes=null、not_metered。已知分项之和不能展示成完整总用量；扫描失败保留上次值并标 stale，无样本时为 unknown。已生效存储预算、待开通增量预留和实际使用分开。

本阶段的套餐预算分配给文件盘。其他服务的计量和分项硬限额留到后续适配，不承诺当前多个存储服务共享精确原子总硬限额。相同个人盘内的工程和缓存只扫描一次。

扩容控制器先校验已记录 Namespace/PVC/PV/Longhorn 标识及保留策略，再按 UID/resourceVersion 条件增加 ResourceQuota 和原 PVC 请求。只读 StorageClass 确认可扩容。观察 PVC/PV 容量、文件系统待扩容条件和个人服务实际可见文件系统大小后才生效。文件系统有元数据开销，可见总量与块设备额度不完全相同；容差由服务端配置。超时明确失败，允许用户重试观测同一任务，不缩回已扩大的卷。

扩容期间不自动终止编程进程或删除用户文件；需要运行环境重启等人工处理时显示等待/失败状态。普通上传、读取和下载继续使用原来的个人服务。

## 验收

本地验证：订单并发幂等、快照、重复确认/重试/取消、越权拒绝、未知计量、应用数权益、扩容条件写、超时与进程重启恢复；数据库并发在一次性本地 PostgreSQL 验证。Kubernetes HTTP 模拟不冒充真实扩容。

真实界面：只使用指定用户，从 UI 创建并确认测试订单；观察原卷扩容及同一 UID，确认原文件可预览、已发布服务仍可访问、磁盘与应用数显示新权益，再重复请求验证无重复订单。失败和未执行检查写入独立验收记录。

依据：[Kubernetes PVC 扩容](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#expanding-persistent-volumes-claims)、[Longhorn 扩容](https://longhorn.io/docs/1.13.0/nodes-and-volumes/volumes/expansion/)。具体 Kata 环境能否在线完成以实际观测为准。
