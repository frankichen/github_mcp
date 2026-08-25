# MyGithut12 DX-1 规划总览

## 1. 文档状态与权威性

本文是 `frankichen/github_mcp` 在继续使用 GitHub 前提下进行开发体验优化的权威规划入口，代号 **DX-1**。

本文件和同系列文档只定义需求、设计、开发顺序和验收标准，不代表代码已经实现或生产已经发布。任何后续开发窗口必须以执行时 fresh-read 的 `main`、Manifest、Capability、Repository Policy 和生产运行版本为准。

基线日期：`2026-08-25`

| 项目 | 当前基线 |
|---|---|
| Repository | `frankichen/github_mcp` |
| Base branch | `main` |
| Base Commit | `2e8fcf312697a6d2698aa4326d54905d787e84d0` |
| Base Tree | `c28dcc244d55fc193624c08b60d28a1078224d30` |
| MCP 名称 | `MyGithut12` |
| 现网版本 | `12.0.5` |
| 现网 Build SHA | `a74dd1872c6e835f5feadd8c2e62a2d485eaf475` |
| 当前工具数 | 155 |
| Manifest 组成 | 118 legacy + 37 MyGithut12 |
| Index version | `12.0.0-1` |
| Repository policy | GitHub read/write、Private CI 允许；test deploy、自部署禁止 |
| 当前 Full CI | `repo-auto-check`，近期约 39～41 秒 |
| 当前索引复用 | exact Tree reuse 已可在约 60 ms 完成 |

## 2. 本轮目标

DX-1 不再继续扩充“GitHub 原生功能”，重点优化 AI 开发主路径：

```text
任务准备
  ↓
代码修改
  ↓
快速验证
  ↓
完整验证和收尾
```

目标主路径建议收敛为 4 个长期稳定的高层 MCP 工具：

1. `prepare_development_task`
2. `apply_development_change_set`
3. `validate_development_task`
4. `finalize_development_task`

服务器内部仍然复用现有 Workspace、CAS、Patch、Upload、Index、Private CI、Attestation 和 Merge Gate。高层工具只负责编排、状态、补偿和紧凑结果，不复制第二套底层写入逻辑。

## 3. 外部接口与名称策略

### 3.1 名称冻结

- MCP 外部名称继续为 `MyGithut12`。
- Connector URL、认证方式和现有仓库授权不变。
- 本轮不创建 `MyGithut13`，不要求用户安装一个新名称的 Connector。
- 目标服务版本建议为 `12.1.0`，属于兼容式功能扩展。

### 3.2 工具变化

| 类型 | 数量 | 处理 |
|---|---:|---|
| 当前工具 | 155 | 全部保留 |
| 新增高层工具 | 4 | 一次性新增 |
| 硬删除 | 0 | DX-1 禁止 |
| 目标总数 | 159 | Manifest、运行时和客户端发现必须一致 |

`12.x` 中不改变现有工具的必填参数和既有安全语义。已有 deprecated 工具继续保留并给出替代项。任何未来删除必须经过独立大版本、至少一个完整弃用周期和客户端影响评估。

### 3.3 ChatGPT Connector 影响

本轮只允许一次受控的工具发现更新，用于让客户端发现新增 4 个工具。完成后，`12.x` 原则上冻结工具名称集合，后续优化优先通过内部实现、可选参数兼容扩展、Capability 字段和 Resource 内容演进完成，避免每次都重新发布或改 MCP 名称。

## 4. 核心优化范围

### 4.1 Development Session

在现有 Development Workspace 之上增加高层 Session，保存：

- repository、branch、base/head/tree；
- Workspace ID、Workspace revision、Session revision；
- lease、owner、scope、index identity；
- PR、Fast CI、Full CI、Attestation；
- 幂等、审计、失败摘要和状态机。

Session 不能替代 Workspace 的写权限。任何写入仍必须验证 Workspace lease/revision、expected HEAD、expected Blob 和 GitHub fresh read-back。

### 4.2 本地 Git Mirror 读取

对允许仓库维护独立 bare Mirror：

- fresh ref、写入和最终确认仍以 GitHub 为事实来源；
- exact Commit/Tree/Blob/file/diff/history 优先从 Mirror 读取；
- Mirror 是可重建缓存，损坏或身份不一致时回退 GitHub API；
- 调用方不能传任意 remote URL。

### 4.3 Context Pack V2

默认返回最相关的 symbol、path、test、contract 和片段，而不是直接返回大量完整文件。完整内容通过 Resource 分页展开。每项必须携带 SHA、行范围、选择理由、权威性和 omitted 统计。

### 4.4 两级 Private CI

- `repo-fast-check`：开发中快速反馈，明确 `merge_eligible=false`。
- `repo-auto-check`：合并前完整门禁，可生成 Attestation。

Fast CI 采用 affected workspace/test；Full CI 默认保持保守完整性，除非仓库策略明确允许受影响范围执行。

### 4.5 依赖环境缓存

依据 runtime、镜像 digest、workspace 相对路径、依赖文件/锁文件 hash、CI profile 版本生成不可变环境身份。环境必须 build、verify、seal 后只读复用，禁止跨仓库、跨运行时或跨依赖身份串用。

### 4.6 Failure Pack

CI 失败后自动聚合：

- job、Commit、Tree、profile；
- failed step、exit code、日志尾；
- changed files、受影响模块和测试；
- 相关 symbol、配置、manifest、cache evidence；
- 稳定错误码和候选修复方向。

### 4.7 Blue/Green 发布

Green 在独立端口和运行代际启动，Blue 全程继续服务。Green 完成真实只读、Canary 写入、Private CI、Resource/Upload、活动 Workspace 和回滚演练后，才允许通过反向代理原子切流。切流后 Blue 保持热备，不能立即停机或删除。

## 5. 当前完成度

### 已完成基础

- `DONE`：MyGithut12 12.0.5 运行身份和完整 Build SHA。
- `DONE`：155 工具 Manifest 与 Capability 基线。
- `DONE`：expected HEAD、expected Blob、Workspace CAS、幂等和 durable write read-back。
- `DONE`：Development Workspace lease/revision、漂移和 overlap 基础。
- `DONE`：exact Commit Repository Index、Tree reuse、Symbol/Text/Context 基础。
- `DONE`：Private CI `repo-auto-check`、source mirror 和结构化 job evidence。
- `DONE`：response resource fallback 和 32 KiB inline budget。

### 已有能力但需补齐

- `PARTIAL`：`repo-fast-check` 代码和配置存在，但仓库 profile discovery 尚未稳定暴露。
- `PARTIAL`：CI 有 pip cache，但 venv/依赖环境仍重复 bootstrap。
- `PARTIAL`：Change Impact/Affected Tests 已可分析，但尚未完整驱动 CI workspace 选择。
- `PARTIAL`：本地 source mirror 已用于 CI，Controller exact-read 仍大量走 GitHub API。
- `PARTIAL`：Context Pack 可用，但大结果仍容易产生较大 Resource。
- `PARTIAL`：多种 response detail level 已存在，但个别聚合工具仍返回过大结果。
- `PARTIAL`：现有部署可回滚，但还没有完整的 Blue/Green 状态共享、leader 和资源连续性协议。

### 未开始

- `NOT_STARTED`：4 个高层 Development Task 工具。
- `NOT_STARTED`：Development Session 状态表和事件。
- `NOT_STARTED`：Controller Local Git Mirror read path。
- `NOT_STARTED`：sealed dependency environment cache。
- `NOT_STARTED`：自动 Failure Pack。
- `NOT_STARTED`：Blue/Green generation、leader lease、Resource/Upload continuity 验收。
- `NOT_STARTED`：ChatGPT 客户端对 159 工具的最终发现验收。

## 6. 文档导航

- [DX-1 需求文档](MYGITHUB12_DX需求文档.md)
- [DX-1 开发设计](MYGITHUB12_DX开发设计.md)
- [DX-1 接口变更清单](MYGITHUB12_DX接口变更清单.md)
- [DX-1 开发清单](MYGITHUB12_DX开发清单.md)
- [DX-1 验收与蓝绿发布](MYGITHUB12_DX验收与蓝绿发布.md)

## 7. 状态和证据规则

每一项只能使用：

- `DONE`：代码、测试、真实运行证据和文档齐全。
- `PARTIAL`：有实现但缺少主流程接入、客户端发现、真实性能或发布验证。
- `NOT_STARTED`：尚未实现或无证据。
- `BLOCKED`：存在明确外部阻塞，并记录解除条件。
- `SUPERSEDED`：经正式决策被新方案替代。

不能仅凭“代码文件存在”标记完成。Manifest 未更新、客户端不可发现、Green 未真实验证、回滚不可用、性能只来自 mock、资源跨代失效等情况均不得标记 `DONE`。

## 8. 完成定义

DX-1 只有在以下条件全部成立时完成：

1. 4 个高层工具真实进入 Manifest、运行时和 ChatGPT 客户端发现。
2. 现有 155 个工具全部回归通过，无必填参数和安全门禁破坏。
3. 常规任务从准备到 Draft PR 的高层调用数 P50 不超过 5、P95 不超过 7。
4. exact-commit warm read、Context Pack、Fast CI 和环境缓存满足真实性能门槛。
5. Full CI、Attestation 和 Merge Gate 未被 Fast CI 或缓存绕过。
6. Blue 持续服务时 Green 完成全部预检和 Canary。
7. 原子切流、活动 Workspace、Resource、Upload、长轮询和幂等请求连续性通过。
8. Green 异常时仅通过 upstream 回切即可恢复 Blue。
9. 文档、代码、Manifest、Connector、镜像 Build SHA 和生产运行身份一致。
