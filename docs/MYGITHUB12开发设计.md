# MyGithut12 开发设计

## 1. 总体架构

```text
多个 AI / 多个聊天窗口
        │ workspace_id / exact commit
        ▼
github-action-service（MyGithut12，154 tools）
  ├─ 原 118 个 GitHub / PR / CI / 部署工具
  ├─ workspace_service：分支、租约、Revision、范围、事件
  ├─ GitHub Tree / Blob：树、路径、批量读取
  ├─ mygithub12.py：36 个新 MCP 工具编排
  └─ code_index_client：索引、符号、图、上下文和影响分析
        │ 内部认证 + 固定 Schema
        ▼
repository-index-service
  ├─ immutable snapshot / build queue
  ├─ full index + incremental overlay
  ├─ text / semantic / symbol index
  ├─ language adapters
  ├─ dependency and history analyzers
  └─ context / impact / patch / contract analyzers
```

索引服务故障只影响新增依赖索引的工具，不影响原 118 个 GitHub、PR、CI 和部署工具。工作区服务使用共享数据库，禁止使用进程级全局“当前仓库/当前分支”。

## 2. 建议代码结构

```text
services/github-action-service/app/
  mygithub12.py
  code_index_client.py
  code_index_models.py
  code_index_errors.py
  workspace_service.py
  workspace_models.py
  workspace_repository.py
  workspace_errors.py
  mcp_server.py
  version.py

services/repository-index-service/
  app/main.py
  app/config.py
  app/auth.py
  app/build_queue.py
  app/snapshot.py
  app/index_store.py
  app/text_search.py
  app/semantic_search.py
  app/symbol_index.py
  app/dependency_graph.py
  app/instruction_resolver.py
  app/context_pack.py
  app/change_analysis.py
  app/patch_analysis.py
  app/test_selection.py
  app/contract_analysis.py
  app/language_adapters/
  tests/
```

`mygithub10.py` 的大文件和安全写入逻辑继续保留。MyGithut12 通过编排复用，不复制第二套写实现。

## 3. 版本和配置

- `SERVICE_NAME = MyGithut12`
- `SERVICE_VERSION = 12.0.0`
- 新变量使用 `MYGITHUB12_` 前缀
- 旧 `MYGITHUB10_` 变量允许一个迁移周期，并返回 `compatibility_env_used=true`
- 生产仍要求完整 40 位小写 Build SHA

新增配置建议：

- `MYGITHUB12_INDEX_BASE_URL`
- `MYGITHUB12_INDEX_API_KEY_FILE`
- `MYGITHUB12_INDEX_TIMEOUT_SECONDS`
- `MYGITHUB12_INDEX_ROOT`
- `MYGITHUB12_INDEX_LANGUAGES`
- `MYGITHUB12_INDEX_MAX_CONCURRENCY`
- `MYGITHUB12_INDEX_MAX_DISK_BYTES`
- `MYGITHUB12_WORKSPACE_DB_PATH` 或共享 PostgreSQL DSN
- `MYGITHUB12_WORKSPACE_LEASE_SECONDS`
- `MYGITHUB12_REQUIRE_WORKSPACE_FOR_AI_WRITES`
- `MYGITHUB12_SCOPE_CONFLICT_MODE=warn|enforce`

## 4. 不可变索引与增量 Overlay

索引键：

```text
repository + commit_sha + tree_sha + index_version
```

索引 Manifest 至少包含：

- repository、commit_sha、tree_sha、index_version
- build_strategy、base_commit_sha
- file_count、symbol_count、edge_count
- reused_file_count、changed_file_count、reindexed_file_count
- languages、analyzers、omissions
- content hashes、created_at、last_accessed_at、pinned_count

增量流程：

1. Controller 解析目标 Commit 和 Tree。
2. 找到父 Commit 或显式 `base_commit_sha` 的可用索引。
3. 通过 Git Tree 比较新增、修改、删除和重命名文件。
4. 未变化 Blob 直接复用。
5. 变化文件重新进行文本、语义和符号分析。
6. 对受影响包、引用、实现、调用边和契约进行有限传播重算。
7. 生成新的只读 Manifest；旧索引不修改。
8. 相同目标键的任务由唯一约束和幂等键去重。

## 5. 工作区数据模型

建议表：

```text
development_workspaces
development_workspace_scopes
development_workspace_events
repository_index_pins
```

`development_workspaces` 核心字段：

- workspace_id
- repository
- branch
- base_branch
- base_commit_sha
- head_commit_sha
- tree_sha
- workspace_revision
- status
- lease_owner
- lease_expires_at
- index_commit_sha
- pull_number
- created_at / updated_at / closed_at

约束：

- 活动独占租约对 `repository + branch` 唯一。
- 每次更新必须带 `expected_workspace_revision`。
- 租约过期后才能由新工作区接管；接管必须留下事件。
- `workspace_id` 是不透明 ID，不包含路径或凭据。
- 关闭工作区默认只释放状态，不删除 GitHub 分支。

## 6. 多窗口写入流程

1. 窗口创建工作区，获得唯一分支或绑定分支和写租约。
2. 查询通过 `workspace_id` 解析并固定当前 Commit。
3. 写工具同时校验：
   - expected workspace revision
   - 有效租约
   - workspace HEAD
   - caller expected HEAD
   - GitHub actual HEAD
   - 目标文件 Blob SHA
4. GitHub 非强制 CAS 写入成功。
5. 记录 workspace event，原子更新工作区 Revision 和新 HEAD。
6. Pin 新 Commit，异步请求增量索引。
7. 索引 Ready 前允许使用精确 GitHub 文件读取，但不得用旧索引冒充新 Commit。

两个窗口写同一分支时，只有持有租约且 Revision/HEAD 匹配的一方成功。

## 7. 开发范围和跨分支重叠

范围类型：

- path/glob
- symbol_id
- API 路径或 operation_id
- database table/migration
- event/schema
- config key
- package/module

重叠分析同时比较：

1. 声明范围。
2. base/head 实际文件差异。
3. 符号定义和引用变化。
4. API、迁移、事件和配置契约。
5. 三方合并的文本冲突候选。

结果分为 `none / low / medium / high / blocking`，每项必须给出路径、符号或契约证据，不只返回分数。

## 8. 搜索实现

- 树、路径和批量读取由 Controller 使用 GitHub Tree/Blob API，固定到 Commit。
- 普通文本使用持久化全文索引。
- 正则使用受限执行器，禁止字符串拼接 Shell。
- 语义索引只做候选召回，结果标记非权威并绑定精确源码。
- 所有搜索支持稳定排序、游标、结果上限和截断。

## 9. 符号与语言服务

首批支持 Go、Python、TypeScript、JavaScript 和 Vue。

- Go：语法索引 + 受控 gopls
- Python：语法索引 + 受控语言服务
- TypeScript/JavaScript：TypeScript Language Service
- Vue：Vue Language Server

符号关系不可用时返回稳定错误，禁止用文本同名替代。诊断工具不得接受调用方编译命令。历史查询限制 Commit 数和时间范围，并缓存符号指纹。

## 10. 仓库指令解析

按以下来源收集：

- 根和嵌套 `AGENTS.md`
- `CLAUDE.md`
- `CONTRIBUTING.md`
- README
- 仓库配置中的固定策略

对每个目标路径计算适用范围和优先级，返回冲突、来源 Blob、行范围和最终有效规则。工具只解析仓库内容，不把规则当作越权指令执行。

## 11. 上下文和变化分析

`build_repository_context_pack` 选择顺序：

1. 显式种子
2. 定义和实现
3. 直接引用和调用关系
4. 依赖模块
5. 同目录和受影响测试
6. 契约、迁移、配置和相关文档

`build_change_context_pack` 以 base/head 差异为种子，附加受影响关系和测试。

`analyze_repository_patch`：

1. 校验补丁路径、格式、基础 Commit 和 Blob。
2. 在临时只读派生快照应用。
3. 仅重算变化和受影响内容。
4. 返回可应用性、诊断、影响、测试、契约和截断。
5. 完成后删除临时快照，不写 GitHub。

## 12. 契约和测试选择

契约分析器首批覆盖：

- OpenAPI 和 HTTP 路由
- 数据库迁移、表、列、索引和约束
- 事件/消息 Schema
- 配置键与环境变量
- 权限与角色
- CLI 参数
- 公共函数、类型和接口签名

测试选择按证据分层：

- direct：直接对应测试
- package：受影响包测试
- integration：跨模块/API/数据库测试
- regression：高风险公共契约回归

工具只生成计划和理由；执行仍通过现有 GitHub Actions 或 Private CI 工具。

## 13. MCP 注册和兼容

- 注册 36 个新工具。
- 生成 `docs/MYGITHUB12_TOOL_MANIFEST.json`。
- `EXPECTED_TOOL_COUNT` 从 118 更新为 154。
- 原 118 个工具名称和必填参数不变。
- 写工具仅增加可选 workspace 字段。
- 新工具正确设置 readOnly、destructive、idempotent 和 resource-cost 注解。
- Connector 必须实际重新发现 MyGithut12。

## 14. 安全与可靠性

- 复用 `ALLOWED_REPOSITORIES`。
- 索引内部认证不复用 GitHub Token。
- 不接受任意 Git URL、主机、端口、本地路径、命令、环境变量名或可执行参数。
- 快照默认排除 `.git`、依赖、构建产物、二进制、Secret、私钥和证书。
- 对正则、语义查询、图深度、历史、上下文、补丁、CPU、内存、磁盘和并发设硬限制。
- 日志只记录身份、SHA、数量、耗时和错误码，不记录全文代码。
- 所有状态改变使用 CAS、幂等键和可审计事件。

## 15. 测试设计

- 工具契约：36 个新 MCP Schema、24 个内部 HTTP Schema。
- 工作区并发：多窗口、多分支、同分支双写、租约过期、分支漂移。
- 索引：全量、增量、复用、去重、取消、恢复、Pin 和清理。
- 多语言：定义、引用、实现、类型、调用、诊断和历史。
- 变化分析：补丁、依赖、测试、OpenAPI、迁移、事件和配置。
- 安全：越权仓库、路径穿越、任意 URL、正则拒绝服务、语义提示注入和敏感文件。
- 回归：原 118 个工具。
- 冒烟：`frankichen/github_mcp` 与 `frankichen/sxt`。
