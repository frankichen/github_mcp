# LensHub 私有 CI 部署文档

> 最后更新：2026-07-24 | 维护者：xiaowu
> 部署环境：新 Ubuntu 物理机 (gongshi-pc) + de 云服务器

---

## 一、网络拓扑

```
┌─────────────────────────────────────────────────────────────┐
│                     公共互联网                               │
│                                                             │
│  MyGithub10 (GitHub App) ────→ frankichen/sxt PR           │
│  Web MCP 客户端                                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              de 服务器 (Hetzner 德国)                        │
│  ┌──────────────────────────────────────────┐               │
│  │ 主机名: s201516.love-is.nexus            │               │
│  │ 公网 IP:  94.159.102.167                 │               │
│  │ Tailscale: 100.118.124.97 (exit node)    │               │
│  │ OS: Ubuntu / Linux 6.8.0-106-generic     │               │
│  └──────────────────────────────────────────┘               │
│                                                             │
│  ┌ Nginx (反向代理) ─────────────┐                          │
│  │  HTTP → HTTPS 重定向          │                          │
│  │  代理 /internal/ci/* → 8788   │                          │
│  └───────────────────────────────┘                          │
│                  │                                          │
│  ┌ Docker: github-action-service ──────────────────────┐    │
│  │  Image: github-action-service:mygithub10-10.0.0-pr4 │    │
│  │  Ports: 100.118.124.97:8788 → 8000                  │    │
│  │  Volume: /var/lib/docker/volumes/.../github_action_data │ │
│  │  DB: ci.db, deployments.db, idempotency.db          │    │
│  │  Secrets: /opt/github-action-service/secrets/       │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌ Docker: registry-mirror ──┐                              │
│  │  100.118.124.97:5555      │                              │
│  └───────────────────────────┘                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
              Tailscale 加密隧道
              (Worker 主动 → Controller :8788)
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│         新 Ubuntu 物理机 (gongshi-pc)                        │
│  ┌──────────────────────────────────────────┐               │
│  │ 主机名: ubuntu                            │               │
│  │ LAN IP:  192.168.1.183/24                │               │
│  │ Tailscale: 100.127.108.20                 │               │
│  │ OS: Ubuntu 26.04 LTS (Resolute Raccoon)  │               │
│  │ Kernel: 7.0.0-28-generic                 │               │
│  │ CPU: Intel, 内存: 30GB, 磁盘: NVMe 512GB │               │
│  └──────────────────────────────────────────┘               │
│                                                             │
│  ┌ systemd 服务 ────────────────────────────────────────┐   │
│  │  private-ci-agent.service (enabled) ← CI Worker      │   │
│  │  private-ci-deploy-executor.service (enabled)         │   │
│  │  tailscaled.service (enabled)                         │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌ CI Worker 详情 ─────────────────────────────────────┐    │
│  │  Worker ID: wsl-ci-01                                │    │
│  │  运行用户: ciworker (uid=1500, gid=1500)              │    │
│  │  服务类型: 系统级 systemd service                     │    │
│  │  工作目录: /srv/private-ci/agent                     │    │
│  │  HOME: /home/ciworker                                │    │
│  │  XDG_RUNTIME_DIR: /run/user/1500                     │    │
│  │  DBUS: unix:path=/run/user/1500/bus                  │    │
│  │  Profiles: repo-auto-check, python-check,            │    │
│  │            go-check, node-check                       │    │
│  └───────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌ Rootless Podman (ciworker) ──────────────────────────┐   │
│  │  graphRoot: /srv/private-ci/podman-storage            │   │
│  │  runRoot:  /run/user/1500/containers                  │   │
│  │  driver: overlay, cgroup: v2 (cgroupfs)              │   │
│  │  OCI runtime: crun                                   │   │
│  │  代理: HTTP_PROXY=http://127.0.0.1:10808              │   │
│  └───────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌ Docker (rootful) ────────────────────────────────────┐   │
│  │  lenshub-postgres (:5432)    — unless-stopped        │   │
│  │  lenshub-redis (:6379)       — unless-stopped        │   │
│  │  lenshub-rabbitmq (:5672)    — unless-stopped        │   │
│  │  logstack-loki-1 (:3100)     — unless-stopped        │   │
│  │  github-action-service (:8765)— unless-stopped        │   │
│  └───────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、关键路径与配置文件

### Worker 服务

| 文件 | 用途 |
|------|------|
| `/etc/systemd/system/private-ci-agent.service` | Worker 主 unit |
| `/etc/systemd/system/private-ci-agent.service.d/dev-mirrors.conf` | 国内镜像源 (GOPROXY, NPM 等) |
| `/etc/systemd/system/private-ci-agent.service.d/go-image.conf` | Go 镜像前缀 |
| `/etc/systemd/system/private-ci-agent.service.d/rootless-podman.conf` | Podman user session 依赖 + DBUS |
| `/etc/systemd/system/private-ci-agent.service.d/podman-storage.conf` | ReadWritePaths 追加 |
| `/srv/private-ci/agent/bin/private-ci-preflight` | 启动前检查脚本 (已 patch) |

### Worker 配置

| 文件 | 用途 |
|------|------|
| `/etc/private-ci/worker.env` | Worker token (权限 640 root:ciworker) |
| `/etc/private-ci/proxy.conf` | 代理策略 (静态) |
| `/etc/private-ci/profiles.yml` | CI Profile 定义 |
| `/etc/private-ci/repositories.yml` | 仓库白名单和 workspace 配置 |
| `/srv/private-ci/run/proxy.runtime.conf` | 代理运行时配置 (动态生成) |

### ciworker Podman 配置

| 文件 | 用途 |
|------|------|
| `/home/ciworker/.config/containers/storage.conf` | graphRoot 指向 /srv/private-ci |
| `/home/ciworker/.config/containers/containers.conf` | cgroupfs + 代理 |

### CI Agent 代码

| 文件 | 用途 |
|------|------|
| `/srv/private-ci/agent/private_ci_agent/main.py` | Agent 主入口 |
| `/srv/private-ci/agent/private_ci_agent/podman.py` | PodmanRunner (image_available 已 patch) |
| `/srv/private-ci/agent/private_ci_agent/services.py` | ServiceManager (PG/Redis/RabbitMQ) |
| `/srv/private-ci/agent/private_ci_agent/executor.py` | JobExecutor |
| `/srv/private-ci/agent/run-agent-with-proxy.sh` | 代理启动脚本 |

### Controller (de 服务器)

| 文件/路径 | 用途 |
|------|------|
| Docker 容器: `github-action-service` | Controller 主服务 |
| Volume: `/var/lib/docker/volumes/github-action-service_github_action_data/_data/` | 数据目录 |
| `ci.db` | CI 数据库 (workers, jobs, steps, logs) |
| `deployments.db` | 部署记录 |
| `/opt/github-action-service/secrets/` | Secret 挂载目录 |

---

## 三、已修复问题清单

### 问题 1：Podman rootless 存储只读 → Agent 无法启动

**现象：** `private-ci-agent.service` 启动失败，preflight 报 `podman unshare failed`

**根因链：**
```
ProtectSystem=strict + ProtectHome=read-only
  → /home/ciworker 只读
  → podman graphRoot 默认在 /home/ciworker/.local/share/containers
  → 无法写入 → 启动失败
```

**修复：**
1. 将 graphRoot 移到 `/srv/private-ci/podman-storage`（通过 `/home/ciworker/.config/containers/storage.conf`）
2. 在 service 中添加 `ReadWritePaths=/srv/private-ci/podman-storage`
3. Patch preflight：podman unshare 失败降级为 warning + 启动前预清理

---

### 问题 2：`newuidmap`/`newgidmap` 所有者异常

**现象：** `podman unshare` 间歇性失败

**根因：** `/usr/bin/newuidmap` 和 `/usr/bin/newgidmap` 所有者为 `nobody:nogroup`（Reasonix 沙箱视角误判，实际系统上为 `root:root`）

**修复：** 确认系统上实际为 `root:root` 且 setuid 位正常，无需修改。沙箱显示是 misreport。

---

### 问题 3：CI 镜像 unavailable → `CONFIGURATION_ERROR`

**现象：** CI Job `3101a32bc36b48ed` failed, exit=2:
```
CONFIGURATION_ERROR: CI image is unavailable: docker.io/library/golang:1.26.4
CONFIGURATION_ERROR: CI image is unavailable: docker.io/library/node:22
```

**根因链：**
```
CI 代理 → 直连 Docker Hub 不通（需要代理）
  → podman pull 失败
  → image_available() 强制 podman pull → 返回 False
  → 虽然 image exists 成功，但逻辑先 pull 后 exists → pull 失败就报 unavailable
```

**修复：**
1. 配置 ciworker podman 代理：`/home/ciworker/.config/containers/containers.conf` 中设置 `HTTP_PROXY=http://127.0.0.1:10808`
2. Patch `podman.py` 的 `image_available()`：先 `podman image exists`（fast path），本地存在则跳过 pull
3. 预拉取 CI 所需镜像到 ciworker 存储

---

### 问题 4：`/var/tmp` 只读 → `podman pull` 失败

**现象：** podman pull 报错 `mkdir /var/tmp/container_images_storage*: read-only file system`

**根因：** `ProtectSystem=strict` 使 `/var/tmp` 只读，podman pull 需要在此创建临时目录

**修复：** Patch `image_available()` 先检查本地缓存（见问题 3）。对于初始镜像拉取，使用 root + ciworker 存储配置绕过限制。

---

### 问题 5：crun sd-bus OCI 权限错误 → 容器无法启动

**现象：** CI Job `cc03b365d0144262` failed, exit=1:
```
Error: crun: sd-bus call:
Access denied as the requested operation requires interactive authentication.
OCI permission denied
```

**根因链：**
```
之前 pkill -9 -u 1500 → 误杀 user@1500.service (systemd user manager)
  → /run/user/1500/bus 消失
  → Worker 进程缺失 DBUS_SESSION_BUS_ADDRESS
  → Podman 无法通过 systemd user manager 管理 cgroup
```

**修复：**
1. 重启 `user@1500.service`
2. 在 Worker service 中添加 `Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus`
3. 添加 `Requires=user@1500.service` 和 `After=user@1500.service`
4. 设置 `cgroup_manager = "cgroupfs"`（备选方案，避免 systemd delegation 问题）

---

### 问题 6：PostgreSQL 隔离容器启动失败 → `POSTGRES_UNAVAILABLE`

**现象：** CI Job `115a571890fb485d` failed:
```
[services:prepare] Starting isolated PostgreSQL/Redis/RabbitMQ
[services:failed] POSTGRES_UNAVAILABLE
```

**根因：** CI 服务镜像（`postgres:16-alpine`, `redis:7-alpine`, `rabbitmq:3-management-alpine`）未预加载。`ServiceManager._run()` 直接调用 `podman run`，本地无镜像 → podman 自动 pull → `/var/tmp` 只读 → 失败。

**修复：** 通过 root 拉取 3 个镜像到 ciworker 存储，chown 修复所有权。

---

## 四、必要的 CI 镜像清单

所有镜像必须预加载到 ciworker 的 Rootless Podman 存储中：

| 镜像 | 用途 | 大小 |
|------|------|------|
| `docker.io/library/golang:1.26.4` | Go workspace | ~900 MB |
| `docker.io/library/node:22` | Node workspace（无浏览器 smoke） | ~1.16 GB |
| `100.118.124.97:5555/library/node-chromium:22` | Node 浏览器 smoke 受控运行时（含 Chromium 系统库） | ~1.5 GB |
| `docker.io/library/alpine:latest` | 基础回退镜像 | ~9 MB |
| `docker.io/library/postgres:16-alpine` | CI 隔离 PG | ~297 MB |
| `docker.io/library/redis:7-alpine` | CI 隔离 Redis | ~40 MB |
| `docker.io/library/rabbitmq:3-management-alpine` | CI 隔离 RabbitMQ | ~180 MB |

`node-chromium:22` 由 `services/private-ci-agent/deploy/Dockerfile.node-chromium` 构建
（`FROM docker.io/library/node:22` + Chromium 系统依赖），构建后推送到受控 registry
并以 root 预加载到 ciworker 存储。`prepare-playwright-cache` 与浏览器 smoke 工作区
只读使用该镜像，仓库输入不能指定任意镜像。

**拉取命令模板** (以 root 执行)：
```bash
export XDG_RUNTIME_DIR=/run/user/1500
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus
export HOME=/home/ciworker
export CONTAINERS_STORAGE_CONF=/home/ciworker/.config/containers/storage.conf
export CONTAINERS_CONF=/home/ciworker/.config/containers/containers.conf

/usr/bin/podman pull docker.io/library/<image>:<tag>
chown -R 1500:1500 /srv/private-ci/podman-storage
chown -R 1500:1500 /run/user/1500/containers
```

---

## 五、常用运维命令

### 查看 Worker 状态
```bash
systemctl status private-ci-agent.service
journalctl -u private-ci-agent.service -n 50 --no-pager
```

### 查看 ciworker Podman
```bash
# 以 root 执行：
runuser -u ciworker -- env XDG_RUNTIME_DIR=/run/user/1500 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus /usr/bin/podman images
runuser -u ciworker -- env XDG_RUNTIME_DIR=/run/user/1500 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus /usr/bin/podman ps -a
```

### 查看 systemd user session
```bash
systemctl status user@1500.service
loginctl show-user ciworker -p Linger -p State
ls -l /run/user/1500/bus
```

### 测试容器运行
```bash
# 确认节点可见：
runuser -u ciworker -- env XDG_RUNTIME_DIR=/run/user/1500 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus /usr/bin/podman run --rm docker.io/library/node:22 node --version

# 确认 go 可见：
runuser -u ciworker -- env XDG_RUNTIME_DIR=/run/user/1500 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus /usr/bin/podman run --rm docker.io/library/golang:1.26.4 go version
```

### 心跳验证
```bash
TOKEN="<从 worker.env 获取>"
curl -s --noproxy '*' -H "Authorization: Bearer ${TOKEN}" -H "X-Worker-ID: wsl-ci-01" \
  "http://100.118.124.97:8788/internal/ci/workers/heartbeat" -X POST -d '{}'
```

### de 数据库查询
```bash
ssh root@100.118.124.97 "sqlite3 /var/lib/docker/volumes/github-action-service_github_action_data/_data/ci.db 'SELECT worker_id, status, last_heartbeat FROM ci_workers'"
ssh root@100.118.124.97 "sqlite3 /var/lib/docker/volumes/github-action-service_github_action_data/_data/ci.db 'SELECT job_id, status, exit_code FROM ci_jobs ORDER BY created_at DESC LIMIT 10'"
```

### 重启 Worker
```bash
# 确保 current_job=null 且 status=idle 后再执行：
systemctl daemon-reload
systemctl restart private-ci-agent.service
```

---

## 六、Service 安全限制与注意事项

Worker service 有以下限制，修改配置时必须遵守：

```ini
ProtectSystem=strict      # /usr, /etc, /boot 只读
ProtectHome=read-only     # /home, /root 只读
ReadWritePaths=           # 只有这些路径可写：
  /srv/private-ci/workspaces
  /srv/private-ci/cache
  /srv/private-ci/logs
  /srv/private-ci/run
  /run/user/1500
  /srv/private-ci/podman-storage
NoNewPrivileges=false     # 允许 setuid (newuidmap/newgidmap)
PrivateTmp=no             # /tmp 和 /var/tmp 共享（但只读）
```

**因此：**
- Podman graphRoot **必须**在 `/srv/private-ci/` 下，不能在 `/home/ciworker`
- 任何需要写入 `/var/tmp` 的操作都会失败
- 镜像预拉取**必须**通过 root + ciworker 存储路径完成

---

## 七、快速故障排查流程

```
CI Job 失败？
  ├── status=failed, exit_code=2, "CI image is unavailable"
  │   → 检查该镜像是否在 ciworker podman images 中
  │   → 如缺失，用 root 预拉取 + chown
  │
  ├── status=failed, exit_code=1, "POSTGRES_UNAVAILABLE"
  │   → 同镜像缺失流程，预拉取 postgres/redis/rabbitmq
  │
  ├── status=failed, "crud sd-bus" / "OCI permission denied"
  │   → systemctl status user@1500.service
  │   → 如 failed: systemctl start user@1500.service
  │   → 确认 /run/user/1500/bus 存在
  │   → 确认 DBUS_SESSION_BUS_ADDRESS 已设置
  │
  ├── Worker 无法启动 (preflight 失败)
  │   → 检查 podman unshare 是否 pass
  │   → 如 fail: pkill -9 -u 1500; rm -rf /run/user/1500/{containers,libpod}
  │   → podman system migrate as ciworker
  │
  └── Worker offline / heartbeat 失败
      → Tailscale: tailscale status | grep de
      → 网络: curl --noproxy '*' http://100.118.124.97:8788/
      → Token: 验证 worker.env 中 CI_WORKER_TOKEN 是否与 de 数据库一致
```
