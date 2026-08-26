# MyGithut12 DX-2 规划总览

## 1. 文档定位

本文是 `frankichen/github_mcp` 在 MyGithut12 `12.3.2` 完成真实 self-deploy E2E 后，下一阶段 **开发效率与稳定性优化** 的权威入口，代号 **DX-2**。

DX-2 不再以“增加更多 GitHub 小工具”为主要目标，而是解决 Web AI / 多窗口 / Private CI / Development Session / Workspace / self-deploy 在真实使用过程中已经暴露的效率问题。

本文及同系列文档只定义需求、优先级、边界、开发顺序和验收标准，不代表功能已经实现、合并或生产发布。任何接手者都必须以执行时 fresh-read 的 `main`、Capability、Manifest、Repository Policy、Private CI Worker、Workspace 和生产运行版本为准。

基线日期：`2026-08-27`

## 2. 当前准确基线

| 项目 | 基线 |
|---|---|
| Repository | `frankichen/github_mcp` |
| Base branch | `main` |
| Base Commit | `4856cc882e49f0e24d04dc7fed1300cc38da567e` |
| Base Tree | `ad11cc5b635f9359b2b8280af5e80aba5ca7a990` |
| 生产服务 | `MyGithut12` |
| 生产版本 | `12.3.2` |
| Production Build SHA | `4856cc882e49f0e24d04dc7fed1300cc38da567e` |
| Canonical production tools | `163` |
| Compatibility registration | `166` |
| Repository Index version | `12.0.0-1` |
| Repository Policy | `github=true`、`private_ci=true`、`self_deploy=true`、`test_deploy=false` |
| 当前 Private CI Worker | 仅 `wsl-ci-01` |
| Worker concurrency | `max_concurrent=1` |
| `frankichen/sxt` Workspace 观测 | `status=active` 共 23 个；历史项中存在大量 `lease_valid=false` |

最近 `frankichen/sxt` 真实 `repo-auto-check` 观测值约为 221～231 秒，例如：

- `c359bdc30a214d8f`：221.73 秒；
- `bce4077116a44eaf`：224.50 秒；
- `35b01afe460d4632`：226.12 秒；
- `207451c496514ed3`：228.11 秒；
- `5076d8da85604636`：230.96 秒。

这些数值只作为 2026-08-26/27 的真实性能基线，不能写死为未来结论。

## 3. 为什么需要 DX-2

DX-1 已经完成了 Development Session、Development Workspace、Index、Fast/Full CI、Failure Pack、Blue/Green、Resource fallback 等主框架。12.3.x 又补齐了基础设施 self-deploy。

现在影响开发速度的主要问题已经从“缺功能”转为“编排和生命周期摩擦”：

1. Private CI 只有一个 Worker，多个 AI 窗口会形成串行队列；
2. 底层 Range/Patch/Upload 写入可以推进 Workspace，但旧 Development Session 可能保留旧 HEAD，造成假性 recovery/drift 阻塞；
3. Workspace 的持久状态与 Lease 生命周期没有自动收敛，过期历史 Workspace 长期显示 `active`，增加 overlap 噪声；
4. Infrastructure Executor 在同步执行长发布脚本时主循环无法持续 heartbeat，运行中的 executor 可能被暂时显示为 offline；
5. Infrastructure deployment 只有单次查询，缺少与 Private CI / Index 对等的 long-poll 和紧凑诊断；
6. Commit 后 Index → Change Context → Impact → Contract → Affected Tests → Full CI 仍需要多个固定调用；
7. 新窗口接手 branch/PR 时，仍需要人工恢复 main、HEAD、Tree、Workspace、Session、Lease、Index、CI、PR 和 overlap；
8. 真实 CI 性能尚未形成长期可比较的 performance capability 和回归门禁。

## 4. DX-2 总目标

### 4.1 速度目标

- 两个互不冲突的 Private CI job 可以真实并行执行，不再被单 Worker 串行阻塞；
- 新窗口从 branch 或 PR 恢复开发上下文，目标 1 个高层调用返回可继续工作的完整状态；
- Commit 后标准收敛流程由一个高层调用完成编排；
- self-deploy 等待过程不需要高频重复查询；
- 历史 Lease 过期 Workspace 不再污染“活动 Writer”判断。

### 4.2 稳定性目标

- 不降低 HEAD / Tree / Blob / Workspace Revision / Lease / Idempotency / durable read-back 等现有门禁；
- 不允许“自动恢复”覆盖真实外部 branch drift；
- 不因扩容 Private CI 引入重复执行、交叉缓存、共享 worktree 污染或两个 Worker 同时提交同一 attempt；
- self-deploy 仍保持 fixed executor、fail-stop、no-auto-rollback；
- 任何高层编排都不能绕过 `repo-auto-check`、Merge Gate 或明确的合并/发布授权。

## 5. 优先级

### P0：直接影响当前开发吞吐或造成假阻塞

#### DX2-CI-01：Private CI 双 Worker 与安全调度

新增至少一个独立 Worker（建议 `wsl-ci-02`），每个 Worker 初始仍保持单槽执行，通过 Controller 调度实现真实并行，而不是在同一 Worker 内粗暴提高并发。

#### DX2-SESSION-01：Session / Workspace 安全恢复

解决 Development Session stale HEAD 与 Workspace/GitHub 已一致时的假性 recovery/drift 阻塞。恢复只能在完整身份链可证明时发生。

#### DX2-WS-01：Workspace Lease 生命周期自动收敛

Lease 到期的 Workspace 不再长期作为 active Writer 参与默认 overlap；保留审计、branch、历史和 Index evidence，不自动删 branch。

### P1：显著减少重复调用与诊断成本

#### DX2-INFRA-01：Infrastructure Executor 独立 heartbeat

部署脚本执行期间仍持续 heartbeat，避免 running deployment 被误报 executor offline。

#### DX2-INFRA-02：Infrastructure deployment wait / compact diagnostics

提供 long-poll 等待能力和有限日志摘要，减少重复 `get_infrastructure_deployment` 调用。

#### DX2-CONVERGE-01：`converge_development_task`

编排新 Commit 的 Index、Change Context、Impact、Contract、Affected Tests 和 CI，不替用户合并或发布。

#### DX2-RESUME-01：`resume_development_task`

按 repository + branch 或 PR 恢复跨窗口状态，并在安全条件下修复 stale Session；真实 drift 必须 fail-stop。

### P2：可量化性能与长期治理

#### DX2-PERF-01：真实 CI Performance Validation

建立真实 job 样本、分位数、回归阈值和 capability，只有达到真实证据门槛后才能把 `supports_real_ci_performance_validation` 置为 true。

#### DX2-HYGIENE-01：历史 Workspace / Draft PR 可见性治理

提供只读审计和明确分类，避免历史对象被误当成当前工作。任何关闭 PR、删除 branch 等动作仍需独立授权。

## 6. 公共接口策略

DX-2 原则：**少加工具名，优先组合现有能力或增加兼容可选参数。**

计划新增的公共高层工具最多两个：

1. `resume_development_task`
2. `converge_development_task`

Infrastructure deployment 的 wait/log 能力优先通过现有 `get_infrastructure_deployment` 的兼容可选参数实现，除非实现评审证明独立工具更安全或更清晰。

若最终新增两个工具，则 Canonical production tool count 的目标从 163 变为 165；Compatibility 数量必须按真实 Manifest 重新生成，不能手工写死。

任何 Schema 变化应尽量合并为一次受控 Connector refresh，避免多次客户端刷新。

## 7. 不做范围

DX-2 不做以下事项：

- 不迁移 GitHub 到其它代码托管平台；
- 不允许任意 Shell、SQL、主机、端口、Git remote、文件系统路径成为 MCP 输入；
- 不允许 AI 自动合并、自动生产发布、自动回滚或自动删除 branch；
- 不用 Fast CI 替代最终 `repo-auto-check`；
- 不把 Workspace 过期等同于删除 branch、PR、Commit 或审计记录；
- 不通过提高同一 Worker 的无界并发来解决排队；
- 不为了性能关闭 source immutability、image digest、toolchain、dependency、CAS 或权限检查；
- 不为了把 capability 改成 true 而实现没有真实使用价值的 `gofmt_autofix` 或旧 Artifact 路径。

## 8. 交付阶段

### Phase A：P0 收敛

1. 双 Worker 调度与隔离；
2. Session 安全恢复；
3. Workspace Lease 自动收敛。

Phase A 完成后，目标是多窗口并发不再因为单 Worker 或历史 Workspace 产生明显串行/误判。

### Phase B：P1 编排效率

1. Infrastructure 独立 heartbeat；
2. Infrastructure wait / compact diagnostics；
3. `resume_development_task`；
4. `converge_development_task`。

### Phase C：P2 性能治理

1. Real CI performance validation；
2. 真实性能基线、趋势和回归阈值；
3. 依据数据决定是否进一步做 test sharding、cache 优化或 Worker 扩容。

## 9. 接手者必读顺序

任何新窗口或其他 AI 接手 DX-2，建议按以下顺序：

1. 根目录 `README.md`、`SECURITY.md`；
2. `docs/MYGITHUB12_DX2规划总览.md`；
3. `docs/MYGITHUB12_DX2需求与验收标准.md`；
4. `docs/MYGITHUB12_DX2开发清单.md`；
5. 现有 DX-1 文档，理解当前 Session/Workspace/CI 架构；
6. fresh-read 当前 main / Tree / Capability / Policy / Manifest；
7. 再读取本次任务涉及的 Controller、Private CI Agent 或 Executor 源码和测试。

历史聊天、旧文档中的 SHA、旧 Worker 状态和旧性能数值只能作为线索，不能替代当前远端事实。

## 10. 完成定义

DX-2 只有在以下条件全部满足时才能整体标记 `DONE`：

1. P0 三项全部有代码、测试和生产/真实环境证据；
2. 两个独立 CI job 可真实并行且没有重复执行或隔离破坏；
3. stale Session 能安全恢复，真实 branch drift 仍 fail-stop；
4. 过期 Workspace 不再默认作为 active Writer；
5. self-deploy 长任务 heartbeat 不再出现正常执行却 executor offline 的误报；
6. 新窗口可从 branch/PR 高层恢复完整开发上下文；
7. Commit 后标准收敛可由一个高层工具编排；
8. `repo-auto-check`、Merge Gate、显式合并/发布授权均未被绕过；
9. Capability、Manifest、客户端可见 Schema、README 和文档与生产运行身份一致；
10. Real CI performance capability 只有在真实样本和门槛满足后才可置为 true。

DX-2 文档完成不代表实现完成。
