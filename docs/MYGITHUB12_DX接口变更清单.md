# MyGithut12 DX-1 接口变更清单

## 1. 结论

DX-1 是兼容式扩展：

- MCP 名称保持 `MyGithut12`；
- 现有 155 个工具保留；
- 新增 4 个高层开发工具；
- 硬删除 0 个；
- 目标工具总数 159；
- 现有工具必填参数不变；
- 已 deprecated 工具继续保留；
- 只允许一次 ChatGPT Connector 工具发现更新。

## 2. 新增工具

### 2.1 `prepare_development_task`

用途：一次完成 policy、base identity、Workspace/branch、lease、Index、Instructions、Context 和 overlap。

建议参数：

| 参数 | 类型 | 必填 | 默认 |
|---|---|---:|---|
| repository | string | 是 | - |
| task_name | string | 是 | - |
| base_ref | string | 否 | `main` |
| branch | string | 否 | 自动唯一 `ai/` |
| owner | string | 否 | `chatgpt` |
| create_branch | boolean | 否 | true |
| seed_paths_json | string | 否 | `[]` |
| seed_symbols_json | string | 否 | `[]` |
| include_tests | boolean | 否 | true |
| include_docs | boolean | 否 | true |
| lease_seconds | integer | 否 | 1800 |
| context_budget_bytes | integer | 否 | 262144 |
| idempotency_key | string | 否 | 空 |

关键返回：

- development_session_id；
- workspace_id、workspace_revision；
- repository、branch、base/head/tree；
- lease、policy、index；
- instructions/context/overlap summary；
- response resource。

注解：consequential、idempotent with key、non-destructive。

### 2.2 `apply_development_change_set`

用途：基于 Session 以一个确定性 Commit 应用 Patch/Range/Upload ChangeSet。

建议参数：

| 参数 | 类型 | 必填 | 默认 |
|---|---|---:|---|
| development_session_id | string | 是 | - |
| expected_session_revision | integer | 是 | - |
| expected_workspace_revision | integer | 是 | - |
| expected_head_sha | string | 是 | - |
| change_set_json | string | 是 | - |
| commit_message | string | 是 | - |
| dry_run | boolean | 否 | true |
| idempotency_key | string | 否 | 空 |
| create_pull_request | boolean | 否 | false |
| pull_request_json | string | 否 | `{}` |

关键返回：

- dry-run canonical hash；
- Commit、Tree、old/new HEAD；
- changed files 和 Blob evidence；
- new Session/Workspace revision；
- Index warm status；
- PR summary；
- operation/trace ID。

注解：consequential、idempotent with key、destructive=false、open-world GitHub write。

### 2.3 `validate_development_task`

用途：运行 fast/full validation、复用有效证据并生成 Failure Pack。

建议参数：

| 参数 | 类型 | 必填 | 默认 |
|---|---|---:|---|
| development_session_id | string | 是 | - |
| expected_session_revision | integer | 是 | - |
| mode | string | 否 | `fast` |
| base_sha | string | 否 | Session base |
| force_rerun | boolean | 否 | false |
| supersede_previous | boolean | 否 | true |
| wait_seconds | integer | 否 | 55 |
| include_failure_pack | boolean | 否 | true |
| idempotency_key | string | 否 | 空 |

`mode`：

- `fast`
- `full`
- `reuse_or_full`

关键返回：

- resolved Commit/Tree；
- selected profile；
- affected workspaces/tests；
- job ID/status/step/revision；
- cache evidence；
- merge_eligible；
- Attestation；
- Failure Pack Resource。

注解：consequential（启动 CI）、idempotent with key、non-destructive。

### 2.4 `finalize_development_task`

用途：准备 PR、只读 readiness、受控 merge 或关闭 Session。

建议参数：

| 参数 | 类型 | 必填 | 默认 |
|---|---|---:|---|
| development_session_id | string | 是 | - |
| expected_session_revision | integer | 是 | - |
| action | string | 否 | `readiness` |
| pull_request_json | string | 否 | `{}` |
| merge_method | string | 否 | `squash` |
| required_private_ci_job_id | string | 否 | 空 |
| confirm | boolean | 否 | false |
| delete_head_branch | boolean | 否 | false |
| idempotency_key | string | 否 | 空 |

`action`：

- `prepare_pr`
- `readiness`
- `merge`
- `close`

`merge` 必须 `confirm=true`，并调用现有 Merge Gate。

## 3. 现有工具处理

### 3.1 保留且继续支持

所有当前 155 工具继续注册和测试，包括：

- GitHub/PR/Issue/Actions；
- Patch、Range、Upload；
- Workspace；
- Repository Index；
- Text/Symbol/Context/Impact；
- Private CI；
- Artifact、Attestation、Deployment；
- response resource。

### 3.2 已有 deprecated 工具

继续保留：

| 工具 | 替代 |
|---|---|
| `get_github_file` | `get_github_file_manifest` + `read_github_file_chunk` |
| `commit_github_files` | `apply_github_patch` 或 uploaded workflow |
| `get_test_deployment_logs` | `get_test_deployment_log_tail` |

DX-1 不删除它们。高层工具内部不得依赖 deprecated 接口，除非兼容测试明确覆盖。

### 3.3 不改变的安全参数

以下现有约束不得弱化：

- expected_head_sha；
- expected_blob_sha；
- workspace_id；
- expected_workspace_revision；
- idempotency_key；
- dry_run；
- confirm；
- required_private_ci_job_id；
- repository operation policy。

## 4. 新增 Capability 字段

建议新增：

- `supports_development_task_orchestration`
- `supports_development_sessions`
- `supports_local_git_mirror_reads`
- `supports_context_pack_v2`
- `supports_fast_feedback_ci`
- `supports_dependency_environment_cache`
- `supports_ci_affected_selection`
- `supports_ci_failure_pack`
- `supports_blue_green_runtime`
- `supports_runtime_generation_leader`
- `supports_cross_generation_resources`
- `tool_manifest_count=159`

Capability 字段是兼容式增加，不改变旧字段含义。

## 5. 新增稳定错误码

### Session

- `DEVELOPMENT_SESSION_NOT_FOUND`
- `DEVELOPMENT_SESSION_CLOSED`
- `DEVELOPMENT_SESSION_STATE_INVALID`
- `DEVELOPMENT_SESSION_REVISION_MISMATCH`
- `DEVELOPMENT_SESSION_WORKSPACE_MISMATCH`
- `DEVELOPMENT_SESSION_RECOVERY_REQUIRED`

### Mirror

- `MIRROR_UNAVAILABLE`
- `MIRROR_ORIGIN_MISMATCH`
- `MIRROR_FETCH_FAILED`
- `MIRROR_OBJECT_MISSING`
- `MIRROR_IDENTITY_MISMATCH`

只读场景允许带证据回退 GitHub；不能回退时失败关闭。

### CI

- `FAST_CI_NOT_MERGE_ELIGIBLE`
- `CI_PROFILE_DISCOVERY_MISMATCH`
- `CI_ENV_CACHE_INVALID`
- `CI_ENV_CACHE_BUILD_FAILED`
- `AFFECTED_SELECTION_INCOMPLETE`
- `FAILURE_PACK_UNAVAILABLE`

### Runtime

- `RUNTIME_SCHEMA_INCOMPATIBLE`
- `RUNTIME_GENERATION_NOT_READY`
- `RUNTIME_LEADER_CONFLICT`
- `CROSS_GENERATION_RESOURCE_UNAVAILABLE`
- `CROSS_GENERATION_UPLOAD_UNAVAILABLE`

所有错误必须带 trace_id、retryable、details 和不泄密 message。

## 6. Manifest 规则

目标 `docs/MYGITHUB12_TOOL_MANIFEST.json`：

```json
{
  "service_name": "MyGithut12",
  "service_version": "12.1.0",
  "legacy_tool_count": 118,
  "new_tool_count": 41,
  "tool_count": 159
}
```

`new_tools` 在当前 37 个后按固定顺序追加：

```text
prepare_development_task
apply_development_change_set
validate_development_task
finalize_development_task
```

禁止重排现有工具名称，以减少客户端 diff 和回归噪声。

## 7. Connector 发布策略

### 7.1 不改名称

- Connector 显示名保持 MyGithut12。
- MCP endpoint 保持不变。
- Auth token/headers 保持不变。
- 不新增 MyGithut13 或临时 production 名称。

### 7.2 一次发现更新

Green 通过全部验收后：

1. 保持 Blue 在线；
2. 切流 Green；
3. 验证稳定 endpoint；
4. 在 ChatGPT Connector 中刷新/重新发布工具发现；
5. 验证 159 个唯一工具；
6. 验证旧工具和 4 个新工具；
7. 若发现失败，先回切 Blue，再恢复旧 discovery。

不得在 Green 尚未稳定前更新 Connector。

### 7.3 后续冻结

`12.1.0` 发布后，`12.x` 不计划增删工具名。后续能力使用：

- 新增兼容 Capability 字段；
- 现有 JSON payload 的 schema_version；
- Resource 内容版本；
- 内部实现优化；
- 可选参数且有默认值。

## 8. 删除策略

DX-1 删除数为 0。

未来删除必须：

1. 在 Capability 和工具描述中标记 deprecated；
2. 提供等价替代；
3. 至少保留一个完整大版本周期；
4. 采集调用证据；
5. 形成 Connector 迁移计划；
6. 在新大版本中执行；
7. 不在 Blue/Green 同次发布临时删除。

## 9. 接口验收

- 运行时工具数 159；
- 名称唯一；
- Manifest 顺序完全一致；
- 现有 155 工具 schema diff 无破坏；
- 4 个新工具 annotation 正确；
- Connector 真实发现；
- 新旧客户端调用成功；
- rollback Blue 后旧 155 工具仍可用；
- Green-only 4 工具在回切期间返回明确不可用，不误路由旧版。
