# MyGithub09 现网源码基线审计

> 生成方式：从现网 Controller/Private CI Agent 工作区读取白名单文件，并与 GitHub 基线提交进行三方比较。运行时数据、密钥、虚拟环境、缓存、worktree 和 Git mirror 均排除。该报告对应 PR1，不代表已部署。

## 审计对象

- Controller 工作区：`/home/xiaowu/work/private-ci-controller-node-workspace`
- Private CI Agent 工作区：`/home/xiaowu/work/private-ci-agent-node-workspace`
- GitHub 基线：`frankichen/github_mcp`，提交 `8e268634c9c226be158bd123486eb0bfddf1bb18`
- 线上 Controller 入口：`app.main:app`、`app.mcp_server:mcp`
- 线上 Private Deploy Agent 入口：`python -m app.deploy_worker`，工作目录 `/opt/private-deploy-agent`
- WSL/发布 Executor：`scripts/deploy_executor.py`；systemd 模板见 `services/private-ci-deploy-executor/systemd/`

## 版本与工具基线

- Controller 源码提交：见 `docs/MYGITHUB09_TOOL_MANIFEST.json` 的 `source_commit`
- Controller 镜像：manifest 的 `controller_image` 字段；本次未从线上运行时复制镜像或凭据
- Worker 版本：当前工作区无独立发布号，按源码基线记录
- Executor 版本：当前工作区无独立发布号，按源码基线记录
- PyGithub：`2.5.0`
- MCP SDK：`requirements.txt` 中的 `mcp>=1.7.0`
- 实际 MCP 工具数量：见 manifest；工具名称由 `await mcp.list_tools()` 生成

## 三方文件差异

本次完整文件清单位于 `docs/source-audit/`：

- `controller-workspace-files.txt`
- `private-ci-agent-workspace-files.txt`
- `github-repo-baseline-files.txt`

### GitHub 基线缺失、现网需要同步的路径

```text
services/github-action-service/app/ci_database.py
services/github-action-service/app/ci_mcp.py
services/github-action-service/app/ci_models.py
services/github-action-service/app/ci_repository_config.py
services/github-action-service/app/ci_source_proxy.py
services/github-action-service/app/ci_worker_auth.py
services/github-action-service/app/deploy_worker.py
services/github-action-service/app/deployment_service.py
services/github-action-service/app/github_utils.py
services/github-action-service/app/routers/ci.py
services/github-action-service/app/routers/ci_worker.py
services/github-action-service/app/routers/deployments.py
services/github-action-service/config/ci_repositories.yml
services/github-action-service/config/github_report_identities.yml
services/github-action-service/config/github_report_rules.yml
services/github-action-service/deploy/private-deploy-agent.service
services/github-action-service/docs/mygithub09-authentication.md
services/github-action-service/scripts/deploy_executor.py
services/github-action-service/tests/test_ci_node_workspace_config.py
services/github-action-service/tests/test_ci_performance_features.py
services/github-action-service/tests/test_ci_worker_register.py
services/github-action-service/tests/test_deploy_executor_source.py
services/github-action-service/tests/test_deployment_callbacks.py
services/github-action-service/tests/test_github_auth_config.py
services/github-action-service/tests/test_github_checks_classification.py
services/github-action-service/tests/test_github_draft_ready.py
services/github-action-service/tests/test_github_merge_gates.py
services/github-action-service/tests/test_merge_deploy_tools.py
services/private-ci-agent/bin/healthcheck
services/private-ci-agent/bin/private-ci-preflight
services/private-ci-agent/deploy/profiles.yml
services/private-ci-agent/deploy/repositories.yml
services/private-ci-agent/private_ci_agent/__init__.py
services/private-ci-agent/private_ci_agent/cleanup.py
services/private-ci-agent/private_ci_agent/config.py
services/private-ci-agent/private_ci_agent/controller_client.py
services/private-ci-agent/private_ci_agent/executor.py
services/private-ci-agent/private_ci_agent/logs.py
services/private-ci-agent/private_ci_agent/main.py
services/private-ci-agent/private_ci_agent/models.py
services/private-ci-agent/private_ci_agent/podman.py
services/private-ci-agent/private_ci_agent/profiles.py
services/private-ci-agent/private_ci_agent/reconcile.py
services/private-ci-agent/private_ci_agent/security.py
services/private-ci-agent/private_ci_agent/services.py
services/private-ci-agent/private_ci_agent/source.py
services/private-ci-agent/private_ci_agent/workspace.py
services/private-ci-agent/pyproject.toml
services/private-ci-agent/run-agent-with-proxy.sh
services/private-ci-agent/run-agent.sh
services/private-ci-agent/tests/test_executor.py
services/private-ci-agent/tests/test_podman_security.py
services/private-ci-agent/tests/test_profiles.py
services/private-ci-agent/tests/test_services.py
services/private-ci-agent/tests/test_source_mirror.py
```
### GitHub 基线中不属于现网白名单源码的路径

无。

## 安全边界

已排除 `.env`、数据库/SQLite、日志、备份、release 产物、私钥/TLS、node_modules、虚拟环境、缓存、运行 worktree、Git mirror 和部署历史。配置文件只作为源码模板提交；任何生产 token、PAT、API key、密码和 Authorization/Bearer 值不得进入仓库。

## PR1 验收

PR1 应运行 `git diff --check`、Python 编译检查、Controller/Worker/Executor 测试、manifest 一致性校验、Docker 容器测试、secret/大文件扫描和新增 Git 历史扫描。PR1 不部署 Controller、不重启服务、不修改现网数据库、不修改 `frankichen/sxt`。
