# MyGithut12 DX-1 开发设计

## 1. 总体架构

```text
ChatGPT / Codex / MCP Client
            │
            ▼
Stable endpoint: MyGithut12
            │
   ┌────────┴────────────────────────────┐
   │                                     │
DX Orchestration                   Existing 155 tools
   │                                     │
   ├─ Development Session                ├─ GitHub / PR / Issue
   ├─ Workspace + CAS                    ├─ Patch / Range / Upload
   ├─ Context Pack V2                    ├─ Repository Index
   ├─ Fast / Full CI                     ├─ Private CI
   └─ Finalize / Readiness               └─ Deploy / Artifact
            │
  ┌─────────┼─────────────┬─────────────────────┐
  │         │             │                     │
GitHub  Local Git Mirror  Index Store    Controller State DB
  │         │             │                     │
truth   immutable reads   symbols/context session/workspace/idempotency
                                              resources/uploads/leader
            │
            ▼
Private CI Controller ── WSL / Rootless Podman Agent
            │
            ├─ affected selection
            ├─ sealed dependency env cache
            ├─ repo-fast-check
            └─ repo-auto-check
```

## 2. 代码边界

建议新增：

```text
services/github-action-service/app/
  development_orchestrator.py
  development_session_models.py
  development_session_store.py
  development_failure_pack.py
  local_git_mirror.py
  runtime_generation.py

services/private-ci-agent/private_ci_agent/
  environment_cache.py
  affected_selection.py
  failure_summary.py
  leader_lease.py
```

继续复用：

- `mygithub10.py`：Patch、Range、Upload、HEAD/Blob CAS、durable verify；
- `mygithub12.py`：Index、Workspace、Context、Impact；
- `mygithub12_workspace.py`：Workspace lease/revision；
- `ci_mcp.py` / `ci_database.py`：Private CI；
- `github_utils.py`：PR readiness、conflict 和 merge gate；
- `mcp_response.py`：structuredContent、inline budget 和 Resource fallback。

高层编排不得复制底层 Git 写入和 Merge Gate。

## 3. Development Session 状态机

```text
preparing
  ├─> active
  └─> prepare_failed

active
  ├─> validating_fast
  ├─> validating_full
  ├─> pr_ready
  ├─> drifted
  ├─> blocked
  └─> closing

pr_ready
  ├─> merged
  ├─> active
  └─> closing

merged / closed / abandoned
```

建议表：

```text
development_sessions
development_session_events
development_session_validations
runtime_generations
runtime_leader_leases
```

`development_sessions` 核心字段：

- session_id、workspace_id；
- repository、branch、base_branch；
- base_commit_sha、head_commit_sha、tree_sha；
- session_revision、workspace_revision；
- status、owner、lease_expires_at；
- index_commit_sha、pull_number；
- last_fast_ci_job_id、last_full_ci_job_id；
- last_attestation_id、last_failure_resource_uri；
- created_at、updated_at、closed_at。

约束：

- 活动 Session 对 workspace_id 唯一；
- 每次更新必须带 expected_session_revision；
- Workspace revision 是写权限事实；
- Session revision 是编排状态事实；
- 事件只追加，不修改历史。

## 4. 高层工具流程

### 4.1 `prepare_development_task`

执行：

1. 校验 allowlist 和 operation policy；
2. fresh-read base ref、Commit、Tree；
3. 检查 idempotency replay；
4. 创建/绑定 Workspace 和 branch；
5. 获取 lease/revision；
6. reuse/build exact Commit Index；
7. 解析 repository/path instructions；
8. 构建 Context Pack V2；
9. 分析活动 Workspace overlap；
10. 创建 Session 和 event；
11. 返回 compact summary 和可选 Resource。

补偿：

- branch 创建成功、Workspace 失败：记录 orphan branch，不静默删除；
- Workspace 成功、Session 写失败：可用 workspace_id 幂等恢复；
- Context/Index 失败：Session 可进入 active，但明确状态；
- 所有失败返回已创建资源和下一步。

### 4.2 `apply_development_change_set`

输入采用版本化 JSON：

```json
{
  "schema_version": 1,
  "mode": "patch",
  "expected_blob_shas": {"path": "sha"},
  "patch": "...",
  "range_operations": [],
  "uploaded_files": []
}
```

规则：

- 单次只允许一种 mode；
- 归一化为 `changed[path] = bytes | delete`；
- dry-run 保存 canonical request hash；
- commit 使用相同 canonical request；
- 校验顺序：Session CAS → Workspace CAS/lease → HEAD → Blob → ChangeSet；
- Commit 后 fresh-read branch、commit、tree、blob；
- durable verify 后推进 Workspace/Session revision；
- 自动请求新 Commit incremental Index；
- Index failure 独立报告。

### 4.3 `validate_development_task`

Fast 路径：

```text
fresh session/workspace/head
        ↓
change impact + affected selection
        ↓
repo-fast-check
        ↓
compact wait/result
        ↓
failure pack on failure
```

Full 路径：

```text
fresh session/workspace/head
        ↓
repo-auto-check
        ↓
identity/evidence validation
        ↓
attestation
        ↓
session validation record
```

复用键：

```text
repository
commit_sha
tree_sha
profile
image digest
toolchain versions
dependency manifest hash
test config hash
profile version
expiry/revocation
```

### 4.4 `finalize_development_task`

- `prepare_pr`：创建或更新 Draft PR；
- `readiness`：调用现有只读 readiness；
- `merge`：调用现有安全 merge gate，要求 confirm；
- `close`：关闭 Session/Workspace，释放 lease/index pin。

## 5. Local Git Mirror

目录建议：

```text
/var/lib/mygithub12/mirrors/<owner>-<repo>.git
/var/lib/mygithub12/mirror-locks/
/var/lib/mygithub12/mirror-metadata/
```

安全要求：

- repository 必须来自 allowlist；
- authoritative remote 由服务端根据 repository 构造；
- origin 必须精确匹配预期；
- fetch 有锁、超时、prune、审计；
- exact object 读取验证 SHA；
- Mirror 可删除重建；
- Secret 不进入 argv、日志或持久化 metadata。

读取策略：

1. ref 请求先解析为 GitHub fresh Commit；
2. exact Commit/Tree/Blob 从 Mirror 读取；
3. object missing 时受控 fetch；
4. fetch 后仍缺失则回退 GitHub API；
5. response 返回 source=`mirror|github_fallback`、fetch generation 和 SHA evidence。

写入仍使用现有 GitHub API/durable verify，不通过 Mirror push。

## 6. Context Pack V2

候选来源：

1. 显式 seed path/symbol；
2. 定义和实现；
3. callers/callees/references；
4. dependency modules；
5. changed files 和 affected tests；
6. contract、migration、config；
7. instructions 和相关 docs。

默认 inline 结构：

```json
{
  "task_summary": "...",
  "identity": {"commit_sha": "...", "tree_sha": "..."},
  "items": [
    {
      "kind": "symbol",
      "path": "...",
      "symbol_id": "...",
      "start_line": 1,
      "end_line": 20,
      "score": 0.91,
      "reason": "...",
      "authoritative": true
    }
  ],
  "omitted": {...},
  "resource_uri": "..."
}
```

不默认嵌入完整文件。用户明确扩展时再读取 Resource 或 file chunk。

## 7. Private CI 优化

### 7.1 Profile 发现一致性

`repo-fast-check` 必须同时存在于：

- Controller profile registry；
- repository allowlist config；
- Agent supported profiles；
- deploy profile；
- `list_private_ci_profiles`；
- tests 和 docs。

任何一处不一致均阻止发布。

### 7.2 Dependency Environment Cache

缓存 key：

```text
stack
runtime version
image digest
workspace relative path
requirements/lockfile hashes
profile version
bootstrap command version
```

生命周期：

```text
miss
  ↓
build in isolated temp
  ↓
verify toolchain/imports
  ↓
seal read-only
  ↓
publish atomically
  ↓
reuse
```

并发相同 key 只构建一次。无效缓存隔离后重建，不能原地修补。

### 7.3 Affected Selection

输入：

- base/head changed files；
- repository dependency graph；
- contract change；
- workspace config；
- repository conservative rules。

输出：

- selected workspaces/tests；
- selection reasons；
- completeness；
- fallback-to-full 原因。

`incomplete=true` 时不得得到空集合。

### 7.4 Failure Pack

由 Controller 生成 compact summary，完整证据写 Resource。生成 Failure Pack 失败不能覆盖原 CI failure。

## 8. Response Contract

高层工具统一：

- inline 为 compact；
- 详情按 Resource 读取；
- 所有响应带 response_meta；
- list 带 total/truncated；
- 每个状态改变带 operation_id、trace_id；
- 大字段默认移除；
- `include_details=false` 不得返回 steps command、完整日志或 changed content。

## 9. Blue/Green 运行代际

每个实例有：

- generation_id；
- build_sha；
- schema compatibility range；
- role=`active|standby|draining`；
- leader lease status；
- started_at/readiness。

共享或兼容状态：

- Controller DB；
- idempotency/audit；
- Workspace/Session；
- CI jobs；
- Index metadata/pins；
- response resource；
- upload staging。

推荐 Resource/Upload 方案：

1. 优先使用共享持久化目录，文件名包含随机 ID 和 generation；
2. metadata 写共享 DB；
3. cleanup 只由 leader 执行；
4. Blue/Green 均可按 ID 读取；
5. 文件创建使用 0600、原子 rename 和 SHA 校验。

## 10. Migration 策略

Expand 阶段允许：

- 新表；
- nullable 新列；
- 有安全默认值的新列；
- 新索引；
- 新事件类型。

禁止：

- drop/rename；
- 改变已有字段含义；
- 无默认值 not-null；
- 批量不可逆 rewrite；
- 旧版无法解析的同字段格式替换。

Contract 阶段必须独立任务、独立版本、独立备份和回滚计划。

## 11. 可观测性

Metrics：

- 高层 tool latency/outcome；
- 底层调用数；
- Mirror hit/miss/fallback；
- Index reuse/warm；
- CI queue/bootstrap/test；
- env cache hit/miss；
- affected selection；
- Session conflict/drift；
- generation/leader；
- Resource/Upload continuity；
- rollback reason。

日志只记录身份、SHA、数量、耗时和错误码，不记录 Secret 或大段源码。

## 12. 实施顺序

1. 修复已知可靠性和 profile discovery。
2. 建 Session store 和 4 个高层工具骨架。
3. 接入现有 Workspace/CAS/Index/CI。
4. Local Git Mirror read path。
5. Context Pack V2 和 compact response。
6. Environment cache 和 affected selection。
7. Failure Pack。
8. Blue/Green generation、leader、resource/upload continuity。
9. 全量回归、真实性能和 Connector 发现。
10. Green 发布、切流、热备和回滚验收。
