# MyGithut12 验收清单

## 1. 版本与发现

- [ ] `get_mygithub_capabilities.name` 精确等于 `MyGithut12`。
- [ ] 版本精确等于 `12.0.0`。
- [ ] Build SHA 为当前部署源码的完整 40 位小写 Commit SHA。
- [ ] MCP Connector 界面实际显示 `MyGithut12`，不是只修改服务内部常量。
- [ ] 客户端实际发现 130 个唯一工具。
- [ ] 新增 12 个工具名称与规划总览完全一致。
- [ ] 原 118 个工具仍可发现，名称和必填参数没有破坏性变化。
- [ ] `docs/MYGITHUB12_TOOL_MANIFEST.json` 与运行时 `list_tools` 一致。

## 2. 精确 Commit 一致性

- [ ] 每个新工具返回 `resolved_commit_sha` 和 `tree_sha`。
- [ ] 文件结果返回 `blob_sha`，文本片段返回明确行范围。
- [ ] ref 与 Commit 不一致时返回 `INDEX_COMMIT_MISMATCH`。
- [ ] 索引落后时返回 `INDEX_STALE`，不得使用旧结果。
- [ ] 同一 Commit 重复查询结果顺序稳定，分页游标可复现。

## 3. 索引生命周期

- [ ] 未建立索引返回 `INDEX_NOT_FOUND`。
- [ ] 相同仓库和 Commit 的并发构建只生成一个有效任务。
- [ ] `request_repository_index_build` 支持幂等键。
- [ ] 构建中可以查询当前步骤和进度。
- [ ] 构建失败返回稳定错误码和可审计原因。
- [ ] 构建成功的 Manifest 包含仓库、Commit、Tree、索引版本和统计。
- [ ] 配额、过期清理和失败任务回收有效。

## 4. 树、文件和文本搜索

- [ ] `list_repository_tree` 正确处理路径、深度、过滤、限制和游标。
- [ ] `search_repository_files` 只搜索路径，不混入文件内容结果。
- [ ] `get_github_files_batch` 对部分失败返回逐文件状态。
- [ ] 批量读取超过文件数或总字节限制时返回稳定错误码。
- [ ] `search_repository_text` 支持普通文本、大小写和路径过滤。
- [ ] 合法正则可执行，非法或高风险正则被拒绝或超时终止。
- [ ] 文本结果包含匹配区间、上下文、Blob SHA 和分页信息。

## 5. 符号、定义和调用关系

- [ ] 五种首批语言均能建立符号目录。
- [ ] 同名方法在不同包、类型或文件中拥有不同 `symbol_id`。
- [ ] `get_symbol_definition` 支持 symbol_id 和文件位置两种入口。
- [ ] `find_symbol_references` 不把普通文本同名误报为可靠引用。
- [ ] `get_symbol_call_hierarchy` 支持 callers、callees 和 both。
- [ ] 循环调用不会无限展开，并有循环或已访问标记。
- [ ] 不支持的语言明确返回 `SYMBOL_LANGUAGE_UNSUPPORTED`。
- [ ] 语言服务不可用时返回 `SYMBOL_RELATION_UNAVAILABLE`，不伪造结果。

## 6. 上下文包

- [ ] 每个选中的文件或片段都有 `reason`。
- [ ] 优先包含显式种子、定义、直接引用、调用关系和测试。
- [ ] 严格遵守 `max_files` 和 `max_total_bytes`。
- [ ] 截断时返回 omitted 项和原因。
- [ ] 上下文包中所有文件都属于请求的精确 Commit。

## 7. 安全

- [ ] 未授权仓库全部拒绝。
- [ ] 参数中不能注入任意 Git URL、主机、本地路径或 Shell。
- [ ] 路径穿越、绝对路径、控制字符和符号链接逃逸被拒绝。
- [ ] `.env`、私钥、证书、Token 文件和二进制默认不进入索引。
- [ ] 日志和 Metrics 不包含文件全文、凭据或 Authorization Header。
- [ ] 内部索引 API 使用独立认证，不能复用外部 MCP API Key 明文。
- [ ] 正则、并发、CPU、内存、磁盘和查询时长存在硬限制。

## 8. 性能与稳定性

在约 50 MiB 的 `frankichen/sxt` 或等价仓库热索引上：

- [ ] 索引状态和文件路径搜索 P95 小于 1 秒。
- [ ] 文本与符号搜索 P95 小于 3 秒。
- [ ] 定义和引用查询 P95 小于 5 秒。
- [ ] 上下文包 P95 小于 10 秒。
- [ ] 查询超时不会拖垮 Controller。
- [ ] 索引服务停止时，原 118 个 GitHub/CI 工具仍正常。

## 9. 测试与发布证据

- [ ] `pytest`、Ruff 和现有 CI 全部通过。
- [ ] 工具总数测试固定为 130。
- [ ] 12 个 MCP 契约测试和 9 个内部接口契约测试全部通过。
- [ ] 多语言 Fixture 测试通过。
- [ ] `frankichen/github_mcp` 和 `frankichen/sxt` 冒烟测试通过。
- [ ] 容器镜像版本、Build SHA 和源码 Commit 一致。
- [ ] 灰度、回滚和并行期方案经过演练。

## 10. 验收结论

只有当客户端真实发现 MyGithut12 和 130 个工具、精确 SHA 一致性成立、安全测试通过、原 118 个工具无回归时，才能签署发布验收。服务端存在代码但客户端不可发现，验收结论必须为不通过。
