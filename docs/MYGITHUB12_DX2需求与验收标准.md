# MyGithut12 DX-2 需求与验收标准

## 1. 文档目的

本文定义 DX-2 的详细功能需求、非功能要求、安全边界和验收标准。开发实现、测试、PR、发布和最终验收均必须逐条对照本文，不得用“代码存在”“单测通过”或“看起来可用”替代真实验收。

状态只允许：`NOT_STARTED`、`IN_PROGRESS`、`BLOCKED`、`DONE`、`SUPERSEDED`。

`DONE` 必须同时具备：准确源码身份、自动化测试、真实环境证据、文档/Manifest/Capability 同步，以及不存在未解释的安全或兼容性 Finding。

## 2. 统一约束

### 2.1 身份与事实来源

所有功能必须绑定准确的：

- repository；
- branch / PR；
- 40 位 Commit SHA；
- Tree SHA；
- Worker / deployment / CI job identity；
- 生产 Build SHA（涉及运行时验收时）。

任何缓存、Session、Workspace、Index、CI、Deployment 都不能用旧 SHA 冒充当前 SHA。

### 2.2 安全底线

DX-2 所有功能均必须保持：

- repository allowlist；
- expected HEAD / expected Blob；
- Workspace lease / revision CAS；
- Session revision CAS；
- durable GitHub fresh read-back；
- idempotency；
- source immutability；
- Private CI attempt/lease fencing；
- fixed deployment contract；
- Secret redaction；
- merge/deploy/rollback 显式授权。

新增高层工具不得接受任意 Shell、SQL、host、port、remote URL、系统文件路径或 Secret 值。

## 3. FR-DX2-CI-01：Private CI 双 Worker 与安全调度

### 3.1 问题

当前生产只有 `wsl-ci-01`，`max_concurrent=1`。多个 Web AI 窗口同时提交 CI 时形成单队列，`frankichen/sxt` 一次 full `repo-auto-check` 近期约 221～231 秒，排队会直接叠加等待时间。

### 3.2 功能需求

1. 至少新增一个独立 Worker，建议 ID 为 `wsl-ci-02`；
2. 两个 Worker 使用独立执行目录、容器名称空间、临时目录和 attempt context；
3. 初始每个 Worker 仍 `max_concurrent=1`，总并发通过 Worker 数量扩展；
4. Controller Scheduler 必须按 job lease / attempt fencing 保证同一 job 同一时刻只有一个有效 Worker；
5. Worker 失联、超时或重注册时，旧 lease 的后续 step/log/status 更新必须被拒绝；
6. 不取消或抢占已经运行的其他用户任务；高优先级只影响 queued job 的选取顺序；
7. `main`、合并后门禁和显式高优先级 job 可以高于普通 AI branch，但不得绕过 FIFO/公平性到造成长期饥饿；
8. 缓存必须按既有 immutable identity 使用；两个 Worker 不得共享可写 source worktree；
9. rootless Podman、网络、CPU、内存、磁盘临时空间必须有明确资源上限；
10. `list_private_ci_workers` 必须准确显示每个 Worker 的 online/status/current_job/max_concurrent；
11. Scheduler 需要暴露最少的 queue evidence：queue position、priority、eligible worker 或不可调度原因；
12. 不改变 `repo-auto-check` 的测试范围和 merge credential 语义。

### 3.3 验收标准

- AC-CI-01：两个互不冲突的真实 CI job 在两个 Worker 上同时进入 `running`，而不是先后串行；
- AC-CI-02：两个 job 的 source directory、container identity、log、step、attempt evidence 不交叉；
- AC-CI-03：人为模拟一个 Worker 离线后，另一个 Worker 仍可继续接收新 job；
- AC-CI-04：旧 Worker 使用 stale lease 回调必须被稳定错误码拒绝；
- AC-CI-05：同一 job 不得出现两个有效 attempt 同时写状态；
- AC-CI-06：两个真实 `repo-auto-check` 并发运行时均能完成，且最终 Worker 回到 `idle/current_job=null`；
- AC-CI-07：CI 运行期间不出现共享 worktree 污染、跨 job 文件变化或 cache identity 混用；
- AC-CI-08：对 `frankichen/sxt` 做至少 5 组并发样本，证明排队等待显著下降；单 job 本身耗时不要求因 Worker 扩容而虚假下降；
- AC-CI-09：Controller/Agent 测试覆盖 claim race、worker loss、stale lease、duplicate callback、queue priority 和 cache isolation；
- AC-CI-10：生产 `list_private_ci_workers` 真实返回至少两个 online Worker 后才能标记 DONE。

## 4. FR-DX2-SESSION-01：Session / Workspace 安全恢复

### 4.1 问题

底层 `apply_github_patch`、Range Edit 或 Upload 在 Workspace CAS 下可以成功推进 GitHub branch 与 Workspace HEAD，但 Development Session 可能仍保留旧 HEAD。此时 GitHub branch 与 Workspace 实际一致，却会在高层 `validate_development_task` 中产生 recovery/drift 类阻塞。

### 4.2 功能需求

1. 必须明确区分“Session stale”与“真实 branch drift”；
2. 允许恢复的必要条件至少包括：
   - GitHub branch HEAD == Workspace HEAD；
   - Workspace `drift_reason=null`；
   - Workspace status 可恢复且 lease 状态可判定；
   - Commit/Tree 可由 GitHub fresh read-back 验真；
   - 写入操作存在 durable success evidence，或当前 branch/Workspace identity 可以被独立证明；
3. Session stale 时可以把 Session head/tree/workspace_revision 同步到 Workspace；
4. Session revision 必须 CAS 推进并记录 recovery event；
5. 若 GitHub branch HEAD != Workspace HEAD，必须返回真实 drift，禁止自动覆盖；
6. 若 branch 已被外部推进且无法证明来源，必须 FAIL-STOP；
7. 恢复不得 rebase、merge、force push、reset 或移动 branch；
8. 恢复后旧 CI/Index evidence 仅在 exact SHA 一致时可复用；
9. 恢复动作必须幂等，多次调用不能不断增加无意义 revision；
10. 所有 recovery 结果返回 `before/after` identity 和 `recovered=true/false`。

### 4.3 验收标准

- AC-SESSION-01：构造“GitHub HEAD == Workspace HEAD != Session HEAD”，一次恢复后 Session 精确同步且可继续 full validation；
- AC-SESSION-02：构造真实外部 branch drift，恢复必须拒绝且不修改 Session/Workspace/branch；
- AC-SESSION-03：恢复后 exact HEAD Index 不存在时自动请求新 Index，但不得使用旧 Index 冒充；
- AC-SESSION-04：恢复后旧 CI job SHA 不一致时不得作为 merge credential；
- AC-SESSION-05：同一 recovery idempotency key 重放返回同一结果；
- AC-SESSION-06：事件审计能区分 `session_recovered`、`external_drift_detected`、`recovery_refused`；
- AC-SESSION-07：不出现 `WORKSPACE_BRANCH_DRIFTED` 被错误用于描述纯 Session stale 的情况。

## 5. FR-DX2-WS-01：Workspace Lease 生命周期自动收敛

### 5.1 问题

当前可见 `status=active` Workspace 中存在大量 `lease_valid=false` 历史项。它们虽然不再拥有有效写 Lease，却仍污染默认列表与 overlap 心智模型。

### 5.2 功能需求

1. Workspace 必须区分 persisted status 与 effective lease state；
2. Lease 到期后应自动或惰性收敛为 `expired`（或等价明确状态），不能无限保留为 active Writer；
3. `status=active` 默认查询只返回真正具备活动资格的 Workspace；
4. overlap 默认只把真正活动 Writer 作为冲突候选；
5. expired Workspace 必须继续保留 branch、head/tree、scope、owner、created/updated、审计和历史 Index identity；
6. 过期不能自动删除 branch、PR、Commit、Index history 或审计；
7. expired Workspace 如需恢复，必须通过显式 resume/recovery 路径重新校验 branch、base、drift 和 Lease；
8. 不能通过简单续 Lease 让已发生真实 drift 的 Workspace 恢复可写；
9. 状态迁移需要兼容旧数据库记录；
10. retention 只能清理可重建缓存或过期 pin，不得破坏 GitHub 事实对象；
11. 在自动续签完成前，Workspace 创建、显式续签和 `prepare_development_task` 的临时默认 Lease 统一为 7200 秒（2 小时），`MAX_LEASE_SECONDS` 继续保持 14400 秒（4 小时）；
12. 后续自动续签必须是 activity-driven，只能由受控 Development Session 编排动作触发；没有用户/AI 活动时不得后台无限续签；
13. 自动续签前必须 fresh-read 并证明 GitHub branch HEAD == Workspace HEAD == Session HEAD、Workspace `drift_reason=null`、status=active、当前 Lease 尚有效；
14. 自动续签必须使用 expected Workspace revision CAS；Workspace revision 推进后必须同步 Session 的 workspace revision/identity，禁止再次制造 `DEVELOPMENT_SESSION_WORKSPACE_MISMATCH`；
15. expired、drifted、closed 或 identity 无法证明的 Workspace 不得自动复活，必须进入显式 resume/recovery；
16. 自动续签必须幂等、可审计，记录 before/after expiry 与 revision；单次自动续签只恢复到当前默认 Lease 窗口，不能突破 `MAX_LEASE_SECONDS`。

### 5.3 验收标准

- AC-WS-01：创建短 Lease Workspace，等待过期后默认 active 列表不再把它当 Writer；
- AC-WS-02：历史记录仍可查询完整 identity/scope/audit；
- AC-WS-03：overlap 不再因 expired Workspace 产生 high/medium 假冲突；
- AC-WS-04：显式 resume 对 branch 未漂移的 expired Workspace 可安全恢复并获得新 revision/lease；
- AC-WS-05：branch 已漂移的 expired Workspace resume 必须拒绝；
- AC-WS-06：升级前已有 `active + lease_invalid` 数据能被兼容读取和收敛；
- AC-WS-07：不会删除任何 branch 或 PR；
- AC-WS-08：`create_development_workspace`、`renew_development_workspace_lease`、`prepare_development_task` 的 Schema 默认 `lease_seconds` 均为 7200，且显式更短/更长合法值仍按现有 60～14400 秒边界工作；
- AC-WS-09：无调用活动时 Workspace 不会被后台自动续签，最终仍可自然过期；
- AC-WS-10：活动 Session 在续签阈值内且三方 identity 一致时，可一次受控自动续到新的 2 小时窗口；
- AC-WS-11：GitHub branch 外部漂移、Workspace drift 或 Session stale 无法安全恢复时，自动续签必须拒绝且不修改 expiry/revision；
- AC-WS-12：已经 expired 的 Workspace 不得由自动续签直接复活，必须走显式 resume/recovery；
- AC-WS-13：自动续签后 Workspace revision 与 Session workspace revision 同步，重复同一幂等请求不会无限延长 Lease。

## 6. FR-DX2-WRITE-01：新文件 Patch Builder / Strict Apply 一致性

### 6.1 问题

2026-08-27 起草 DX-2 文档时，`build_github_patch(original_text="", replacement_text=...)` 对不存在的新文件生成了 `--- a/path` / `+++ b/path` 的普通修改头；同一结果交给 strict `apply_github_patch` 时被解释为“修改已有文件”，最终返回 `FILE_NOT_FOUND`。手工改为 `new file mode 100644`、`--- /dev/null` 后 dry-run 才通过。

这意味着“服务端推荐的 deterministic patch builder”与“服务端 strict apply parser”对新增文件的语义不闭环，会额外消耗人工诊断和 payload 重构时间。

### 6.2 功能需求

1. `build_github_patch` 必须根据“目标是否存在/调用意图”生成与 `_parse_patch_details` 完全兼容的 add/modify/delete patch；
2. 对新文件至少生成 `new file mode 100644`、`--- /dev/null`、`+++ b/path` 和正确 hunk；
3. 对删除文件生成 strict parser 可识别的 delete 语义；
4. 如果纯函数无法知道 GitHub 文件是否存在，应显式增加 operation 参数，而不是猜测；
5. Builder 输出必须能原样传给 `apply_github_patch`，不得要求调用方手工重写 header；
6. LF/CRLF、末尾换行和 UTF-8 中文必须保持确定性；
7. Builder 与 apply 的 operation fingerprint/content SHA 应可审计；
8. 不降低 strict parser 对非法 patch、路径逃逸、重复 path、ambiguous hunk 的拒绝。

### 6.3 验收标准

- AC-WRITE-01：builder 生成的新文件 patch 原样进入 strict apply dry-run 成功；
- AC-WRITE-02：add/modify/delete 各有正向测试；
- AC-WRITE-03：中文、CRLF、无/有末尾换行均做 round-trip byte equality 测试；
- AC-WRITE-04：不存在文件的 modify 仍返回 `FILE_NOT_FOUND`，不能被误当成 add；
- AC-WRITE-05：已存在文件的 add 仍返回稳定冲突错误；
- AC-WRITE-06：Development ChangeSet patch mode 使用 builder 输出时可完成 dry-run → commit → durable read-back → Session/Workspace sync。

## 7. FR-DX2-INFRA-01：Infrastructure Executor 独立 heartbeat

### 7.1 问题

当前 executor 在主循环中 heartbeat，然后同步进入 `execute(row)`。长 Docker build / preheat 期间主循环无法再次 heartbeat，超过 TTL 后 Controller 会暂时显示 executor offline，虽然 deployment 仍在正常运行。

### 7.2 功能需求

1. deployment 执行期间 heartbeat 必须由独立线程或等价独立调度持续发送；
2. heartbeat interval 继续使用固定安全配置，默认约 5 秒；
3. heartbeat 不得读取或输出 callback Secret；
4. Controller 短暂切换/502 时 heartbeat 失败不得直接杀死已在执行的 fixed deploy script；
5. heartbeat 恢复后必须继续报告同一 `current_deployment_id`；
6. deployment 结束后 executor 必须回到 idle，并停止 running heartbeat；
7. heartbeat thread 必须有可靠 stop/join，不能泄漏后台线程；
8. executor 进程退出时不得遗留假的 running state 超过 TTL；
9. 不修改 fixed repository/environment/scope/script/fail-stop/no-auto-rollback 合同。

### 7.3 验收标准

- AC-INFRA-HB-01：执行超过 2 倍 heartbeat TTL 的真实或受控长任务，executor 始终 `online=true/state=running`；
- AC-INFRA-HB-02：Controller 切换期间短暂 heartbeat 失败，Controller 恢复后同一 deployment identity 继续；
- AC-INFRA-HB-03：deployment terminal 后 heartbeat 在一个周期内恢复 `idle/current_deployment_id=null`；
- AC-INFRA-HB-04：单测覆盖 heartbeat thread 生命周期、异常、stop、deployment failure 和 timeout；
- AC-INFRA-HB-05：真实 self-deploy E2E 中不再出现“deployment running 但 executor offline”的正常路径误报。

## 8. FR-DX2-INFRA-02：Infrastructure wait 与紧凑诊断

### 8.1 功能需求

1. 优先扩展现有 `get_infrastructure_deployment`，增加兼容可选参数：`wait_seconds`、`last_known_revision`、`include_log_tail`/`log_tail_lines` 或等价设计；
2. wait 最长不超过 MCP 安全长轮询窗口；
3. 只有 status/step/revision/terminal 发生变化或超时时返回；
4. 默认结果保持 compact，不返回完整部署日志；
5. 日志尾必须经过现有 Secret redaction；
6. 结构化阶段至少能区分 validation、source prepare、controller build/switch、health、preheat、post-verify、completed/failed；
7. Controller 切换造成短暂连接中断时，deployment DB 记录必须连续；
8. 不新增取消、回滚或任意脚本能力。

### 8.2 验收标准

- AC-INFRA-WAIT-01：一次真实 self-deploy 等待不需要每几秒重复调用；
- AC-INFRA-WAIT-02：wait 能在 step/revision 变化时及时返回；
- AC-INFRA-WAIT-03：终态准确返回 `passed/failed + exit_code + error_code`；
- AC-INFRA-WAIT-04：日志尾不出现 Secret、Authorization、Cookie、Token、数据库连接串；
- AC-INFRA-WAIT-05：旧客户端只传 `deployment_id` 的行为完全兼容。

## 9. FR-DX2-RESUME-01：`resume_development_task`

### 9.1 使用场景

用于 ChatGPT 新窗口、上下文压缩后续接、PR 接管或 branch 接管。调用者不应被迫知道旧 Session ID / Workspace ID。

### 9.2 功能需求

1. 输入至少支持 `repository + branch` 或 `repository + pull_number`，两者同时提供时必须验证一致；
2. fresh-read repository policy、current main、branch/PR HEAD/Tree；
3. 查找对应 Workspace、Session、Lease、revision、drift、scope；
4. 查找 exact HEAD Index；
5. 查找当前 HEAD 最近有效 Fast/Full CI 和 Attestation；
6. 汇总 PR state/draft/base/checks/readiness；
7. 汇总 active overlap；
8. 当且仅当满足 FR-DX2-SESSION-01 时允许安全修复 stale Session；
9. lease 过期但 branch 未漂移时，返回可恢复计划；是否自动续 Lease必须由明确参数和 CAS 控制；
10. 不自动改 branch、rebase、merge、close PR、delete branch；
11. 返回 `next_allowed_actions`，例如 `continue_write`、`run_fast_ci`、`run_full_ci`、`prepare_pr`、`readiness`、`recovery_required`；
12. 大详情使用 Resource fallback。

### 9.3 验收标准

- AC-RESUME-01：在全新客户端上下文里只提供 branch，一次调用返回可继续开发所需 identity；
- AC-RESUME-02：只提供 PR number 也能恢复同一信息；
- AC-RESUME-03：Session stale 且 Workspace/GitHub 一致时可安全恢复；
- AC-RESUME-04：真实 drift 返回阻塞证据且不写任何 GitHub 状态；
- AC-RESUME-05：不会把旧 SHA CI/Index/Attestation 当成当前证据；
- AC-RESUME-06：返回结果中明确区分 live fact、historical evidence、candidate next action；
- AC-RESUME-07：常规 warm resume P95 目标小于 5 秒，不包括新 Index 构建等待。

## 10. FR-DX2-CONVERGE-01：`converge_development_task`

### 10.1 功能需求

1. 接受 Development Session ID 和准确 Session revision；
2. 首先验证 Session/Workspace/GitHub HEAD/Tree 一致；
3. 对新 HEAD 请求或复用 exact Repository Index；
4. 自动执行 Change Context Pack；
5. 自动执行 Change Impact；
6. 自动执行 Contract Change Detection；
7. 自动执行 Affected Tests selection；
8. 根据调用模式运行 fast 或 full CI；
9. full 模式必须使用 `repo-auto-check`，不得因 affected selection 缩小最终门禁，除非 repository policy 已明确改变；
10. 新 Commit 产生后旧 CI/Index/impact 立即失效；
11. 任一步降级必须在结果中明确 `degraded=true`、原因和保守扩大策略；
12. CI 失败时直接返回 Failure Pack 引用；
13. 成功结果至少包含 exact HEAD/Tree、Index identity、impact、contract findings、affected tests、CI job/status、Worker final state、merge eligibility；
14. 不创建 merge、不生产 deploy、不 rollback；
15. 默认 compact response，详情进入 Resource。

### 10.2 验收标准

- AC-CONV-01：普通新 Commit 从 post-write 到 full CI 终态可由一个高层调用完成编排；
- AC-CONV-02：输入 stale Session revision 必须 CAS 拒绝；
- AC-CONV-03：Index 对应旧 SHA 时必须新建/请求当前 SHA Index；
- AC-CONV-04：contract/impact 失败或不完整时 Full CI 不得被缩小；
- AC-CONV-05：full success 返回 exact `repo-auto-check` job 且 Worker 最终 idle；
- AC-CONV-06：CI failure 返回稳定 failure pack，不能伪称收敛成功；
- AC-CONV-07：merge/deploy 权限边界与现有工具完全一致。

## 11. FR-DX2-PERF-01：真实 CI Performance Validation

### 11.1 功能需求

1. 只使用真实 Private CI job 作为性能证据；
2. 按 repository/profile/workspace/image/toolchain 分组；
3. 至少统计 queue wait、total wall、关键 step duration、cache hit/miss；
4. 支持 P50/P90/P95 或明确样本量不足；
5. 样本必须排除 cancelled、superseded、infra-invalid job；
6. 记录并发度和 Worker identity，避免把扩容收益误认为单 job 加速；
7. `supports_real_ci_performance_validation` 只有在功能、真实样本、测试和文档全部通过后才能置 true；
8. 性能回归阈值先告警后门禁，门禁启用必须有独立评审；
9. 不为了达标跳过测试、降低 profile 或复用不合法 cache。

### 11.2 验收标准

- AC-PERF-01：至少有 20 个真实 job 的可复现统计；
- AC-PERF-02：能区分 queue wait 与 execution duration；
- AC-PERF-03：能比较扩容前后的 queue P95；
- AC-PERF-04：能识别单个 workspace/step 的主要耗时；
- AC-PERF-05：Capability 在真实验收前保持 false，验收通过后由代码/Manifest/生产一致地变为 true。

## 12. FR-DX2-HYGIENE-01：历史对象可见性治理

### 12.1 功能需求

1. Workspace 列表提供 active/expired/drifted/closed 的明确统计；
2. Draft PR、历史 branch、Workspace 不因为“很旧”自动关闭或删除；
3. 提供只读“可能已废弃”候选及依据，例如 lease 过期、PR 已合并、branch behind 很久；
4. 任何 destructive cleanup 必须是单独授权的后续操作；
5. Index pin retention 与 Workspace effective state 对齐，防止无意义长期占用。

### 12.2 验收标准

- AC-HYGIENE-01：默认活动列表不再被大量 expired 项淹没；
- AC-HYGIENE-02：审计模式仍能查到全部历史对象；
- AC-HYGIENE-03：没有任何自动 branch/PR 删除行为。

## 13. 回归与发布总验收

每个 DX-2 版本合并前必须：

1. exact PR HEAD Repository Index ready；
2. Change Context / Impact / Contract / Affected Tests 已执行；
3. `repo-auto-check` 对当前 PR HEAD passed；
4. PR readiness 满足当前仓库真实门禁；
5. 用户明确授权后才允许合并；
6. 合并后对新 main exact SHA 重新运行 `repo-auto-check`；
7. 用户明确授权发布后才允许 self-deploy；
8. self-deploy 使用 fixed infrastructure deployment contract；
9. deployment `passed / exit_code=0`；
10. 生产 `/health`、`/ready`、version、Build SHA、Worker、Executor 全部验真；
11. 不复用 PR CI 作为生产发布凭据；
12. 无回滚授权时失败必须 fail-stop。

## 14. DX-2 总体验收门槛

DX-2 整体完成时至少满足：

- 真实两个 Private CI Worker 可并行；
- queue wait 有量化下降证据；
- Session stale 可恢复且真实 drift 不可绕过；
- expired Workspace 不再作为默认 active Writer；
- builder 生成的新增文件 patch 可直接 strict apply；
- Infrastructure Executor 长任务持续 online；
- infrastructure deployment 可 long-poll；
- 新窗口 branch/PR 一次 resume 得到完整上下文；
- Commit 后一次 converge 得到 Index/Impact/Contract/Tests/CI 终态；
- 所有安全门禁、授权边界保持；
- Manifest、Capability、README、客户端 Schema、生产 Build SHA 一致；
- 没有未解释的高风险 Finding。
