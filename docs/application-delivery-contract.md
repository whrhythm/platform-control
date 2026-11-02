# 项目发布执行约定

用户从 LibreChat 个人空间中的工程目录发起发布，在同一侧栏看到任务进度、访问地址和应用状态。编程聊天保持在主界面。首版约定工程根目录包含 Dockerfile，HTTP 服务监听 0.0.0.0:8080。

## 用户接口

- `POST /internal/v1/workspace/apps?path=<工程目录>`：验证服务身份及用户归属，复用同一路径的应用记录；已有进行中任务返回原任务，不创建重复构建。完成后的再次发布生成新版本。
- `GET /internal/v1/workspace/apps`：仅列出当前用户应用，返回构建/发布状态、镜像引用、访问地址和可恢复的错误码。
- 生命周期操作通过应用 ID 和所需状态记录为控制器任务；停止或删除应用不删除个人磁盘。
- LibreChat 通过同源 BFF 转发，浏览器不接触模型密钥、Kubernetes 凭据、Harbor 管理员凭据或项目 robot secret。

## 聊天与后台发布

编程任务每轮最多执行 180 秒，超时请求 SDK 中断并结束聊天流；已生成文件保留，不自动重试。停止生成同样会请求中断，重启执行器会将未完成任务标记为 interrupted。

Agent 发布工具只提交意图，不阻塞等待镜像构建。终端返回发布回执后，执行器直接结束当前 Agent 轮次，返回任务 ID 和当前状态；不再让模型自行轮询、重试或为了绕过基础设施错误替换用户 Dockerfile。失败需要用户新指令后再操作。

`X-CodeChat-Task-ID` 将同一聊天任务内的发布请求关联起来：即使该次构建已经失败，重复提交也返回原记录，不启动新版本；新一轮用户指令才可以触发新构建。未携带此头的界面发布保持原有的进行中任务复用行为。

## 执行过程

1. 每个个人环境关联一个私有 Harbor project 和只具备该 project pull/push 权限的 robot。先持久记录创建意图，再将 Harbor 返回的一次性生成密钥立即加密保存；创建结果不明时不自动重建。Harbor v2.15.4 实际创建接口不采用预先传入的 secret，不能只依据 Swagger 字段假设它会采用。身份关联和加密凭据由控制平台保管。
2. 文件服务导出工程快照。只包含普通文件，跳过链接和常用依赖缓存，限制文件数量及总大小；Dockerfile 中的构建过程由构建器执行。
3. 每次构建使用独立 Kata Job，运行 BuildKit；不挂载宿主 Docker socket，不向用户执行进程提供集群凭据。构建环境及其临时凭据随 Job 回收，个人 PVC 保留。
   BuildKit 临时层写入 Kata 虚拟机内的 1 GiB tmpfs，避免共享目录缺少 xattr 导致镜像导出失败；此目录不承载用户持久文件，受构建容器 2 GiB 内存上限约束。
4. BuildKit 推送版本镜像。控制器观察构建成功，并从 Harbor 获取实际镜像 digest，随后将该 digest 部署到用户 namespace。
   BuildKit daemon 的 registry CA 与 buildctl 会话中获取 Harbor token 的客户端信任分别配置；后者通过 SSL_CERT_FILE 加载相同内部 CA，不跳过 TLS 校验。
5. 部署完成并通过 HTTP 探测后才返回可访问状态。局域网无自有域名时，使用 `<应用标识>.192.168.10.30.sslip.io:30097` 独立域名，根路径直达应用，与 LibreChat 登录入口分开。已实际验证该域名模式解析至集群 IP。首版为局域网 HTTP 入口；以后接入用户自己的域名和公开受信任的 HTTPS。

## 可观察与失败

失败计算回收：构建控制器根据记录的 Job UID 请求前台删除，连带回收其 Pod 和临时凭据；首次部署失败将已记录的 Deployment 缩为零；有 last_good 时恢复其镜像和模板。文件、数据库和个人 PVC 不参与该清理。清理状态为 pending/complete，未完成时禁止显式重试，失败任务记录保留。Job TTL 默认为 300 秒，可通过 PLATFORM_BUILD_JOB_RETENTION_SECONDS 配置；运行 deadline 仍限制构建时间。

新版本只有在当前 generation 已观察，replicas/updatedReplicas/readyReplicas/availableReplicas 全为 1，入口 Accepted，且 HTTP 探测通过后才可用。不能因为旧 Pod 仍正常服务就把新失败版本标记成功。迟到观察通过版本和目标状态校验，不能覆盖用户新的发布或停止指令。

提供排队、构建中、部署中、运行、停止、失败状态。外部写入前记录意图，按已记录 UID 操作资源；状态不明时保留现场，不自动重复创建。用户数据和源码不写入共享日志。发布失败保留原个人文件，不能把计划、Dockerfile 或单独的镜像推送标记为应用发布成功。

应用日志和资源用量由控制器从该应用 Pod 读取并在同一应用面板展示。日志仅保留最近 50 行（最多 16 KiB），资源百分比对应容器 CPU 1 核/内存 512 MiB 的上限；无指标时显示不可用，不伪造零值。首版每用户最多 3 个应用；付费容量与应用数需要进一步接入套餐权益模型，再扩展数据库、对象存储及应用持久数据卷。

Harbor 普通服务账号负责创建自己拥有的私有项目，用户构建使用项目级 robot。现有 Harbor 的 project_creation_restriction=everyone，不修改全局账号/项目创建策略。服务账号和用户 robot 均不是 Harbor 系统管理员。Harbor 网页与 Authentik 统一登录、真人成员映射另行实现，不将服务账号提供给普通用户。

`codechat-base` 是平台管理的共享基础镜像目录，只放未加入用户数据的公开上游 Python 基础镜像，用户可拉取，不能使用个人 robot 推送到该目录。用户编译的业务镜像放入自己的私有 project。

发布控制器不使用 ClusterRoleBinding。构建 Job/临时 Secret 的创建权限仅通过 codechat-builds 中的 Role 授予；个人应用的发布、日志和用量权限，通过空间控制器在已绑定的用户 namespace 内创建固定 RoleBinding 授予。发布控制器没有读取或删除 Secret 的权限。特权构建容器限于 Kata 虚拟机内，准入规则拒绝宿主路径、宿主网络/PID/IPC 和 API token 挂载。
