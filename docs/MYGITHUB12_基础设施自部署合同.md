# MyGithut12 基础设施自部署控制面合同

版本：12.3.2 运行合同  
目标仓库：`frankichen/github_mcp`  
目标环境：`mygithub12-production`  
目标范围：`control-plane`

## 1. 目标

MyGithut12 只能通过固定合同提交自己的生产控制面发布意图，不能把 MCP 变成远程 Shell、SSH、任意脚本、任意主机或任意回滚入口。

控制面分为两部分：

1. Controller：验证 exact current `main`、当前 `repo-auto-check`、运行 build SHA CAS、Executor 心跳、单任务互斥，并持久化部署状态。
2. Infrastructure Executor：独立于 Controller 进程运行，只接受固定身份的队列任务，从权威 `frankichen/github_mcp` mirror 准备 exact-main checkout，并固定执行仓库内 `services/private-ci-agent/deploy/apply-fixes.sh`。

## 2. 固定 MCP 面

新增且仅新增：

- `plan_infrastructure_deployment`
- `start_infrastructure_deployment`
- `get_infrastructure_deployment`

`plan/start` 仅允许以下业务输入：

- `repository=frankichen/github_mcp`
- `environment=mygithub12-production`
- `scope=control-plane`
- `commit_sha=<current main full SHA>`
- `private_ci_job_id=<same main SHA repo-auto-check>`
- `expected_current_build_sha=<当前生产 MyGithut12 build SHA>`
- `confirm=true`（仅 start）

禁止出现：

- host / hostname / ssh / user
- shell / command / args
- script / script_path
- image / service list / hook list
- failure_mode / deploy_failure_mode
- rollback / rollback_target
- Secret / Token / callback key

## 3. 强制部署门禁

计划和执行必须满足：

1. repository policy 显式启用 `infrastructure_deployment`；auto-enrollment 永远不能获得生产自部署能力。
2. target Commit 是调用时 GitHub `main` 的准确 40 位 SHA。
3. target Tree 与 GitHub exact Commit 一致。
4. `private_ci_job_id` 必须属于 `frankichen/github_mcp / main / target SHA / repo-auto-check`，且 `status=passed`、`exit_code=0`、未 superseded。
5. `expected_current_build_sha` 必须等于当前 Controller `runtime_build_sha()`。
6. target SHA 已经等于当前 build SHA 时拒绝重复部署。
7. 固定 Executor 必须在 TTL 内有新鲜 heartbeat 且处于 `idle`。
8. 同一 repository/environment 同时只能存在一个 queued/claimed/running 部署。
9. Executor claim 前再次复验 exact current main、Tree、private CI 与 current-build CAS；任何变化都 fail-stop。

## 4. Executor 固定合同

Executor 固定：

- repository：`frankichen/github_mcp`
- authoritative origin：`https://github.com/frankichen/github_mcp.git`
- branch：`main`
- environment：`mygithub12-production`
- scope：`control-plane`
- executor id：`mygithub12-infrastructure-deploy-01`
- deployment script：`services/private-ci-agent/deploy/apply-fixes.sh`
- `MYGITHUB12_DEPLOY_FAILURE_MODE=fail-stop`

Executor systemd unit 继续保持 `NoNewPrivileges=true`。需要以 `ciworker` 身份执行的固定缓存预热命令必须通过 systemd service manager 的固定 `User=ciworker` / `Group=ciworker` broker 启动，并在 Controller 切换前完成 UID 预检；不得通过关闭 `NoNewPrivileges` 绕过降权失败。

Queue 不提供上述运行参数，Executor 代码自身固定它们。

Executor 必须：

- 更新权威 mirror；
- 要求 mirror main、origin/main、checkout HEAD 全部等于 target SHA；
- 要求 checkout `branch=main` 且工作树干净；
- 使用一次性工作目录；
- 对进度日志执行 Secret 脱敏；
- deployment 执行期间由独立 heartbeat thread 约每 5 秒持续报告同一 `current_deployment_id`，不得因同步部署脚本阻塞主循环而过期；
- Controller 重启或蓝绿切换期间允许 progress/heartbeat callback 暂时失败，heartbeat 恢复后继续报告同一 deployment identity，terminal callback 仍做有界长重试；
- deployment terminal 或 executor 异常退出路径必须 stop/join running heartbeat，再恢复 `idle/current_deployment_id=null`；
- 脚本失败或超时只报告失败，不触发自动回滚。

## 5. 失败与回滚

本控制面没有 rollback MCP tool，也不接受 rollback 参数。

部署脚本永远由 Executor 注入：

```text
MYGITHUB12_DEPLOY_FAILURE_MODE=fail-stop
```

因此失败时：

- 不自动恢复旧 Controller；
- 不删除 rollback container / release / DB / Secret；
- 不执行任意人工恢复命令；
- 记录失败状态和脱敏错误；
- 后续恢复或回滚必须获得新的明确授权并走单独流程。

## 6. 成功证据

Executor 脚本返回 0 还不能单独把部署标记 passed。Terminal complete 必须同时满足：

- Executor 验证 Controller `/health` 正常；
- Executor 验证 `private-ci-agent.service` active；
- 重启后的 Controller 自己确认 `runtime_build_sha() == target commit_sha`。

只有三项一致，状态才可进入 `passed`。

## 7. Secret 边界

Infrastructure Executor 使用独立 callback Secret：

```text
INFRASTRUCTURE_DEPLOY_CALLBACK_API_KEY_FILE=/run/secrets/infrastructure_deploy_callback_api_key
```

要求 mode 0600 regular file。Secret 不进入：

- Git 仓库；
- MCP 返回；
- queue row；
- 命令参数；
- deployment log；
- PR / CI 报告。

它不复用 gongshi-test 的 `DEPLOY_CALLBACK_API_KEY_FILE`。

## 8. SQLite 状态

Controller 使用独立数据库文件（默认 `/data/infrastructure-deployments.db`）和两张表：

- `infrastructure_deployments`
- `infrastructure_executor_heartbeats`

表由 Controller startup `CREATE TABLE IF NOT EXISTS` 初始化；不修改业务 PostgreSQL，也不存在 Goose down。

## 9. Bootstrap 边界

12.3.0 首次上线仍需要现有、已经授权的生产发布路径完成 bootstrap，并由运维配置独立 callback Secret、Executor venv/systemd unit 和权威 mirror。

**本合同本身不授权 12.3.0 合并、生产发布、Secret 创建/修改、systemd 安装或首次自部署。**
