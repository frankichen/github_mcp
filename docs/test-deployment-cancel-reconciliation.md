# 测试环境委托部署取消收口（安全恢复合同）

## 适用范围

仅对 `frankichen/sxt` / `gongshi-test` / `fullstack` 的**尚未执行**委托部署实行自动取消收口。控制面源码位于 `frankichen/github_mcp`，WSL 执行器访问已认证的内部接口。本修复不允许对 SXT 支付状态机、API、数据库、Secret、生产环境或历史 Release 作任何修改。

## 故障与握手

private-deploy-agent 的 claim-only 模式把请求置为 `running / claimed` 后委托给 WSL。收到取消时状态变成 `cancel_requested`；历史 WSL `assigned` 只返回 `running / claimed`，但新发布互斥门禁仍把 `cancel_requested` 当作活动任务。因此旧任务不能收口，新任务报 `DEPLOYMENT_ALREADY_ACTIVE`。

修复只在以下所有条件满足时，由认证 WSL 执行器调用 `POST /internal/deployments/{deployment_id}/cancel-ack`：

1. 身份、Repository、Environment、Scope、Commit、claim generation（存在时）、log revision 均一致；状态仍是 `cancel_requested / claimed`，唯一日志为委托记录，未收到任何执行进度。
2. 执行器能够验证其固定 Checkout 根目录存在、部署 ID 合法，且该 Deployment 对应的工作目录**不存在**。源检出总是在首个成功进度回执之后发生且不会被执行器删除；工作目录存在或挂载不可验证时拒绝收口。
3. 控制面要求取消宽限期已到、已有 claim lease（如存在）确已过期，并独立检查原 Release 的 Manifest、Checksum、health、services_healthy 和 SHA。已切换或不可验证时拒绝。
4. 控制面在 SQLite 写事务内以状态、log revision、Commit CAS 更新为 `cancelled`，记录 finished_at、reconciled_at、原因及审计日志；重复回执幂等，不得当作部署 PASS。
5. 已开始执行的任务即使收到取消，进度回执也不能重置为普通 `running`；必须保留 `cancel_requested` 并根据真实执行终态与验证证据收口。首个进度回执如因取消被拒，执行器不得开始检出或执行脚本。
6. 轮询控制器短时超时不会导致 WSL 执行器异常重启。取消回执失败应记录安全摘要并保持阻塞，不尝试越过数据库互斥。

无权威状态证据时保持阻塞；**禁止**手工改部署 SQLite、删除互斥锁、伪造健康证明、关闭预检、绕过受控 CI 或自动回滚。

## 验证与后续发布

在 `services/github-action-service` 内执行相关回归测试与全量私有 CI。随后必须取得**基础设施 PR 合并及控制面发布的分别明确授权**；仅把代码写入 Draft PR 不会更新生产 Controller/WSL 运行版本。

生效后先只读回读旧任务、当前 verified Release 和受控执行器，再执行 `plan_test_deployment` → `start_test_deployment`，使用原 Release CAS。最终必须由 Migration、services_healthy、current_git_sha 及真实接口共同证明已发布。

历史 `PREVIOUS_RELEASE_ROLLBACK_UNSAFE: missing frontend/html/index.html` 属于独立的前端来源完整性问题：必须核对旧后端与旧前端的实际挂载及校验和，不能创建空前端文件、禁用校验或删除旧 Release 来消除错误。


## 2026-10-26 successor release：取消任务后另一独立部署成功的收口规则

故障：同一 main SHA 的旧任务仍为 cancel_requested/claimed、只有一次 WSL
委托日志、固定 checkout 目录从未创建。但另一条独立任务已经成功发布该 SHA，
可信 current release 与旧任务创建时的 current_release_before 不同，旧规则
因 DEPLOYMENT_RELEASE_DRIFT 阻断取消，进一步导致 DEPLOYMENT_ALREADY_ACTIVE。

保持原有安全条件不变：执行器必须直接检查 checkout 根目录存在、旧任务的
固定 checkout 不存在；controller 继续检查 repository、environment、fullstack、
commit、claim generation、log revision=1、委托记录、租约过期、取消宽限期和
SQLite 事务 CAS。不得让已开始执行任务经这条路径取消。

新增且仅新增受限的独立后继发布证明：
- 当前 Release 必须为可信 passed、manifest/checksum/health/services_healthy
  全部真实通过，repository/environment/commit SHA 与旧任务一致。
- current release ID 不得是旧任务的 current_release_before 或 target_release；
  release 目录名必须与 release ID 严格匹配。
- 控制面自己查询到独立 deployment_id、同一 repository/environment/fullstack、
  commit SHA、target_release、current_release_path 完全一致的 passed 任务。
- 该独立任务必须存在真实 finished_at，且完成时间不早于旧任务的
  cancel_requested_at。不存在证据、任一条件不匹配时继续拒绝
  DEPLOYMENT_RELEASE_DRIFT。
- 原 SQL 写事务与 log_revision/commit/status CAS 不变；成功仅把旧任务状态
  收口为 cancelled 并记录独立审计标记
  wslexecutor_unstarted_cancel_ack_after_successor，原已成功的后继发布不得改变。
- 重复回执必须幂等，无 Migration、无任意主机/命令/Secret 传入。

回归测试涵盖已发布独立后继成功、无后继、不同 SHA、错误 release、
不同 repository/environment/scope、未完成、完成时间早于取消、健康失败、
release 路径不符等 fail-closed 条件。
