# MyGithut12 开发设计

## 1. 总体架构

```text
AI / MCP Client
        │ MyGithut12，130 tools
        ▼
github-action-service
  ├─ 原 118 个 GitHub/CI 工具
  ├─ GitHub Tree/Blob：树、文件名搜索、批量读取
  └─ code_index_client：文本、符号、引用、调用层级、上下文包
        │ 内部认证，固定 Schema
        ▼
repository-index-service
  ├─ build queue / immutable snapshot
  ├─ file and text index
  ├─ symbol index
  ├─ language relation adapters
  └─ context pack planner
```

索引服务必须独立运行。它故障时只影响 9 个依赖索引的内部接口和对应 MCP 工具，不影响现有 GitHub、PR、CI 与部署工具。

## 2. 建议代码结构

```text
services/github-action-service/app/
  mygithub12.py
  code_index_client.py
  code_index_models.py
  code_index_errors.py
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
  app/symbol_index.py
  app/language_adapters/
  app/context_pack.py
  tests/
```

`mygithub10.py` 的大文件和安全写入逻辑继续保留，不与索引代码混合。

## 3. 版本和配置

- `SERVICE_NAME = MyGithut12`。
- `SERVICE_VERSION = 12.0.0`。
- 新环境变量使用 `MYGITHUB12_` 前缀。
- `MYGITHUB10_BUILD_SHA`、`MYGITHUB10_RUNTIME_MODE` 等旧变量允许一个迁移周期回退，并在 capability 中返回 `compatibility_env_used=true`。
- 生产环境仍要求完整 40 位小写 Build SHA。

新增配置建议：

- `MYGITHUB12_INDEX_BASE_URL`
- `MYGITHUB12_INDEX_API_KEY_FILE`
- `MYGITHUB12_INDEX_TIMEOUT_SECONDS`
- `MYGITHUB12_INDEX_MAX_FILES`
- `MYGITHUB12_INDEX_MAX_BYTES`
- `MYGITHUB12_INDEX_ROOT`，仅服务端配置，不暴露为 MCP 参数
- `MYGITHUB12_INDEX_LANGUAGES`

## 4. 索引键和一致性

不可变索引键：

```text
repository + commit_sha + tree_sha + index_version
```

请求流程：

1. Controller 通过 GitHub 解析 Commit 和 Tree SHA。
2. 查询索引状态时同时传入两者。
3. 索引服务只返回完全匹配的索引。
4. 只有 ref 变化但 Commit 未变化时允许复用。
5. Commit 或 Tree 不一致时失败关闭，返回 `INDEX_COMMIT_MISMATCH` 或 `INDEX_STALE`。

## 5. 快照与存储

- 只允许从已授权 GitHub 仓库和精确 Commit 构建。
- 快照目录由服务端根据哈希生成，调用方不能提供路径。
- 快照只读，索引完成后写入 Manifest 和校验值。
- 默认排除 `.git`、`node_modules`、`vendor`、构建目录、二进制、超大文件、`.env`、私钥和证书；`.env.example` 可保留。
- 索引可删除和重建，但源码事实仍来自 GitHub。

建议索引内容：

- `manifest.json`：Commit、Tree、索引版本、语言、文件统计和校验值。
- `files.sqlite`：路径、Blob SHA、语言、大小和文本搜索索引。
- `symbols.sqlite`：符号、定义范围、限定名和关系缓存。
- 只读源码快照：用于正则搜索和语言服务查询。

## 6. 搜索实现

### 6.1 树、文件名和批量读取

由 Controller 直接使用 GitHub Git Tree 和 Blob API，结果固定到 Commit。树请求必须限制深度、数量和分页；批量读取限制路径数及总字节。

### 6.2 文本搜索

普通文本优先使用持久化全文索引；正则搜索使用受限、超时和资源限制的本地搜索适配器。禁止把调用方字符串拼接成 Shell。

### 6.3 符号索引

使用语法解析器建立语言无关符号目录，首批支持 Go、Python、TypeScript、JavaScript 和 Vue。符号记录包含：

- `symbol_id`
- 名称和限定名
- symbol kind
- 语言
- 文件、Blob SHA、起止行列
- 签名和父级符号

### 6.4 定义、引用和调用层级

采用语言适配器：Go 使用 gopls，Python 使用受控语言服务，TypeScript/JavaScript 使用 TypeScript Language Service，Vue 使用 Vue Language Server。适配器不可用或语言不支持时返回稳定错误，不使用文本相似度伪造引用。

### 6.5 上下文包

上下文包按以下顺序选择：显式种子、定义、直接引用、调用者/被调用者、同目录测试、接口契约和相关文档。每个选择都记录 `reason`，达到文件数或字节上限后停止并返回 omitted 列表。

## 7. MCP 注册

- 在 `mcp_server.py` 注册 12 个新工具。
- 工具名严格使用规划总览中的名称。
- 所有工具加 read-only 注解；索引构建虽改变缓存，但不改变 GitHub，仍需标明有资源消耗。
- 更新 `docs/MYGITHUB12_TOOL_MANIFEST.json`。
- 将 `EXPECTED_TOOL_COUNT` 从 118 更新为 130。
- capability 返回新增字段和支持语言。

## 8. 安全设计

- 复用 `ALLOWED_REPOSITORIES`。
- 内部索引接口使用单独 API Key 文件或双向 TLS，不与 GitHub Token 共用。
- 不接受任意 Git URL、主机、端口、本地路径、命令、环境变量名或可执行参数。
- 正则、深度、结果数、文件数、字节数、构建并发和磁盘占用都有硬限制。
- 日志只记录仓库、Commit、工具、耗时、数量和错误码，不记录文件全文。

## 9. 错误和降级

- 索引未建立：返回 `INDEX_NOT_FOUND`，附带可执行的构建建议。
- 正在构建：返回 `INDEX_BUILD_IN_PROGRESS` 和 `job_id`。
- 不支持语言关系：返回 `SYMBOL_LANGUAGE_UNSUPPORTED` 或 `SYMBOL_RELATION_UNAVAILABLE`。
- 结果超过限制：返回成功结果、`truncated=true` 和下一页游标，不静默丢失。
- 索引服务不可用：只降级新工具，原工具照常工作。

## 10. 测试设计

- 单元测试：输入校验、游标、限制、错误映射、符号 ID、上下文选择。
- 契约测试：12 个 MCP Schema 和 9 个内部 HTTP Schema。
- 集成测试：创建固定 Fixture 仓库，验证定义、引用、调用层级和同名符号。
- 回归测试：原 118 个工具 Manifest 和行为不变。
- 安全测试：越权仓库、路径穿越、任意 URL、正则拒绝服务、超大仓库和敏感文件排除。
- 冒烟测试：`frankichen/github_mcp` 和 `frankichen/sxt` 的精确 Commit 查询。
