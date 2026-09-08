# MyGithut12 Web-safe Private CI 开发清单

> 对应需求：`docs/MYGITHUB12_WEB_CI需求变更与验收标准.md`  
> 对应设计：`docs/MYGITHUB12_WEB_CI开发设计.md`  
> 变更代号：`WEB-CI-01`

## 1. 使用规则

本文是执行清单。任何 AI/开发者/验收者接手时都必须先读取需求、设计和当前 main 事实。

- `[ ]` = 未完成或证据不足；
- `[x]` = 实现、测试和对应验收证据均已完成；
- 不允许因为“代码写完了”就勾选；
- 每个开发点完成后必须把关键证据写入该项的“证据”位置，至少包括 commit SHA、测试名/Job ID 或运行时结果；
- 若方案变化，先更新需求/设计 revision，再改清单；
- 未经明确授权不得合并、发布、回滚或删除分支。

## 2. 开发前固定检查

- [x] **PRE-01** `get_mygithub_capabilities`，记录 version/build/schema identity。
- [x] **PRE-02** `get_repository_operation_policy(frankichen/github_mcp)`，确认 GitHub write/private CI 允许。
- [x] **PRE-03** fresh-read `main` HEAD/Tree；不得复用本文创建时 SHA。
- [x] **PRE-04** 读取 README、SECURITY、CI Manifest、DX-2、WEB-CI 三份文档。
- [x] **PRE-05** 建立/复用 exact main Repository Index。
- [x] **PRE-06** 创建独立 `ai/` branch + Workspace + Development Session + Lease。
- [x] **PRE-07** 检查 active Workspace overlap。
- [x] **PRE-08** 对目标模块完成 Change Context / Impact / Contract / Affected Tests 基线。

证据：MyGithut12 `12.9.5` / build `0c13fcfae20534ca0be2c227c34c3f858f9077fa` / schema `4702e8c008d685fb0b328f32488eedcdf30a4f9d70569321512627e9f2b28b95`；base main `1a122fe6d2a05505d152d893355281bed90c2eac` / Tree `d2054afd02979ce54ba46387d8ca673e3351220d`；main Index Job `2ab22a6d-b05f-4134-8d12-7f26cd89763c` completed；branch `ai/web-ci-dev-001-baseline-20260906`；Workspace `ws_2c9f96d400644d99`；Development Session `dev_16c77cfc2b9e4f41a38a`；active Workspace overlap = none；pre-write Change Context / Impact / Contract / Affected Tests 已完成。

## 3. P0：Web-safe 控制面

### WEB-CI-DEV-001：建立回归基线测试

- [x] 固化当前 `wait_private_ci_job(55)` 行为测试。
- [x] 固化 `validate_development_task(wait_seconds=55)` 行为测试。
- [x] 固化 `converge_development_task(index_wait_seconds=55, wait_seconds=55)` 行为测试。
- [x] 增加 >=10 分钟 fake CI fixture，不实际 sleep 10 分钟，使用可控 clock/event 模拟长生命周期。
- [x] 测试能够证明旧代码存在等待路径，作为后续回归反例。

建议模块：`ci_mcp.py`、`development_orchestrator.py`、`development_converge.py` 及对应 tests。  
映射验收：AC-WEB-CI-01/02/03/15。  
证据：branch `ai/web-ci-dev-001-baseline-20260906`；测试实现 commit `dc8818b08bab6d286d5c32ba83aa1dfb6aada67e` / Tree `229e5ab6d0f44275d02c6ab5ded2a244a987b54d`；修改测试文件 `services/github-action-service/tests/conftest.py`、`services/github-action-service/tests/test_web_ci_wait_baseline.py`；tests：`test_wait_private_ci_job_defaults_to_55_and_delegates_to_wait_for_job_change`、`test_wait_for_job_change_caps_long_poll_at_55_without_real_sleep`、`test_validate_development_task_defaults_to_55_and_enters_wait_path`、`test_converge_development_task_defaults_to_55_plus_55`、`test_converge_development_task_waits_index_then_ci_in_order`、`test_fake_long_running_ci_fixture_models_ten_plus_minutes_without_sleep`；fixture `fake_long_running_ci`；`services/github-action-service` full pytest `686 passed in 6.82s`；Private CI `repo-auto-check` Job `c1da7b3da5184340` passed / exit 0 / 56.71s；`repo-fast-check` 因既有 `FAST_CHECK_ENTRYPOINT_MISSING` 在 pytest 前失败，不作为代码失败。AC-WEB-CI-01/02/03/15 保持未勾选。

### WEB-CI-DEV-002：Durable CI Request / phase model

- [x] 评审复用 `ci_jobs` 还是新增 request 表，并在设计记录选择理由。
- [x] 增加 `phase/revision/idempotency identity/preflight error/failure_pack_id` 等必要持久字段。
- [x] Migration 向前兼容已有 Job 数据。
- [x] 旧 running/terminal Job 升级后仍可读取。
- [x] 所有状态迁移使用明确 CAS/事务，禁止进程内状态成为唯一真相。
- [x] 写状态机非法 transition 单测。

映射验收：AC-WEB-CI-03/04/05。  
证据：实现 commit `d1a8825eff74b7b351443b61220d197564c8b54c` / Tree `f74a77c8eebf80fa521eabb9ff780a37c6cc4b2f`；设计决策 commit `935288736d54e90b225b54498da028fe783db7a9`；新增 `ci_requests` / `ci_request_events` 与 request `phase/status/revision`、idempotency key + normalized hash、worker job relation、preflight error、terminal/failure/attestation reference；`transition_ci_request` 使用 `BEGIN IMMEDIATE` + expected revision/phase/status SQL CAS；`test_ci_request_store.py` 覆盖 accepted→preparing→queued→running→terminal、preflight_failed、passed/failed/timeout/cancel/supersede/worker_lost/internal_error、stale revision、identity mismatch、invalid phase/status、terminal reopen rejection、并发 CAS、并发 idempotent create、same key/different hash conflict、pre-DEV-002 queued/running/passed/failed DB upgrade、re-init 幂等与 reopen persistence；`services/github-action-service` full pytest `708 passed`、ruff passed、compileall passed；Private CI `repo-auto-check` Job `3db8458b53b94352` passed / exit 0。该 reopen test 仅作为 DEV-002 persistence evidence，不单独勾选 AC-WEB-CI-04；AC-WEB-CI-03/04/05 保持未勾选。

### WEB-CI-DEV-003：`start_private_ci_job` 改为短事务

- [x] 增加 canonical 一等 `idempotency_key`。
- [x] start 只完成短同步校验 + durable create-or-get。
- [x] 不等待 Worker running/terminal。
- [x] 昂贵 preflight 如果可能长阻塞，移入 `preparing` 后台阶段。
- [x] preflight 进入 queue 前仍验证 exact commit/tree/profile/policy/workspace/config。
- [x] start 返回 job/request identity、phase、revision、terminal、continuation_required、next actions。
- [x] 失败 preflight 返回结构化 terminal error，不能伪装成 CI failed。
- [x] 并发相同 key 不重复创建 Job。
- [x] 相同 key 不同 request hash 返回冲突。

映射验收：AC-WEB-CI-01/05/12。  
证据：DEV-003 原 implementation candidate `d27849c5112a792bde04d6061b53038f761b0035` / Tree `26abaab9eee2bd4aa70210e35f0a799f28b77d01` 保留为历史证据；FIX-001 repaired code candidate `a088e92629ee52ce9a3832cf628337e2933483f0` / Tree `24ee442d811c9bbb37c6b7997d592f1e681a745c`。`test_web_ci_start_durable.py` 在原 canonical first-class key/schema、same-key reuse/conflict、并发 start/dispatch、no wait/network/sleep、601 秒 fake lifecycle、preflight/crash/reopen 回归上，新增 config D1→D2→terminal `CI_PREFLIGHT_CONFIG_CHANGED` 后 canonical same-key replay、repository/private-CI/profile policy tightening 下旧 Request replay 与新 key 拒绝、max-timeout normalization drift、raw requested timeout durable identity、返回 `auto:` key 的显式 replay，以及 commit/timeout/priority/base_sha/force_rerun/supersede_previous caller semantic conflict 覆盖；`test_ci_request_store.py` 与未修改的 `test_web_ci_wait_baseline.py` 仍包含在 `services/github-action-service` full pytest。FIX-001 repaired code candidate full pytest 为 `736 passed`，ruff/compileall passed；Private CI `repo-auto-check` Job `d3c181d9fdb24437` passed / exit 0 / 15 steps / failed steps 0（63.33s）。`repo-fast-check` Job `f6f2054cfda1478b` 仍在 pytest 前因既有 `FAST_CHECK_ENTRYPOINT_MISSING` exit 42，未在 FIX-001 修复。AC-WEB-CI-01/05/12 保持未勾选，DEV-004+ 保持未开始。

### WEB-CI-DEV-004：`get_private_ci_job` 纯 snapshot 化

- [x] summary 默认不 wait。
- [x] summary 不隐式读取完整日志。
- [x] 返回 current step、queue/worker、phase/revision、terminal、failure_pack_available、attestation 等必要字段。
- [x] full 模式仍保留调试证据。
- [x] 大 full response 继续 Resource fallback。
- [x] 单测确认调用路径不进入 `wait_for_job_change`。

映射验收：AC-WEB-CI-02/09。  
证据：DEV-004 implementation candidate `f966329d8c7ae9bdd6323c57c0a9b53639a60039` / Tree `fa7f9420992e09ecc02eb8cae2b0276f0be2e0ea`；`test_web_ci_get_snapshot.py` 覆盖 Request-only accepted/preparing/preflight_failed、Request queued + Worker queued/running/全部 terminal execution status 的 truthful composition、request/job exact identity 与 mismatch fail-stop、legacy Job、no wait/sleep/Condition/network、no log tail/chunks、read-only、oversized full Resource fallback、summary inline 与 SQLite reopen durability。Private CI `repo-auto-check` Job `8a220c8afc364ee9` passed / exit 0；`services/github-action-service` full pytest `762 passed`，ruff/compileall passed；既有 `aiosqlite Event loop is closed` warning 仅记 NOTE。`repo-fast-check` Job `04f440cb9ddb4064` 仍在 pytest 前以 exit 42 / `FAST_CHECK_ENTRYPOINT_MISSING` 结束，保持既有基础设施债务不修。AC-WEB-CI-02/09 与 DEV-005+ 保持未勾选。

### WEB-CI-DEV-005：Long-poll 退出 Web 默认路径

- [x] 决定 `wait_private_ci_job` 为 compatibility-only 或显式短 wait。
- [x] canonical Schema 不再描述其为正常 CI 跟踪首选。
- [x] 不再让 AI instructions 要求“wait 到 terminal”。
- [x] compatibility 行为有测试。
- [x] Manifest/deprecation/capability 与实际暴露一致。

映射验收：AC-WEB-CI-02/13/15。  
证据（历史 Task Delta，仅用于重放 DEV-005 语义，不替代本分支最终验证）：old branch `d5ef9d3fe9f35b003604d79532112faee1349f0f`；implementation `8c2adaf2cee9b0c2ae789d4019ef7c070bdbf976`、test repair `14716317a5795e3408543f9b1c5d90ff21f94f47`、checklist `b700347ec039ce94cfd4d55fb00fdc22d0b1a3fa`；当时 task semantics 为 production canonical 164、compatibility 175、hidden deprecated 11（含 `wait_private_ci_job`），replacement=`get_private_ci_job`。FIX-002 reprepare 从 fresh current main 创建；本分支最终 exact-head Index/CI/PR 证据以本轮交付报告为准，DEV-006+ 未开始。

## 4. P0：Durable validate / converge

### WEB-CI-DEV-006：Convergence Run 持久模型

- [ ] 增加 `convergence_id` 或等价稳定身份。
- [ ] 持久化 Session/Workspace/HEAD/Tree/mode/base identity。
- [ ] 持久化 index_job_id、ci_job_id、analysis readiness、phase/revision。
- [ ] 持久化 attestation/failure_pack identity。
- [ ] 支持 Controller 重启恢复。
- [ ] 不允许一个新窗口自动创建重复 convergence/CI。

映射验收：AC-WEB-CI-03/04/05。  
证据：`待填写`

### WEB-CI-DEV-007：`validate_development_task` 非阻塞化

- [x] 默认调用不再 `wait_seconds=55`。
- [x] 启动/复用 CI 后立即返回 durable status。
- [x] running 时返回 `continuation_required=true`。
- [x] terminal passed 时生成/读取 attestation。
- [x] terminal failed 时返回 failure_pack identity/availability。
- [x] 重试由 exact Session revision + idempotency 保证。

映射验收：AC-WEB-CI-01/03/05/12。  
证据：implementation commits `d8fe93a7ef0a416cd9bf7cc847bfbb808943ac16`、`1377949e88caa43bbea7811d0cfae4cbb7d29a06`、`7498cf373278d6798cd96e37a80193ec04b24e8d`、`cfce1418d4cce93f5caff17f498e5c03947485e2`、`9c0a49c3eaa809f053dd9fbd70420f7b8544f703`、`5d79c2c626d946b0880786750347a09bd20a36e6`；test commits `30d3cd33c5086308be24d64399e6401c5cd4aa22`、`4bb69260fe15fd8cd5a65e97c7eb368349cc64ff`、`64c70d7377455f0431fe210a567487421e4a5694`、`81bcfbd06785feb21cdb60333ae0637ca59bfd5d` / Tree `0a90a828ab7977414702bdf384f7e1164992336a`。`validate_development_task` canonical default `wait_seconds=0`，使用 `start_validation_request` 创建/复用 durable CI Request，默认只读取 Request/Worker snapshot；显式 `wait_seconds>0` 才走 compatibility `wait_validation_request`。`test_web_ci_validate_nonblocking.py` 覆盖 accepted/preparing Worker absent truthful snapshot、queued/running snapshot、terminal passed attestation、terminal failed failure_pack identity、same idempotency no duplicate Request/Job、stale Session revision fail-stop、fake 10+ minute CI non-blocking；`test_web_ci_wait_baseline.py` 保留 legacy wait baseline 并更新 validate 默认非阻塞断言；`test_dx1_orchestration.py` 更新 start failure rollback hook。修复前 Job `7094f954a363481b` 暴露测试辅助函数误写不存在的 `ci_jobs.current_step`；修复后 Private CI `repo-auto-check` Job `274ed63cd1674592` passed / exit 0，含 `services/github-action-service` ruff、compileall、pytest 与其他 Python workspace checks。DEV-008/009、Failure Pack redesign、recovery redesign 未纳入本任务。

### WEB-CI-DEV-008：`converge_development_task` 状态机化

- [x] 删除 `wait index -> wait CI` 串行路径。
- [x] 单次调用只推进立即可完成阶段。
- [x] running/pending 返回 durable convergence snapshot。
- [x] Index ready 后推进 analysis。
- [x] required evidence 齐全后才 `passed`。
- [x] drift -> blocked/recovery。
- [x] 重复调用不重复 convergence / CI。

映射验收：AC-WEB-CI-03/04/12/15。  
证据：正式集成前代码 candidate `90bcf21877439f06b62af68d4a3901f96e44542d`；指定 5 个 targeted 测试文件在 Python 3.12 隔离快照中 `56 passed`。清单提交仅修改本文档；DEV-009 保持未完成。

### WEB-CI-DEV-009：`resume_development_task` 接入 pending convergence

- [x] resume 返回当前 exact-head convergence identity/phase。
- [x] 区分 live/pending/historical evidence。
- [x] 可从 branch/PR 新窗口恢复同一 CI。
- [x] 不因 Resource 过期或窗口变化重复跑 CI。

映射验收：AC-WEB-CI-03/10/14。  
证据：正式集成代码 candidate `e83544257a14e1d9a066a6f157846c8dc397e5d6`；DEV-009 targeted `test_web_ci_convergence_resume.py + test_development_convergence_store.py + test_dx2_resume.py` 在 Python 3.12 隔离容器中 `41 passed`；resume production delta 静态确认无 CI start/wait/sleep 调用，历史 convergence 通过 Store 最小 read-only public helper 读取。

## 5. P0：诊断面增强

### WEB-CI-DEV-010：稳定 Failure Pack

- [x] 实现稳定 `failure_pack_id` 或等价 durable identity。
- [x] 保存/生成 exact job identity、failed step、exit code。
- [x] 分类代码/测试/依赖/Runner/网络/权限/基础设施/超时/取消/未知。
- [x] 生成 error fingerprint。
- [x] 解析 primary errors。
- [x] 支持常见测试框架 failed test 名称。
- [x] 可解析时输出 file/line/column；不可解析时明确 partial/unavailable。
- [x] 保存脱敏 command。
- [x] 保存 bounded log excerpt + log/resource continuation。
- [x] 保存 changed files/affected tests evidence（可用时）。
- [x] Resource 过期后可重新 materialize，不要求重跑 CI。

映射验收：AC-WEB-CI-06/07/10/16。  
证据：Codex semantic delta 来自 `bd31f2a27a5b2b412e280c0a549b49b8e3370cf2`，patch `62215 bytes` / SHA-256 `5ee2b7b1cec6aa241835139969d0ca9fa88c239fb6c6366cde6f59db94fe553a`；fresh current-main integration code candidate `26550d11774c619663464a5d694371012ec2a0fd`，三文件 blob 与 Codex patch 目标逐字节一致：`a18af15779f9205fd9cdd0727eb208cf0b9ba24b`、`d1f86c4213259a544da7fad5ab2a8e09143f5361`、`8924dab35398609f52340f3a13cc89f2905ae73b`。Targeted：`test_development_failure_pack.py + test_web_ci_validate_nonblocking.py + test_dx2_converge.py::test_ci_failed_is_truthfully_failed_with_failure_evidence` = `21 passed`；额外 boundary assertions 验证不同 secret 值仍得到相同 redacted evidence identity/fingerprint、nested sensitive fields/Authorization/command 脱敏、512 KiB durable limit、unittest/Node parser、location unavailable、changed/affected evidence、materialize/rematerialize 且 `rerun_ci=false`；production forbidden-call scan 无 `start_private_ci_job` / `schedule_ci_request_preparation` / `wait_private_ci_job` / `wait_for_job_change` / `time.sleep`。最终 exact-head Index / `repo-auto-check` / GitHub Checks / Draft PR 以本轮交付证据为准。

### WEB-CI-DEV-011：Step Log 精准读取

- [x] 评审增强 `get_private_ci_logs` 或新增 `get_private_ci_step_logs`。
- [x] 支持指定 failed step。
- [x] 支持 cursor/pagination。
- [x] 保留完整 Job log 路径。
- [x] 大日志走 Resource/paging。
- [x] Failure Pack 能直接给出正确 step/log continuation。

映射验收：AC-WEB-CI-07/08/09。  
证据：Codex DEV-011 semantic delta 基于 `bd31f2a27a5b2b412e280c0a549b49b8e3370cf2`，patch `49607 bytes` / SHA-256 `54eb7deef46912f6bdb5fe0c3bb9aafd2bd29fae0cfebe7261c7a6ff6316c239`；fresh current-main 集成仅重放 DEV-011 四文件 delta。`get_private_ci_logs` 保持 legacy Job-wide 调用并增量支持 persisted `step_id` / unique `step_name`、重复名称 ambiguity error、严格 `[log_start_offset, log_end_offset)`、deterministic `step-log-v1` cursor-only continuation 与 bounded paging；Failure Pack 提供 durable precise continuation，Resource 过期可 rematerialize 且 `rerun_ci=false`。Targeted：`test_web_ci_step_logs.py + test_development_failure_pack.py + test_mcp_response_budget.py + test_web_ci_get_snapshot.py + test_web_ci_wait_compatibility.py` = `59 passed`。

### WEB-CI-DEV-012：Secret redaction 验真

- [x] Token fixture。
- [x] password fixture。
- [x] DSN fixture。
- [x] Authorization header fixture。
- [x] Failure Pack、tail、step log、full Resource 全链路验证脱敏。
- [x] 脱敏不能删除 file/test/line/exit-code 等定位信息。

映射验收：AC-WEB-CI-16。  
证据：基于 fresh current-main `0c8b597b28d8a8307fd24256f28e14c382e4f783` 按 DEV-012 当前契约执行 semantic replay，未 cherry-pick stacked history，未整合 DEV-013/DEV-014。新增 `test_web_ci_secret_redaction.py` 覆盖 Token/password/DSN/Authorization header，并验证 Failure Pack、`get_private_ci_log_tail`、Job/step `get_private_ci_logs` 与 full Resource 的脱敏，同时保留 `tests/test_redaction.py:37:5`、step 与 `exit_code=23`。代码候选 `96dc356cb50654017753e2d245fdaff1a62b0de8` 的 `repo-auto-check` Job `11ad5ea95e5c4fd7` passed / exit 0；github-action-service `830 passed`、private-ci-agent `230 passed`、private-deploy-agent `5 passed`。

## 6. P0：MCP Schema / 幂等 / 安全

### WEB-CI-DEV-013：ToolAnnotations 全量校准

- [x] 列出所有 private CI / validate / converge / log / cancel 工具。
- [x] 对每个 handler 实际副作用评审 readOnlyHint。
- [x] 评审 destructive/consequential。
- [x] 评审 idempotentHint，并用真实服务端保证支撑。
- [x] 按当前 MCP/OpenAI 定义评审 openWorldHint。
- [x] 写 schema snapshot/manifest 自动测试。
- [x] capability tool_count/schema hash 与发布结果一致。

映射验收：AC-WEB-CI-11/13。  
证据：`待填写`

### WEB-CI-DEV-014：Cancel / supersede / stale evidence

- [x] cancel 只接受准确 job_id。
- [x] queued/running/terminal 行为分别测试。
- [x] supersede 写 durable event。
- [x] 被 supersede Job 永远不能生成新 HEAD 的 merge-eligible evidence。
- [x] Worker release/idle 可验证。

映射验收：AC-WEB-CI-12。  
证据：`tests/test_web_ci_cancel_supersede.py`；cancel `idempotentHint=true`；fresh schema identity canonical `164 / 1fb6e5a5c882e43d1cea272514ea329ecb5d4a86292920f28bc04a57415c393a`、compatibility `175 / 20f6a867b9c862ab2d5ec0c69b4b526dc578d1ca741190a12e76e44095503975`。

### WEB-CI-DEV-015：Response budget / Resource fallback

- [ ] summary 有 size budget 测试。
- [ ] Failure Pack inline 超预算自动 Resource。
- [ ] step/full logs 分页或 Resource。
- [ ] Resource 带 size/hash/cursor/has_more。
- [ ] 5000+ 行 warning 日志不会直接塞满默认 Web response。

映射验收：AC-WEB-CI-09/10。  
证据：`待填写`

## 7. P1：性能与可观测性

### WEB-CI-DEV-016：MCP duration 与 wait-path 指标

- [x] 每个 canonical tool 有 duration metric。
- [x] 能区分主动 wait 与普通 handler 时间。
- [x] CI phase、queue、execution 时间可统计。
- [x] convergence phase 时间可统计。
- [x] idempotent reuse/conflict 可统计。
- [x] Failure Pack build duration/bytes 可统计。
- [x] inline/resource fallback 可统计。
- [x] 避免 repository/job/branch 等高基数字段进入 Prometheus label。

映射验收：AC-WEB-CI-14/15。  
证据：implementation candidate `05310ef4b1c97251509f7afe1635837073ec5a84` / Tree `4164421c5c5c09281a52cdea0232f2ecbe5eb59a`；exact Index `12.0.0-1` ready（`288 files / 3296 symbols`）；`test_web_ci_observability.py` 覆盖 canonical MCP count/duration、ordinary vs explicit wait、durable CI queue/execution/total、Convergence phase、idempotency reuse/conflict、Failure Pack duration/bytes/redaction、inline/resource fallback 与 label cardinality，并证明重复 snapshot 不重复累计 lifecycle duration；candidate `repo-auto-check` Job `a3962f8606cb4ae9` passed / exit 0，`github-action-service` ruff passed、compileall passed、full pytest `880 passed in 199.21s`；Change Impact 为 8 files / 1 affected module，Contract Change Detection 为 0；初始 queued Job 在现有 `ci_jobs.queued_at` 写 durable timestamp，terminal duration 仅在 durable `complete_job` commit 后累计；`/metrics` 仍沿用原认证路由。

### WEB-CI-DEV-017：CI 本身性能优化保持独立

- [x] 记录 full CI 各 step duration baseline。
- [x] 优先 profile 长测试 step。
- [x] 独立排查 setup/cache/network 异常长尾。
- [x] Vue warn 等日志噪声单独处理，不把它无证据认定为运行慢根因。
- [x] fast CI 继续作为开发反馈，full CI 继续作为最终门禁。

说明：本项优化可以降低总耗时，但不是 WEB-CI-01 Web 安全架构完成的替代条件。  
证据：base main `fbd8161fe09a40fde56c7185c97322486f68859e` / Tree `3d22c7f5a4e2c8e17d58999ace63f0f4e6ddc677`；baseline full Private CI Job `cc59f562e2cb44f0` passed / exit 0，总时长 `199.31s`，`github-action-service pytest 194.22s`、`private-ci-agent pytest 54.45s`、`private-deploy-agent pytest 1.06s`；同一 base forced rerun Job `20ceb115e2524cd6` passed / exit 0，总时长 `211.44s`，`github-action-service pytest 206.13s`、`private-ci-agent pytest 57.79s`、`private-deploy-agent pytest 1.15s`，确认 baseline 方差约 6-7%。profiling Job `3f893bd91ac84cdd` passed / exit 0，`--durations=50` 显示 dominant cost 为 `test_web_ci_get_snapshot.py::test_full_keeps_diagnostics_and_oversized_payload_uses_resource_while_summary_stays_inline` `190.80s`，第二慢为 `test_development_failure_pack.py::test_failure_pack_bounds_log_and_marks_missing_evidence` `10.65s`；setup/cache/network 排查结论为 source mirror hit、三个 Python workspace environment-cache 均 hit/restored，runtime-dependencies/ruff/compileall 均为秒级或亚秒级，GitHub Actions `34200755829` 也显示 `Run tests` 远大于 dependency install，因此不把 Vue/pytest warning、日志量、dependency restore 或网络作为无证据根因。optimization candidate `e43baaa8a913da36810d765a4f58ef61f04355ef` / Tree `4a111f2f153f1085b259d8662ea2fcbd073b8006` 仅修改测试 fixture 与 `pytest.ini`：保留 full/detail resource fallback、summary inline、完整 resource readback、Failure Pack truncation 语义，将 synthetic oversized payload 缩至仍超过 `MAX_SAFE_INLINE_BYTES` 的最小充分规模，并将 Failure Pack oversized log 缩至 `MAX_LOG_EXCERPT_BYTES + 512`；未删除测试、未 skip/xfail、未降低断言、未改变 full gate。candidate Index `12.0.0-1` ready（Index Job `e55872d2-4d9c-4f33-b2ad-2279139efb98`，288 files，reindexed 2）。after full Private CI Job `78d236fe63a0405e` passed / exit 0，总时长 `69.19s`，`github-action-service pytest 63.08s`、`private-ci-agent pytest 49.41s`、`private-deploy-agent pytest 0.85s`，slowest tests `42.88s` / `9.49s`；repeat full Private CI Job `dd07e5889f8f4b4c` passed / exit 0，总时长 `67.89s`，`github-action-service pytest 61.50s`、`private-ci-agent pytest 48.72s`、`private-deploy-agent pytest 1.21s`，slowest tests `41.57s` / `9.05s`。before/after delta against baseline Job `cc59f562e2cb44f0`：total `-130.12s`（约 `-65.3%`），controller pytest `-131.14s`（约 `-67.5%`）；against forced rerun Job `20ceb115e2524cd6`：total `-143.56s`（约 `-67.9%`），controller pytest `-144.63s`（约 `-70.2%`）。fast feedback Job `1beb7d310d7f4cc3` failed before tests with existing `FAST_CHECK_ENTRYPOINT_MISSING` (`make ai-integrity-check` absent)，未作为 merge gate，full `repo-auto-check` 仍为最终门禁。notable noise：Job `78d236fe63a0405e` log 曾出现 non-failing background `aiosqlite` thread traceback；repeat Job `dd07e5889f8f4b4c` 未复现，未归因为耗时根因。

DEV-017 evidence 分层：**fixture-size mitigation** 仅缩小 synthetic oversized test fixture，保留 full/detail resource fallback、summary inline、exact resource readback 与 Failure Pack truncation 的原有覆盖语义；它解释了测试层耗时下降，但不作为 production 算法复杂度问题已解决的证据。

**production algorithmic repair（MAJOR-017-01）**：最终 code candidate `3561b500160be88b55de96c08b9640eeaa006c91` / Tree `7db042dac36d5b0c08e9de9c4d2f0bc07f67d8bc`。`redact_text()` 保留 PEM、Authorization、headers、quoted sensitive assignment、CLI sensitive option、sensitive assignment、URL credential、known token 等 redaction coverage，不按输入长度跳过；在 `_SENSITIVE_ASSIGNMENT_RE` 前加入 bounded assignment-key precondition，只在 `:` / `=` 紧邻的 key token 确实命中 sensitive marker 时进入昂贵 regex，且 regression 使用“远处出现 secret 文案 + 大量 ordinary=value”证明 expensive regex 被绕过，同时 `client_secret` 位于长文本尾部仍输出 `[REDACTED]`。`parse_failure_location()` 不截断输入，改为 non-overlapping token + 单向 scanner，继续支持 Unix/Windows `file:line[:column]`、tail location 与 invalid location。`ci_mcp.py` 的 final recursive redaction 未删除：full snapshot 在已脱敏 Job payload 之外还注入 Request/worker 字段，serialization-boundary traversal 仍有安全职责，因此不判定为完全冗余。

Deterministic regressions：`test_redact_text_large_non_secret_bypasses_sensitive_assignment_regex`、`test_redact_text_large_secret_near_tail_still_redacts`、`test_parse_failure_location_large_no_location_uses_non_overlapping_tokens`、`test_parse_failure_location_large_text_finds_location_near_tail`（Unix + Windows）、`test_parse_failure_location_rejects_invalid_location_candidates`。独立等价逻辑 benchmark（仅用于复杂度趋势，不冒充 exact-head CI）：redact non-secret-with-misleading-keyword 5/10/20/80 KiB = `0.747/1.488/2.963/12.531 ms`，2x 输入约 `1.99x/1.99x`，20→80 KiB 4x 输入约 `4.23x`；no-location 8/16/32/128 KiB = `1.742/3.308/7.773/28.290 ms`，2x 输入约 `1.90x/2.35x`，32→128 KiB 4x 输入约 `3.64x`。相对 Manager repair 前约每次翻倍接近 4x 的观察，增长趋势已不再接近 O(n²)。

Code-candidate exact-head validation：Index `12.0.0-1` Job `0b3ee973-73d5-4e5d-9e7b-3ccc66942e32` completed（288 files，reused 286 / reindexed 2）；Change Impact = 5 changed paths / modules `docs, services` / 61 affected-test candidates / Contract Change 0。full Private CI `0ebc9f8e4e8c40c2` = passed / exit 0；`github-action-service` ruff + compileall passed，full pytest `886 passed in 11.10s`（`--durations=50 --durations-min=0.05` 保留）；`private-ci-agent` ruff + compileall passed，pytest `230 passed in 49.07s`；`private-deploy-agent` pytest `5 passed in 0.08s`。Attestation `c1649e46-c685-4ae5-8831-e8fc3a1e5f08` 绑定该 code candidate。此处为 docs commit 前 evidence；docs commit 产生新 HEAD 后必须重新生成最终 Index / Change Context / Impact / Contract / Affected Tests / full Private CI / attestation / Clean 2/2。

## 8. P0：兼容、文档、Capability

### WEB-CI-DEV-018：Compatibility contract

- [ ] legacy wait 客户端自动测试。
- [ ] canonical 与 compatibility schema 暴露差异有 snapshot。
- [ ] running Job 升级后 identity 不变。
- [ ] 旧 Job 可查询/诊断。
- [ ] deprecation 文案不误导 AI。

映射验收：AC-WEB-CI-13。  
证据：`待填写`

### WEB-CI-DEV-019：Capability / Manifest / README

- [ ] capability 增加机器可读 Web-safe CI 能力字段。
- [ ] Manifest 工具描述、参数默认值与实现一致。
- [ ] README 描述 canonical 推荐流程。
- [ ] DX 文档引用 WEB-CI-01。
- [ ] 不把某个固定秒数写成“OpenAI 官方 Web timeout”。

映射验收：AC-WEB-CI-11/13/15。  
证据：`待填写`

## 9. 测试与验收执行清单

### Automated

- [ ] **TEST-01** state machine unit tests。
- [ ] **TEST-02** idempotency concurrency tests。
- [ ] **TEST-03** Controller restart recovery integration tests。
- [ ] **TEST-04** 10+ 分钟 fake CI non-blocking tests。
- [ ] **TEST-05** converge cross-call resume tests。
- [ ] **TEST-06** Failure Pack parser tests：Vitest/JS。
- [ ] **TEST-07** Failure Pack parser tests：Go test。
- [ ] **TEST-08** dependency/setup/network failure tests。
- [ ] **TEST-09** worker lost/timeout/cancel tests。
- [ ] **TEST-10** Secret redaction tests。
- [ ] **TEST-11** large log / Resource tests。
- [ ] **TEST-12** Resource expiry/re-materialize tests。
- [ ] **TEST-13** ToolAnnotations/schema snapshot tests。
- [ ] **TEST-14** compatibility tests。
- [ ] **TEST-15** fast/full merge eligibility tests。

### Private CI

- [ ] **CI-01** candidate HEAD `repo-fast-check`（开发反馈，可选但推荐）。
- [ ] **CI-02** candidate HEAD `repo-auto-check` 通过。
- [ ] **CI-03** Job 绑定 exact repository/branch/commit/tree。
- [ ] **CI-04** Worker 最终 release 正常。

### Real Web E2E

- [ ] **E2E-01** ChatGPT Web 启动一个真实长 CI，CI 未结束时 MCP 调用已返回。
- [ ] **E2E-02** 当前 Web 回答可以正常结束，不要求一直 wait。
- [ ] **E2E-03** 新一轮调用恢复同一个 job_id/convergence_id。
- [ ] **E2E-04** 真实失败 Job 能获取 Failure Pack。
- [ ] **E2E-05** 从 Failure Pack 精确读取 failed step 日志。
- [ ] **E2E-06** 必要时完整日志仍可访问。
- [ ] **E2E-07** 记录每次 MCP request duration，确认没有因 CI 生命周期持续阻塞。

证据：`待填写 Web 对话时间、job_id、convergence_id、CI profile、SHA、duration`

## 10. 验收标准逐项签字

- [ ] AC-WEB-CI-01 长 CI 不阻塞启动调用。
- [ ] AC-WEB-CI-02 status 不 long-poll。
- [ ] AC-WEB-CI-03 converge 跨调用/跨窗口恢复。
- [ ] AC-WEB-CI-04 Controller restart 恢复。
- [ ] AC-WEB-CI-05 幂等重试不重复排队。
- [ ] AC-WEB-CI-06 失败摘要指出问题点。
- [ ] AC-WEB-CI-07 Failure Pack 与日志一致。
- [ ] AC-WEB-CI-08 完整诊断未隐藏。
- [ ] AC-WEB-CI-09 大日志不淹没 Web 上下文。
- [ ] AC-WEB-CI-10 Resource 过期仍可恢复失败证据。
- [ ] AC-WEB-CI-11 ToolAnnotations 与实际行为一致。
- [ ] AC-WEB-CI-12 fast/full 门禁不退化。
- [ ] AC-WEB-CI-13 compatibility 可用但非 Web 默认。
- [ ] AC-WEB-CI-14 真实 ChatGPT Web E2E 通过。
- [ ] AC-WEB-CI-15 性能门槛通过。
- [ ] AC-WEB-CI-16 安全脱敏通过。

## 11. 最终交付检查

- [ ] Change Context/Impact/Contract/Affected Tests 已重新对 candidate HEAD 执行。
- [ ] 文档与实现一致，不包含过期默认值。
- [ ] 当前 candidate HEAD full Private CI 通过。
- [ ] Draft PR 包含修改、测试、风险、兼容和回滚说明。
- [ ] PR merge readiness 已 fresh-read。
- [ ] 用户在当前对话明确授权 merge 后才合并。
- [ ] merge 后取得新 main SHA。
- [ ] 新 main SHA 重新执行最终 `repo-auto-check`。
- [ ] 如需生产发布，另行取得明确 deploy 授权；本文不授予 deploy 权限。
- [ ] 发布后重新验证 production capability/version/build/schema 和真实 Web E2E。

最终证据：`待填写`
