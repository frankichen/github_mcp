# MyGithub10（PR4）部署与回滚手册

本文用于人工部署 `frankichen/github_mcp` 的 Draft PR4。PR4 不会自动替换线上 MyGithub09；真实 Artifact、Artifact-only 部署、gofmt 自动修复和 CI 性能能力，必须先有验收证据后才能打开开关。

## 部署前检查

部署前先创建运行时快照；快照只保存镜像 ID、Compose 文件校验和、挂载/网络/健康状态及 env 文件路径，不保存 env 内容：

```bash
snapshot=/var/backups/github-action-service/$(date -u +%Y%m%dT%H%M%SZ)
scripts/snapshot_mygithub09_runtime.sh --output "$snapshot"
grep -E '^(previous_image_id|compose_file|compose_config_sha256|container_name|health_status)=' "$snapshot/snapshot.meta"
```

构建新镜像但保留旧镜像：

构建时将完整源码提交注入 `MYGITHUB10_BUILD_SHA`；不要把静态 manifest 的生成提交误当作运行时 build SHA。

## 初始开关与兼容性

```text
MYGITHUB10_ARTIFACT_BUILD_ENABLED=false
MYGITHUB10_ARTIFACT_DEPLOY_ENABLED=false
MYGITHUB10_ATTESTATION_REUSE_ENABLED=false
```

先用数据库副本启动并确认旧字段可读、新字段向前兼容，再做只读验收。`frankichen/github_mcp` 的策略永久禁止自身线上部署，只允许 GitHub 分支、PR 和 CI。

Resource URI 只有在 `MYGITHUB10_RESOURCE_TOKEN_SECRET` 至少 32 字节时启用。可生成随机值（真实值不得写入报告或日志）：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

## 只读验收

```bash
python3 scripts/verify_mygithub10_live.py --base-url "$CONTROLLER_URL"
# 无线上地址时：
python3 scripts/verify_mygithub10_live.py --simulate
```

脚本不会调用部署、重启、迁移、构建或撤销接口。

## 回滚

现网实际使用 `/opt/github-action-service/docker-compose.yml`，服务名和容器名均为 `github-action-service`。使用部署前快照打印计划：

```bash
scripts/rollback_mygithub10.sh --snapshot /var/backups/github-action-service/<snapshot-id>
```

确认备份目录、旧镜像 ID、端口、volume、restart policy 无误后才执行：

```bash
scripts/rollback_mygithub10.sh --snapshot /var/backups/github-action-service/<snapshot-id> --confirm
```

脚本读取快照中的 `previous_image_id`，生成临时 Compose override，并验证切换后的容器 Image ID 与健康状态。它不删除数据库、旧镜像或 Release，不执行 down migration。Artifact health 失败时，受控 Executor 会恢复 previous current、重启固定服务并标记 `rolled_back`。
