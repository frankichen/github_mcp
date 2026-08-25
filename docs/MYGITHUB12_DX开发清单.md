# MyGithut12 DX-1 开发清单

说明：

- 每项完成必须附 Commit、PR、测试、Private CI 或运行证据。
- `P0/P1/P2` 表示顺序，不允许跳过最终验收。
- 本清单当前只完成文档规划，代码实现均不得提前标记完成。
- 默认不 merge、不 production deploy，除非执行任务有明确授权。

## P0：基线和文档

- [x] DX-001 固定基线 `main=2e8fcf312697a6d2698aa4326d54905d787e84d0`、Tree 和 12.0.5 Capability。
- [x] DX-002 记录当前 155 工具 Manifest 和 118+37 组成。
- [x] DX-003 建立规划、需求、设计、接口、开发和验收文档。
- [ ] DX-004 后续实施窗口 fresh-read 最新 main 并更新基线。
- [x] DX-005 建立 DX-1 Draft PR 和实施状态摘要（Draft PR #54）。
- [ ] DX-006 修复文档与代码、Manifest、Capability 不一致的历史描述。

## P0：现有可靠性缺口

- [ ] DX-010 修复 development history 聚合中的局部变量未初始化问题。
- [ ] DX-011 增加对应成功、空字段、关闭 PR 和时区测试。
- [ ] DX-012 修复 `repo-fast-check` 已配置但 profile discovery 不一致。
- [ ] DX-013 Controller/Agent/repository/profile/tool discovery 增加一致性启动检查。
- [ ] DX-014 `include_details=false` 增加严格 response budget 回归。
- [ ] DX-015 对所有大列表审计 total/truncated/resource 语义。
- [ ] DX-016 确认旧 155 工具完整回归基线。

## P0：接口冻结

- [ ] DX-020 固化 4 个新增工具名称。
- [ ] DX-021 固化 4 个 input schema、annotation 和稳定错误码。
- [ ] DX-022 将 service version 规划为 12.1.0，名称仍为 MyGithut12。
- [ ] DX-023 Manifest 目标从 155 更新为 159。
- [ ] DX-024 validate manifest 断言新工具固定顺序和总数。
- [ ] DX-025 Capability 增加 DX-1 支持字段。
- [ ] DX-026 证明旧 155 工具必填参数无变化。
- [ ] DX-027 形成 Connector 一次发现更新和回退方案。
- [ ] DX-028 冻结 12.x 后续工具名称增删政策。

## P0：Development Session

- [ ] DX-030 设计并迁移 `development_sessions` 表。
- [ ] DX-031 设计 `development_session_events` 追加事件表。
- [ ] DX-032 设计 `development_session_validations` 表。
- [ ] DX-033 实现 Session revision CAS。
- [ ] DX-034 实现 Session 与 Workspace 唯一绑定。
- [ ] DX-035 实现 Session lease/status 恢复。
- [ ] DX-036 实现幂等 Session 创建。
- [ ] DX-037 实现 drifted/blocked/closed 状态。
- [ ] DX-038 实现 Session 审计和 trace。
- [ ] DX-039 增加 crash/restart 恢复测试。

## P0：高层工具

### prepare

- [ ] DX-040 实现 `prepare_development_task` MCP schema。
- [ ] DX-041 编排 policy、fresh base、Workspace、branch 和 lease。
- [ ] DX-042 编排 Index reuse/build。
- [ ] DX-043 编排 Instructions、Context V2 和 overlap。
- [ ] DX-044 实现部分失败补偿和 orphan evidence。
- [ ] DX-045 实现 compact response/resource。

### apply

- [ ] DX-050 实现 ChangeSet schema_version=1。
- [ ] DX-051 支持 patch mode。
- [ ] DX-052 支持 range mode。
- [ ] DX-053 支持 finalized upload mode。
- [ ] DX-054 将不同 mode 归一化为一个原子 Commit。
- [ ] DX-055 复用现有 HEAD/Blob/Workspace CAS。
- [ ] DX-056 dry-run/commit canonical hash 一致。
- [ ] DX-057 durable read-back 后推进 Session/Workspace。
- [ ] DX-058 自动请求 incremental Index。
- [ ] DX-059 实现成功 Commit + Index failure 分离语义。

### validate

- [ ] DX-060 实现 `validate_development_task`。
- [ ] DX-061 fast mode 绑定 `repo-fast-check`。
- [ ] DX-062 full mode 绑定 `repo-auto-check`。
- [ ] DX-063 reuse identity tuple 完整验证。
- [ ] DX-064 wait 采用最长 55 秒长轮询。
- [ ] DX-065 失败自动生成 Failure Pack。
- [ ] DX-066 Fast CI 明确 merge_eligible=false。
- [ ] DX-067 Full CI 通过后生成/复用 Attestation。

### finalize

- [ ] DX-070 实现 `finalize_development_task`。
- [ ] DX-071 prepare_pr 创建/更新 Draft PR。
- [ ] DX-072 readiness 复用现有只读门禁。
- [ ] DX-073 merge 必须 confirm 并复用现有 merge gate。
- [ ] DX-074 close 释放 Session/Workspace，不默认删 branch/PR。
- [ ] DX-075 action 状态机和幂等回归。

## P1：Local Git Mirror

- [ ] DX-080 实现 repository allowlist 到固定 remote 映射。
- [ ] DX-081 实现独立 bare Mirror 和文件锁。
- [ ] DX-082 实现 fetch/prune/timeout/audit。
- [ ] DX-083 实现 exact Commit/Tree/Blob/file 读取。
- [ ] DX-084 实现 diff/history/merge-base 读取。
- [ ] DX-085 ref 请求先 GitHub fresh resolve。
- [ ] DX-086 object missing 时 fetch，再回退 GitHub API。
- [ ] DX-087 返回 mirror generation/source/evidence。
- [ ] DX-088 Mirror 损坏自动隔离和重建。
- [ ] DX-089 禁止任意 remote/path/credential 泄露。
- [ ] DX-090 建立 warm/cold/fallback 性能基线。

## P1：Context 和 Response

- [ ] DX-100 实现 Context Pack V2 ranking。
- [ ] DX-101 默认只返回相关片段和 metadata。
- [ ] DX-102 每项带 reason、authority、SHA、line range。
- [ ] DX-103 omitted 统计完整。
- [ ] DX-104 大结果统一 Resource。
- [ ] DX-105 高层工具默认 compact。
- [ ] DX-106 所有列表 total/truncated/cursor 一致。
- [ ] DX-107 response content hash 和 UTF-8 offset 正确。
- [ ] DX-108 Blue/Green 跨代 Resource 读取测试。

## P1：Private CI 提速

### Profile 和 affected selection

- [ ] DX-110 `repo-fast-check` 在允许仓库可发现。
- [ ] DX-111 Fast profile 配置与 Agent 一致。
- [ ] DX-112 实现 changed files → workspace 映射。
- [ ] DX-113 接入 dependency graph。
- [ ] DX-114 接入 contract changes。
- [ ] DX-115 incomplete 时保守扩大。
- [ ] DX-116 Fast CI affected-only。
- [ ] DX-117 Full CI 默认完整执行。
- [ ] DX-118 输出 selection reasons。

### Dependency environment cache

- [ ] DX-120 固化 env cache key。
- [ ] DX-121 隔离临时 build。
- [ ] DX-122 验证 runtime/tool/import。
- [ ] DX-123 原子发布只读 cache。
- [ ] DX-124 相同 key 并发去重。
- [ ] DX-125 invalidation/eviction。
- [ ] DX-126 防跨仓库/跨 runtime 串用。
- [ ] DX-127 cache hit/miss metrics。
- [ ] DX-128 真实性能测试。

### Failure Pack

- [ ] DX-130 聚合 failed step/exit/log tail。
- [ ] DX-131 关联 changed files/affected tests。
- [ ] DX-132 关联 symbol/config/manifest。
- [ ] DX-133 关联 env cache evidence。
- [ ] DX-134 compact summary + Resource。
- [ ] DX-135 Failure Pack 自身失败不覆盖原 CI error。

## P1：Blue/Green 基础

- [ ] DX-140 实现 runtime generation identity。
- [ ] DX-141 实现 active/standby/draining role。
- [ ] DX-142 实现 shared leader lease/CAS。
- [ ] DX-143 Green standby 不消费副作用任务。
- [ ] DX-144 Session/Workspace/CI/idempotency 跨代兼容。
- [ ] DX-145 Resource metadata 和文件跨代可读。
- [ ] DX-146 Upload staging 跨代可继续。
- [ ] DX-147 Expand-only migration。
- [ ] DX-148 `/ready` 返回 schema/generation/leader。
- [ ] DX-149 反向代理原子 upstream 配置。
- [ ] DX-150 Blue connection drain 和热备。
- [ ] DX-151 仅 upstream 回切演练。
- [ ] DX-152 Contract migration 独立发布约束。

## P2：测试

- [ ] DX-160 4 个高层工具 schema/annotation 测试。
- [ ] DX-161 Session 状态机和 CAS 测试。
- [ ] DX-162 Workspace/Session 双重并发测试。
- [ ] DX-163 同分支双写至少一方稳定失败。
- [ ] DX-164 crash/retry/idempotency replay。
- [ ] DX-165 Mirror identity/fallback/corruption。
- [ ] DX-166 Context ranking/resource。
- [ ] DX-167 Fast/Full CI 门禁隔离。
- [ ] DX-168 env cache 隔离和失效。
- [ ] DX-169 affected selection 保守性。
- [ ] DX-170 Failure Pack。
- [ ] DX-171 旧 155 工具全量回归。
- [ ] DX-172 `github_mcp` 真实冒烟。
- [ ] DX-173 `sxt` 真实只读/索引/Private CI 冒烟。
- [ ] DX-174 Blue/Green 活动 Workspace。
- [ ] DX-175 Blue 创建 Resource，Green 读取。
- [ ] DX-176 Blue 开始 Upload，Green 完成。
- [ ] DX-177 切流期间 long-poll/idempotency。
- [ ] DX-178 Green 故障原子回切。

## P2：性能

- [ ] DX-180 warm exact-read P95 < 500 ms。
- [ ] DX-181 prepare P95 < 5 秒。
- [ ] DX-182 Context V2 P95 < 3 秒。
- [ ] DX-183 Fast CI P95 < 15 秒。
- [ ] DX-184 cache-hit bootstrap P95 < 2 秒。
- [ ] DX-185 小变更 Full CI 目标 P95 < 35 秒。
- [ ] DX-186 主路径调用数 P50 ≤ 5、P95 ≤ 7。
- [ ] DX-187 性能证据来自真实 trace/job。

## P2：发布

- [ ] DX-190 构建 Green immutable image。
- [ ] DX-191 Build SHA、version、Manifest 一致。
- [ ] DX-192 Expand migration 备份和 dry-run。
- [ ] DX-193 Green 独立端口启动，role=standby。
- [ ] DX-194 Green read-only real repo smoke。
- [ ] DX-195 Canary branch durable write。
- [ ] DX-196 Fast CI 和 Full CI Canary。
- [ ] DX-197 Resource/Upload/Workspace continuity。
- [ ] DX-198 rollback rehearsal。
- [ ] DX-199 原子切流，Blue 保持运行。
- [ ] DX-200 ChatGPT Connector 刷新为 159 工具。
- [ ] DX-201 post-cutover smoke。
- [ ] DX-202 Blue 热备观察。
- [ ] DX-203 只有全部验收通过才退役 Blue。
- [ ] DX-204 不在本轮执行 Contract migration。

## 完成定义

任何条目只有同时具备实现、测试、真实运行证据、文档和可回滚性才能勾选。只在服务端存在但 Manifest/客户端不可发现、只在 mock 通过、只在单实例可用、或切流后旧状态失效，均视为未完成。
