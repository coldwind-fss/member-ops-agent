# 服务器部署与验收

Linux 部署使用 Python 3.14.6、PostgreSQL 18 和 systemd。前端用 Vite 构建后由 FastAPI 同源提供。外部商家、微信群与通知通过测试适配器演示。官方 DeepSeek 使用独立的密钥配置。

| 资源 | 路径或监听 |
| --- | --- |
| 发布与当前版本 | /opt/private-ops/releases、/opt/private-ops/current |
| 应用、模型环境 | /etc/private-ops/runtime.env、/etc/private-ops/model.env |
| 数据库目录 | /var/lib/private-ops/pgdata |
| API 与后台 | 127.0.0.1:18404 |
| 独立 PostgreSQL | 127.0.0.1:55434 |
| systemd 服务 | private-ops-postgres、private-ops-api、private-ops-worker |

完整回归使用独立数据库并串行启动测试进程。在多服务共用的主机上，部署验收使用 1.5 GiB swap。使用 uv 安装 Python 时，需给 `private_ops` 服务组授予运行时目录的读取和执行权限。

## 访问后台

将示例主机、SSH 用户及私钥路径换成自己的配置。在终端保持隧道开启，然后打开 [运营后台](http://127.0.0.1:18404/)。

```powershell
ssh -i "PATH_TO_SSH_KEY" -o ExitOnForwardFailure=yes -N -L 18404:127.0.0.1:18404 deploy@example.com
```

首次部署时用管理命令创建运营账号和密码。配置 `OPERATOR_ORIGIN` 时使用浏览器实际访问的来源。配置 HTTPS 域名后 Cookie 按该来源启用 Secure。

## 更新部署

1. 使用锁文件安装 Python 依赖，在 Node 24 执行 `npm ci` 和 `npm run build`。服务器安装 Linux 对应架构的包。构建产物放进同一发布目录。
2. 服务账号需能读取发布目录和写入专用数据库。发布前备份目标数据库，从新发布的 `backend` 目录执行 `python -m alembic upgrade head` 和 `python -m app.manage checkpoints`。首次配置用 `python -m app.manage operator operator` 初始化运营账号。演示环境使用 `python -m app.manage seed` 加载虚构知识。
3. 将 `/opt/private-ops/current` 切换到新目录，安装并重载 [systemd 单元](../deploy)，重启 API 与 Worker。
4. 检查 `systemctl is-active private-ops-postgres private-ops-api private-ops-worker` 和 `curl -fsS http://127.0.0.1:18404/api/v1/health/ready`。从浏览器登录并查看工作台。
5. 接口和业务验收从独立测试数据库运行。预留测试端口 `18405`，用 `PRIVATE_OPS_PORT` 启动测试 API。运行 `python -m pytest backend/tests -q` 前，分别设置 `WP01_TEST_DATABASE_URL` 到 `WP04_TEST_DATABASE_URL`。

运行日志使用 `journalctl -u private-ops-api -u private-ops-worker`。真实供应商仍需在各适配器接入凭据、验证签名和回执。浏览器和服务端都可以经现有 SSH 通道验收。
