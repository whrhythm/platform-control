# 多租户开发平台控制面

本目录包含现有 Python 雏形扩展后的第一版控制面。主要入口是浏览器；桌面客户端作为可选入口。适用范围是单节点 k3s 上的内部可信用户。

## 当前交付边界

已实现：JWT/JWKS 与 API scope 校验、个人租户、成员角色、平台管理员、项目、五类资源申请、容量预留、幂等键、事务审计、计划 worker、Kubernetes/数据库/对象存储计划、浏览器管理界面与 OpenAPI 契约。

原有资源申请 worker 仍只生成计划：`requested` 表示已受理，`planned` 表示已生成方案。个人空间、编程执行和应用交付由独立控制器处理，不以计划状态冒充运行状态。用户登录后复用一份持久磁盘，文件服务和 OpenHands SDK 工具在该磁盘中工作；没有为每位用户部署完整 OpenHands Web 服务。

本地新增的测试订单仅供模拟：设置 `PLATFORM_TEST_PAYMENTS_ENABLED=true` 和 `PLATFORM_REQUIRE_PROJECT_ENTITLEMENT=true` 后，租户所有者或管理员可创建测试订单，只有平台管理员可模拟确认，确认后该租户才能创建项目。关闭模拟支付后测试订单不能授予项目权益；这不是实际支付、回调验签、订阅或生产计费实现。默认两个开关均为 false，保持旧环境行为；要验证付费门槛须在隔离测试环境同时打开。更改数据库前须先运行 Alembic 迁移并备份。

项目资源回收目前仅限尚未开通的申请：项目所有者或管理员可批量取消项目中全部 requested/planned 计划并释放预留；若发现任何已开通或状态未知的资源，则整批拒绝，必须等待未来真实销毁流程。该接口不删除 Kubernetes 资源或项目数据。

部署位于 k3s 的 `platform-system`，镜像通过内部 Harbor 分发，配置由 ops 仓库与 Argo CD 管理。普通用户入口为 LibreChat，控制平台负责管理员和资源治理。实现包含聊天编程、个人文件管理、Kata 构建、私有 Harbor、应用交付、项目数据库与自动私有对象存储；部署事实见各次验收记录。五类存储按实际来源计量，分类额度独立，真实支付、跨服务统一硬配额和生产级隔离验收仍待完成。

平台地址：`https://platform.k3s.local:30443/`。首次访问需要配置两个域名和内部 CA，详见使用手册；不要直接用 IP 登录。

## 文档

- [模型选择、模拟充值与中文套餐手册](docs/model-membership-guide.md)
- [聊天、模型与套餐改进协议](docs/chat-and-membership-contract.md)
- [个人存储使用手册](docs/storage-accounting-guide.md)
- [五类逻辑计量与对象存储协议](docs/storage-accounting-contract.md)
- [架构和数据流](docs/architecture.md)
- [权限、隔离与配额](docs/security.md)
- [部署、桌面进程边界及验收计划](docs/delivery.md)
- [API 行为](docs/api.md)
- [已执行验证及待验收项](docs/verification.md)
- [实际聊天编程验收](docs/programming-acceptance-20261102.md)
- [应用发布与失败处理约定](docs/application-delivery-contract.md)
- [使用手册](docs/user-guide.md)
- [文件、任务能力、统一容量与套餐演进方案](docs/product-evolution-20261102.md)
- [个人空间完善阶段 A 验收](docs/space-management-acceptance-20261102.md)
- [OpenAPI](contracts/openapi.json)

## 本地运行

Python 3.10+。SQLite 只用于单进程开发；共享服务必须使用 PostgreSQL。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
cp .env.example .env
# 填入实际 Authentik 的 issuer、client id、JWKS 和 audience；不要提交 .env。
set -a
. ./.env
set +a
alembic upgrade head
python -m control_plane --host 127.0.0.1 --port 8088
```

API 启动时验证数据库 revision，不自动建表或升级。使用 Alembic 显式执行迁移；迁移前备份。若保留了旧雏形创建的无版本开发数据库，请先备份，再使用新的数据库验证迁移，不要直接 stamp 跳过 schema 差异。

仓库包含 `uv.lock`；也可使用 `uv sync --frozen --extra test` 安装锁定依赖。不同 Python 版本可能选择锁文件内不同的兼容分支；本地 Python 3.10 和部署镜像 Python 3.12 的回归测试均已通过。

另开终端，同样加载环境变量：

```bash
python -m control_plane.worker
```

浏览器前端：

```bash
cd web
npm ci
npm run dev
# http://localhost:5173，开发服务器将 /v1 代理到 127.0.0.1:8088。
```

前端使用 Authentik Authorization Code + PKCE；不提供绕过认证的开发账号。没有完成 OIDC 配置时，受保护接口返回 503。配置完成后，无 Bearer token 返回 401。

```bash
python -m pytest -q
python scripts/export_openapi.py
# 可选：真实 PostgreSQL 并发测试，需要本地 Docker；结束自动移除临时容器
PLATFORM_TEST_POSTGRES_IMAGE=postgres:16 python scripts/test_postgres.py
cd web && npm run build
```

桌面 Sidecar 打包和启动方式见 [desktop/README.md](desktop/README.md)。不要将集群 kubeconfig、PostgreSQL 管理密码或 MinIO 管理密钥放进桌面应用。
- [使用手册](docs/user-guide.md)
