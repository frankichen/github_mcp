# 本机 GitHub Action MCP 服务

> 最后更新：2026-07-25 | 维护者：xiaowu
> 相关文档：lenshub-ci-deployment.md（私有 CI Worker 部署）

---

## 一、概述

`github-action-service` 是一个本地 MCP (Model Context Protocol) 服务，为 AI 编程助手提供 GitHub 文件读写、分支创建、PR 管理和 CI 触发能力。与 de 服务器上的同名容器是同一镜像的不同部署实例。

| 属性 | 本机 (gongshi-pc) | de 服务器 |
|------|--------------------|-----------|
| 端口 | `127.0.0.1:8765 → 8000` | `100.118.124.97:8788 → 8000` |
| 网络 | 仅本地回环 | Tailscale 公网 |
| 功能 | MCP + GitHub 操作 + CI 触发 | MCP + GitHub 操作 + CI Controller + Worker 管理 |
| 数据库 | 独立 volume | 独立 volume（含 ci.db） |

---

## 二、部署信息

### Docker Compose

- **路径：** `/home/xiaowu/github-action-service/docker-compose.yml`
- **镜像：** `github-action-service-github-action-service`（本地构建）
- **重启策略：** `unless-stopped`

```yaml
services:
  github-action-service:
    build: .
    container_name: github-action-service
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:8765:8000"
    volumes:
      - github_action_data:/data
```

### 环境变量（脱敏）

| 变量 | 值 | 说明 |
|------|-----|------|
| `GITHUB_TOKEN` | *** | GitHub Classic PAT |
| `ACTION_API_KEY` | *** | MCP 客户端认证 Key |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API 地址 |
| `ALLOWED_REPOSITORIES` | `*` | 允许操作的仓库（* = 全部） |
| `ALLOW_DEFAULT_BRANCH_WRITE` | `false` | 禁止直接写默认分支 |
| `MAX_FILE_CHARACTERS` | `60000` | 单文件读取上限 |
| `MAX_TOTAL_CHARACTERS` | `80000` | 多文件总字符上限 |
| `MAX_FILES_PER_COMMIT` | `20` | 单次提交最大文件数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 三、MCP 工具列表

本服务通过 MCP 协议暴露以下工具，供 AI 编程助手调用：

### GitHub 文件操作

| 工具 | 说明 |
|------|------|
| `get_github_file` | 读取指定仓库的文件内容 |
| `list_github_directory` | 列出目录结构 |
| `commit_github_files` | 创建提交（写入/修改文件） |
| `create_github_branch` | 创建新分支 |
| `create_github_pull_request` | 创建 Pull Request |

### CI 管理

| 工具 | 说明 |
|------|------|
| `start_ci_job` | 启动 GitHub Actions CI Job |
| `get_ci_job` | 查询 CI Job 状态 |
| `get_ci_logs` | 获取 CI Job 日志 |
| `list_ci_jobs` | 列出 CI Job |
| `list_ci_workers` | 列出 CI Worker（GitHub Actions Runners） |
| `list_ci_profiles` | 列出可用的 CI Profile |
| `cancel_ci_job` | 取消 CI Job |

### 诊断

| 工具 | 说明 |
|------|------|
| `diagnose_text_payload` | 诊断文本负载 |

> **注意：** `start_ci_job` 等 CI 工具操作的是 GitHub Actions（公有 CI），不是私有 CI (wsl-ci-01)。私有 CI 由 de 服务器上的 Controller 管理，通过 `start_private_ci_job` 工具触发（同样在本服务中注册）。

---

## 四、HTTP API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/api/v1/github/file` | 读取文件 |
| GET | `/api/v1/github/directory` | 列出目录 |
| POST | `/api/v1/github/commits` | 创建提交 |
| POST | `/api/v1/github/branches` | 创建分支 |
| POST | `/api/v1/github/pull-requests` | 创建 PR |
| GET | `/docs` | Swagger 文档 |
| GET | `/openapi.json` | OpenAPI 规范 |
| GET | `/actions-openapi.json` | Actions 扩展规范 |

---

## 五、源代码结构

```
/home/xiaowu/github-action-service/
├── docker-compose.yml
├── Dockerfile
├── .env                          # 环境变量（含 Secret，勿提交）
├── requirements.txt
├── app/
│   ├── main.py                   # FastAPI 入口
│   ├── mcp_server.py             # MCP 协议实现 + 工具注册
│   ├── config.py                 # 配置管理
│   ├── auth.py                   # API Key 认证
│   ├── github_client.py          # GitHub REST API 客户端
│   ├── models.py                 # Pydantic 数据模型
│   ├── exceptions.py             # 自定义异常
│   ├── idempotency.py            # 幂等性控制
│   ├── oauth.py                  # OAuth 相关
│   ├── routers/
│   │   ├── github.py             # GitHub 操作路由
│   │   └── health.py             # 健康检查路由
│   └── services/                 # 业务逻辑层
└── tests/                        # 测试
```

---

## 六、常用运维命令

### 服务管理
```bash
# 进入项目目录
cd /home/xiaowu/github-action-service

# 启动
docker compose up -d

# 停止
docker compose down

# 查看日志
docker logs github-action-service --tail 50

# 重启
docker compose restart
```

### 健康检查
```bash
curl http://127.0.0.1:8765/health
# → {"status":"ok","service":"github-action-service","github_configured":true}
```

### 测试文件读取
```bash
curl -H "X-API-Key: <ACTION_API_KEY>" \
  "http://127.0.0.1:8765/api/v1/github/file?repository=frankichen/sxt&path=README.md&ref=main"
```

---

## 七、与私有 CI 的关系

```
AI 编程助手
    │
    ├── MCP: github-action-service (127.0.0.1:8765)
    │   ├── GitHub 读写 → api.github.com
    │   ├── start_ci_job → GitHub Actions (公有 CI)
    │   └── start_private_ci_job → de:8788 → wsl-ci-01 (私有 CI)
    │
    └── 直接读取本地文件系统
```

`start_private_ci_job` MCP 工具会将请求转发到 de 服务器上的 Controller，由 Controller 将 Job 入队，wsl-ci-01 Worker 领取并执行。这就是 Web MCP 调用能触发私有 CI 的链路。

---

## 八、注意事项

1. **不要**修改 `.env` 中的 `GITHUB_TOKEN` 或 `ACTION_API_KEY`，这会导致 MCP 认证失败
2. **不要**使用 `docker compose down -v`，这会删除 volume 中的持久化数据
3. 本服务仅监听 `127.0.0.1:8765`，不暴露到局域网或公网
4. 如果 MCP 调用返回 401，检查 `ACTION_API_KEY` 是否与 MCP 客户端配置一致
5. 如果 GitHub 操作返回 403，检查 `GITHUB_TOKEN` 是否过期或权限不足
6. 容器重启后服务在 2-3 秒内恢复
