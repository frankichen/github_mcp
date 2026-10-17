# Development Session 安全续接：12.9.25

## 根因与修复边界

`resume_development_workspace` 保留历史 pinned base，这符合租约恢复语义。原 `resume_development_task` 却将 live main SHA 传入要求等于 pinned base 的 stale-Session adoption 校验。因此 Workspace 的 HEAD/Tree 正确、main 只是前进时，也会产生 `RECOVERY_BASE_CHANGED`。

12.9.25 将两种身份分开验证：Workspace 与 Session 必须保留相同 pinned base；live base 必须仍等于本次读取的精确 SHA；历史 base→live base、历史 base→Workspace HEAD、历史 base→Session HEAD 和 Session→Workspace 的 ancestry 均需证明。比较按 rename-aware 路径集合限量检查；达到 GitHub compare 上限、不可验证或 base/task 文件重叠均拒绝恢复。不会将 live main 偷换为已同步的 pinned base。

Scope 扩展仍须调用方审阅并提交精确路径集合。Writer owner 必须一致且只有一个 canonical Workspace/Session；Workspace 租约必须有效。原地 adoption 保留有效租约，不因 `lease_seconds` 延长租约。只有 Scope 确实扩展时才推进 Workspace Revision；Session Revision 只推进一次。旧 HEAD 的 CI、attestation、failure pack 和 Session Index 引用清空，Index 必须重新按精确 HEAD/Tree 查询。

同 HEAD 的租约/Revision 恢复也在 SQLite 事务中重读持久 Workspace，重新核对身份、owner、Revision、HEAD/Tree、base、租约和状态，阻止旧快照覆盖并发更新。

## 幂等与并发

- 原 CAS 与相同 idempotency key 可以在成功后重试，包含进程重启后的重试。每次仍验证 fresh GitHub HEAD/Tree/base、租约和 ownership/overlap。
- 相同 CAS 输入的并发恢复只写一次。CAS 失败者 fresh-read；仅当 durable recovery record 的完整 request 和结果仍匹配当前 Writer 状态时，返回 winner 结果。不同操作的 CAS 冲突保持拒绝。
- 不会通过“读取新 Revision 后自动重新写入”覆盖另一个 Writer。不同 owner、非法 HEAD/Tree、过期 Lease、未审阅 Scope、Overlap、Index 或 ancestry 不满足时继续 fail closed。
- 成功记录已被后续状态推进、旧 key 配合不同 payload、旧 HEAD/Tree 或租约时，不重放旧证据。应先只读 fresh-read，再发出新的精确操作。
- `status_summary` 保持为 bounded inline 元数据，包含准确 Workspace/Session ID、Revision、HEAD/Tree、Lease、阻塞分类、recovery_performed 和 next_allowed_actions。完整历史和审计可能通过 MCP response resource 返回，应读取 resource，不能将 summary 缺省字段解释为成功。

## A #522 原 Session 的续接

身份必须保持：

| 对象 | 值 |
| --- | --- |
| Repository | `frankichen/sxt` |
| Branch | `ai/sxt-a522-pozo-dark-shell-20261016` |
| Draft PR | `1092` |
| Workspace | `ws_8636ec1f6f254bbd` |
| Session | `dev_b1c6d1f3c46a4c9da12b` |
| HEAD | `7c55f21f68c8f2161ed599e51a4138cb2cf4bf3b` |
| Tree | `02f6225420e5f5776ae6be2ab967abd33715ddfb` |
| Pinned base | `5de1a9d3cbe72dc2b40fd549f48aba98cc41af7c` |

先读取当前状态，不续租：

```json
{"tool":"resume_development_task","arguments":{"repository":"frankichen/sxt","branch":"ai/sxt-a522-pozo-dark-shell-20261016","pull_number":1092,"recover_stale_session":false,"renew_lease":false}}
```

读取 `workspace.revision`、`development_session.session_revision` 和两者身份。2026-10-17 的生产快照为 Workspace 6、Session 2，Scope 为空。真实 GitHub 证据要求审阅的扩展路径只有 `h5/lenshub-console-react/src/styles.css`。若 fresh-read 仍为该身份和 Revision，可由原 Writer/Manager 审阅后调用：

```json
{
  "tool": "resume_development_task",
  "arguments": {
    "repository": "frankichen/sxt",
    "branch": "ai/sxt-a522-pozo-dark-shell-20261016",
    "pull_number": 1092,
    "recover_stale_session": true,
    "renew_lease": false,
    "expected_workspace_revision": 6,
    "expected_session_revision": 2,
    "lease_seconds": 7200,
    "idempotency_key": "sxt-a522-original-session-resume-12.9.25-20261017",
    "reviewed_scope_expansion_paths_json": "[\"h5/lenshub-console-react/src/styles.css\"]"
  }
}
```

如果 Scope 不完整，服务返回 `RECOVERY_SCOPE_VIOLATION`、精确 `required_scope_expansion_paths` 和 `recovery.retry_arguments`。先审阅路径，再执行这些参数。不得把审阅字段当作自动接受任意 Scope 的开关。

在生产状态快照、真实 GitHub、真实 Streamable HTTP MCP 的隔离实例中，该操作已保留原 Workspace/Session 身份、HEAD/Tree、原租约和 pinned base，将 Workspace 6→7、Session 2→3，`exact_head=true`、无 blockers。Workspace 的推进来自审阅后的 Scope 扩展。真实生产 A/C 的有效租约不用于此回归。

成功后使用返回的最新 Workspace/Session Revision。原请求可原样重试验证幂等；后续代码写入使用最新 Revision。如果租约已过期，先由原 Writer 用 fresh Workspace Revision 执行 `resume_development_workspace`，再读取 Session/Workspace 并按返回身份恢复。不得用新建 Task/Session/Workspace 绕过冲突。

## main 前进且存在文件重叠

返回 `RECOVERY_BASE_OVERLAP`、rename-aware `overlapping_paths`、`sync_plan_arguments` 和有顺序的 `baseline_convergence_steps`。先执行只读 `plan_development_workspace_sync` 并审阅准确差异。若另行授权同步，可使用 GitHub 官方 `update_github_pull_request_branch(repository,pull_number,expected_head_sha)`，随后按精确 Revision `refresh_development_workspace`，只读 `resume_development_task` 获取 fresh `recover_base_synced_development_task` 身份参数，再按该工具原有 exact overlap/scope review 合同恢复。

本次修复和验证不自动同步任何 SXT Branch，不 force-push/rebase、不 merge PR、不发布业务代码。任何无法证明 ancestry、base、Scope 或 ownership 的路径都不能继续写。

## C #525 与兼容性

`ws_6402a5a9bf71482b` / `dev_885b3f49eb424958bf17` 的 Workspace 8、Session 9、HEAD `d2d2052c85838c9ffd9f8f925c593da18122f810` 续接仍有效。过时 Revision 若属于不同操作仍返回 Revision mismatch，必须 fresh-read；只有精确匹配已完成恢复证据的请求可以 reconcile。

没有新增或删除 MCP 工具，没有改变参数 Schema，没有数据库迁移。165 个 canonical tools / 176 个 compatibility registrations 保持不变。新增错误分类 `RECOVERY_BASE_OVERLAP`、`INDEX_IDENTITY_MISMATCH` 和结构化恢复信息；客户端应使用 `next_allowed_actions` 和 fresh Revision，而不只匹配旧的 `RECOVERY_BASE_CHANGED` 文本。

## 验证与部署原则

回归使用独立容器/临时 SQLite，覆盖 historical base→new main、Workspace 已恢复/Session 过期、无重叠/有重叠、HEAD/Tree/Lease/owner 改变、CAS 竞争、重复 key、不同 key 的同一操作、Scope、Index/CI 失效、第二次调用和新进程幂等。另使用生产只读快照与真实 GitHub 通过隔离 MCP 验证 A/C。

部署必须使用已提交且远端可解析的 exact commit 镜像，保留现有环境、密钥挂载和持久卷；只更新本机 Controller，de 保持公网 Nginx 入口。禁止向旧进程热覆盖 Python 源码以免内存模块与 build SHA 不一致。滚动切换前检查未完成 GitHub 写入，保留旧容器/镜像和完整启动配置。回滚使用旧镜像与原挂载，不能用旧数据库快照覆盖已经继续推进的 Writer 状态。
