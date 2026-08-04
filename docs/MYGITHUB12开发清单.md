# MyGithut12 开发清单

说明：所有任务完成后才允许把服务对外登记为 MyGithut12。勾选时应附 PR、测试或运行证据。

## P0：版本和契约

- [ ] M12-001 将外部服务名改为 `MyGithut12`，版本改为 `12.0.0`。
- [ ] M12-002 增加 `MYGITHUB12_` 配置，并实现一次迁移周期的旧变量兼容。
- [ ] M12-003 扩展 capability，加入 12 项索引相关能力和支持语言。
- [ ] M12-004 固化 12 个新 MCP 工具 Schema，禁止开发中随意改名。
- [ ] M12-005 固化 9 个内部索引 HTTP Schema 和稳定错误码。

## P0：索引服务基础

- [ ] M12-006 新建 `services/repository-index-service` 工程、健康检查和配置。
- [ ] M12-007 实现内部认证、仓库白名单和请求审计。
- [ ] M12-008 实现精确 Commit/Tree 快照下载、只读缓存和校验。
- [ ] M12-009 实现索引任务队列、去重、重试、超时和状态查询。
- [ ] M12-010 实现索引 Manifest、配额、过期清理和失败恢复。

## P0：仓库和文本工具

- [ ] M12-011 实现 `list_repository_tree`。
- [ ] M12-012 实现 `search_repository_files`。
- [ ] M12-013 实现 `get_github_files_batch`。
- [ ] M12-014 实现 `search_repository_text`，含普通文本、受限正则、分页和上下文。
- [ ] M12-015 实现索引状态、构建请求和任务查询三个工具。

## P0：符号能力

- [ ] M12-016 实现 Go、Python、TypeScript、JavaScript、Vue 符号抽取。
- [ ] M12-017 实现稳定 `symbol_id` 和同名符号消歧。
- [ ] M12-018 实现 `search_repository_symbols`。
- [ ] M12-019 实现 `get_symbol_definition`。
- [ ] M12-020 实现 `find_symbol_references`。
- [ ] M12-021 实现 `get_symbol_call_hierarchy`，处理循环、深度和截断。

## P0：上下文和 MCP 接入

- [ ] M12-022 实现 `build_repository_context_pack` 和选择理由。
- [ ] M12-023 在 `mcp_server.py` 注册全部 12 个新工具。
- [ ] M12-024 确认所有新工具均经过仓库授权检查。
- [ ] M12-025 将工具总数测试从 118 更新为 130，并校验唯一性。
- [ ] M12-026 生成 `docs/MYGITHUB12_TOOL_MANIFEST.json`。

## P0：安全和可靠性

- [ ] M12-027 禁止调用方传入 Git URL、本地路径、Shell 和可执行参数。
- [ ] M12-028 增加文件数、字节数、深度、正则、查询时间、构建并发和磁盘配额。
- [ ] M12-029 验证敏感文件、二进制、依赖和构建目录默认排除。
- [ ] M12-030 实现索引 Commit/Tree 不匹配时失败关闭。
- [ ] M12-031 实现索引服务故障隔离，确保原 118 个工具不受影响。

## P1：测试和可观测性

- [ ] M12-032 为每个新工具增加输入、成功、分页、截断和错误测试。
- [ ] M12-033 建立多语言 Fixture 仓库和定义/引用/调用关系金丝雀测试。
- [ ] M12-034 增加 `frankichen/github_mcp` 和 `frankichen/sxt` 冒烟测试。
- [ ] M12-035 增加低基数 Metrics、结构化日志和 trace_id。
- [ ] M12-036 增加索引构建取消、失败重试和僵尸任务回收测试。
- [ ] M12-037 完成旧 118 工具全量回归。

## P1：发布

- [ ] M12-038 更新 README、部署文档、环境变量样例和 Docker 配置。
- [ ] M12-039 构建并发布 MyGithut12 镜像，Build SHA 与 Git Commit 一致。
- [ ] M12-040 更新 MCP Connector 注册名称为 `MyGithut12`。
- [ ] M12-041 验证客户端实际发现 130 个工具，而不只验证服务端代码。
- [ ] M12-042 完成灰度、回滚和 MyGithut11 并行期方案。

## 完成定义

单个任务只有同时满足代码、测试、文档、错误码和可观测性要求才可勾选。任何新工具如果未进入 Manifest 或客户端不可发现，都视为未完成。
