# MyGithub10 PR4：真实集成、验收与可部署准备

## 范围与安全边界

- Base `main` 基线：`823a9196b521fcdf4cfe837520ee01ec22e8e77f`
- Head：本次续作提交完成后以 PR 页面显示的完整 40 位 commit SHA 为准；本分支未 force push、未修改 main、未合并、未部署、未修改 Ruleset/Branch Protection。
- 线上 MyGithub09 保持不变；Artifact build/deploy、Attestation reuse、gofmt autofix、5x performance capability 默认均为 `false`。
- 禁止输出 Token、API Key、数据库密码、`.env`；未执行 prune、down migration、WSL/Windows 重启。

## 已实现

1. 受控 Artifact registry/build/verify：固定来源、passed Private CI、exact HEAD/tree、clean worktree、Attestation、toolchain/dependency/config gates；实包包含 `release.tar.zst`、manifest、checksums、provenance，并采用安全解压校验。
2. Artifact-only state machine：校验→incoming→migration→previous current→原子切换→固定服务重启→新 release health；失败恢复 previous current 并标记 rollback，不执行 goose down。
3. Migration runner 事件：database wait/ready、started、heartbeat、completed、failed、timeout。
4. 服务器持有的仓库权限策略：`frankichen/sxt` 可 private CI/gongshi-test，`frankichen/github_mcp` 仅 GitHub/CI，禁止自部署、环境/token/systemd/数据库/ruleset/force-push/delete-main。
5. 新增只读在线验收：`scripts/verify_mygithub10_live.py`；新增中文部署/备份/回滚手册 `docs/MYGITHUB10_PR4_DEPLOYMENT.md`。
6. 5x 脚本使用每轮独立 MCP session、精确稳定 SHA、`force_rerun=true`、`supersede_previous=false`，逐轮保留 JSONL 结果。
7. 实际 MCP 入口统一执行仓库策略，拒绝未知仓库和 `github_mcp` 的 test_deploy/self_deploy，允许其 GitHub/CI 操作；新增 `scripts/rollback_mygithub10.sh`，匹配现网 `/opt/github-action-service/docker-compose.yml`。

## 验收证据

- Controller：158 passed；Private CI Agent：34 passed；Deploy Executor：5 passed，2 skipped（依赖真实外部运行时/数据库，默认安全跳过）。
- 容器内 Controller：157 passed；容器内 compileall、工具清单生成（84 tools）和 `verify_mygithub10_live.py --simulate`：通过。
- 本地/模拟在线验收：通过 7 项核心检查；真实公网验收仍需部署后由人工执行，当前不伪造 live 结果。
- `compileall/py_compile`、`bash -n`、`git diff --check`：通过。
- 真实 sxt 5x：已取得五个历史 passed Job 的完整证据；由于修正脚本无法在未部署的旧 Controller 上完成 schema/stream 验证，`supports_real_ci_performance_validation` 仍保持 false。
- 同一稳定 SHA 的五个真实历史 passed Job 证据：`6545eed41839439e`、`b45c084921534e7a`、`2332022c22624c53`、`8f35c5ee22534e38`、`600398a63b9a45b5`；五次 commit 均为 `f1a9368e649e47eeb3481474b31f8c51580fa955`，tree 均为 `8a31b7620acd8ed97e53b110f421e7328448ea01`，耗时依次为 237.715/253.651/230.566/206.051/200.119 秒，median=230.566 秒，nearest-rank P90=253.651 秒。Migration=74.834/77.793/71.156/69.364/64.552 秒；Go test=72.419/74.449/68.216/60.352/63.569 秒；Admin test=13.172/10.982/9.250/8.364/9.208 秒；Console test=146.634/158.783/148.057/114.041/101.124 秒；build=57.256/61.838/52.425/48.127/51.709 秒。镜像 digest 均为 `sha256:f92b729f5f76b045df75ee1cb324ea68658bbc82feecd286c6ce08bf339fd74d`，Go=`go1.26.0`、Node=`v22.22.1`、npm=`10.9.2`。
- 修正后的脚本对当前仍运行 MyGithub09 的旧 Controller 在 `initialize/list_tools` 阶段遇到 stream 读取失败后立即退出；因此未把这次重新执行计入 5x capability，能力值仍保持 false。
- Docker 新镜像已用临时 `DOCKER_CONFIG` 构建，未修改全局配置；当前完整 image ID：`sha256:17b7a630a015b30222d3af1c57c96f01fb5e81214dc8cafe36aa4eaf9fc9a106`。旧镜像未覆盖。

## 尚未打开的能力

`supports_artifact_deployment=false`、`supports_gofmt_autofix=false`、`supports_real_ci_performance_validation=false`。这些值反映真实证据状态，不因代码存在而提前宣称支持。

## 部署与回滚

本 PR 只停在 Draft，不部署。人工部署前备份 CI/Deployment SQLite、systemd 配置、旧 image/container 信息和工具清单；初始三个开关全部 false。Artifact health 失败由受控 Executor 恢复 previous current；人工回滚保留数据库和旧镜像，不执行 down migration。

可执行回滚脚本：`scripts/rollback_mygithub10.sh`；默认只打印计划，必须显式 `--confirm` 才会执行 Compose 重建。
