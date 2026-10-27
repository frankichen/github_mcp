# SXT 快速发布的 Tree 等价 CI 凭据

## 范围

此规则只适用于 `frankichen/sxt` 的 `gongshi-test/fullstack` 测试部署。标准路径仍优先使用当前 `main` SHA 上通过的 `repo-auto-check`。

快速发布在 squash merge 后会产生新的 main Commit SHA，但文件 Tree 与已通过完整检查的 PR HEAD 完全相同。控制器允许复用该 PR CI 时，必须同时满足：

- main Commit SHA 是当前 GitHub `main` SHA，部署计划和开始部署时都会重新核验。
- CI job 属于 `frankichen/sxt`，branch 以 `ai/` 开头，profile 为 `repo-auto-check`，状态为 `passed`、exit code 为 `0`，且未被 supersede。
- CI worker 记录的 `summary.git_tree_sha` 是有效完整 SHA。
- GitHub 对该 CI job 的 Commit 返回的 Tree 与 worker 记录一致；该 Tree 还必须与目标 main Commit 的 GitHub Tree 一致。
- 缺少任一身份或 Tree 证据、GitHub 无法读取 Commit、Tree 不匹配时，门禁 fail-closed。

Tree 等价证据会在 `plan_test_deployment` 响应中标记 `deployment_ci_evidence.mode=tree_equivalent_pr`，并保留 tested Commit、tested Tree、目标 main Commit 和目标 Tree。精确 main SHA 路径标记为 `exact_main_sha`。

手动发布预检先寻找当前 main SHA 的精确 CI。没有可用记录时，只在最近 200 个通过的 CI 记录中查找满足上述 `ai/` 和 Tree 条件的 job；选中的 job ID 写入受控发布回执。发布后报告仍须重新校验当前 main、同一个 CI job、发布 ID、当前 release CAS、服务器 manifest/checksum、运行中容器挂载和健康证明。

此例外不改变 PR 合并门禁，不适用于其它仓库或分支，也不放宽 MyGithut12 控制器基础设施自部署的 exact-main CI 要求。它不会接受单独由调用方声明的 Tree；Tree 必须来自 GitHub Commit API 与 worker 结果的双重核验。

## 回归验收

- 精确 main SHA 的现有 CI 路径继续通过。
- `ai/` PR CI 仅在 worker Tree、PR Commit Tree、当前 main Tree 三者一致时通过。
- 任意其它非 main 分支、其它仓库、缺失 Tree、worker 与 GitHub Tree 不一致、CI 未通过或已 superseded 时拒绝。
- 手动预检只选择精确 main CI 或符合 Tree 等价规则的 `ai/` job；其它 job 不得成为发布凭据。
