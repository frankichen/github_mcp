# MyGithut12 Drifted Workspace / Development Session Recovery

版本：12.7.1  
状态：正式 MCP contract  
工具：`recover_drifted_development_task`

## 1. 目标

为 `Workspace.status=drifted` 且 `drift_reason=branch_moved_externally` 的开发任务提供显式、受保护的控制面恢复入口。

Recovery 的唯一语义是：**把已经存在并重新验真的当前 GitHub branch HEAD / Tree 安全 adopt 到 Workspace 与 Development Session 控制面状态。**

它不是 Git 操作，不允许执行 reset、branch update、force push、rebase、merge、commit 或文件写入。

## 2. MCP 输入合同

必需身份/CAS 输入：

- `repository`
- `branch`
- `workspace_id`
- `development_session_id`
- `expected_workspace_revision`
- `expected_session_revision`
- `expected_current_head_sha`
- `expected_current_tree_sha`
- `expected_base_branch`
- `expected_base_sha`
- `idempotency_key`

可选：`lease_seconds`，默认 7200，仍受现有最大 Lease 限制。

调用方不能只提供 Workspace ID 无条件恢复；所有关键身份必须由 fresh GitHub 事实和 CAS 共同证明。

## 3. Fail-stop 门禁

恢复前必须同时满足：

1. Workspace / Session 的 repository、branch、base branch、相互绑定关系准确一致。
2. Workspace 必须仍是 `drifted + branch_moved_externally`；closed、branch deleted、未知 drift reason 等全部拒绝。
3. Workspace / Session revision 与 expected revision 完全一致。
4. 再次 fresh-read GitHub：actual branch HEAD、Tree、base branch HEAD 分别等于 expected identity。
5. 旧 Session HEAD 必须是 current branch HEAD 的 ancestor，且 compare 必须是纯 forward-only（ahead > 0、behind = 0、merge-base = old Session HEAD）。
6. old Session HEAD 对应 Tree 必须仍等于 Session 保存的旧 Tree；无法证明 ancestry、force-push、rollback、rewrite、unrelated history 全部返回 `RECOVERY_ANCESTRY_MISMATCH`。
7. old Session HEAD → current HEAD 的全部 changed paths 必须属于 Workspace 声明 scope；否则 `RECOVERY_SCOPE_VIOLATION`。
8. 目标 drifted Workspace 必须仍是该 branch 既有 owner，不允许第二个 active/drifted owner，也不允许 high-overlap active Workspace。
9. 在事务提交前再次 fresh-read GitHub HEAD / Tree / base；任一变化导致事务整体回滚。

## 4. 原子状态迁移

所有门禁通过后，Workspace 与 Development Session 在同一个 SQLite 事务中推进。

Workspace：

- `head_sha = current HEAD`
- `tree_sha = current Tree`
- `status = active`
- `drift_reason = NULL`
- Lease 更新
- `index_commit_sha = NULL`
- `revision += 1`

Development Session：

- `head_commit_sha = current HEAD`
- `tree_sha = current Tree`
- `workspace_revision = new Workspace revision`
- `status = active`
- Lease 更新
- `index_commit_sha = NULL`
- 旧 fast/full CI、attestation、failure evidence 清空
- `session_revision += 1`

任何中途异常都回滚，禁止出现 Workspace active / Session stale 或相反的半恢复状态。

## 5. 审计与幂等

成功恢复写入 `manual_branch_recovery` Session event，并记录：repository、branch、Workspace/Session ID、old/new revisions、old Session HEAD、adopted HEAD/Tree、base SHA、原 drift reason、ancestry/scope/ownership 验证结果和 idempotency key 的 SHA-256 identity。

审计不得记录 Token、Authorization、Cookie、DSN 或 Secret。

相同 idempotency key + 相同完整请求 identity 重放时返回同一 recovery result，不再次推进 revision；同 key 不同 payload 返回 `IDEMPOTENCY_CONFLICT`。

## 6. Index 与 Writer readiness

控制面恢复与 Index readiness 明确分离：

- 成功状态使用 `CONTROL_PLANE_RECOVERY_SUCCESS`。
- recovered exact HEAD Index 已 ready 时：`index_required=false`、`writer_ready=true`。
- Index 未 ready 时请求 fresh exact-HEAD Index。
- Index 查询/请求失败不会反向破坏已经完成的 Workspace/Session 原子恢复，但返回 `index_required=true`、`writer_ready=false`。
- P2P、权限、支付、OTA、密钥、状态机等高风险任务在 exact recovered HEAD Index ready 前仍不得继续 Writer 操作。

## 7. `resume_development_task` 集成

`resume_development_task` 仍保持保守：

- stale-but-safe Session 继续走现有 DX2 recovery；
- expired Workspace 继续只能显式 `resume_development_workspace`；
- drifted Workspace **不得**由 resume 自动恢复；
- drifted response 明确返回 `recovery_tool=recover_drifted_development_task` 和 `manual_recovery_required=true`。

推荐流程：

`resume_development_task` → `WORKSPACE_BRANCH_DRIFTED` → `recover_drifted_development_task` → Fresh Index → `resume_development_task` → normal active context。

## 8. `put_generated_files` 安全模型保持不变

本功能不得重新暴露 caller-managed Workspace revision / Session revision / blob staging。

`put_generated_files` 仍只接受 repository、branch、expected HEAD、结构化 files、commit message、dry-run 和可选 idempotency key，由服务端自动解析 Workspace / Development Session / collaboration CAS。

未 recovery 的 drifted Workspace 继续返回 `WORKSPACE_BRANCH_DRIFTED`；只有正式 recovery 后才允许重新绑定 active collaboration state。

## 9. 验收矩阵

1. forward-only external branch advance → PASS
2. actual HEAD ≠ expected HEAD → FAIL
3. actual Tree ≠ expected Tree → FAIL
4. Workspace revision mismatch → FAIL
5. Session revision mismatch → FAIL
6. old Session HEAD 非 current HEAD ancestor → FAIL
7. force-push / rewrite / rollback → FAIL
8. branch deleted → FAIL
9. base HEAD changed → FAIL
10. changed path 超 Workspace scope → FAIL
11. wrong repository → FAIL
12. wrong branch → FAIL
13. closed Workspace → FAIL
14. unsupported/unknown drift reason → FAIL
15. second active branch owner / high overlap → FAIL
16. same idempotency replay → same result / no extra revision
17. same key different payload → `IDEMPOTENCY_CONFLICT`
18. transaction injected failure → Workspace/Session both rollback
19. success 后 Workspace/Session HEAD/Tree 一致
20. success 后两类 revision 各精确 +1
21. success 后 resume normal context 可继续
22. success 后 generated-file collaboration resolver 绑定 recovered state
23. old Workspace/Session revision 不可继续使用
24. unrecovered drifted Workspace 仍阻止 generated-file write

此外必须回归现有 expired/resume/renew/write fail-stop、DX2 stale-safe recovery、refresh drift detection、HEAD/Workspace/Session CAS 和 scope isolation。
