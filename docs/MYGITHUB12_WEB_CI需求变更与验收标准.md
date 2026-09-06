# MyGithut12 Web-safe Private CI 需求变更与验收标准

> 变更代号：`WEB-CI-01`  
> 状态：`APPROVED_FOR_DEVELOPMENT`  
> 创建日期：2026-09-06  
> 基线：MyGithut12 `12.9.5`，`main=0c13fcfae20534ca0be2c227c34c3f858f9077fa`。该 SHA 仅记录本需求建立时的事实，后续开发必须 fresh-read 当前 `main`，不得把本文基线 SHA 当作长期真相。

## 1. 变更目的

当前 Private CI 的真实执行时间可能达到数分钟，部分仓库完整 `repo-auto-check` 甚至更长。现有 Web/MCP 编排仍存在 `wait_private_ci_job(timeout_seconds=55)`、`validate_development_task(wait_seconds=55)`、`converge_development_task(index_wait_seconds=55, wait_seconds=55)` 等同步等待语义；其中 converge 还可能先等待 Index、再等待 CI。

本变更的目标不是通过隐藏日志来“缩短输出”，而是把 **CI 生命周期与 ChatGPT Web 单次 MCP 调用解耦**：服务器端持久运行 CI，Web 端只进行短调用、状态读取和可恢复续接。CI 即使运行 5、10、30 分钟，也不应要求一个 ChatGPT Web 回答持续阻塞到终态。

同时必须保证：**收紧 CI 控制面，不收紧 CI 诊断面**。失败后用于定位代码、测试、依赖、Runner、网络、权限和基础设施问题的证据必须保留并增强。

## 2. 外部约束与工程结论

1. OpenAI 公开文档支持 MCP/自定义工具、结构化工具结果、长流程状态管理和异步工具工作流，并强调工具名称/描述、输入输出、状态与副作用应明确。
2. 本项目不得依赖一个未被 OpenAI 明确承诺的“ChatGPT Web 自定义 MCP 单调用安全阻塞秒数”。当前公开文档没有提供可作为本项目长期兼容性合同的固定 Web MCP 阻塞 SLA。
3. 因此，现有“55 秒 MCP 安全窗口”只能视为历史工程假设，不再作为 Web-facing canonical contract 的设计依据。
4. OpenAI 参考资料：
   - `https://developers.openai.com/`（MCP、Tools、Async tool calling、Responses state）
   - `https://developers.openai.com/api/docs/guides/latest-model`（tool-heavy / long-running workflow guidance）

## 3. 不变的安全底线

本变更不得削弱现有质量和安全门禁：

- CI 必须绑定准确 `repository + branch + commit_sha + tree_sha`；
- 新 Commit 后旧 CI 不得继续证明新 HEAD；
- fast CI 只用于开发反馈，不得变成 merge-eligible 证据；
- 最终候选 HEAD 仍必须运行仓库定义的最终合并级 full CI；
- merge、deploy、rollback、branch delete 的授权边界不因本变更扩大；
- Workspace / Development Session / revision CAS / drift / lease / overlap 门禁不得绕过；
- Secret、凭据、DSN、Token 等仍必须脱敏，Failure Pack 和日志读取不得泄漏敏感信息；
- 不允许因为 Web 超时问题而跳过测试、缩短测试范围或把失败当成功。

## 4. 术语

- **Web-facing canonical tool**：正常暴露给 ChatGPT Web/AI 使用的正式工具。
- **Compatibility tool**：为历史客户端保留，但不作为 Web AI 推荐路径的兼容入口。
- **CI Request**：一次持久化的 CI 请求身份。请求可以处于 preflight/queued/running/terminal，而调用方不需要保持在线。
- **Continuation**：后续窗口或后续调用恢复同一 Development Session / CI Request / Convergence Run 所需的稳定身份与 revision。
- **Failure Pack**：对失败 CI 的结构化诊断包，不等于删减后的日志；它是定位入口，完整证据仍可进一步读取。

## 5. 功能需求

### WEB-CI-REQ-01：Web-facing 调用不得等待 CI 生命周期

Canonical Web 工具不得为了“等 CI 跑完”而在同一次工具调用中持续 long-poll。启动、验证、converge 等入口必须在完成必要的短事务后返回持久身份和当前状态。

禁止把 `CI terminal` 作为 `start/validate/converge` 单次调用返回的前置条件。

### WEB-CI-REQ-02：持久异步 CI Request

Private CI 请求必须服务端持久化，并至少支持：

`preparing -> queued -> running -> terminal`

终态至少覆盖：`passed / failed / timed_out / cancelled / superseded / worker_lost / internal_error / preflight_failed`。

调用方断开、ChatGPT Web 回答结束、Controller 进程重启或后续窗口接手，都不得改变已接受 CI Request 的唯一身份。

### WEB-CI-REQ-03：启动与重试必须幂等

相同的 `repository + exact commit/tree + profile + effective config + idempotency_key` 重试不得重复创建不可解释的多个 Job。

启动返回至少包含：

- `job_id` 或稳定 `ci_request_id`；
- repository / branch / commit_sha / tree_sha（tree 尚在异步 preflight 时必须明确 `pending`，不能伪造）；
- profile；
- phase/status；
- `terminal`；
- `revision`；
- `continuation_required`；
- 可安全执行的 next actions。

### WEB-CI-REQ-04：状态读取必须短、紧凑、可恢复

`get_private_ci_job` 默认只返回 gate-safe summary，不等待状态变化，不读取大日志。

Canonical Web 路径不应要求模型维护 log offset、Condition revision 或 55 秒 wait loop 才能继续。若保留 wait 能力，必须是显式 opt-in、短边界、非默认路径；历史 55 秒 long-poll 应迁移为 compatibility-only 或非推荐入口。

### WEB-CI-REQ-05：Development Converge 必须改为持久可续接状态机

`validate_development_task` 与 `converge_development_task` 不得在一次调用中顺序阻塞等待 Index 和 CI。

Converge 必须有稳定 `convergence_id`（或等价持久身份）与 revision，能够保存：

- exact Session/Workspace/HEAD/Tree；
- Index Job；
- Change Context / Impact / Contract / Affected Tests 阶段；
- CI Job；
- attestation / failure evidence；
- 当前 phase、terminal、next actions。

后续调用只推进或读取同一个 convergence，不得因重试创建重复 CI。

### WEB-CI-REQ-06：允许安全并行，不允许门禁降级

当业务依赖允许时，Index/分析和 full CI 可以并行启动以降低总墙钟时间；但所有依赖 exact Index 才能得出的分析结果必须在 Index ready 后生成或重新验证。

并行化不能把“尚未验证”解释成“通过”。

### WEB-CI-REQ-07：控制面收紧，诊断面增强

Canonical Web 工具集合应优先围绕用户目标，而不是暴露内部 polling 细节。推荐正常路径：

`plan -> start -> get/status -> failure diagnostics (only when needed) -> cancel (explicit)`

低层 wait/revision/log-offset 机制可以保留为 compatibility/debug 能力，但不应要求 Web AI 正常使用。

### WEB-CI-REQ-08：Failure Pack 必须成为一等诊断能力

失败 CI 必须可获得结构化 Failure Pack，至少包含：

- job/request identity 与 exact commit/tree/profile；
- terminal status、failed step、exit code；
- failure category（代码/测试/依赖/Runner/网络/权限/基础设施/取消/超时/未知）；
- error fingerprint；
- primary errors；
- failing test 名称；
- 可解析时的 file/path/line/column；
- 失败 step 的实际 command（脱敏后）；
- 关键日志上下文和 log cursor/resource；
- changed files / affected-test evidence（可用时）；
- artifact/report/resource 引用（可用时）；
- redaction metadata。

Failure Pack 不能成为唯一证据。完整 step 日志和完整 Job 日志仍必须可按需读取。

### WEB-CI-REQ-09：诊断必须支持由浅入深

推荐诊断顺序：

1. `get_private_ci_job(summary)`；
2. Failure Pack；
3. 指定 failed step 的日志；
4. 必要时完整日志 / artifact / report。

默认响应必须避免把几千行正常 build 输出和 warning 全部塞给模型，但任何关键错误不得因 compact summary 被静默丢弃。

### WEB-CI-REQ-10：稳定失败证据，不依赖一次性 Web 会话

Failure Pack 应有稳定 `failure_pack_id` 或可重建身份。即使短期 Resource URI 过期，也必须能够依据 Job ID 重新生成/重新打开同一失败证据，而不是要求用户重新跑 CI 才能诊断。

### WEB-CI-REQ-11：MCP ToolAnnotations 与 schema 必须准确

所有 CI / converge 工具必须显式声明与真实行为一致的 annotations/metadata。至少验证：

- 纯状态/日志/Failure Pack 查询：read-only、可安全重试；
- start/validate/converge：会写 CI/控制面状态，不能标成 read-only；
- cancel/supersede：必须显式体现 consequential/destructive 风险；
- 幂等工具必须真的由服务端 idempotency 保证，不能只写 annotation。

### WEB-CI-REQ-12：响应大小与日志传输继续使用 Resource fallback

summary 必须保持紧凑；大 Failure Pack、大日志、大 artifact 必须走分页或 MCP Resource，不允许为了“诊断完整”突破 inline transport budget。

返回 Resource 时必须包含 size/hash/has_more/cursor 等可验证元数据。

### WEB-CI-REQ-13：兼容层必须有明确迁移策略

现有 55 秒 long-poll、旧参数默认值和历史客户端如果继续保留，必须：

- 与 canonical Web schema 分离；
- 文档标记 compatibility/deprecated；
- 不影响已有 running Job；
- 不改变 Job identity；
- 有自动化兼容测试。

不得直接删除导致旧客户端无可诊断的破坏性行为。

### WEB-CI-REQ-14：可观测性必须覆盖“Web 安全”指标

至少记录：

- tool name；
- request duration；
- 是否发生 wait；
- durable request/convergence phase；
- CI queue wait 与 execution duration；
- idempotent reuse count；
- Failure Pack build duration/size；
- response inline/resource mode；
- timeout/error classification。

不得记录 Secret 或完整敏感日志。

## 6. 验收标准

### AC-WEB-CI-01：长 CI 不阻塞启动调用

构造一个真实或受控的 >=10 分钟 CI。调用 canonical `start_private_ci_job` 后，工具必须在 CI 仍未终态时返回稳定 Job/Request identity；调用结束不得依赖 CI 完成。

### AC-WEB-CI-02：状态查询不 long-poll

对 running Job 连续调用 canonical status 工具，必须立即返回当前 snapshot；服务端测试证明该路径没有进入 `wait_for_job_change` 或等价 Condition wait。

### AC-WEB-CI-03：converge 可跨调用/跨窗口恢复

启动 full converge，在 Index 或 CI 仍 running 时结束当前调用；后续使用同一 Session/convergence identity 恢复，必须继续原 Job，不重复创建 CI，不丢失阶段结果。

### AC-WEB-CI-04：Controller 重启后可恢复

CI/convergence running 时重启 Controller（在安全测试环境执行）。重启后通过 durable store 能读取同一 identity、phase、revision、job_id，并继续收敛。

### AC-WEB-CI-05：幂等重试不重复排队

对同一 idempotency key 进行并发和串行重试；最终只允许一个 canonical Request/Job 生效。若参数不同，必须返回明确 idempotency conflict，而不是静默复用错误 Job。

### AC-WEB-CI-06：失败摘要可直接指出第一问题点

注入一个已知测试失败，summary/Failure Pack 至少能够指出 failed step、exit code、failing test，以及可解析时的文件和行号。不能只返回 `status=failed`。

### AC-WEB-CI-07：Failure Pack 与原始日志一致

Failure Pack 中 primary error、failed step、exit code、测试名必须能在原始 Job/step 日志中交叉验证；不得生成日志中不存在的错误结论。

### AC-WEB-CI-08：完整诊断没有被隐藏

对失败 Job，必须能从 Failure Pack 继续读取指定 step 日志，并能继续读取完整日志/Resource。compact summary 不得导致原始证据不可访问。

### AC-WEB-CI-09：大日志不淹没 Web 上下文

使用包含 >=5000 行日志的 Job 验证：默认 status/Failure Pack inline 响应保持在项目 inline budget 内；完整日志通过分页/Resource 获取；关键错误仍出现在 Failure Pack。

### AC-WEB-CI-10：Resource 过期后仍可重新打开失败证据

让 Failure Pack 的临时 Resource 过期后，仅凭稳定 Job/failure_pack identity 重新请求，必须得到可验证的新 Resource 或等价重建结果，不要求重跑 CI。

### AC-WEB-CI-11：ToolAnnotations 自动化验真

自动测试逐个检查 canonical CI 工具 annotations，并用真实 handler side effect 对照：read-only 工具不得写状态，写工具不得伪装 read-only，cancel 必须有明确高风险语义。

### AC-WEB-CI-12：fast/full 门禁不退化

fast CI 通过后仍不得 merge-eligible；final full CI 必须绑定当前候选 HEAD/Tree。新增 Commit 后旧 full CI 必须失效。

### AC-WEB-CI-13：兼容入口可用但不再是 Web 默认

兼容测试证明 legacy wait 客户端仍可按迁移合同工作；canonical schema/描述明确引导 Web AI 使用短调用，不再建议“循环 wait 到 terminal”。

### AC-WEB-CI-14：真实 ChatGPT Web E2E

至少执行一次真实 ChatGPT Web + MyGithut12 + 长 Private CI E2E：

1. Web 调用启动 CI；
2. CI 仍运行时工具调用正常返回；
3. 当前回答可以结束，不要求在同一回答等终态；
4. 后续对话/后续调用能恢复同一 Job；
5. CI 失败时可取得 Failure Pack 和 step 日志；
6. 整个过程不需要用户重新提交相同 CI 才能恢复。

记录真实 tool duration 和 Job identity 作为验收证据。

### AC-WEB-CI-15：性能门槛

在本地/测试 Controller 的 deterministic integration test 中，所有 canonical status/continuation handler 在没有外部网络阻塞的情况下应在 2 秒内完成；不得存在主动 sleep/long-poll 等待 CI terminal 的路径。

真实 Web E2E 以“单次 MCP 调用不因 CI 生命周期持续阻塞”为硬门禁，不把某个固定秒数宣称为 OpenAI 官方 SLA。

### AC-WEB-CI-16：安全与脱敏

用包含 Token/DSN/password-like fixture 的失败日志验收 Failure Pack、log tail、step log、Resource：敏感值必须脱敏；错误定位所需的 file/test/step/exit code 仍然保留。

## 7. 完成定义（Definition of Done）

本变更只有同时满足以下条件才算开发完成：

- 本需求对应开发清单全部应做项已勾选并附真实证据；
- unit/integration/compatibility/security tests 全绿；
- exact candidate HEAD 的最终 `repo-auto-check` 通过；
- 真实 ChatGPT Web 长 CI E2E 通过；
- Manifest/capability/tool descriptions 与实际行为一致；
- Failure Pack + step/full logs 能真实定位至少一个受控失败；
- 未降低 merge/deploy/Workspace/Session/CI 安全门禁；
- 代码、文档、测试和运行时版本身份一致；
- 如果需要生产发布，必须另行取得明确发布授权并完成发布后验收。
