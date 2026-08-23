# Private CI 通用运行时遗失实现恢复计划（2026-08-23）

## 1. 背景与问题定义

2026-08-08 的 `github_mcp-1202-main` 工作树中存在一批未正式提交到 Git 的 Private CI 实现。该实现基于旧基线 `419949a83d2b64ed9cd8d8ddab5702a9bbb889a4`，其 Private CI 单测可独立通过 `108/108`，因此不是纯草稿；但 2026-08-08 之后 `main` 已连续合入自动纳管、Python CI、OpenAPI Profile、发布 fail-stop、private-deploy-agent 修复等改动，不能原样恢复旧工作树。

本计划以最新正式 `main` 为唯一基线，仅恢复仍有价值且未被后续正式实现替代的能力，并保留后续安全/部署合同。

开发基线：

- Repository: `frankichen/github_mcp`
- Base: `b15632bfce9909c0689db96971a98ef29629dc1a`
- Legacy reference: `/home/xiaowu/AgentDock/releases/github_mcp-1202-main`（只读，不作为提交基线）

## 2. 目标

1. 将 Private CI 从“Go/Node/Python + LensHub 特例”收口为可安全自动纳管的通用 CI runtime。
2. 保留 `frankichen/*` 自动纳管机制，不让新增技术栈绕过 allowlist / profile gate。
3. 将 PostgreSQL/Redis/RabbitMQ 从“所有 Go workspace 默认启动”改为“仅 operator-controlled repository workspace 显式请求时启动”。
4. 恢复 Rust / Maven / Gradle / .NET 的受控自动识别、固定 Profile、缓存和镜像边界。
5. 将 attestation 从 Go/npm 特定 lock 身份扩展为通用 dependency manifest 身份，并记录 source immutability。
6. 修复 source cleanup 中 `frankichen-sxt` fallback，避免自动纳管其他仓库时清理错误 mirror。
7. 全程不向 MCP caller 开放任意 shell、任意 image、任意 service、任意 host path。

## 3. 明确不做

本恢复不得回滚或覆盖 2026-08-08 后已经正式演进的设计：

- 不恢复旧 `ci_repository_config.py` 整文件；保留 #34 的 `frankichen/*` CI-only auto-enrollment。
- 不恢复旧 `apply-fixes.sh`；保留 #39 的 `MYGITHUB12_DEPLOY_FAILURE_MODE` / fail-stop 合同。
- 不恢复旧 Playwright `mcr.microsoft.com/playwright:*` runtime；继续使用当前受控 `localhost/node-chromium:*` + preheat/cache 设计。
- 不删除当前 Go/Node/Python/OpenAPI Profile。
- 不改变生产部署、Controller/Worker systemd、Secret 或数据库运行实例。
- 不执行生产发布；本计划只完成代码、测试、PR、合并验收。

## 4. 开发阶段

### Phase A — Generic Runtime Foundation

#### A1. 修复 source cleanup

当前 `remove_source_worktree()` 在无法解析 repository 时 fallback 到 `frankichen-sxt.git`。自动纳管已支持 `frankichen/*` 后，该 fallback 不再成立。

目标行为：

- 能解析 repository：只操作对应 mirror。
- 无法解析 repository：直接返回，不猜测仓库。
- 不允许 cleanup 越界到其他 repository mirror。

#### A2. workspace 显式 services/hooks

从 operator-controlled repository config 读取 workspace 的：

- `services`: 仅允许 `postgres` / `redis` / `rabbitmq`
- `hooks`: 仅允许内建 hook 名称，不允许 repository 自己注入 shell

规则：

- 自动发现普通 Go workspace 默认 `services=[]`、`hooks=[]`。
- `frankichen/sxt` 根 workspace 显式配置三个 service 与 `go-migrate`、`ai-integrity`。
- Node/Python/Rust/Maven/Gradle/.NET 不因技术栈而自动获得 DB/MQ 服务。

#### A3. ServiceManager 按需启动

`ServiceManager.prepare()` 改为接受显式 service 集合：

- 只启动 requested services。
- 不请求 PostgreSQL 时不创建数据库。
- 不请求 Redis/RabbitMQ 时不启动对应容器。
- `services.env` 只写已启动服务对应的有效连接信息；未启动服务使用空值或不注入，不能伪造可用地址。
- cleanup 仍按 job label 精确清理。

#### A4. 内建 workspace hooks

允许的 hook 固定在源码 allowlist 中，例如：

- `go-migrate`
- `ai-integrity`

repository config 只能选择 hook 名称，不能提供命令文本。

### Phase B — Multi-language Private CI

恢复并按最新安全边界重构：

- Rust: `Cargo.toml` / `Cargo.lock` → `rust-check`
- Maven: `pom.xml` → `maven-check`
- Gradle: `build.gradle` / `build.gradle.kts` → `gradle-check`
- .NET: `*.csproj` / `*.sln` → `dotnet-check`

#### B1. Profile 安全要求

每个 Profile 必须：

- 使用代码内固定/受控 image；不接受 caller image。
- setup 阶段需要网络时显式使用受控 proxy；check 阶段默认 network isolated。
- cache 只挂载固定 cache root 子目录。
- 不执行 repository 自定义任意脚本入口；只运行生态标准、固定命令。
- 缺少必要 manifest/工具时 fail closed 或明确 configuration_error。

#### B2. 缓存

增加固定缓存：

- Cargo: `/srv/private-ci/cache/cargo` → `/ci-cache/cargo`
- Maven: `/srv/private-ci/cache/maven` → `/ci-cache/maven`
- Gradle: `/srv/private-ci/cache/gradle` → `/ci-cache/gradle`
- NuGet: `/srv/private-ci/cache/nuget` → `/ci-cache/nuget`

#### B3. 自动纳管

#34 的 `frankichen/*` auto-enrollment 保留；默认 auto profiles 扩展为：

- go-check
- python-check
- node-check
- rust-check
- maven-check
- gradle-check
- dotnet-check

`openapi-check` 仍然只对显式授权 repository 开放，不自动加入默认集合。

### Phase C — Generic Attestation

#### C1. 通用 dependency manifest identity

Worker 对被测 workspace 计算稳定的 `dependency_manifest_sha256`，覆盖至少：

- Go: `go.mod`, `go.sum`
- Node: package manifest + lock file
- Python: pyproject/requirements/lock 类文件
- Rust: Cargo.toml/Cargo.lock
- Maven: pom.xml
- Gradle: build.gradle(.kts), settings.gradle(.kts), gradle wrapper properties/lock
- .NET: project/solution + packages.lock.json / Directory.Packages.props 等

哈希必须包含相对路径和文件内容，避免同内容不同文件集合碰撞。

#### C2. source immutability

Worker summary evidence 明确输出 `source_immutable=true/false`：

- checkout 后 exact HEAD 必须等于 requested commit。
- CI 完成后 source tree 不得出现未允许的修改。
- 任何 mutated source 不允许创建可复用 attestation。

#### C3. Attestation schema 兼容

新增字段必须采取兼容式 `ALTER TABLE ... ADD COLUMN` / default：

- 现存 `ci_tree_attestations` 数据不可失效或删除。
- 老记录保持可读取。
- 新记录优先使用 `dependency_manifest_sha256`。
- 旧 Go/npm lock 字段保留兼容读取，不在本 PR 做破坏性 schema 清理。

## 5. 安全约束

全阶段必须满足：

1. MCP caller 不新增 `command` 参数。
2. MCP caller 不新增 `image` 参数。
3. MCP caller 不新增 `services` 或 `hooks` 参数。
4. services/hooks 只能来自本机受控 repository policy。
5. 镜像必须在源码 allowlist /固定 Profile 内选择。
6. CI 容器继续 rootless、cap-drop、no-new-privileges、pids/memory 限制。
7. 网络仅在 setup / image pull 等明确阶段开放；check 阶段保持隔离。
8. Secret、token、proxy credential 不进入日志、summary、attestation。
9. Source mirror identity 必须绑定 authorized `owner/repo`，不得 fallback 到 LensHub。
10. 不改变现有 deployment authorization / self_deploy policy。

## 6. 开发顺序与提交边界

计划拆为三个独立逻辑提交/PR，避免把高风险基础设施改动揉成一个大 diff。

### PR-A: Generic runtime foundation

文件预计：

- `services/private-ci-agent/private_ci_agent/source.py`
- `services/private-ci-agent/private_ci_agent/profiles.py`
- `services/private-ci-agent/private_ci_agent/executor.py`
- `services/private-ci-agent/private_ci_agent/services.py`
- `services/github-action-service/app/ci_repository_config.py`
- `services/github-action-service/config/ci_repositories.yml`
- `services/private-ci-agent/deploy/repositories.yml`
- 对应 tests

### PR-B: Multi-language profiles

文件预计：

- `profiles.py`
- `podman.py`
- `config.py`
- Controller repository policy/profile union
- deploy profiles/config
- tests

### PR-C: Generic attestation

文件预计：

- Worker evidence generation
- `services/github-action-service/app/attestation_registry.py`
- attestation tests
- 必要 schema compatibility logic

## 7. 验收标准

### Phase A acceptance

必须全部满足：

- [ ] `remove_source_worktree()` 无 repository 时不再 fallback `frankichen-sxt`。
- [ ] 普通 Go workspace 的 plan 不自动带 postgres/redis/rabbitmq。
- [ ] `frankichen/sxt` root workspace 仍显式获得 postgres/redis/rabbitmq。
- [ ] 只请求 Redis 时不会启动 PostgreSQL/RabbitMQ；其他组合等价成立。
- [ ] 未知 service/hook 被拒绝，不能转换成 shell。
- [ ] `go-migrate` / `ai-integrity` 仍能通过内建 hook 工作。
- [ ] 现有 Go/Node/Python/OpenAPI 回归不退化。
- [ ] Private CI Agent 全量 pytest 通过。
- [ ] Git diff check / compileall / Ruff 通过。

### Phase B acceptance

- [ ] Cargo.toml 能识别为 rust workspace。
- [ ] pom.xml 能识别为 Maven workspace。
- [ ] build.gradle(.kts) 能识别为 Gradle workspace。
- [ ] csproj/sln 能识别为 .NET workspace。
- [ ] 四种 profile 都使用固定受控 image，caller 无 image override。
- [ ] 四种生态 cache 均只映射固定 cache root。
- [ ] setup/check 网络边界有回归测试。
- [ ] `frankichen/*` auto-enrollment 能自动获得四个新 profile，但 deployment 权限不会自动获得。
- [ ] `openapi-check` 不被误加入默认 auto profile。
- [ ] Private CI Agent 全量 pytest 通过。

### Phase C acceptance

- [ ] dependency manifest hash 对 Go/Node/Python/Rust/Maven/Gradle/.NET 都有确定性单测。
- [ ] manifest 内容变化会导致 hash 变化。
- [ ] manifest 文件集合/路径变化会导致 hash 变化。
- [ ] source dirty/mutated 时 attestation 创建失败。
- [ ] source clean/exact commit 时 attestation 可创建并可 validate。
- [ ] 旧 attestation schema 可平滑升级且原记录可读取。
- [ ] 不把 dependency 内容、Secret、token 写入 attestation。
- [ ] Controller/Worker 相关全量测试通过。

### Final acceptance

三个阶段完成后：

- [ ] 每个 PR 都基于当时最新 main，无覆盖式恢复旧文件。
- [ ] 每个 PR 都有明确 diff-first review。
- [ ] 每个 PR 的 exact HEAD 通过项目自身 Private CI `repo-auto-check`。
- [ ] PR 合并后重新读取 `main` 验证目标能力存在。
- [ ] 未执行生产 deployment/restart。
- [ ] 旧 `github_mcp-1202-main` 只有在所有恢复项确认进入 main 后才允许删除。

## 8. 回滚策略

- 每阶段独立 PR，可通过后续 revert 单独撤销。
- Phase A 不做数据库 schema 变更，回滚仅涉及 runtime behavior/config。
- Phase B 新 Profile 为增量能力，回滚不会影响原 Go/Node/Python/OpenAPI。
- Phase C schema 只增列、不删列；代码回滚后新增列可保留，不要求 destructive down migration。
- 生产发布不属于本计划，因此代码修复本身不触发 Controller/Worker runtime 切换。

## 9. 完成定义

只有当 A/B/C 三阶段代码与测试均进入正式 Git `main`，且 Private CI 验收通过后，遗失实现才视为“已恢复”。在此之前：

`/home/xiaowu/AgentDock/releases/github_mcp-1202-main`

必须继续保留为只读恢复证据源。
