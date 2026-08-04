# MyGithut12 规划总览与接口统计

## 1. 文档状态

- 当前现网：`MyGithut11`，服务版本 `10.1.1`。
- 目标版本：`MyGithut12`，服务版本 `12.0.0`。
- 本文是 MyGithut12 的权威接口基线，不表示代码已经实现或部署。
- 源码基线：`frankichen/github_mcp` 的 MyGithut11 主线。
- 设计原则：接口尽量完整，但不暴露任意 Shell、任意 Git URL、任意主机路径、任意 SQL 或明文 Secret。

升级命名空间的原因是客户端只能识别实际注册和发现的工具。新增 Python 函数但不更新服务名、Manifest、Connector 和部署配置，不能算 MyGithut12。

## 2. 版本范围统计

| 项目 | 数量 | 说明 |
|---|---:|---|
| MyGithut11 已有 MCP 工具 | 118 | 名称和必填参数保持兼容 |
| MyGithut12 新增 MCP 工具 | 36 | 索引、工作区、搜索、符号和变更分析 |
| 删除工具 | 0 | 不删除旧工具 |
| 目标工具总数 | 154 | 运行时 Manifest 必须固定验证 |
| 新增索引服务内部 HTTP 接口 | 24 | Controller 与 repository-index-service 之间使用 |
| Controller 工作区服务能力 | 9 | 使用共享数据库，不依赖进程内全局状态 |

## 3. 新增 MCP 工具总表

### A. 索引生命周期，6 个

1. `get_repository_index_status`
   - 查询精确 Commit 的索引、Tree、语言、文件、符号、构建策略和复用统计。
2. `request_repository_index_build`
   - 请求全量或增量构建；支持 `auto / incremental / full`、`base_commit_sha`、优先级和幂等键。
3. `get_repository_index_job`
   - 查询任务队列、阶段、进度、复用文件数、重建文件数和稳定错误码。
4. `wait_repository_index_job`
   - 最长 55 秒长轮询，按状态、步骤或日志修订变化返回，避免高频轮询。
5. `cancel_repository_index_job`
   - 安全取消排队任务或请求在构建边界停止；不得删除已完成的不可变索引。
6. `list_repository_indexes`
   - 分页列出仓库已有索引、活动工作区引用、最后访问时间、大小和过期状态；不返回源码内容。

### B. 多聊天窗口开发工作区，9 个

7. `create_development_workspace`
   - 从精确基础 Commit 创建唯一工作区；可创建唯一 `ai/` 分支或绑定已有分支；返回 `workspace_id`、分支、HEAD、Tree、租约和索引状态。
8. `get_development_workspace`
   - 返回工作区 Revision、基础分支、基础 Commit、当前 HEAD、索引、租约、漂移状态、PR 和 CI 摘要。
9. `list_development_workspaces`
   - 按仓库、状态、分支和拥有者分页查询活动工作区，支持多窗口可视化。
10. `renew_development_workspace_lease`
    - 通过 `workspace_id + expected_workspace_revision` 延长独占写租约；不能覆盖其他工作区的有效租约。
11. `refresh_development_workspace`
    - 重新读取 GitHub 分支 HEAD；检测外部推送、强制移动、删除或 PR 状态变化，并以 CAS 更新工作区。
12. `close_development_workspace`
    - 关闭工作区、释放租约和索引 Pin；默认不删除分支、不关闭 PR、不合并代码。
13. `declare_development_scope`
    - 声明计划修改的路径、符号、API、表、迁移或配置；默认是可审计的软占用，可由仓库策略启用独占范围。
14. `analyze_development_workspace_overlap`
    - 比较活动工作区的声明范围、实际变更文件、符号、契约和迁移，返回冲突等级及证据。
15. `plan_development_workspace_sync`
    - 比较工作区基础 Commit、当前基础分支和工作区 HEAD，规划 merge/rebase/update-branch 风险；只做分析，不移动分支。

### C. 仓库导航和搜索，5 个

16. `list_repository_tree`
    - 读取精确 Commit 的受限递归树，支持深度、Glob、数量和游标。
17. `search_repository_files`
    - 仅按文件名和路径搜索，返回 Blob SHA、大小、语言和匹配区间。
18. `get_github_files_batch`
    - 批量读取明确路径，逐文件返回状态、Blob SHA、内容 SHA256、截断和总字节统计。
19. `search_repository_text`
    - 普通文本或受限正则搜索，支持大小写、路径、上下文、分页和精确行范围。
20. `search_repository_semantic`
    - 根据自然语言检索候选代码和文档；每条结果必须绑定路径、Blob SHA、行范围和评分，并明确 `authoritative=false`。不得替代真实定义、引用或调用关系。

### D. 符号和语言智能，8 个

21. `search_repository_symbols`
    - 搜索函数、方法、类型、接口、变量和常量，返回稳定 `symbol_id`。
22. `get_symbol_definition`
    - 通过 `symbol_id` 或文件位置获取唯一或候选定义、签名、文档和 Blob SHA。
23. `find_symbol_references`
    - 查询读、写、调用、实现、类型和未知引用；不可靠时不得猜测。
24. `get_symbol_call_hierarchy`
    - 查询 callers、callees 或双向调用图，处理循环、深度、节点和边限制。
25. `get_symbol_implementations`
    - 查询接口、抽象类型、协议、Trait 或基类的实现，返回语言适配器证据。
26. `get_symbol_type_hierarchy`
    - 查询父类型、子类型、嵌入、继承和组合关系。
27. `get_symbol_diagnostics`
    - 返回精确 Commit 上与符号或文件相关的解析、类型和语言服务诊断；只返回受控诊断，不执行调用方命令。
28. `get_symbol_history`
    - 在受限提交范围内追踪符号的创建、重命名、签名变化和删除；每个事件绑定 Commit 和文件证据。

### E. 架构、上下文和变更分析，8 个

29. `get_repository_dependency_graph`
    - 返回包、模块或目标符号的依赖图，区分 import、调用、实现和生成关系。
30. `get_repository_agent_instructions`
    - 解析 `AGENTS.md`、`CLAUDE.md`、`CONTRIBUTING.md`、README 和路径级规则，返回适用于目标文件的优先级与证据。
31. `build_repository_context_pack`
    - 根据任务、种子路径和符号生成可审计的最小上下文包，记录每个选择理由。
32. `build_change_context_pack`
    - 根据 base/head Commit 或 PR 生成变化文件、依赖、调用方、测试、契约和相关文档上下文。
33. `analyze_repository_change_impact`
    - 基于两个精确 Commit 分析受影响符号、模块、API、数据表、配置、测试和风险，不输出无证据结论。
34. `analyze_repository_patch`
    - 在不写 GitHub 的情况下把补丁应用到精确基础 Commit 的临时快照，返回可应用性、诊断、影响、受影响测试和契约变化。
35. `get_affected_tests`
    - 根据 Commit 差异、补丁、文件或符号选择直接测试、包测试、集成测试和回归候选，并说明选择原因。
36. `detect_repository_contract_changes`
    - 检测 API/OpenAPI、数据库迁移、事件 Schema、配置、权限、CLI 和公共符号契约变化，区分 breaking、compatible 和 unknown。

## 4. 现有写接口与工作区集成

不新增一套重复的文件写工具。以下现有写工具增加可选的 `workspace_id` 和 `expected_workspace_revision`：

- `apply_github_patch`
- `edit_github_file_ranges`
- `commit_github_uploaded_files`
- `commit_github_files` 兼容期
- PR、Review、CI 等需要绑定工作区的操作

仓库策略可启用 `REQUIRE_WORKSPACE_FOR_AI_WRITES=true`。启用后：

1. AI 分支写入必须属于活动工作区。
2. 工作区必须持有有效写租约。
3. `expected_head_sha`、工作区 HEAD 和 GitHub 实际 HEAD 必须三者一致。
4. 写成功后原子更新工作区 Revision，并为新 Commit 请求增量索引。
5. 分支被外部移动时返回 `WORKSPACE_BRANCH_DRIFTED`，不得自动覆盖。

## 5. 索引键、复用和多窗口隔离

不可变索引键：

```text
repository + commit_sha + tree_sha + index_version
```

- 分支名不是索引身份。
- 多个分支指向相同 Commit 时共享同一索引。
- 分支提交后创建新 Commit 索引，不原地修改旧索引。
- 增量索引使用父 Commit 索引作为 Base，并以变化文件和受影响关系形成 Overlay。
- 相同目标 Commit 的并发构建自动去重。
- 工作区 Pin 防止活跃索引被清理。
- 不得保存进程级 `current_repository` 或 `current_branch`。

## 6. 内部索引服务接口，24 个

### 索引任务和状态

- `POST /v1/index/builds`
- `GET /v1/index/builds/{job_id}`
- `GET /v1/index/builds/{job_id}/wait`
- `POST /v1/index/builds/{job_id}/cancel`
- `GET /v1/index/repositories/{repository}/indexes`
- `GET /v1/index/repositories/{repository}/commits/{commit_sha}/status`

### 搜索和符号

- `POST /v1/search/text`
- `POST /v1/search/semantic`
- `POST /v1/search/symbols`
- `POST /v1/symbols/definition`
- `POST /v1/symbols/references`
- `POST /v1/symbols/call-hierarchy`
- `POST /v1/symbols/implementations`
- `POST /v1/symbols/type-hierarchy`
- `POST /v1/symbols/diagnostics`
- `POST /v1/symbols/history`

### 架构、上下文和变更分析

- `POST /v1/graphs/dependencies`
- `POST /v1/instructions/resolve`
- `POST /v1/context-packs/repository`
- `POST /v1/context-packs/change`
- `POST /v1/analysis/change-impact`
- `POST /v1/analysis/patch`
- `POST /v1/analysis/affected-tests`
- `POST /v1/analysis/contract-changes`

目录树、文件名搜索和批量读取优先由 Controller 使用 GitHub Tree/Blob API 完成。工作区状态由 Controller 的共享数据库管理，不使用索引服务保存聊天状态。

## 7. Capability 新增字段

`get_mygithub_capabilities` 增加：

- `supports_repository_index_jobs`
- `supports_incremental_repository_index`
- `supports_index_job_wait`
- `supports_development_workspaces`
- `supports_workspace_write_lease`
- `supports_workspace_scope_overlap`
- `supports_repository_tree_snapshot`
- `supports_repository_file_search`
- `supports_repository_text_search`
- `supports_repository_semantic_search`
- `supports_batch_file_read`
- `supports_repository_symbol_index`
- `supports_symbol_definition`
- `supports_symbol_references`
- `supports_symbol_call_hierarchy`
- `supports_symbol_implementations`
- `supports_symbol_type_hierarchy`
- `supports_symbol_diagnostics`
- `supports_symbol_history`
- `supports_repository_dependency_graph`
- `supports_repository_agent_instructions`
- `supports_repository_context_pack`
- `supports_change_context_pack`
- `supports_change_impact_analysis`
- `supports_patch_analysis`
- `supports_affected_test_selection`
- `supports_contract_change_detection`
- `supported_index_languages`
- `repository_index_version`
- `tool_manifest_count=154`

## 8. 稳定错误码

除既有错误码外，必须稳定返回并附带 `trace_id`：

- 索引：`INDEX_NOT_FOUND`、`INDEX_NOT_READY`、`INDEX_BUILD_IN_PROGRESS`、`INDEX_BUILD_FAILED`、`INDEX_BUILD_CANCELLED`、`INDEX_STALE`、`INDEX_COMMIT_MISMATCH`、`INDEX_BASE_UNAVAILABLE`、`INDEX_QUOTA_EXCEEDED`。
- 工作区：`WORKSPACE_NOT_FOUND`、`WORKSPACE_CLOSED`、`WORKSPACE_REVISION_MISMATCH`、`WORKSPACE_LEASE_REQUIRED`、`WORKSPACE_LEASE_CONFLICT`、`WORKSPACE_BRANCH_DRIFTED`、`WORKSPACE_SCOPE_CONFLICT`。
- 搜索：`SEARCH_QUERY_INVALID`、`SEARCH_REGEX_INVALID`、`SEARCH_SCOPE_EXCEEDED`、`SEMANTIC_INDEX_UNAVAILABLE`。
- 批量读取：`BATCH_FILE_LIMIT_EXCEEDED`、`BATCH_TOTAL_BYTES_EXCEEDED`。
- 符号：`SYMBOL_NOT_FOUND`、`SYMBOL_AMBIGUOUS`、`SYMBOL_LANGUAGE_UNSUPPORTED`、`SYMBOL_RELATION_UNAVAILABLE`、`SYMBOL_HISTORY_LIMIT_EXCEEDED`。
- 分析：`CONTEXT_PACK_EMPTY`、`CONTEXT_PACK_LIMIT_EXCEEDED`、`PATCH_ANALYSIS_INVALID`、`IMPACT_ANALYSIS_INCOMPLETE`、`CONTRACT_ANALYZER_UNAVAILABLE`。

## 9. 发布边界

发布 MyGithut12 必须同时完成：服务名与版本、154 工具 Manifest、36 个新工具、24 个内部接口、工作区共享存储、增量索引、Connector 注册、容器镜像、部署配置、回归测试、并发验收和文档。只修改名称、只增加函数或只在服务端可见均不算完成。
