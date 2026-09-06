# MyGithut12 Web-safe Private CI 开发设计

> 对应需求：`docs/MYGITHUB12_WEB_CI需求变更与验收标准.md`  
> 变更代号：`WEB-CI-01`  
> 本文描述目标架构，不表示这些能力已经实现。开发者必须以当前 main 源码、Schema、DB Migration 和运行时 capability 为准。

## 1. 设计总则

目标架构只有一句话：

> **CI 在服务器端持久运行；ChatGPT Web 只做短事务、短查询和可恢复续接。**

同时坚持：

> **收紧控制面，增强诊断面。**

不能通过删除日志、减少错误证据或跳过测试来解决 Web 超时。

## 2. 当前需要替换的同步模式

当前 12.9.5 仍存在以下同步等待契约：

- `wait_private_ci_job(timeout_seconds=55)`；
- `validate_development_task(wait_seconds=55)`；
- `converge_development_task(index_wait_seconds=55, wait_seconds=55)`；
- converge 可以先等待 Index，再等待 CI。

这些接口即使每次 MCP HTTP 请求本身经常因为事件提前返回，也会诱导 AI 在一个回答中持续“wait -> wait -> wait”直到 CI terminal。设计上必须移除这种 Web 默认行为。

## 3. 目标总体架构

```text
ChatGPT Web
   │
   │ short MCP call
   ▼
MyGithut12 Controller
   │
   ├─ durable CI Request / Convergence Run store
   │       │
   │       ├─ preflight / exact identity
   │       ├─ index / impact / contract / affected tests
   │       ├─ queue private CI
   │       └─ build attestation or failure pack
   │
   └──────────────► Private CI Worker(s)
                    long-running execution

ChatGPT Web later
   │
   ├─ get status
   ├─ resume converge
   └─ read failure pack / step logs
```

Web 连接是否存在，不参与 CI 生命周期正确性。

## 4. Durable CI Request 状态机

DEV-002 将 Request 生命周期与 Worker execution 生命周期明确拆开。Request 的 canonical phase/status 组合为：

```text
phase=accepted   status=accepted
  -> phase=preparing status=preparing
       -> phase=terminal status=preflight_failed
       -> phase=queued status=queued
            -> phase=running status=running
                 -> phase=terminal status=passed
                 -> phase=terminal status=failed
                 -> phase=terminal status=timed_out
                 -> phase=terminal status=cancelled
                 -> phase=terminal status=superseded
                 -> phase=terminal status=worker_lost
                 -> phase=terminal status=internal_error
```

`phase` 表示控制面生命周期大阶段；`status` 在非终态与 phase 同名，在 `phase=terminal` 时表示具体终态原因。`preflight_failed` 是 Request 终态，不是 Worker CI 的 `failed`。

DEV-002 只提供持久模型和 transition primitive，**没有**把当前 `start_private_ci_job` 接到 `accepted/preparing`。未来 DEV-003 才把 start 改成短的 durable accept/create-or-get transaction。昂贵的 GitHub compare、workspace discovery、dependency preparation 等如果无法稳定保持短时，应在 DEV-003 之后进入 Request `preparing` 阶段。

安全要求：异步化只改变“在哪里等待”，不改变“执行前必须验证什么”。在进入 Request `queued/running` 前仍必须完成 repository policy、exact commit/tree、profile applicability、workspace/config 等现有门禁。

### 4.1 Transition 与 CAS

DEV-002 的 transition primitive 使用数据库作为 truth source：

- 新 Request 初始 `revision=0`，并写 revision 0 的 durable event；
- 每次成功 transition 必须把 `revision` 精确增加 1；
- transaction 使用 SQLite `BEGIN IMMEDIATE`；
- 最终更新使用 `WHERE request_id=? AND revision=? AND phase=? AND status=?` 的数据库 CAS；
- CAS 更新 `rowcount != 1` 时明确返回 stale revision conflict；
- illegal transition、identity mismatch、非法 phase/status 组合均 fail-stop；
- event 与 Request CAS 更新位于同一 transaction，CAS 失败会整体 rollback；
- terminal Request 没有重新进入 queued/running 的合法边。

因此禁止的实现模式 `SELECT -> Python 判断 -> unconditional UPDATE` 未被采用。Controller 的进程内 lock 只用于本进程写入协调，不是正确性的唯一来源；持久 revision + SQL CAS + transaction 才是并发真相。

## 5. DEV-002 数据模型决策

### 5.1 最终选择：新增 durable `ci_requests`

DEV-002 选择 **方案 B：新增 durable CI Request 表，并通过 `worker_job_id` 与现有 `ci_jobs` 关联**，不扩展 `ci_jobs` 去承载 Request 的 `accepted/preparing`。

选择理由：

1. 当前 `ci_jobs` 是 Worker execution 表；现有 `create_or_get_job` 创建成功后立即进入 `queued`，没有 Worker 排队前的 stable durable request identity。
2. 当前 `ci_jobs.status=preparing` 是 Worker 已 lease/download 后的 execution substate；Request 的 `preparing` 是 Worker 排队前的控制面 preflight。把二者塞进同一 status 会造成语义冲突。
3. Request 生命周期必须能在 Worker Job 尚不存在时持久化 `accepted/preparing/preflight_failed`，因此 Request identity 必须早于 execution job identity。
4. Request restart/reopen 应从 `ci_requests + ci_request_events` 恢复；Worker execution 的 lease、attempt、runtime substate 继续由 `ci_jobs` 恢复。两者分别是各自生命周期的 truth source。
5. idempotency key + normalized request hash 绑定 Request，而不是 execution job，因为重复调用需要在 preflight/queue 之前就稳定解析到同一 durable identity。
6. 新表是 additive Migration，不需要改写现有 `ci_jobs` status/reader/worker transition，因此 migration risk 和 compatibility risk 都低于方案 A。

放弃方案 A 的原因：扩展 `ci_jobs` 会让 `accepted/preparing` 同时表示 Request preflight 和 Worker execution substate，并要求修改当前 Job 创建、读取、Worker claim/recovery 或 legacy status 语义；这既提高旧数据库兼容风险，也容易越界进入 DEV-003/005。

### 5.2 Schema / relationship

`ci_requests` 持久字段：

- `request_id`：stable Request identity；
- `repository`、`branch`、`commit_sha`、`tree_sha`；
- `profile`、`effective_config_digest`；
- `phase`、`status`、`revision`；
- `idempotency_key`、`normalized_request_hash`；
- `worker_job_id`：nullable/unique FK 到 `ci_jobs(job_id)`；
- `preflight_error_id`、`preflight_error_code`；
- `terminal_reason`；
- `failure_pack_id`、`attestation_id`；
- `created_at`、`updated_at`；
- `last_event_id`。

`ci_request_events` 持久每个 revision 的 event identity、from/to phase/status、event data 与时间；`UNIQUE(request_id, revision)` 防止同一 revision 重复事件。

索引/约束：

- `idempotency_key` 非 NULL 时唯一；
- `worker_job_id` 唯一；
- repository/commit/profile、phase/status、worker job 读取索引；
- DB CHECK 直接限制合法 phase/status 组合；
- `revision >= 0`。

已有 `ci_jobs` 的 repository/branch/commit/profile/status/worker lease/attempt/执行时间/日志字段继续复用为 execution evidence，不在 Request 表机械复制 Worker-only 字段。

### 5.3 Legacy Migration

Migration 通过当前 `ci_database.init_db()` 的 additive 初始化路径创建新表/索引，并对已有 `ci_jobs` 做幂等 projection；**不会 UPDATE、DELETE、requeue 或重新执行 legacy `ci_jobs`**。

映射：

- `queued -> request queued/queued`；
- `leased/downloading/preparing -> request queued/queued`，因为这些已是 Worker execution substate；
- `running/cancel_requested -> request running/running`；
- `passed/failed/timed_out/cancelled/superseded/worker_lost/internal_error -> request terminal/<same status>`。

Legacy projection 使用原 `job_id` 作为 `request_id` 且 `worker_job_id=job_id`，从而不改变历史 Job identity。旧 schema 无法证明的 `tree_sha`、effective config digest、Request idempotency key、normalized request hash 保持 `NULL`，禁止猜造。重复 `init_db()` 通过唯一约束与 `INSERT OR IGNORE` 保持幂等；terminal Job 不会被 reopen。

### 5.4 DEV-003 接入点

DEV-003 应在 canonical start 中先构造 normalized request hash，并在一个短 transaction 内调用 durable create-or-get：

- same key + same hash：返回同一 `request_id`；
- same key + different hash：明确 `IDEMPOTENCY_CONFLICT`；
- 新请求：持久化 `accepted/revision=0` 后立即返回/继续短阶段；
- preflight 后创建/复用 Worker Job，验证 repository/branch/commit/profile identity，再以 CAS 把 Request 从 `preparing -> queued` 并持久化 `worker_job_id`。

DEV-002 **尚未**修改 canonical `start_private_ci_job` 参数、Schema 或外部行为，也没有修改 `get_private_ci_job`、`wait_private_ci_job`、validate/converge wait 路径。

任何 continuation 必须只依赖数据库持久状态，不能只依赖 Controller 进程内 `Condition`。

## 6. Canonical Web 工具设计

### 6.1 `plan_private_ci_job`

保持 read-only。用于用户/AI希望在真正启动前查看 applicability/profile/workspace 计划的场景。不得创建 Job。

### 6.2 `start_private_ci_job`

改为短调用语义：

- 参数语法、allowlist、基础 policy 做同步校验；
- durable create-or-get；
- 返回 `accepted/preparing/queued/...`；
- 不等待 running/terminal；
- 增加一等 `idempotency_key`；
- 返回稳定 `job_id/request_id + revision + continuation_required`。

如果某个 fresh GitHub 检查不可在短时间可靠完成，把它放入后台 preflight，而不是阻塞 Web；preflight 失败要以结构化 terminal error 返回。

### 6.3 `get_private_ci_job`

默认：

- 立即 snapshot；
- `detail_level=summary`；
- 不 wait；
- 不自动加载日志；
- 返回 current step、queue/worker、terminal、revision、failure/attestation availability。

`detail_level=full` 仍可保留，超预算必须 Resource fallback。

### 6.4 `wait_private_ci_job`

不再作为 canonical Web 推荐入口。推荐方案：

- canonical production schema 隐藏；
- compatibility registration 保留；
- 文档标记 deprecated for Web orchestration；
- 如果保留 canonical，必须显式 opt-in、默认不等待，并把 hard bound 降到项目选择的短边界；任何情况下不得再要求 AI 循环 wait 到 terminal。

### 6.5 Failure Pack

新增或正式暴露稳定诊断入口，例如：

`get_private_ci_failure_pack(job_id)`

建议返回：

```json
{
  "job_id": "...",
  "failure_pack_id": "...",
  "status": "failed",
  "failed_step": "node:...:test:run",
  "exit_code": 1,
  "failure_category": "test",
  "error_fingerprint": "...",
  "primary_errors": [
    {"message":"...","path":"...","line":128,"column":7}
  ],
  "failed_tests": [
    {"name":"...","path":"...","line":128}
  ],
  "command": "redacted command",
  "log_excerpt": "...",
  "step_log_available": true,
  "full_log_available": true,
  "resource_uri": "...",
  "redaction": {"applied": true}
}
```

`failure_pack_id` 应稳定；临时 Resource URI 可以变化。Resource 过期后可由 job/failure_pack identity 重新生成。

### 6.6 Step logs

优先增强现有日志 API，避免无必要增加工具数。方案二选一：

1. `get_private_ci_logs` 增加 `step_id/cursor/level`；或
2. 新增 `get_private_ci_step_logs(job_id, step_id, cursor, limit)`。

无论选哪种，都必须支持“只看 failed step”，同时保留完整 Job log 获取路径。

### 6.7 Cancel

`cancel_private_ci_job` 保留为显式 consequential 工具。canonical description 要说明 queued/running/terminal 三种行为，不得与普通 status 查询混淆。

## 7. Development Convergence 设计

建议新增 durable `convergence_id`；如果复用 Development Session，则也必须有独立 phase/revision 字段，避免把 Session revision 当成所有异步阶段的唯一游标。

建议状态：

```text
accepted
 -> index_requested
 -> analysis_pending
 -> ci_requested
 -> ci_running
 -> post_ci_finalize
 -> passed | failed | blocked
```

实现可以并行两条轨道：

- Analysis track：Index -> Change Context -> Impact -> Contract -> Affected Tests；
- Validation track：full/fast CI request -> queue -> running -> terminal。

若 CI 启动不依赖 Index 结果，两条轨道可以并发；最终结果只有在所有要求证据齐全后才能 terminal success。

`converge_development_task` 每次调用只做以下之一：

1. 创建/复用 convergence；
2. 推进一个安全短阶段；
3. 读取当前 durable snapshot；
4. terminal 时组装最终结果。

禁止内部执行 `wait index <=55s -> wait CI <=55s` 的串行等待。

返回建议字段：

- `convergence_id`；
- `development_session_id`；
- exact HEAD/Tree；
- phase/status/revision/terminal；
- index_job_id/index_status；
- ci_job_id/ci_status；
- analysis readiness；
- attestation_id / failure_pack_id；
- `continuation_required`；
- `next_actions[]`。

## 8. Continuation 与跨窗口恢复

恢复时禁止依赖模型记住一堆 offset。最小恢复输入应是：

- repository + branch/PR；或
- development_session_id；或
- convergence_id；或
- ci job_id。

`resume_development_task` 应读取 live durable state，把同一 convergence/job 重新暴露出来；不能因为新窗口到来就创建新的 CI。

所有 response 都应明确区分：

- live facts；
- historical evidence；
- pending work；
- candidate next actions。

## 9. Failure Pack 构建策略

Failure Pack 不应只在某一次 `validate/converge` response 内临时生成。

建议在 CI 进入非 passed terminal 后：

1. 记录 terminal event；
2. 生成或登记稳定 `failure_pack_id`；
3. 解析 step/exit code；
4. 对常见测试框架解析 test/file/line；
5. 抽取 bounded error context；
6. 脱敏；
7. 保存结构化 metadata；
8. 大 payload 用 Resource；
9. 后续按 job_id 可重新 materialize Resource。

如果解析失败，仍必须返回原始 failed step、exit code、log resource，并标明 `parser_status=unavailable/partial`，不能伪造精准行号。

## 10. 日志与证据分层

建议四层：

- L0：Job summary，几十到几百字节；
- L1：Failure Pack，机器可读核心错误；
- L2：单 step 日志；
- L3：完整 Job 日志 / artifacts。

这样 Web 默认不被 warning 噪声淹没，但证据链完整。

Vue warn 等高频非 fatal 输出可以作为独立性能/噪声优化项；在没有 profiling 证据前，不得宣称它是测试运行慢的根因。

## 11. Idempotency / supersede / cancel

### 11.1 Idempotency

服务端应保存 normalized request hash。相同 key + 相同 hash 返回同一 identity；相同 key + 不同 hash 返回 `IDEMPOTENCY_CONFLICT`。

### 11.2 Supersede

新 HEAD supersede 旧 Job 时必须写持久事件，并保证旧 Job 不能再生成当前 HEAD attestation。

### 11.3 Cancel

cancel 必须针对准确 job_id；不能用“最新 Job”隐式取消。Worker 最终 release 状态必须可验证。

## 12. ToolAnnotations 与 Schema

实现时建立一张自动化表，逐个核验实际副作用：

| 类别 | readOnly | destructive/consequential | idempotent | 说明 |
|---|---|---|---|---|
| list/get/plan/log/failure pack | true | false | true | 纯读取 |
| start CI | false | false | true（有服务端保证） | 创建/复用 durable CI request |
| validate/converge | false | false | true（有 key/revision） | 写控制面并可能启动 CI |
| cancel/supersede | false | true | 应安全重试 | 改变执行中的 Job |

`openWorldHint` 等字段必须依据当前 MCP/OpenAI 定义和工具真实外部交互重新评审，不允许机械复制。

## 13. 性能与可观测性

新增指标至少包括：

- MCP tool duration histogram；
- `web_wait_seconds` / `wait_path_used`；
- CI request phase duration；
- convergence phase duration；
- queue/execution duration；
- idempotent reuse/conflict；
- failure pack build duration/bytes；
- inline/resource fallback count；
- controller restart recovery count。

不得把 job_id、branch、commit 等高基数字段直接做 Prometheus label；需要时写结构化 audit/event。

## 14. 兼容与迁移

推荐分两层发布：

1. **Compatibility layer**：旧 wait 参数/工具继续工作，保持旧客户端可用；
2. **Canonical Web layer**：新 schema 默认短调用，描述明确禁止等待 CI terminal。

迁移期 capability 应显式暴露类似：

- `supports_web_safe_private_ci=true`；
- `private_ci_web_wait_default_seconds=0`；
- `supports_ci_failure_pack=true`；
- `supports_durable_convergence=true`；
- `legacy_private_ci_long_poll_exposed=false/true`。

具体字段名开发时可调整，但必须有机器可读能力声明和 Manifest 对应测试。

## 15. 测试设计

必须至少覆盖：

- unit：state transition、idempotency、annotations、failure parser、redaction；
- integration：10+ 分钟 fake Job，start/status/converge 不等待；
- restart：Controller 重启后恢复同一 request/convergence；
- concurrency：并发重复 start 只生成一个 canonical Job；
- failure：JS/Vitest、Go test、setup/network、timeout、worker lost 等失败包；
- resource：大日志、Resource 分页、Resource 过期后重建；
- compatibility：legacy wait；
- security：Secret fixture 脱敏；
- gate：fast 不 merge eligible、full exact-head 才可 attestation；
- real E2E：ChatGPT Web + current production-like MCP + 长 Private CI。

## 16. 不在本变更中顺手完成的事项

以下可以单独优化，但不能混入 WEB-CI-01 的完成条件：

- 把所有仓库测试本身都优化到几十秒；
- 无证据地删除 Vue warnings；
- 提高 Worker 并发到任意值；
- 修改仓库测试覆盖率策略；
- 自动 merge/deploy；
- 引入任意 shell/host 参数。

WEB-CI-01 应先让调用架构对“CI 很慢”天然安全，再独立优化 CI 性能。
