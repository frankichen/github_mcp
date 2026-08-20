# AI 重新部署指南

本文是给 AI 编程代理使用的可执行部署合同。AI 在执行任何写操作前，必须先读取本文件、仓库根目录的 `SECURITY.md` 和目标服务 README，并向用户报告目标主机、目标环境、将要修改的文件和回滚方式。

## 0. 严格边界

AI 不得：

- 读取、打印或提交 Secret 的值；
- 把 `.env`、私钥、数据库、日志、报告或运行缓存上传到 Git；
- 修改生产服务、删除 release 或清理数据，除非用户明确授权；
- 接受任意用户输入作为 SSH 主机、Shell 命令、脚本路径或 Docker socket 操作；
- 在未完成健康检查和回滚准备前切换线上 release。

AI 可以：

- 检查版本、服务状态、端口和文件清单；
- 创建虚拟环境、安装 requirements、生成示例配置；
- 在用户批准后提交 Git、重启指定服务和执行受控部署脚本。

## 1. 仓库与服务识别

```bash
git clone https://github.com/frankichen/github_mcp.git
cd github_mcp
find services -maxdepth 2 -type f | sort
```

当前仓库服务：

| 服务 | 角色 | 默认位置 |
|---|---|---|
| `github-action-service` | GitHub API、CI、MCP 和部署编排 | 本地或 Docker |
| `private-ci-agent` | 私有 CI 容器执行、日志、源码镜像 | WSL/Private CI 节点 |
| `private-deploy-agent` | 私有 CI 测试环境部署 Worker | `root@de:/opt/private-deploy-agent` |
| `private-ci-deploy-executor` | WSL 发布 Executor、测试和 systemd 模板 | WSL/发布节点 |

股票服务不在仓库中，不要重新添加。

Worker 的业务发布目标是独立仓库 `frankichen/sxt`。本仓库只保存 Worker 和最小发布契约；AI 需要按用户授权另外克隆 `frankichen/sxt`，并确认其 `scripts/deploy_gongshi_test.sh`、`scripts/sync_test_env.sh` 和 `deploy/` 与 `services/private-deploy-agent/deploy-contracts/sxt/` 的版本匹配。

## 2. 控制端部署

```bash
cd services/github-action-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中填写 Secret，不要让 AI 读取其值。最小配置：

```dotenv
GITHUB_TOKEN=由用户或 Secret 管理器注入
ACTION_API_KEY=由用户或 Secret 管理器注入
GITHUB_API_URL=https://api.github.com
ALLOWED_REPOSITORIES=frankichen/sxt
ALLOW_DEFAULT_BRANCH_WRITE=false
IDEMPOTENCY_DB_PATH=/data/idempotency.db
```

启动：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

或：

```bash
docker compose up -d --build
```

验证：

```bash
curl -fsS http://127.0.0.1:8765/healthz
```

如果控制端没有 8765 端口，使用实际反向代理地址；不要为了“修复”而直接开放公网端口。

## 3. 服务器 Worker 部署

以下命令由具备权限的管理员在服务器执行。AI 只能在用户明确授权后执行。

### 创建用户和目录

```bash
sudo useradd --system --create-home --home-dir /opt/private-deploy-agent deployworker || true
sudo install -d -o deployworker -g deployworker /opt/private-deploy-agent /var/lib/private-ci
```

### 安装代码和依赖

```bash
sudo rsync -a services/private-deploy-agent/ /opt/private-deploy-agent/
sudo -u deployworker python3 -m venv /opt/private-deploy-agent/venv
sudo -u deployworker /opt/private-deploy-agent/venv/bin/pip install -r /opt/private-deploy-agent/requirements.txt
```

### 写入 Secret

```bash
sudo install -d -m 0750 -o root -g deployworker /etc/private-ci
sudo cp services/private-deploy-agent/deploy/deploy-worker.env.example /etc/private-ci/deploy-worker.env
sudo chown root:deployworker /etc/private-ci/deploy-worker.env
sudo chmod 0640 /etc/private-ci/deploy-worker.env
```

管理员必须编辑该文件填写真实 Secret。AI 不得回显该文件。

至少需要确认这些非敏感参数正确：

```dotenv
ALLOWED_REPOSITORIES=frankichen/sxt
DEPLOYMENT_DB_PATH=/var/lib/private-ci/deployments.db
CI_DB_PATH=/var/lib/private-ci/ci.db
DEPLOY_WORKSPACE=/srv/private-ci/deploy-workspace/sxt
ENVIRONMENT_URL=http://gongshi-test
DEPLOY_EXECUTION_MODE=claim_only
```

### 安装 systemd

```bash
sudo cp services/private-deploy-agent/deploy/private-deploy-agent.service.example \
  /etc/systemd/system/private-deploy-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now private-deploy-agent.service
```

验证：

```bash
sudo systemctl is-active private-deploy-agent.service
sudo journalctl -u private-deploy-agent.service -n 100 --no-pager
sudo test -f /var/lib/private-ci/gongshi-test-status.json
```

日志中只允许出现 deployment id、状态和脱敏后的错误，不应出现 Token、Authorization Header、密码或完整环境变量。

## 4. CI 和发布端验证

先验证控制端：

1. 健康检查返回 200；
2. `tools/list` 能返回工具列表；
3. 使用已授权的测试仓库做一次只读查询；
4. 创建 CI 任务时确认 repository、branch、commit SHA 和 profile；
5. 检查 Worker 是否注册、心跳是否更新、任务状态是否变化。

再验证部署队列：

1. 只允许仓库 `frankichen/sxt`；
2. 只允许环境 `gongshi-test`；
3. 只允许 scope `fullstack`；
4. 私有 CI job 必须在 `main` 且状态为 passed；
5. commit SHA 必须与当前 main 一致；
6. 修改 `deploy/` 或发布脚本时必须显式确认；
7. 第一次只使用 `claim_only`，确认领取后再由管理员决定是否执行。

## 5. 发布与回滚

### Controller 发布失败模式

`services/private-ci-agent/deploy/apply-fixes.sh` 支持受控环境变量 `MYGITHUB12_DEPLOY_FAILURE_MODE`，仅允许以下两个枚举值：

- `auto-rollback`：默认值；保持既有无人值守语义。新 Controller 启动失败或 health check 失败时，删除失败的新 Controller 容器（如存在），把保留的 rollback container 改回正式名称并启动旧 Controller。
- `fail-stop`：只禁止失败后的自动恢复动作。新 Controller 启动失败或 health check 失败时，脚本以非 0 退出，保留旧 Controller 的 rollback container，不自动 rename/start 旧 Controller，并尽可能保留失败的新 Controller 供诊断。日志必须包含 `AUTO_ROLLBACK_DISABLED`、失败阶段、rollback container 名称和“人工恢复需要另行授权”的提示。

非法值必须在停止或重命名当前 Controller 之前失败。成功路径不受该模式影响，Controller health 通过后仍继续 cache preheat、Worker restart 和 Worker health/status 检查。

该变量只属于本地正式部署脚本的失败处理合同，不是 MCP caller 输入，不得据此增加任意 command/image、SSH、生产部署 API 或放宽 repository `test_deploy` / `self_deploy` policy。

发布前必须保留：

- 当前 release id；
- 当前 commit SHA；
- 上一个已验证 release；
- 数据库备份；
- 当前 systemd 单元和环境文件权限信息。

回滚前 AI 必须报告目标 release id、当前 release id、影响服务和预计中断时间。回滚后检查健康接口、关键 API、日志和数据库连接。

## 6. 故障排查

### Worker 不启动

```bash
systemctl status private-deploy-agent.service --no-pager
journalctl -u private-deploy-agent.service -n 200 --no-pager
```

重点检查虚拟环境、`PYTHONPATH`、`/var/lib/private-ci` 权限和环境文件权限。

### Worker 在线但没有领取任务

检查：

- `DEPLOYMENT_DB_PATH` 是否与控制端队列数据库一致；
- repository、environment、scope 是否分别为 `frankichen/sxt`、`gongshi-test`、`fullstack`；
- 私有 CI job 是否 passed 且 commit SHA 一致；
- `DEPLOY_EXECUTION_MODE=claim_only` 是否只是领取而不执行。

### 控制端返回鉴权错误

不要打印 Token。只检查环境变量是否存在、文件权限是否正确、GitHub Token 是否仍有效，以及 `ALLOWED_REPOSITORIES` 是否包含目标仓库。

### 部署后服务异常

立即停止继续发布，保留日志和状态快照，比较当前 release 与上一已验证 release；只有在用户批准后执行回滚。

## 7. AI 完成报告格式

AI 完成部署后应报告：

```text
目标主机：
控制端版本/commit：
Worker 版本/commit：
systemd 状态：
健康检查：
CI 验证：
部署模式：claim_only / 受控执行
是否发生写操作：
是否执行回滚：
未解决风险：
```

不得在报告中包含 Token、密码、Cookie、私钥或完整环境文件内容。
