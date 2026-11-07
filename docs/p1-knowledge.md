# P1-02 项目知识导入与检索

日期：2026-11-07。状态：实施与验收进行中。不能将本文当作 P1-02 已完成或商业上线证明。

## 本次交付范围

创建者在「我的空间 → 项目 → AI 项目 → 项目知识」导入 UTF-8 Markdown/TXT；页面展示等待索引、正在索引、可检索、失败可重试、清理中。就绪的最新知识用于原生业务助手试用，沿用主聊天面板及文件引用。OpenHands 不参与业务问答，普通聊天文件不自动索引。

每次导入是完整知识修订。修改资料后重新导入全部当前资料；成功前继续使用上次可用修订，失败不覆盖。删除当前修订后停止检索，不自动回退旧资料。此前正式发布与受邀代付尚未交付；旧修订保留为 P1-03 固定发布的基础，而不是已完成发布机制。

## 契约与恢复

- 控制面 `project_knowledge`：原文加密、文件清单、内容摘要、逻辑容量、状态、租约；Alembic `0024_project_knowledge`。`ai_project_bindings.knowledge_revision_id` 指向当前试用。
- 同项目同 request_id 内容一致返回同一记录；内容不同返回冲突。先锁用户环境、检查容量并预留，再排队。知识占用加入已有总空间及版本快照准入，不增加购买容量。
- 知识服务仅集群内部访问，令牌不交给浏览器。专用运行数据库角色无 DDL 权限，迁移凭据仅迁移任务持有；网络只允许控制面 API/索引器访问知识服务，服务仅访问自己的 PostgreSQL/DNS。
- 索引入库原子提交。请求失败可重试，先核验已提交摘要；删除有永久墓碑。处理租约过期可由新索引器恢复，旧持有者不能覆盖新的控制面状态。
- 资料内容视为不可信输入，不赋予工具或代码执行权限。检索以认证归属和服务端修订为边界，不能依靠模型拒绝越权。一次工具实例首个查询后固定修订；跨会话正式版本策略属于 P1-03。
- 所有提供给用户的错误均为固定安全代码，审计只记操作/对象/身份，不记录资料正文、提示或凭据。

## 配置与运维

控制面：`PLATFORM_KNOWLEDGE_ENABLED`、`PLATFORM_KNOWLEDGE_URL`、Secret 中的 `PLATFORM_KNOWLEDGE_SERVICE_TOKEN`。文件数/字节/分块/修订上限以及请求、查询、轮询时限均有 Settings 配置。LibreChat：`aiProjects.knowledgeEnabled` 开关与 `requestTimeoutMs`。关闭入口不删除已有知识。

镜像：知识服务 `harbor.k3s.local/internal/platform-knowledge@sha256:d611310195c5e60e33b9e6e6554a04fdb8752cad90788b15d84401117e57fe10`；向量数据库 `harbor.k3s.local/internal/platform-knowledge-pg@sha256:a17b491c76e1b3938395ed0dbcb98d71a69b40556a6c2d6296ed2b460c59056d`。模型修订见 [选型记录](p1-rag-decision.md)。主机目录 `/home/edge/platform-releases/0.1.0-knowledge-20261107.1/context/model/manifest.json` 保存模型文件摘要。

新增独立 4 GiB Longhorn 数据卷，禁止 ArgoCD prune。没有更换原共享 PostgreSQL 镜像。此卷必须加入后续备份清单；当前单节点及本地副本不能抵御整机损坏。重建索引使用加密原文，必须同步保管控制面备份及原文加密密钥。不能通过清空数据库修复任务。

## 交付证据与遗留门槛

已执行隔离服务测试：真实 CPU 中文模型 + 一次性 pgvector，导入/重复写/查询/不同项目空结果/删除后拒绝重建均通过。用户资料未用于这一测试。服务端清单 dry-run 通过，ArgoCD `knowledge` 已观测为 Synced / Healthy。

第一轮控制面全量测试：356 通过、35 跳过、1 失败，失败是迁移版本期望仍为 0023；已修正，需补跑。新增 5 项导入/项目测试通过。PostgreSQL 并发、LibreChat 构建类型/规范检查、真实 UI、亮暗/窄屏、K01/K02/K04/K05/K07 验收仍在进行。多身份真实 UI、20 次/多日及性能门槛不据此宣称通过。
