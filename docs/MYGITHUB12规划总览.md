# MyGithut12 规划总览与接口统计

## 1. 文档状态

- 当前现网：`MyGithut11`，服务版本 `10.1.1`。
- 目标版本：`MyGithut12`，服务版本 `12.0.0`。
- 本文是需求和开发基线，不表示代码已经实现或部署。
- 基线 Commit：`31c2f3923c53e5e557c61b82cee2aa99a23b4d5c`。

升级到新 MCP 命名空间的原因是客户端只能识别已经注册和发现的工具。仅在旧服务中增加 Python 函数，并不能保证 AI 客户端自动得到新接口；发布时必须同时更新服务名、工具清单、Connector 注册和部署配置。

## 2. 版本范围统计

| 项目 | 数量 | 说明 |
|---|---:|---|
| MyGithut11 已有 MCP 工具 | 118 | 保持名称和 Schema 兼容 |
| MyGithut12 新增 MCP 工具 | 12 | 本次代码检索和索引能力 |
| 删除工具 | 0 | 不删除旧工具 |
| 目标工具总数 | 130 | `test_tool_manifest.py` 必须固定验证 |
| 新增内部索引接口 | 9 | Controller 与索引服务之间使用 |

## 3. 新增 MCP 工具总表

### A. 索引生命周期，3 个

1. `get_repository_index_status`
   - 用途：查询指定仓库和 Commit 的索引状态。
   - 必填：`repository`。
   - 可选：`commit_sha`、`ref`。二者同时出现时必须一致。
   - 返回：`resolved_commit_sha`、`tree_sha`、`status`、`index_version`、语言、文件数、符号数、构建时间和失败原因。

2. `request_repository_index_build`
   - 用途：为精确 Commit 请求创建或复用索引任务。
   - 必填：`repository`、`commit_sha`。
   - 可选：`force`、`priority`、`idempotency_key`。
   - 返回：`job_id`、`status`、`deduplicated`、目标 Commit 和 Tree SHA。

3. `get_repository_index_job`
   - 用途：查询索引任务的队列、阶段、进度和终态。
   - 必填：`job_id`。
   - 返回：任务状态、当前步骤、文件进度、错误码、开始和结束时间。

### B. 仓库快照与文件读取，3 个

4. `list_repository_tree`
   - 用途：一次读取精确 Commit 的受限递归目录树。
   - 必填：`repository`、`commit_sha`。
   - 可选：`path`、`max_depth`、`include_globs_json`、`exclude_globs_json`、`limit`、`cursor`。
   - 返回：路径、类型、Blob/Tree SHA、大小、语言和分页游标。

5. `search_repository_files`
   - 用途：按文件名和路径搜索，不搜索文件内容。
   - 必填：`repository`、`commit_sha`、`query`。
   - 可选：`path_prefix`、`extensions_json`、`limit`、`cursor`。
   - 返回：匹配路径、Blob SHA、大小和匹配位置。

6. `get_github_files_batch`
   - 用途：批量读取一组已知路径，减少连续工具调用。
   - 必填：`repository`、`commit_sha`、`paths_json`。
   - 可选：`include_content`、`max_total_bytes`。
   - 返回：每个文件的 Blob SHA、内容 SHA256、字节数、是否截断和内容；单个文件失败不得掩盖其他结果。

### C. 文本和符号关系，5 个

7. `search_repository_text`
   - 用途：在精确 Commit 上进行文字或正则搜索。
   - 必填：`repository`、`commit_sha`、`query`。
   - 可选：`regex`、`case_sensitive`、`path_globs_json`、`context_lines`、`limit`、`cursor`。
   - 返回：路径、Blob SHA、行范围、片段、匹配区间和分页信息。

8. `search_repository_symbols`
   - 用途：搜索函数、方法、类型、接口、变量和常量。
   - 必填：`repository`、`commit_sha`、`query`。
   - 可选：`kinds_json`、`languages_json`、`path_prefix`、`limit`、`cursor`。
   - 返回：稳定 `symbol_id`、限定名、种类、语言、路径和定义范围。

9. `get_symbol_definition`
   - 用途：通过 `symbol_id` 或文件位置获取定义。
   - 必填：`repository`、`commit_sha`，以及 `symbol_id` 或 `path + line + column`。
   - 返回：唯一或候选定义、签名、文档片段和 Blob SHA。

10. `find_symbol_references`
    - 用途：查找指定符号的引用。
    - 必填：`repository`、`commit_sha`、`symbol_id`。
    - 可选：`include_definition`、`limit`、`cursor`。
    - 返回：引用类型、路径、行列、片段、Blob SHA 和分页信息。

11. `get_symbol_call_hierarchy`
    - 用途：读取调用者、被调用者或双向调用层级。
    - 必填：`repository`、`commit_sha`、`symbol_id`。
    - 可选：`direction`、`depth`、`limit`。
    - 返回：节点、边、调用位置、是否截断和不支持原因。

### D. 上下文组装，1 个

12. `build_repository_context_pack`
    - 用途：根据任务描述、种子路径和种子符号生成可审计的最小代码上下文包。
    - 必填：`repository`、`commit_sha`、`task`。
    - 可选：`seed_paths_json`、`seed_symbols_json`、`max_files`、`max_total_bytes`、`include_tests`、`include_docs`。
    - 返回：所选文件和片段、选择理由、符号关系、总字节数、遗漏原因和精确 SHA。

## 4. 内部索引服务接口，9 个

- `POST /v1/index/builds`
- `GET /v1/index/builds/{job_id}`
- `GET /v1/index/repositories/{repository}/commits/{commit_sha}/status`
- `POST /v1/search/text`
- `POST /v1/search/symbols`
- `POST /v1/symbols/definition`
- `POST /v1/symbols/references`
- `POST /v1/symbols/call-hierarchy`
- `POST /v1/context-packs`

目录树、文件名搜索和批量文件读取优先由 Controller 使用 GitHub Tree/Blob API 完成，不要求索引服务重复代理。

## 5. Capability 新增字段

`get_mygithub_capabilities` 继续保留原工具名，新增：

- `supports_repository_tree_snapshot`
- `supports_repository_file_search`
- `supports_repository_text_search`
- `supports_batch_file_read`
- `supports_repository_index_jobs`
- `supports_repository_symbol_index`
- `supports_symbol_definition`
- `supports_symbol_references`
- `supports_symbol_call_hierarchy`
- `supports_repository_context_pack`
- `supported_index_languages`
- `repository_index_version`

## 6. 稳定错误码

必须稳定返回并附带 `trace_id`：

- `INDEX_NOT_FOUND`
- `INDEX_NOT_READY`
- `INDEX_BUILD_IN_PROGRESS`
- `INDEX_BUILD_FAILED`
- `INDEX_STALE`
- `INDEX_COMMIT_MISMATCH`
- `INDEX_QUOTA_EXCEEDED`
- `INDEX_SNAPSHOT_UNAVAILABLE`
- `SEARCH_QUERY_INVALID`
- `SEARCH_REGEX_INVALID`
- `SEARCH_SCOPE_EXCEEDED`
- `BATCH_FILE_LIMIT_EXCEEDED`
- `BATCH_TOTAL_BYTES_EXCEEDED`
- `SYMBOL_NOT_FOUND`
- `SYMBOL_AMBIGUOUS`
- `SYMBOL_LANGUAGE_UNSUPPORTED`
- `SYMBOL_RELATION_UNAVAILABLE`
- `CONTEXT_PACK_EMPTY`
- `CONTEXT_PACK_LIMIT_EXCEEDED`

## 7. 发布边界

发布 MyGithut12 必须同时完成：服务名和版本、环境变量、130 工具 Manifest、Connector 注册、容器镜像、部署配置、回归测试和文档。只改 `SERVICE_NAME` 或只增加工具函数均不算完成。
