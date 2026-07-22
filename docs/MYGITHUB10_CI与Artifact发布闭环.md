# MyGithub10 CI、日志、Artifact 与发布闭环

本 PR 对应第三个 Draft PR。代码完成后停在“可审查、未部署”，不自动发布 `gongshi-test`。

## CI 与日志

- Controller 注册 `get_private_ci_log_tail`，只返回指定 job 的日志尾部。
- Agent 日志批处理默认 32KB，并保留日志上限和 `truncated` 状态。
- `wait_private_ci_job`、`wait_test_deployment`、`get_test_deployment_log_tail` 保留长轮询和尾部读取能力。
- `scripts/test_local_parallel.sh` 并行运行 Controller、Private CI Agent、Executor 三套测试。
- `scripts/ci_performance_5x.sh` 连续执行五次本地完整测试并生成 JSONL 性能记录。

## Migration 超时

`services/private-ci-deploy-executor/scripts/migrate_with_timeout.sh` 要求固定 migration 命令，先做 `pg_isready`（如果可用），再输出心跳并执行总超时。不得通过跳过 migration 解决卡死；生产编排还应采集 `pg_stat_activity`、锁等待摘要并回收隔离容器。

## Tree SHA Attestation

`private_ci_agent.attestation` 记录 repository、tested commit/tree、base SHA、job/profile、容器 image digest、Go/Node 版本、lockfile 和 test config hash。只有这些身份全部一致时才允许复用测试结果；任一项不一致必须重新跑 main CI。

## Artifact-only 发布

`artifact_release.py` 生成 manifest、文件 SHA256 和 `tar.zst`；`deploy_artifact_only.sh` 只接受已校验的 artifact、manifest 和 checksums，解包到新的 release 目录后原子切换 `current`。

打包机必须安装 `tar` 和 `zstd`。本次 WSL 检查发现 `zstd` 未安装，所以完成了 manifest/checksum 测试，但 `tar.zst` 实包验收留待具备该依赖的 CI 容器执行。

artifact-only 发布不得在测试环境再次执行 `git pull`、`go test`、`npm test` 或 `npm build`。顺序为：

```text
main SHA CI 通过 → 构建 artifact → manifest/checksums/tar.zst
→ 注册 artifact → 下载校验 → 按需 migration → 原子切换 current
→ health check → 注册 release
```

## Go 安全自动修复与回滚

`gofmt_safe.sh` 默认只检查；只有明确设置 `GOFMT_AUTO_FIX=true` 才执行 `gofmt -w`。自动修复必须在独立工作区完成并重新运行完整 CI。

回滚使用已验证的上一 release、manifest 和 checksums，恢复 `current` 指针并重新 health check；不删除 release、不执行 destructive migration、不 force push。本 PR 未部署、未重启服务、未修改数据库和 `frankichen/sxt`。
