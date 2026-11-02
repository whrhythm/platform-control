# 用户空间文件服务契约（实现中）

该文档描述第一阶段需要达到的行为；它不是已部署的 HTTP API。用户只能访问经过控制面身份验证与空间归属校验后，为本人指定的工作卷。浏览器、用户提供的文件名和路径不能决定宿主机路径、其他用户身份或挂载卷。

- 在工作卷相对路径中列目录、创建目录、读取及上传文件。拒绝绝对路径、空段、`.`、`..`、反斜线、NUL、所有符号链接和具有多个硬链接的文件；只允许常规文件与目录。后续要分别定义隐藏文件和特殊文件的产品规则。
- 上传按流处理，限制单文件大小；先写同目录临时文件，再原子发布。默认拒绝覆盖；上传中断或超出限制时保持原文件不变。发布完成后的目录同步若报错，结果可能已生效，调用方必须重新查询，不得假定没有写入。不能把临时文件显示给用户。
- 用户卷的硬容量由存储系统执行，文件服务的单次上传上限不等于总配额。容量不足及临时空间耗尽时返回可理解的错误；临时文件、并发上传和未释放空间也必须考虑。
- 认证、授权、用户与卷绑定、请求限流、操作审计和跨容器传输必须在对外暴露前完成；文件内容、密钥和用户原始路径不得写入日志。当前阶段没有对浏览器开放文件接口。
- 删除用户资料必须明确授权、确认范围、考虑备份与保留期；此阶段不提供递归删除或自动回收用户数据。

验收至少覆盖双用户隔离、路径穿越、符号链接、并发覆盖、超限或磁盘满时原文件保留、断开连接后临时文件清理、重启及卷重新挂载后的持久化。生产是否支持大文件分片上传、下载 Range、病毒扫描和乐观并发控制仍需确定并验证。

目录文件描述符与 `O_NOFOLLOW` 可阻止已测试的路径穿越与符号链接访问，硬链接数检查是额外防护，但均不替代独立用户卷、授权及网络隔离。敏感凭据不得放在用户代码可读的同一文件系统中；硬链接检查不能消除用户代码已经拥有的文件访问权限。同一用户的 Agent 与文件服务若能并发修改同一目录，普通原子覆盖采用最后一次成功写入，不提供可靠的跨进程乐观并发控制；父目录重命名等并发对抗场景也尚未验证。在完成这些防护前不能声称已具备完整的恶意代码隔离。进程被强制杀死后残留的临时文件也需要安全对账清理。

计划中的单卷内部 HTTP 服务接受服务间 Bearer 凭据；它不接受用户 ID 或其他卷路径，只操作启动时绑定的一个工作卷。服务间凭据必须由控制面绑定到该用户、单独生成和撤销，网关必须先验证用户身份及空间权限，再经过加密且受限的连接转发；内部服务本身不提供用户身份校验。未实现这些条件前不得部署或对外暴露。单个请求的传输上限和并发上限与总存储配额是不同限制。

## 2026-11-02 实现与验证

`control_plane/workspace_files.py` 提供尚未对外开放的本地文件操作基础；它不负责身份验证、用户与卷绑定、总配额或资源生命周期。自动测试覆盖相对路径校验、符号链接和多重硬链接拒绝、分块写入、大小限制、显式覆盖、并发创建、上传中断与模拟写盘失败。

`scripts/workspace_files_cluster_acceptance.py` 在临时创建的两组 Kata 沙箱和 Longhorn 卷中运行上述代码，检查各自文件、上传大小限制、符号链接和硬链接保护以及暂停恢复后文件保留；还在沙箱回环地址运行内部 HTTP 服务，验证无凭据拒绝、持测试凭据上传和读取。脚本会回收并核查测试 namespace、PV 和 Longhorn 卷。该测试不证明跨 Pod 加密传输、浏览器授权、文件服务网络安全或异常断电恢复。真实用户工作空间、浏览器上传和资源控制器尚未接入。

`control_plane/workspace_file_service.py` 新增**未部署**的单卷内部 HTTP 服务：服务凭据认证后可创建和列出目录、上传与读取文件；默认不覆盖，单次文件传输和并发内存有界。对应契约为 `contracts/workspace-files.openapi.json`，本地 HTTP/Postman 和自动化测试使用临时目录及合成凭据。默认监听 `127.0.0.1` 且关闭访问日志；只有同时提供 TLS 证书和私钥时才允许监听非回环地址。本地以临时证书验证了可信证书、未受信证书和服务凭据。该服务仅验证服务间凭据，不能识别或授权最终用户；工作卷绑定、单用户密钥安全发放、生产证书签发及轮换、客户端校验证书、请求时限、审计和访问网关未完成，不得绕过这些条件直接暴露。当前上传在有界内存中缓冲后再原子写入，并非端到端流式上传。
# Personal environment lookup and storage retention (2026-11-02)

`GET /v1/me/environment` returns only the environment record bound to the verified issuer and subject, or 404 if the user has not requested one. It does not create a workspace or demonstrate readiness. The response is not cached. The existing project-scoped request remains the entry point for creating plan metadata; concurrent requests are arbitrated by the database identity constraint.

Workspace plans now refer to `codechat-workspace-retain`; the Argo CD prerequisite deploys this Longhorn class with `Retain`. A disposable cluster test verified that deleting a PVC leaves its PV in Released and its Longhorn volume present, then explicitly reclaimed only the identified test volume. This test did not validate file restoration after rebinding, a backup, authorization to reclaim user data, or protection from node/disk loss. The live API image has not been updated for the new endpoint or plan.
# Workspace volume identity verification (2026-11-02)

Migration `0005_workspace_bindings` introduces a record for the environment, namespace UID, PVC UID, PV UID and Longhorn volume handle. Unique constraints prevent a recorded identifier from being assigned to two environment records. A downgrade refuses to drop a nonempty binding table. The migration and validator are local source changes and have not been deployed or run against the live database.

`workspace_binding.verify_volume_binding` checks the authenticated user's derived namespace against previously recorded identifiers and live observations, including PVC/PV claim references, the Longhorn driver and Retain policy. Its `ready` result describes the storage objects only; it must never be treated as sandbox, application, or backup readiness. A replaced object with the same name fails verification. Kubernetes reads are not transactional snapshots, so a future caller must revalidate on access and guard mutations with UID/resource-version preconditions.

This code has no Kubernetes access, creator, public endpoint or production binding writer. The future controller must persist a durable intent and provenance of every successful create response, handle crashes between each external create and database commit without adopting unrelated pre-existing objects, and authorize operations before any production apply. Binding values must never come from a client or be inferred merely from labels. The binding record is not a backup; storage restore, quota enforcement, concurrency with user code, and controlled user-data deletion remain separate unfinished work.
# Create-only provisioning journal prototype (2026-11-02)

Migration `0006_workspace_intents` adds a journal keyed by environment. The internal prototype first commits `namespace_inflight` or `pvc_inflight`, calls an injected provider once, validates the response and records its UID. Namespace identity is read and checked again before PVC creation. It does not set environment or volume readiness. Concurrent PostgreSQL workers serialize on environment then intent. A repeated call observing an in-flight state does not create or adopt anything.

A timeout, conflict, process failure, malformed response or a failed commit after a provider call leaves an in-flight state. This is deliberately an **unknown outcome**, not proof that the provider failed, and it requires manual investigation; no automatic recovery, retry, adoption or deletion is implemented. Failed reads before a PVC create also conservatively leave `pvc_inflight`. A downgrade refuses to discard journal entries. Database owner-level operations can still modify or delete records; this is not tamper-proof audit storage.

The journal now has a restricted Kubernetes adapter but remains disconnected from any production worker or authenticated trigger. Before enabling it, the production controller requires admission/RBAC that prevent namespace deletion/recreation between verification and PVC creation, PV readiness and binding persistence, and audited operator recovery for unknown outcomes. No live user resources have been created by this prototype.

## 2026-11-02 Kubernetes 适配与隔离顺序

`KubernetesWorkspaceProvider` 只允许为绑定身份生成的指定资源执行创建或读取，不支持任意路径、更新或删除。正常连接要求 HTTPS、证书校验和有限超时；仅测试可以显式启用回环地址 HTTP。集群执行器目前没有部署，也没有获得集群 RBAC。

迁移 `0007_workspace_isolation` 为配额和网络策略增加执行记录。原型按 namespace → quota → default-deny network policy → PVC 的顺序操作，每次先提交执行状态；创建 PVC 前重新核对配额和网络策略的 UID、配置及命名空间 UID。状态为 in-flight 时不会重复调用。策略被替换或放宽会停止流程。默认拒绝不保证 DNS 和模型服务可达，也无法单凭策略对象证明网络插件已执行规则；策略可叠加，必须防止用户创建放行策略。

本地 Postman 模拟验证了真实 HTTP 适配器与创建流程、同名冲突和创建次序。真实集群测试在随机命名空间创建并读取配额与网络策略，PVC 只进行服务端 dry-run，测试命名空间通过 UID 前置条件删除。另用两个临时 Kata Pod 验证了基线可连接、目标入口拒绝阻断连接、恢复连接后来源出口拒绝也阻断连接，测试资源已清理；这只证明当前集群与该测试流量组合，不保证所有协议或其他策略组合。该验证不等于生产控制器可用：尚未部署、未做真实用户卷创建或卷绑定，无法从 LibreChat 使用。Kubernetes 官方说明：[网络策略](https://kubernetes.io/docs/concepts/services-networking/network-policies/)、[PVC API](https://kubernetes.io/docs/reference/kubernetes-api/core/persistent-volume-claim-v1/)。

`scripts/workspace_provisioning_cluster_acceptance.py` 使用临时 SQLite 日志、随机测试身份及现有 kubectl 代理，在真实集群依序创建 namespace、quota、network policy 和保留策略的 Longhorn PVC，确认 PVC 绑定以及 PV 的声明 UID、驱动和回收策略。测试完成后，按资源 UID 和声明 UID 核验并回收临时 PVC、PV、Longhorn 卷及 namespace。此验证尚未运行生产 PostgreSQL 控制器，未将 PVC/PV 信息写入生产绑定表，也未验证用户文件恢复。

## 卷绑定观察与持久记录（原型）

`observe_and_record_volume` 只接受完成 PVC 创建日志的环境，重新读取并核对命名空间、配额和网络策略，然后根据已记录的 PVC UID 读取 PVC 及其指向的 PV。PVC 尚未绑定时返回 `pending`；PVC/PV 双向关联、Longhorn 驱动和 `Retain` 策略验证通过后，记录命名空间、PVC、PV 及后端卷标识。提交前再次锁定并核对数据库中的环境和操作状态；重复观察同一卷不会重复记录，已记录的卷身份发生变化时拒绝替换。

本地模拟和真实集群临时卷均已验证上述流程，真实集群测试使用临时 SQLite，执行结束后回收测试卷。返回的 `bound` **仅表示这次观察到 PVC/PV 已绑定**，不代表 Longhorn 副本健康、文件可读写、用户环境就绪或数据已备份。Kubernetes 多次读取不是原子快照，使用卷前仍须重新核验并限制命名空间变更；现有原型没有接入生产 worker、RBAC 或真实用户，也没有自动恢复和销毁用户数据的流程。
