# MyGithub10 PR4：真实集成、验收与可部署准备

## 范围与安全边界

- Base `main` 基线：`823a9196b521fcdf4cfe837520ee01ec22e8e77f`
- Head：以 PR 页面显示的完整 commit SHA 为准；本分支未 force push、未修改 main、未合并、未部署、未修改 Ruleset/Branch Protection。
- 线上 MyGithub09 保持不变；Artifact build/deploy、Attestation reuse、gofmt autofix、5x performance capability 默认均为 `false`。
- 禁止输出 Token、API Key、数据库密码、`.env`；未执行 prune、down migration、WSL/Windows 重启。

## 已实现

1. 受控 Artifact registry/build/verify：固定来源、passed Private CI、exact HEAD/tree、clean worktree、Attestation、toolchain/dependency/config gates；实包包含 `release.tar.zst`、manifest、checksums、provenance，并采用安全解压校验。
2. Artifact-only state machine：校验→incoming→migration→previous current→原子切换→固定服务重启→新 release health；失败恢复 previous current 并标记 rollback，不执行 goose down。
3. Migration runner 事件：database wait/ready、started、heartbeat、completed、failed、timeout。
4. 服务器持有的仓库权限策略：`frankichen/sxt` 可 private CI/gongshi-test，`frankichen/github_mcp` 仅 GitHub/CI，禁止自部署、环境/token/systemd/数据库/ruleset/force-push/delete-main。
5. 新增只读在线验收：`scripts/verify_mygithub10_live.py`；新增中文部署/备份/回滚手册 `docs/MYGITHUB10_PR4_DEPLOYMENT.md`。
6. 5x 脚本使用每轮独立 MCP session、精确稳定 SHA、`force_rerun=true`、`supersede_previous=false`，逐轮保留 JSONL 结果。

## 验收证据

- Controller：157 passed；Private CI Agent：34 passed；Deploy Executor：5 passed，2 skipped（依赖真实外部运行时/数据库，默认安全跳过）。
- 本地只读验收脚本：通过。
- `compileall/py_compile`、`bash -n`、`git diff --check`：通过。
- 真实 sxt 5x：正在/已由外部队列执行；只有 5/5 passed 才会把 `supports_real_ci_performance_validation` 改为 true。最终报告将列出 5 个 job_id、耗时、中位数/P90；未完成前该 capability 保持 false。
- Docker 新镜像：本机 Docker credential helper 报 WSL vsock `error getting credentials`，因此没有生成或伪造 image ID；旧镜像未覆盖。人工部署命令见部署手册。

## 尚未打开的能力

`supports_artifact_deployment=false`、`supports_gofmt_autofix=false`、`supports_real_ci_performance_validation=false`。这些值反映真实证据状态，不因代码存在而提前宣称支持。

## 部署与回滚

本 PR 只停在 Draft，不部署。人工部署前备份 CI/Deployment SQLite、systemd 配置、旧 image/container 信息和工具清单；初始三个开关全部 false。Artifact health 失败由受控 Executor 恢复 previous current；人工回滚保留数据库和旧镜像，不执行 down migration。
