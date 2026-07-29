# MCP 与 CI 部署服务

本仓库包含四个相互独立的服务边界：

- `services/github-action-service`：现网 MyGithub09 Controller 源码，负责 GitHub API、MCP 工具、CI 查询和部署意图编排。
- `services/private-ci-agent`：Private CI 执行 Agent，负责受控容器任务、日志和源码镜像。
- `services/private-deploy-agent`：服务器 `root@de` 上运行的私有部署 Worker，负责领取部署队列、记录状态并按受控策略执行测试环境部署。
- `services/private-ci-deploy-executor`：WSL/发布 Executor 的脚本、测试和 systemd 模板。

股票选股和股票 MCP 服务已经从仓库移除。

## 两端关系

```text
AI / MCP 客户端
        │
        ▼
github-action-service
  GitHub API / MCP / CI / 部署编排
        │ 受控部署队列
        ▼
private-ci-agent（CI 执行端）
        │ 私有 CI 结果
        ▼
private-deploy-agent（服务器端）
  claim_only 或受控执行
        │
        ▼
测试环境 / 发布工作区
```

本仓库不包含生产环境 Secret、`.env`、数据库、私有 SSH 密钥、Xray 配置和运行数据。部署时必须从 Secret 管理系统或服务器受限环境文件注入。

## 快速入口

- AI 重新部署：[`docs/AI重新部署指南.md`](docs/AI重新部署指南.md)
- 迁移与部署说明：[`docs/迁移与部署说明.md`](docs/迁移与部署说明.md)
- 安全说明：[`SECURITY.md`](SECURITY.md)
- GitHub/MCP 服务：[`services/github-action-service`](services/github-action-service)
- Private CI Agent：[`services/private-ci-agent`](services/private-ci-agent)
- 私有部署 Worker：[`services/private-deploy-agent`](services/private-deploy-agent)
- WSL Executor：[`services/private-ci-deploy-executor`](services/private-ci-deploy-executor)

## GitHub Action Service

MyGithub10 当前版本为 `10.1.0`。文件写入使用非强制 CAS、expected HEAD/blob 校验和写后 blob/content SHA 回读证明；大于 64 KiB 的文件应使用 manifest、chunk read/upload 和 finalize/commit 流程，不应退化为普通全文提交。

Python 服务现有 117 个 MCP 工具，除文件、目录、分支、提交、Pull Request 和 CI 外，还覆盖 Issue、Review Thread、Actions Artifact/Job/Rerun、Release、Tag、Deployment、Environment、Ruleset/Branch Protection、Webhook、Events 和 Notifications。所有新增写操作要求显式 `confirm=true`，所有仓库级操作共用 `ALLOWED_REPOSITORIES` 授权边界。

GitHub 认证支持 PAT Secret 文件和 GitHub App installation token。GitHub App 模式会在内存中缓存短期 token，并在到期前自动刷新；状态工具只返回认证类型、installation ID 和过期时间，不返回凭据。服务默认应只监听 `127.0.0.1`，通过 HTTPS 反向代理或安全隧道提供 MCP 访问。

```bash
cd services/github-action-service
python3 -m venv .venv
source .venv/bin/activate
pip install -c constraints.txt -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

生产镜像同时使用 `constraints.txt` 固定传递依赖；更新直接依赖后必须重建镜像并重新生成约束文件。

Docker：

```bash
cd services/github-action-service
cp .env.example .env
docker compose up -d --build
```

必须配置 PAT（优先 `GITHUB_TOKEN_FILE`）或 GitHub App 三项配置、`ACTION_API_KEY`，并按最小权限设置 `ALLOWED_REPOSITORIES` 和 `ALLOW_DEFAULT_BRANCH_WRITE`。
`ALLOWED_REPOSITORIES` 默认拒绝全部仓库；只有显式设置为逗号分隔仓库列表或明确设置为 `*` 才会放行。`/health` 仅表示进程存活，`/ready` 检查 GitHub 配置和 Controller 数据库，受 API Key 保护的 `/metrics` 提供低基数请求计数与累计耗时。

## Private Deploy Agent

该服务从 `root@de` 的 `/opt/private-deploy-agent/app` 提取了非敏感源码。服务器当前 systemd 服务名为 `private-deploy-agent.service`，实际环境文件在服务器的 `/etc/private-ci/deploy-worker.env`，不会提交到 Git。

Worker 支持 `claim_only` 模式：只领取部署任务并把执行交给受控的 WSL 流程；生产部署时必须明确审核执行模式、工作区、目标环境和回滚策略。

详细部署步骤、变量表、systemd 模板、健康检查和故障处理请阅读 [AI 重新部署指南](docs/AI重新部署指南.md)。

## 安全底线

- 不提交 `.env`、Token、API Key、密码、Webhook Secret、SSH 私钥或 TLS 私钥；
- 不把任意 SSH、Shell、主机或脚本路径暴露为 MCP 参数；
- 默认禁止直接写默认分支；
- 部署前执行计划检查、变更范围检查和人工确认；
- 任何 Token 一旦出现在日志、备份或聊天记录中，立即撤销并轮换。
