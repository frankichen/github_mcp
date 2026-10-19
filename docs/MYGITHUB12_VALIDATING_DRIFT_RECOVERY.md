# 验证中 Session 与漂移 Workspace 的恢复（12.9.26）

当 Session 仍为 `validating_fast` / `validating_full`，而 Workspace 已经标记
`branch_moved_externally` 时，旧版直接恢复会报 Session 状态错误，直接验证
又会在 Writer/Lease 预检处拒绝漂移 Workspace。

现在两个入口都先核对旧验证的准确 generation、Request/Worker、SHA/Tree、
Profile、Base 和终态证据。验证仍在运行或证据不完整时继续拒绝接管，
返回 `DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS` 或
`DEVELOPMENT_SESSION_RECOVERY_REQUIRED`，并提供查询/恢复动作。
`force_rerun` 不会跳过这一步，也不会启动第二次 CI。

## 原身份续接

1. 调用 `resume_development_task(repository=..., branch=...,
   recover_stale_session=false, renew_lease=false)` 读取最新身份、修订号和恢复计划。
2. 使用计划中的 `recover_drifted_development_task`、
   `recover_base_synced_development_task` 或 `recover_retargeted_development_task`，
   传入准确 CAS、当前 HEAD/Tree、Base、已审阅的 Scope/Overlap 和稳定幂等键。
   恢复入口现在可以核对并结清终态旧验证后继续恢复，无须先手工改 Session 状态。
3. 原 Task/Branch/PR/Workspace/Session 身份保持，恢复后重新验证新 HEAD。
   旧 HEAD 的 CI、Attestation、Failure/Index 指针由现有恢复事务失效处理。

另一条合法路径是先调用 `validate_development_task`，使用原 Session ID、
最新 Session Revision 和原 mode。它会先结清旧验证并返回
`recovery_required=true`、`writer_ready=false`、`merge_eligible=false`、最新身份
和后续恢复步骤，Workspace 仍然保持漂移，随后再按新修订号调用恢复接口。

```json
{
  "development_session_id": "dev_86b16dfe364f4c9683ce",
  "expected_session_revision": 39,
  "mode": "fast",
  "force_rerun": false
}
```

以上是排查时的真实 Session 示例；调用前必须 fresh-read 当前 Revision。
本次发布没有替业务 Worker 续租或接管该 Session。

## CAS、幂等与发布

验证结清是独立的历史证据持久化；分支采用仍由原 Workspace/Session CAS 事务执行。
事务使用本次结清产生的准确 Session Revision，原请求参数保留在幂等记录中，
成功后原幂等键及原参数重放不会再续租或增加修订号。
若结清后发生竞争或进程中断，Workspace 不会被提前激活；fresh-read 最新修订号
再恢复。已有的 SHA/Tree、Ancestry、Scope、Overlap、Owner、Lease 与 Index 检查保留。

控制面发布为 12.9.26，MCP 输入 schema 不变，无数据库 schema 迁移。
通过保留的旧 Controller 容器可回滚；历史验证审计仍可由旧版读取。
回滚到旧版会恢复旧接口限制，需要使用 `resume_development_task` 的历史恢复路径。
