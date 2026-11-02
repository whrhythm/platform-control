# 验证记录

2026-11-02 最新真实验收见 [应用交付记录](application-delivery-acceptance-20261102.md)：已有用户通过 LibreChat 完成聊天修改代码、测试、私有镜像构建推送、访问 v2/v3、查看日志/用量、停止再启动。下文保留 2026-10-31 的历史范围和结果，其中“尚未证明”仅指当时状态。

日期：2026-10-31。环境：WSL Ubuntu 22.04、Python 3.10.12；前端 Node 22.21.1；临时 PostgreSQL 16 Docker 容器。

## 已执行

| 检查 | 结果 | 证明范围 |
| --- | --- | --- |
| `python -m pytest -q` | 20 passed，3 skipped | 真实本地 RSA/JWKS 验签、scope、角色、租户/项目隔离、幂等、预留与取消、计划、安全模板、数据库复合归属约束、Sidecar、安全代理、迁移 |
| `PLATFORM_TEST_POSTGRES_IMAGE=postgres:16 python scripts/test_postgres.py` | 3 passed | 临时库经 Alembic 初始化；20 并发额度、20 并发相同 key、10 轮 worker/取消竞态；容器结束自动移除 |
| `npm run build`（web） | 通过 | TypeScript 检查、正式前端构建 |
| `cargo fmt --manifest-path desktop/src-tauri/Cargo.toml -- --check` | 通过 | Rust 源码解析与格式；不是 Tauri 完整编译 |
| PyInstaller Linux onefile 构建 | 通过 | 生成约 12.9 MB 的 `dist/platform-sidecar`，包含 Python runtime，独立于 Rust |
| 设置 `PLATFORM_TEST_SIDECAR_BINARY` 后运行实际进程测试 | 1 passed | 打包二进制真实启动、动态 loopback 端口、本地凭据拒绝/允许、父管道 EOF 后退出 |
| OpenAPI 导出 | 已生成 | 契约来自实际类型化路由 |

普通 pytest 中跳过的 3 项就是需真实 PostgreSQL 的并发测试，它们已在单独临时容器运行通过，不是把跳过视为成功。

测试有一个 Starlette 关于 httpx TestClient 的弃用提示，未导致失败；后续依赖升级时需处理对应迁移。

首次尝试下载 PostgreSQL 17 Alpine 镜像因外网超时失败，后改用本地已有 PostgreSQL 16。上述结果不冒充集群 PostgreSQL 17 的验证。

## 尚未证明

- PostgreSQL 17 并发压力、多 API/worker 副本、节点故障与备份恢复。
- 实际创建 Kata 工作空间、执行 Agent 编码/测试、创建数据库/bucket、发布域名与 TLS。
- 项目间真实网络/数据库/MinIO 拒绝、LLM 限额、OTel 用量归属和用户监控页面。
- Tauri Windows/macOS/Linux 安装包、真实窗口关闭和 GUI 强杀、崩溃自动重启的端到端行为。
- 桌面登录、WebSocket/远程终端、托盘和文件对话框。

这些必须在相应阶段完成并更新记录，才能将计划状态提升为实际运行状态。

## 集群部署与真实验收（2026-10-31）

镜像：`harbor.k3s.local/internal/platform-control:0.1.0-20261031.1`，运行清单固定 digest `sha256:fa0cb0fe1e3524034c27e3dbfc87113c3e9bc6c29c7eca8a0fac981c216b93f2`。API 与计划 worker 已由 Argo CD 部署，数据库迁移 hook 成功。

- Python 3.12 的实际镜像内回归：20 passed、3 PostgreSQL 用例另测而跳过。
- 两名临时用户分别通过真实 Authentik Authorization Code + PKCE、授权确认和回调；管理员组和 API scope 生效。
- 浏览器创建空间、项目；实际数据库中的幂等重放、worker planned/executable=false、容量预留/取消释放与审计读取通过。
- 普通用户管理接口 403，跨租户读/写 404。
- 替换 API Pod 后，同一项目记录保持不变。
- 实际 API/worker Pod 的连接使用 verify-full；均非超级用户、不能建库/建角色、不能连接其他数据库、不能更新或删除审计；UID 10001、无 ServiceAccount token。
- 两个 HTTPS 域名使用导出的内部 CA 严格校验通过，证书校验未关闭；OIDC discovery issuer 与配置完全一致。隔离浏览器测试临时接受内部 CA，另有严格 TLS 检查，不代表已向用户日常浏览器安装 CA。
- ApisixRoute/ApisixTls 的 Accepted=True；不只依据 Argo CD Healthy 判断。
- 临时 QA 用户和测试租户元数据已清理，审计保留。原始结果位于本地 release/live-qa-results.json，不包含 token 或密码。

大镜像跨 VPN 传输持续超时，改用 SHA256 校验的小构建包在 edge 重建并推送 Harbor。包依赖从 uv.lock 导出并使用哈希校验；没有将未完成的大镜像归档传输记录为成功。

集群实际节点 PodCIDR=10.42.0.0/24、Calico pool=172.16.0.0/16；服务访问发生节点 SNAT。APISIX 管理接口已改用私有 Secret 并允许必要节点来源，但真实租户运行环境仍需先整改 CIDR，再做网络隔离负向测试。
