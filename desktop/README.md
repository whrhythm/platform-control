# 可选桌面客户端

本目录包含 Tauri 2 窗口与进程监督源码，以及独立 Python Sidecar 的入口。业务接口仍在 `control_plane` 中；Rust 没有租户、配额、数据库或 Kubernetes 业务逻辑。

已实现源码行为：

1. Tauri 自动启动 Sidecar，把每次启动的随机本地凭据通过 stdin 管道传入。
2. Sidecar 只绑定 `127.0.0.1:0`，由操作系统分配端口；启动完成后通过 stdout 返回端口和协议版本。
3. React 通过受限的 `sidecar_info` IPC 获取端口及本地凭据，然后直接发 HTTP 请求。
4. Rust 监视子进程退出，按 1/2/4/8/16/30 秒退避自动重启；运行稳定 60 秒后重置退避。前端监听重启事件更新端口。
5. 关闭窗口会终止并回收 Sidecar。GUI 被意外杀死时，Sidecar 因 stdin EOF 退出；Sidecar 当前不派生任何子进程。
6. 本地接口校验 Host、Origin 和随机 token；只代理固定服务端的 `/v1/*`，不开放任意 URL 或系统 shell。租户授权仍由服务端执行。

**当前没有完成各系统的 Tauri 打包和安装验收。桌面登录、远程终端/WebSocket 中继、托盘、弹窗和文件对话框仍是后续工作。** 前端在桌面环境会明确提示使用浏览器登录；不能声称桌面端已可作为完整业务入口。

## 构建

在各目标操作系统上分别构建；PyInstaller 不能把 Linux 打包产物直接用作 Windows/macOS 二进制。

```bash
# platform-control 根目录，当前平台 Python 虚拟环境
python -m pip install -e '.[desktop]'
python -m PyInstaller --onefile --name platform-sidecar \
  --paths . --collect-submodules uvicorn \
  desktop/sidecar_entry.py
```

将产物复制为：

- Windows：`desktop/src-tauri/binaries/platform-sidecar-x86_64-pc-windows-msvc.exe`
- Linux x64：`desktop/src-tauri/binaries/platform-sidecar-x86_64-unknown-linux-gnu`
- macOS ARM：`desktop/src-tauri/binaries/platform-sidecar-aarch64-apple-darwin`

目标三元组以本机 `rustc -vV` 的 host 为准。先在 `web/` 执行 `npm ci && npm run build`，再在 `desktop/` 使用 Tauri 2 CLI 构建。开发模式还需要启动 Vite；`tauri.conf.json` 已配置 devUrl 和构建目录。

本次已生成 `dist/platform-sidecar`（Linux x64），并通过实际启动、HTTP 本地凭据和父管道 EOF 退出测试。该文件是开发验收产物，未签名、未作为 Windows 安装包发布。

`PLATFORM_UPSTREAM` 为服务端 HTTPS origin，例如 `https://platform.k3s.local`。只允许 HTTPS；本地 `http://127.0.0.1`/`localhost` 可用于开发。不得使用 `verify=false`；内部 CA 通过系统信任或 `SSL_CERT_FILE` 配置。

正式验收：连续启动/退出、端口冲突、后端崩溃重启、启动失败退避、窗口关闭、GUI 强杀、安装路径含空格、升级旧进程清理；每项均要确认无残留进程且本地凭据无日志泄漏。

后续远程终端和 WebSocket 需要一次性握手凭据，不能把长效访问 token 放入 URL；服务端在建连、每条命令及成员撤销时重新校验权限。Tauri 不执行用户 shell 命令。
