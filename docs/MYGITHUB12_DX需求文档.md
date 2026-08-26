# MyGithut12 DX-1 需求文档

## 1. 背景

MyGithut12 12.0.5 已具备较完整的 GitHub、PR、Workspace、Index、Private CI 和安全写入能力。当前影响开发体验的主要问题是：

- 日常任务仍需调用较多底层工具并反复传 repository、SHA、Workspace 和 revision；
- Controller 对 exact Commit 文件和 Tree 的读取仍存在重复 GitHub API 往返；
- Private CI 每轮仍有重复 venv 和依赖 bootstrap；
- 已有影响分析没有完整驱动 affected workspace/test；
- CI 失败后 AI 仍需多次读取 job、logs、文件和 symbol；
- 大结果虽然有 Resource fallback，但默认摘要仍可进一步压缩；
- 生产发布缺少经过验证的 Blue/Green 共享状态和切流协议；
- 每次新增或删除工具都可能触发 ChatGPT Connector 更新，因此公共接口必须长期稳定。

DX-1 必须提高开发速度，但不能降低 HEAD/Blob/Workspace CAS、写后验证、幂等、审计、Private CI、Attestation 和 Merge Gate。

## 2. 目标

1. 新增 4 个长期稳定的高层开发工具。
2. 保持 `MyGithut12` 名称、Connector URL、认证和现有 155 工具兼容。
3. 建立 Development Session，把重复身份和流程状态收口到服务端。
4. 使用本地 bare Mirror 加速 immutable Git 读取。
5. 把 Index、Instructions、Context 和 overlap 预检组合进任务准备。
6. 建立 Fast CI / Full CI 双层验证。
7. 建立不可变依赖环境缓存和 affected selection。
8. 自动生成 CI Failure Pack。
9. 高层工具默认返回 compact summary，详情通过 Resource。
10. 以 Blue/Green 方式发布，切流前 Blue 持续可用，切流后保留热备。

## 3. 非目标

- 不迁移 Gogs/Gitea，不替换 GitHub。
- 不删除现有工具，不创建 MyGithut13。
- 不开放任意 Shell、SQL、Git URL、主机、端口、本地路径或 Secret。
- 不允许 Fast CI 满足 merge gate。
- 不把本地 Mirror 当成分支 ref 的唯一事实来源。
- 不自动 merge、生产 deploy 或删除 branch。
- 不在同次 Blue/Green 发布执行破坏旧版兼容的 Contract migration。
- 不一次性重写全部 legacy GitHub 工具。

## 4. 主要使用场景

### UC-001 启动开发任务

AI 输入 repository、任务名称和可选 seed path/symbol。服务完成 policy、fresh base、Workspace、唯一分支、lease、Index、Instructions、Context 和 overlap，返回 `development_session_id`。

### UC-002 小步提交

AI 提交一个 ChangeSet。服务验证 Session/Workspace revision、lease、HEAD 和 Blob，将变更归一化为一个确定性 Commit，完成 fresh read-back，并推进状态和 Index。

### UC-003 快速反馈

AI 对当前 Session 运行 Fast CI。服务根据 changed files 和依赖图选择受影响 workspace/test，使用安全环境缓存，失败时返回 Failure Pack。

### UC-004 合并前完整验证

AI 运行 Full CI，生成或复用精确身份 Attestation，并汇总 PR、Checks、Review、base/head 和 merge method readiness。

### UC-005 多窗口并发

多个聊天窗口分别持有独立 Session/Workspace。系统检测同分支 lease 冲突、外部 drift、声明 scope 和实际变更 overlap，不允许静默覆盖。

### UC-006 蓝绿切换期间继续开发

Blue 承接现有开发；Green 在 standby 状态验证。切流后新请求进入 Green，已有 Blue 连接允许 drain。任何异常可原子回切 Blue，活动 Workspace、Upload 和 Resource 不丢失。

## 5. 功能需求

### FR-DX-001 名称和版本兼容

- 外部服务名保持 `MyGithut12`。
- 目标版本建议 `12.1.0`。
- Connector URL、认证和仓库授权保持稳定。
- 现有 155 工具全部保留。
- 新增 4 个工具后目标总数为 159。
- `12.x` 不再计划新增或删除工具名称。

### FR-DX-002 Development Session

Session 至少保存：

- session ID、Workspace ID；
- repository、branch、base branch；
- base/head/tree、Session revision、Workspace revision；
- owner、lease、status、scope；
- Index identity、PR、Fast CI、Full CI、Attestation；
- last failure、created/updated/closed；
- 幂等请求和事件证据。

Session 只负责编排；Workspace lease/revision 和 GitHub identity 仍为写权限事实。

### FR-DX-003 任务准备

`prepare_development_task` 必须完成：

1. repository allowlist 和 operation policy；
2. fresh base ref、Commit 和 Tree；
3. 幂等重放检查；
4. Workspace/branch 创建或绑定；
5. lease 和 revision；
6. Index reuse/build；
7. Agent Instructions；
8. bounded Context Pack V2；
9. 活动 Workspace overlap；
10. Session 和事件写入。

任何中间失败必须返回已创建资源和补偿状态，不能留下不可识别孤儿。

### FR-DX-004 ChangeSet 写入

`apply_development_change_set` 必须：

- 支持 patch、range edit 和 finalized upload 引用；`mode=upload` 必须支持一个或多个 finalized upload；
- 多 upload 必须一次性校验全部 path/upload/expected Blob，并通过同一个 Git Tree/Commit 原子写入；任一项失败不得形成部分 Commit；文件数量和聚合字节数必须有服务端硬上限并通过 capability 暴露；
- 单次请求最终形成一个 Commit；
- dry-run 和 commit 使用同一 canonical request；
- 校验 Session CAS、Workspace CAS、lease、expected HEAD 和 expected Blob；
- 复用现有 durable write 和 fresh read-back；
- 写成功后推进 Workspace/Session revision；
- 请求新 Commit incremental Index；
- 返回 changed files、old/new Blob、Commit、Tree 和 operation ID。

Index 失败不能把已经成功的 GitHub Commit伪装成失败，应独立报告 `commit_succeeded=true` 和 index 状态。

### FR-DX-005 验证编排

`validate_development_task` 支持：

- `fast`：运行 `repo-fast-check`，`merge_eligible=false`；
- `full`：运行 `repo-auto-check`，通过后可生成 Attestation；
- `reuse`：只有完整身份 tuple 一致时复用；
- `wait`：最长 55 秒长轮询；
- `failure_pack`：失败时自动聚合诊断。

复用身份至少包括 repository、Commit、Tree、profile、image digest、toolchain、dependency manifest、test config、profile version、有效期和撤销状态。

### FR-DX-006 任务收尾

`finalize_development_task` 支持：

- `prepare_pr`：创建或更新 Draft PR；
- `readiness`：只读汇总；
- `merge`：必须 `confirm=true` 并复用现有 Merge Gate；
- `close`：关闭 Session/Workspace，默认不删除 branch/PR。

### FR-DX-007 本地 Git Mirror

- 每个授权 repository 有独立 bare Mirror。
- 分支 ref 必须经 GitHub fresh-read 或受验证 fetch。
- exact Commit/Tree/Blob/file/diff/history 可从 Mirror 读取。
- 返回结果包含 Commit、Tree、Blob、Mirror generation/fetch evidence。
- Mirror 损坏、缺失或身份不一致时回退 GitHub API。
- 不接受调用方传任意 remote URL。

### FR-DX-008 Index 自动预热

- Session 创建时复用 base Commit Index。
- ChangeSet Commit 成功后自动请求新 Commit incremental Index。
- Index 未 ready 时可读 exact Git 文件，但不得使用旧 Index 冒充新 Commit。
- Index warm failure 单独记录和告警。

### FR-DX-009 Context Pack V2

默认 inline 只返回：

- 任务解析；
- 最相关 path/symbol/module/test/contract；
- score、reason、authority、SHA 和行范围；
- omitted 统计；
- Resource URI。

不得默认返回大量完整文件。

### FR-DX-010 Fast CI 发现

- `repo-fast-check` 必须在允许仓库的 `list_private_ci_profiles` 中可发现。
- Controller、Agent、repository config 和 discovery 必须一致。
- Fast CI 永远不能产生 merge-eligible Attestation。

### FR-DX-011 依赖环境缓存

缓存身份至少包含：

```text
runtime version
container image digest
workspace relative path
dependency/lockfile hashes
CI profile version
bootstrap command version
```

环境必须 build、verify、seal 后只读复用。缓存 hit/miss/invalidated/evicted 均可观测。

### FR-DX-012 Affected Selection

- 根据 changed files、dependency graph、contract changes 和仓库固定规则选择 workspace/test。
- 分析不完整时必须保守扩大范围。
- Fast CI 默认 affected-only。
- Full CI 默认完整执行，除非仓库策略明确允许 affected-only。

### FR-DX-013 Failure Pack

至少包含：

- job/profile/Commit/Tree；
- failed step、exit code、稳定错误码和日志尾；
- changed files、affected modules/tests；
- 相关 symbol、配置、manifest；
- cache evidence；
- 候选下一步，不宣称已修复。

### FR-DX-014 Compact Response

- 高层工具默认只返回决策所需字段。
- 超过 inline budget 使用 `read_mcp_response_resource`。
- `include_details=false` 必须真正移除大字段。
- 列表必须有 total、truncated、cursor/resource 和 content hash。
- 修复聚合工具中局部变量未初始化等已知可靠性问题。

### FR-DX-015 Blue/Green 状态连续性

Blue 和 Green 并存时必须共享或可路由：

- Workspace、Session、lease、revision；
- idempotency、audit；
- CI controller state；
- response resource；
- chunked upload；
- Index metadata/pins；
- schema version、runtime generation 和 leader lease。

Blue 创建的 Resource/Upload 不能因切流立即失效。

### FR-DX-016 单 Leader 副作用

Green 预热时默认 standby：

- 不领取 Private CI job；
- 不执行 cleanup/retention；
- 不推进 deployment；
- 不重复消费 Index queue；
- 不发送重复 callback。

Leader handoff 使用共享 lease/CAS，不能只依赖启动顺序。

### FR-DX-017 Expand/Contract Migration

- Green 启动前只执行向后兼容 Expand。
- Blue/Green 均能读写 Expand 后 Schema。
- rename/drop/not-null-without-default/语义重写禁止同次执行。
- Contract 在 Blue 完全退役和回滚窗口结束后独立发布。

### FR-DX-018 发布预检

Green 必须通过：

1. build identity；
2. 159 工具 Manifest；
3. Capability；
4. DB compatibility；
5. 真实 repository 只读 smoke；
6. 专用 Canary branch durable write；
7. Fast CI 和 Full CI；
8. Resource/Upload continuity；
9. Workspace/Session concurrency；
10. rollback rehearsal。

## 6. 非功能需求

### 性能

热状态和正常网络下：

- `prepare_development_task` P95 < 5 秒；大索引可异步返回。
- warm exact-commit file/manifest/tree P95 < 500 ms。
- Context Pack V2 compact P95 < 3 秒。
- 小范围 Fast CI P95 < 15 秒。
- dependency env cache-hit bootstrap P95 < 2 秒/Workspace。
- `github_mcp` 小变更 Full CI 目标 P95 < 35 秒。
- 从任务创建到 Draft PR 的高层调用数 P50 ≤ 5、P95 ≤ 7。

所有性能结论必须来自真实 job/trace，不接受只用 mock。

### 可用性

- 切流前 Blue readiness 持续为 true。
- 切流不 stop/restart Blue。
- 新连接进入 Green，旧连接允许在 Blue drain。
- Green 异常只需 upstream 回切。
- 故障不破坏已有 Git Commit、Workspace、CI job、Upload 或 Resource。

### 安全

- 仓库 allowlist 和默认分支策略不变。
- 高层工具不接受任意 command/host/path/URL。
- consequential action 保留 confirm、idempotency、audit 和 CAS。
- 缓存隔离，不保存凭据。
- 日志不记录 Secret、Authorization、完整大文件或大 Patch。

## 7. 成功标准

- 159 个工具 Manifest、运行时和客户端发现一致。
- 旧 155 工具全量回归。
- 4 个高层工具完成真实 repository 端到端闭环。
- Fast CI、env cache、affected selection 和 Failure Pack 有真实证据。
- Blue/Green 连续服务、切流、活动状态和回切全部通过。
- 开发清单和验收清单无未解释的未完成项。
