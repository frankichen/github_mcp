# MyGithut12 DX-2 开发清单

## 1. 使用说明

本文是 DX-2 的执行清单。它不是需求替代品；每个任务的业务/安全要求和验收标准仍以 `docs/MYGITHUB12_DX2需求与验收标准.md` 为准。

任何 AI、开发者或后续窗口接手时，都必须先 fresh-read 当前远端事实，再更新本文状态。不得因为本文记录了旧 SHA、旧 Worker 或旧 PR 就直接复用。

### 1.1 状态定义

| 状态 | 含义 |
|---|---|
| `NOT_STARTED` | 尚未建立准确 Workspace/分支进行实现 |
| `IN_PROGRESS` | 已建立开发对象并开始实现或测试 |
| `BLOCKED` | 有明确、可复现、不可安全绕过的阻塞 |
| `DONE` | 代码、测试、真实验收、文档和运行身份均满足对应 AC |
| `SUPERSEDED` | 被新的明确方案取代，必须注明替代任务 |

禁止使用“基本完成”“应该好了”“代码已写”作为状态。

### 1.2 每个 Batch 开始前固定动作

1. `get_mygithub_capabilities`；
2. `get_repository_operation_policy(repository=frankichen/github_mcp)`；
3. fresh-read `main` HEAD / Tree；
4. 读取 README、SECURITY、Manifest、相关 DX-2 文档；
5. 对 exact main 建立/复用 Repository Index；
6. 创建独立 `ai/` branch + Development Workspace + Lease；
7. 声明预计修改 path/config/symbol scope；
8. 检查 active Workspace overlap；
9. 读取相关源码和测试；
10. 写入前完成 Patch Analysis / 影响范围 / 契约候选 / 受影响测试选择。

### 1.3 每个 Batch 完成前固定动作

1. Commit 后 fresh read-back HEAD / Tree / Blob；
2. 对新 HEAD 重新 Index；
3. Change Context Pack；
4. Change Impact；
5. Contract Change Detection；
6. Affected Tests；
7. 当前 HEAD `repo-auto-check`；
8. Worker 最终 `idle/current_job=null`；
9. Draft PR；
10. PR body 写清修改、验证、风险、回滚和未完成项；
11. 未经明确授权不合并；
12. 合并后必须对新 main 重新跑 exact `repo-auto-check`；
13. 未经明确发布授权不 self-deploy；
14. 发布失败时无回滚授权则 fail-stop。

## 2. 依赖与推荐顺序

```text
DX2-00 文档基线
  ├─> DX2-WRITE-01 Patch Builder 一致性
  ├─> DX2-CI-01 双 Worker
  ├─> DX2-WS-01 Workspace 生命周期
  │     └─> DX2-SESSION-01 Session 安全恢复
  │              └─> DX2-RESUME-01 resume_development_task
  ├─> DX2-INFRA-01 Executor heartbeat
  │     └─> DX2-INFRA-02 infrastructure wait/diagnostics
  └─> DX2-CONVERGE-01 converge_development_task

DX2-CI-01 + DX2-CONVERGE-01
  └─> DX2-PERF-01 Real CI Performance Validation

DX2-WS-01 + DX2-RESUME-01
  └─> DX2-HYGIENE-01 历史对象治理
```

推荐不要把所有 P0/P1 压进一个大 PR。优先按风险域拆分，降低 self-deploy 和 CI 基础设施同时变化的爆炸半径。

## 3. 总表

| ID | 优先级 | 状态 | 核心目标 | 关键验收 |
|---|---|---|---|---|
| DX2-00 | P0 | `IN_PROGRESS` | 固化 DX-2 规划、需求、清单和入口 | 文档落库、README 可发现、Draft PR |
| DX2-WRITE-01 | P1 | `IN_PROGRESS` | 新文件 builder 输出可原样 strict apply | AC-WRITE-01～06 |
| DX2-CI-01 | P0 | `NOT_STARTED` | 两个独立 Private CI Worker 真并行 | AC-CI-01～10 |
| DX2-WS-01 | P0 | `IN_PROGRESS` | Lease 过期对象退出默认 active Writer + guarded auto-renew | AC-WS-01～13 |
| DX2-SESSION-01 | P0 | `NOT_STARTED` | 安全恢复 stale Session，不掩盖真实 drift | AC-SESSION-01～07 |
| DX2-INFRA-01 | P1 | `NOT_STARTED` | 长 self-deploy 持续 heartbeat | AC-INFRA-HB-01～05 |
| DX2-INFRA-02 | P1 | `NOT_STARTED` | self-deploy long-poll 和紧凑诊断 | AC-INFRA-WAIT-01～05 |
| DX2-RESUME-01 | P1 | `NOT_STARTED` | 新窗口一次恢复 branch/PR 开发上下文 | AC-RESUME-01～07 |
| DX2-CONVERGE-01 | P1 | `NOT_STARTED` | 一次调用编排 post-write 收敛 | AC-CONV-01～07 |
| DX2-PERF-01 | P2 | `NOT_STARTED` | 真实 CI 性能统计和回归依据 | AC-PERF-01～05 |
| DX2-HYGIENE-01 | P2 | `NOT_STARTED` | 历史 Workspace/PR 可见性治理 | AC-HYGIENE-01～03 |

## 4. DX2-00：DX-2 文档基线

### 状态

`IN_PROGRESS`

### 目标

确保任何接手者无需依赖聊天记录即可知道：为什么开发、先做什么、不能做什么、如何验收、怎样发布。

### 修改范围

- `docs/MYGITHUB12_DX2规划总览.md`
- `docs/MYGITHUB12_DX2需求与验收标准.md`
- `docs/MYGITHUB12_DX2开发清单.md`
- `README.md` 快速入口

### 完成检查

- [x] 记录 12.3.2 基线和当前性能/Worker/Workspace 观测；
- [x] P0/P1/P2 目标写清；
- [x] 每个功能有 FR 与 AC；
- [x] 新发现的 Patch Builder 问题进入正式需求；
- [ ] README 快速入口已加入 DX-2 三份文档；
- [ ] final exact HEAD Index ready；
- [ ] Change Impact / Contract / Affected Tests 完成；
- [ ] final `repo-auto-check` passed；
- [ ] Draft PR 创建并回读。

### DONE 条件

以上全部勾选，且 PR 保持 Draft；不要求合并，不要求生产发布。

## 5. DX2-WRITE-01：Patch Builder / Strict Apply 新文件一致性

### 状态

`IN_PROGRESS`

### 真实来源

本次 DX2-00 已真实复现：`build_github_patch` 对不存在文件生成普通 modify header，strict apply 返回 `FILE_NOT_FOUND`；改成 `--- /dev/null` add 语义后通过。

### 预计修改模块

- `services/github-action-service/app/mygithub10.py`
- `services/github-action-service/app/mygithub12_dx_mcp.py`（若高层包装需要暴露 operation）
- `services/github-action-service/tests/test_mygithub10_multi_file_patch_parser.py`
- `services/github-action-service/tests/test_mygithub10_patch_from_ref.py`
- 新增/现有 builder 测试文件
- Tool Manifest / README（仅 schema 真变化时）

### 实现清单

- [ ] 明确 builder API：`operation=add|modify|delete` 或可证明等价语义；
- [ ] add 生成 `/dev/null` + `new file mode`；
- [ ] delete 生成 `/dev/null` new side；
- [ ] modify 保持 expected Blob 约束；
- [ ] UTF-8/CRLF/final newline 字节保持；
- [ ] builder 输出直接进入 strict apply；
- [ ] 路径逃逸/重复 path/非法 hunk 仍 fail-closed；
- [ ] Development ChangeSet 路径回归。

### 测试清单

- [ ] add round-trip；
- [ ] modify round-trip；
- [ ] delete round-trip；
- [ ] 中文文件；
- [ ] CRLF；
- [ ] 无末尾换行；
- [ ] 已存在目标 add 拒绝；
- [ ] 不存在目标 modify 拒绝；
- [ ] 多文件混合 add/modify/delete。

### 验收

对应 `AC-WRITE-01`～`AC-WRITE-06`。

### 非目标

不放宽 strict patch parser；不新增任意文件系统读取；不允许调用者绕过 expected HEAD/Blob。

## 6. DX2-CI-01：Private CI 双 Worker 与安全调度

### 状态

`NOT_STARTED`

### 建议拆分

#### Batch CI-A：Controller 多 Worker 调度正确性

预计模块：

- `services/github-action-service/app/ci_database.py`
- Private CI routers/service/scheduler 相关模块
- Controller CI 测试

任务：

- [ ] 复核 worker registration / heartbeat / claim / lease / attempt 数据模型；
- [ ] scheduler 支持多个 eligible Worker；
- [ ] 同 job lease fencing 不变；
- [ ] stale callback 拒绝；
- [ ] queue priority + fairness；
- [ ] queue evidence 可观察；
- [ ] worker loss 后安全重新调度规则明确。

#### Batch CI-B：第二 Worker 运行时隔离

预计模块：

- `services/private-ci-agent/**`
- systemd/config 示例与部署脚本
- CI cache / rootless Podman 配置
- 运维文档

任务：

- [ ] 定义 `wsl-ci-02` 固定身份；
- [ ] 独立 work root；
- [ ] 独立容器命名与临时目录；
- [ ] cache 只共享 immutable 内容，可写状态隔离；
- [ ] CPU/RAM/disk 限额；
- [ ] 两个 Worker 均能注册和 heartbeat；
- [ ] 单 Worker 故障不拖垮另一个。

#### Batch CI-C：真实并发验收

- [ ] 启动两个互不冲突的真实 job；
- [ ] 证明 running 时间窗口重叠；
- [ ] 分别绑定两个 Worker；
- [ ] 最终均 terminal；
- [ ] 两 Worker 均 idle；
- [ ] 连续至少 5 组 `frankichen/sxt` 并发样本；
- [ ] 对比扩容前后 queue wait P50/P95；
- [ ] 不把单 job duration 下降作为必须目标。

### 验收

对应 `AC-CI-01`～`AC-CI-10`。

### 回滚原则

若第二 Worker 引发异常，只允许在明确授权下停止/禁用新增 Worker；不能清空 CI DB、cache、volume 或取消他人正在运行的任务。

## 7. DX2-WS-01：Workspace Lease 生命周期自动收敛

### 状态

`IN_PROGRESS`

### 预计修改模块

- `services/github-action-service/app/mygithub12_workspace.py`
- Workspace SQLite/schema 初始化逻辑
- overlap analysis
- Workspace MCP 列表/读取工具
- Workspace 测试

### 临时缓解：2 小时默认 Lease

- [x] `DEFAULT_LEASE_SECONDS` 从 1800 调整为 7200；
- [x] `create_development_workspace`、`renew_development_workspace_lease`、`prepare_development_task` 的默认值统一；
- [x] `MAX_LEASE_SECONDS=14400` 保持不变；
- [x] Schema/单测明确校验 7200 默认值；
- [ ] 合并并发布后以 production Schema/capability 复验。

### 后续自动续签

- [ ] 采用 activity-driven renew，不启后台无限续签线程；
- [ ] 续签阈值和窗口使用受控常量；
- [ ] fresh-read GitHub / Workspace / Session 三方 HEAD/Tree；
- [ ] `drift_reason=null`、status active、Lease 尚有效；
- [ ] expected Workspace revision CAS；
- [ ] 续签成功后同步 Session workspace revision；
- [ ] expired/drifted/closed 不自动复活；
- [ ] renewal idempotency + audit before/after；
- [ ] 任一 identity/CAS 校验失败在写操作前 fail-stop。

### 实现清单

- [ ] 定义 `expired` effective/persisted state 契约；
- [ ] 兼容历史 `status=active + lease_valid=false`；
- [ ] 默认 active 查询过滤 expired；
- [ ] overlap 默认忽略 expired Writer；
- [ ] 审计查询仍可看到 expired；
- [ ] Index pin 生命周期与状态对齐；
- [ ] resume 前重新验 branch HEAD/Tree/drift；
- [ ] 绝不自动删 branch/PR。

### 数据迁移要求

- [ ] 若修改 SQLite schema，Migration 必须幂等；
- [ ] 升级前历史数据可直接打开；
- [ ] 不丢 scope、owner、revision、head/tree、timestamps；
- [ ] down/兼容路径不破坏历史记录。

### 验收

对应 `AC-WS-01`～`AC-WS-13`。

## 8. DX2-SESSION-01：Session / Workspace 安全恢复

### 状态

`NOT_STARTED`

### 依赖

建议在 DX2-WS-01 状态模型明确后实现。

### 预计修改模块

- `services/github-action-service/app/development_session_store.py`
- `services/github-action-service/app/development_orchestrator.py`
- `services/github-action-service/app/mygithub12_workspace.py`
- `services/github-action-service/app/mygithub12_dx_mcp.py`
- Development Session / Workspace 测试

### 实现清单

- [ ] 新增内部 recovery primitive；
- [ ] fresh-read GitHub branch；
- [ ] 比较 GitHub / Workspace / Session 三方 HEAD/Tree；
- [ ] 仅允许 Session stale 场景前进；
- [ ] 真实 external drift fail-stop；
- [ ] Session revision CAS；
- [ ] recovery event audit；
- [ ] exact HEAD Index 重新绑定；
- [ ] 旧 CI/attestation 仅 exact SHA 可复用；
- [ ] 幂等重放。

### 必测矩阵

| GitHub | Workspace | Session | 预期 |
|---|---|---|---|
| A | A | A | no-op |
| B | B | A | 安全 recovery（有证据时） |
| B | A | A | external drift，拒绝 |
| B | B | C | 不明 identity，拒绝或要求显式恢复证据 |
| A | A | A 但 lease expired | 进入 Workspace resume 流程 |

### 验收

对应 `AC-SESSION-01`～`AC-SESSION-07`。

## 9. DX2-INFRA-01：Infrastructure Executor 独立 heartbeat

### 状态

`NOT_STARTED`

### 预计修改模块

- `services/private-ci-deploy-executor/scripts/infrastructure_deploy_executor.py`
- Executor tests
- `docs/MYGITHUB12_基础设施自部署合同.md`

### 实现清单

- [ ] heartbeat 独立线程/调度；
- [ ] running/idle state 并发访问安全；
- [ ] deployment ID 一致；
- [ ] Controller 切换短暂 502 可容忍；
- [ ] stop event / join；
- [ ] failure/timeout/exception 后 idle 回报；
- [ ] Secret redaction 不变；
- [ ] fixed deploy contract 不变。

### 验收

对应 `AC-INFRA-HB-01`～`AC-INFRA-HB-05`，必须包含一次真实 self-deploy E2E。

## 10. DX2-INFRA-02：Infrastructure wait / compact diagnostics

### 状态

`NOT_STARTED`

### 预计修改模块

- `services/github-action-service/app/infrastructure_deployment_store.py`
- `services/github-action-service/app/infrastructure_deployment_service.py`
- `services/github-action-service/app/infrastructure_deployment_mcp.py`
- `services/github-action-service/app/routers/infrastructure_deployments.py`
- 相关 tests / Manifest

### 实现清单

- [ ] 评审“扩展现有 get”与“新增 wait tool”两种方案；
- [ ] 优先兼容可选参数，减少 tool count；
- [ ] long-poll revision/status/step change；
- [ ] bounded wait <= MCP 安全窗口；
- [ ] compact default response；
- [ ] redacted log tail；
- [ ] 结构化 step；
- [ ] Controller blue/green 切换连续性；
- [ ] 旧 schema 调用保持兼容。

### 验收

对应 `AC-INFRA-WAIT-01`～`AC-INFRA-WAIT-05`。

## 11. DX2-RESUME-01：`resume_development_task`

### 状态

`NOT_STARTED`

### 依赖

DX2-WS-01、DX2-SESSION-01。

### 预计修改模块

- `services/github-action-service/app/mygithub12_dx_mcp.py`
- `services/github-action-service/app/development_orchestrator.py`
- Development Session / Workspace store
- PR/readiness/CI/index 聚合逻辑
- Tool Manifest / version tests

### 输入契约候选

只允许高层受控字段，例如：

- `repository`
- `branch`（可选）
- `pull_number`（可选）
- `renew_lease`（默认 false）
- `recover_stale_session`（默认 true，但只走严格安全条件）
- `idempotency_key`

不得接受 Shell、path、host、remote、token。

### 输出必须包含

- current main SHA/Tree；
- branch/PR HEAD/Tree；
- Workspace ID/revision/lease/drift/scope；
- Session ID/revision/status；
- exact Index identity；
- current HEAD Fast/Full CI；
- PR draft/base/checks/readiness；
- overlap；
- recovery evidence；
- `next_allowed_actions`；
- degraded / blocker。

### 验收

对应 `AC-RESUME-01`～`AC-RESUME-07`。

## 12. DX2-CONVERGE-01：`converge_development_task`

### 状态

`NOT_STARTED`

### 预计修改模块

- `services/github-action-service/app/mygithub12_dx_mcp.py`
- `services/github-action-service/app/development_orchestrator.py`
- Index / Context Pack / impact / contract / affected tests orchestration
- Private CI orchestration
- Tool Manifest / tests

### 编排顺序

```text
Session/Workspace/GitHub identity gate
  -> exact HEAD Index
  -> Change Context Pack
  -> Change Impact
  -> Contract Change Detection
  -> Affected Tests
  -> fast|full CI
  -> failure pack or success evidence
  -> Worker final state
  -> merge eligibility (read-only)
```

### 实现清单

- [ ] stale revision CAS 拒绝；
- [ ] 新 HEAD 永不复用旧 Index；
- [ ] candidate analysis 不冒充事实；
- [ ] 任一分析降级时保守扩大测试；
- [ ] full 固定 `repo-auto-check`；
- [ ] Failure Pack 直达；
- [ ] compact + Resource fallback；
- [ ] 不 merge/deploy/rollback。

### 验收

对应 `AC-CONV-01`～`AC-CONV-07`。

## 13. DX2-PERF-01：Real CI Performance Validation

### 状态

`NOT_STARTED`

### 依赖

建议至少在 DX2-CI-01 完成后开始正式验收，否则无法区分排队和执行瓶颈。

### 预计修改模块

- Private CI job/step metrics 聚合
- `get_github_development_history` 或独立性能聚合服务（需评审）
- capability / Manifest
- 性能验证脚本和 tests
- DX-2 文档

### 指标

必须至少记录：

- queue wait；
- execution wall；
- workspace step duration；
- Worker ID；
- 并发度；
- image/toolchain identity；
- cache hit/miss；
- job status/superseded/cancelled classification。

### 实现清单

- [ ] 样本过滤规则；
- [ ] P50/P90/P95；
- [ ] 小样本明确 insufficient；
- [ ] 扩容前后 queue wait 对比；
- [ ] 最慢 workspace/step 排名；
- [ ] 先观测/告警，不直接卡 Merge；
- [ ] capability 真实验收后再 true。

### 验收

对应 `AC-PERF-01`～`AC-PERF-05`。

## 14. DX2-HYGIENE-01：历史 Workspace / Draft PR 可见性治理

### 状态

`NOT_STARTED`

### 依赖

DX2-WS-01；若要提供一键接手建议，也依赖 DX2-RESUME-01。

### 实现清单

- [ ] Workspace 状态统计；
- [ ] 默认 active 只显示有效 Writer；
- [ ] audit/all 模式可读历史；
- [ ] abandoned candidate 只读分类；
- [ ] 候选依据可解释；
- [ ] 不自动 close PR；
- [ ] 不自动 delete branch；
- [ ] Index pin retention 透明。

### 验收

对应 `AC-HYGIENE-01`～`AC-HYGIENE-03`。

## 15. 版本与 PR 建议

为了控制风险，建议按以下版本批次，而不是一次发布全部：

### 12.3.x 稳定化批次

优先：

1. DX2-WRITE-01；
2. DX2-INFRA-01；
3. DX2-INFRA-02。

这些属于当前 12.3 self-deploy / DX 基础设施的直接稳定化。

### 12.4.x P0 并发与状态机批次

1. DX2-CI-01；
2. DX2-WS-01；
3. DX2-SESSION-01。

CI Worker 扩容涉及真实运行基础设施，建议代码 PR 与生产 bootstrap/验收分阶段执行。

### 12.5.x 高层编排批次

1. DX2-RESUME-01；
2. DX2-CONVERGE-01；
3. Schema/Manifest 一次性刷新。

### 后续性能治理

DX2-PERF-01、DX2-HYGIENE-01 根据真实数据决定版本，不为追版本号强行合并。

版本号只是建议，实际开发时必须根据当前 main 和已合并内容重新裁决。

## 16. 接手交接模板

任何任务移交给新窗口时，至少留下：

```text
Task ID=
repository=frankichen/github_mcp
base main SHA=
base Tree=
branch=
branch HEAD=
branch Tree=
Workspace ID=
Workspace revision=
Lease valid/until=
drift_reason=
Development Session ID=
Session revision=
Index job/status/indexed SHA=
changed paths=
contract findings=
affected tests=
latest fast CI=
latest full repo-auto-check=
PR number/state/draft/head=
merge authorization=yes|no
deploy authorization=yes|no
current blocker=
next exact action=
```

不得只留下“继续上一轮”“CI 已经跑过”之类无准确身份的交接。

## 17. 完成证据回写规则

每完成一个任务，必须在对应章节追加/更新：

- 最终 PR；
- PR HEAD / Tree；
- merge commit（如已授权合并）；
- exact main post-merge CI job；
- production deployment ID（如已授权发布）；
- 生产版本和 Build SHA；
- AC 验收结果；
- 未通过项和风险；
- 是否需要后续 Repair。

只有真实工具结果能把状态改成 `DONE`。

## 18. 当前下一步

DX2-00 完成并形成 Draft PR 后，建议第一项代码任务领取 **DX2-WRITE-01**：它范围最小、风险低，而且是本次文档工作真实暴露的工具闭环缺陷。随后可并行规划 DX2-INFRA-01 与 DX2-CI-01，但在 CI Worker 基础设施实际变更前必须重新确认生产资源和隔离方案。
