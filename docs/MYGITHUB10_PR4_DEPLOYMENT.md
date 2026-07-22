# MyGithub10（PR4）部署与回滚手册

本文用于人工部署 `frankichen/github_mcp` 的 Draft PR4。PR4 不会自动替换线上 MyGithub09；真实 Artifact、Artifact-only 部署、gofmt 自动修复和 CI 性能能力，必须先有验收证据后才能打开开关。

## 部署前检查

备份 CI 数据库、Deployment 数据库、systemd 配置、旧镜像 ID、旧容器环境变量和旧工具清单。备份目录应为权限 700，且不要把密钥或 `.env` 写入报告：

```bash
backup=/var/backups/github-action-service/mygithub10-pr4-$(date +%Y%m%d-%H%M%S)
install -d -m 700 "$backup"
sqlite3 /var/lib/private-ci/ci.db ".backup '$backup/ci.db'"
sqlite3 /var/lib/private-ci/deployments.db ".backup '$backup/deployments.db'"
systemctl cat github-action-service > "$backup/systemd.txt"
```

构建新镜像但保留旧镜像：

```bash
docker build -t github-action-service:mygithub10-10.0.0-pr4 services/github-action-service
docker image inspect github-action-service:mygithub10-10.0.0-pr4 --format '{{.Id}}'
```

## 初始开关与兼容性

```text
MYGITHUB10_ARTIFACT_BUILD_ENABLED=false
MYGITHUB10_ARTIFACT_DEPLOY_ENABLED=false
MYGITHUB10_ATTESTATION_REUSE_ENABLED=false
```

先用数据库副本启动并确认旧字段可读、新字段向前兼容，再做只读验收。`frankichen/github_mcp` 的策略永久禁止自身线上部署，只允许 GitHub 分支、PR 和 CI。

## 只读验收

```bash
python3 scripts/verify_mygithub10_live.py --base-url "$CONTROLLER_URL"
# 无线上地址时：
python3 scripts/verify_mygithub10_live.py --simulate
```

脚本不会调用部署、重启、迁移、构建或撤销接口。

## 回滚

保留旧镜像和数据库，不执行 down migration：

```bash
docker stop github-action-service-mygithub10-pr4
docker rm github-action-service-mygithub10-pr4
docker run ... github-action-service-github-action-service
```

`...` 必须由人工根据旧容器备份补全，禁止从公网 MCP 参数接收任意 host、shell、路径或服务名。若 Artifact health 失败，受控 Executor 会恢复 previous current、重启固定服务并标记 `rolled_back`。
