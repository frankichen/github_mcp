# MyGithut12 DX-1 验收与蓝绿发布

## 1. 验收原则

- 所有 P0/P1/P2 必须有证据。
- 先完成 Private CI 和 Green 验证，再允许切流。
- 切流前 Blue 必须持续 ready 和可用。
- 切流后 Blue 保持热备，不立即停止或删除。
- 任一关键项失败即停止发布并保持/回切 Blue。
- Fast CI 不能替代 Full CI。
- 服务端代码存在但客户端不可发现不算完成。

## 2. 版本、名称和 Manifest

- [ ] Capability name 精确为 `MyGithut12`。
- [ ] Version 精确为目标版本 `12.1.0`。
- [ ] Build SHA 为源码完整 40 位 Commit。
- [ ] Connector URL 和认证未改变。
- [ ] Manifest 工具总数 159。
- [ ] 工具名称唯一、顺序固定。
- [ ] 4 个新增工具与接口清单一致。
- [ ] 现有 155 工具仍可发现。
- [ ] 旧工具必填参数无破坏性变化。
- [ ] deprecated 工具仍可调用。
- [ ] 运行时 list_tools 与 Manifest 完全一致。
- [ ] ChatGPT 客户端真实发现 159 个工具。

## 3. 高层工具

### prepare

- [ ] 创建唯一 Session/Workspace/branch。
- [ ] 返回 base/head/tree 和两类 revision。
- [ ] policy、lease、Index、Instructions、Context、overlap 完整。
- [ ] 幂等重放不重复建 branch。
- [ ] 中途失败返回补偿状态。

### apply

- [ ] patch/range/upload 三种 mode。
- [ ] dry-run 与 commit canonical hash 一致。
- [ ] Session/Workspace/HEAD/Blob CAS 有效。
- [ ] 一个请求只产生一个预期 Commit。
- [ ] branch/commit/tree/blob fresh read-back。
- [ ] 写后推进 revision。
- [ ] Index warm 单独报告。
- [ ] 幂等重复不产生额外 Commit。

### validate

- [ ] fast 使用 repo-fast-check。
- [ ] fast 明确 merge_eligible=false。
- [ ] full 使用 repo-auto-check。
- [ ] full 通过后生成有效 Attestation。
- [ ] reuse 检查完整 identity tuple。
- [ ] wait 最长 55 秒。
- [ ] failure 自动返回 Failure Pack。

### finalize

- [ ] prepare_pr 创建或更新 Draft PR。
- [ ] readiness 只读。
- [ ] merge 无 confirm 必须拒绝。
- [ ] merge 复用现有 expected head/base、review、Checks、Full CI 门禁。
- [ ] close 不默认删除 branch/PR。
- [ ] Session/Workspace 正确释放。

## 4. 兼容和并发

- [ ] 三个窗口、三个 Session、三个 branch 互不串线。
- [ ] 同一 branch 双写至少一个稳定失败。
- [ ] 外部 push 后 Session/Workspace drifted。
- [ ] refresh 后才能继续。
- [ ] lease 过期和接管有事件。
- [ ] 旧低层工具与新高层工具交叉使用仍保持 CAS。
- [ ] 旧客户端在 Green 上仍可完成原流程。
- [ ] 回切 Blue 后旧 155 工具仍正常。

## 5. Mirror、Index 和 Context

- [ ] ref 先 fresh resolve。
- [ ] exact Commit/Tree/Blob 与 GitHub 身份一致。
- [ ] warm read 主要命中 Mirror。
- [ ] object missing 可 fetch/fallback。
- [ ] origin mismatch 和 corruption 失败关闭。
- [ ] Index 不使用旧 Commit 冒充新 Commit。
- [ ] Commit 后自动 incremental warm。
- [ ] Context V2 每项有 reason/authority/SHA/lines。
- [ ] inline 不返回不必要完整文件。
- [ ] Resource UTF-8 offset、SHA 和 continuation 正确。

## 6. Private CI

- [ ] `list_private_ci_profiles` 可发现 repo-fast-check。
- [ ] Controller/Agent/config/profile 一致。
- [ ] Fast CI 只运行受影响集合。
- [ ] incomplete selection 保守扩大。
- [ ] Full CI 默认完整。
- [ ] env cache key 包含全部身份。
- [ ] cache build/verify/seal/atomic publish。
- [ ] 跨仓库/运行时/manifest 不串用。
- [ ] cache invalidation/eviction 正确。
- [ ] Failure Pack 不覆盖原错误。
- [ ] Full CI 证据可进入 Attestation。

## 7. 性能

使用真实 repository 和真实 Private CI：

- [ ] warm exact-read P95 < 500 ms。
- [ ] prepare P95 < 5 秒。
- [ ] Context V2 P95 < 3 秒。
- [ ] Fast CI P95 < 15 秒。
- [ ] cache-hit bootstrap P95 < 2 秒/Workspace。
- [ ] `github_mcp` 小变更 Full CI 目标 P95 < 35 秒。
- [ ] 主路径调用数 P50 ≤ 5、P95 ≤ 7。
- [ ] 报告包括样本数、冷热状态、机器和 Commit。

## 8. 安全

- [ ] 未授权 repository 拒绝。
- [ ] 任意 URL/host/path/shell/sql 拒绝。
- [ ] 路径穿越、绝对路径和控制字符拒绝。
- [ ] 日志无 Secret/Authorization/大段源码。
- [ ] Mirror 不保存凭据。
- [ ] env cache 不保存凭据且权限正确。
- [ ] consequential action 有 annotation、幂等、审计。
- [ ] merge/deploy confirm 和 policy 未弱化。

## 9. Blue/Green 前置条件

### Blue

- [ ] 当前 Blue build/version/capability 已记录。
- [ ] Blue `/health` 和 `/ready` 正常。
- [ ] Blue 仍承接真实 MCP 请求。
- [ ] Blue image、配置和数据库备份可回滚。
- [ ] 不修改 Blue 端口、进程或 upstream。

### 数据和 Schema

- [ ] 完成数据库备份和恢复测试。
- [ ] migration 仅 Expand。
- [ ] Blue 可读写新 Schema。
- [ ] Green 可读写新 Schema。
- [ ] 无 drop/rename/破坏性 semantic change。
- [ ] schema compatibility range 写入 readiness。

### 共享状态

- [ ] idempotency/audit 共享。
- [ ] Workspace/Session/lease/revision 共享。
- [ ] CI jobs 共享。
- [ ] Index metadata/pins 共享。
- [ ] response Resource 共享/可路由。
- [ ] Upload staging 共享/可路由。
- [ ] leader lease 共享。

## 10. Green 启动

- [ ] Green 使用独立端口、进程或容器名。
- [ ] Green image digest 和 Build SHA 固定。
- [ ] Green role=standby。
- [ ] Green 不领取 CI job。
- [ ] Green 不执行 cleanup/retention。
- [ ] Green 不推进 deployment。
- [ ] Green 不发送重复 callback。
- [ ] Green `/health` 正常。
- [ ] Green `/ready` 检查 GitHub、DB、schema、resource store。
- [ ] Green capability=MyGithut12/12.1.0/159。

## 11. Green 预检

### 只读

- [ ] 读取 `github_mcp` repository/main/Commit/Tree。
- [ ] exact file/tree/search/symbol/context。
- [ ] Mirror hit/fallback evidence。
- [ ] 读取活动 Workspace。
- [ ] 读取现有 Private CI job。
- [ ] 读取 Blue 创建的 Resource。

### Canary 写入

使用专用 canary repository 或专用 `ai/` branch：

- [ ] 创建 Workspace/Session。
- [ ] dry-run ChangeSet。
- [ ] Commit durable verify。
- [ ] Index warm。
- [ ] Fast CI。
- [ ] Full CI。
- [ ] Attestation。
- [ ] Draft PR/readiness。
- [ ] 不 merge production branch。
- [ ] 清理只按既定策略，不破坏证据。

### 连续性

- [ ] Blue 创建 Upload，Green 继续 append/finalize/read。
- [ ] Blue 创建 Resource，Green 读取完整 bytes。
- [ ] Blue 长轮询切流后不丢失结果。
- [ ] 同一 idempotency key 跨代不重复写。
- [ ] Blue/Green 同时处理不同 Session 不冲突。
- [ ] leader handoff 演练成功。

## 12. 原子切流

切流条件：前述所有项通过。

步骤：

1. 保持 Blue active。
2. 将 Green 从 standby 切到 ready-for-traffic，但副作用 leader 尚不转移。
3. 对反向代理配置执行语法验证。
4. 通过单次原子 reload 将新连接 upstream 指向 Green。
5. 不 stop/restart Blue。
6. 保持已有 Blue 连接 drain。
7. 对 Green 执行 capability、read、write、CI 状态 smoke。
8. 确认 Green 业务请求正常后，使用共享 lease 转移副作用 leader。
9. 验证只有一个 leader。
10. Blue 进入 hot-standby/draining，但保持可回切。

## 13. 切流后验收

- [ ] 新请求全部到 Green。
- [ ] 已有 Blue 请求正常完成。
- [ ] 活动 Workspace 可继续续租和写入。
- [ ] Session revision 连续。
- [ ] Upload/Resource 连续。
- [ ] Private CI job 不重复执行。
- [ ] cleanup/index consumer 只有一个 leader。
- [ ] GitHub API error、5xx、latency 无异常。
- [ ] 4 个高层工具真实调用成功。
- [ ] 旧工具 smoke 成功。
- [ ] Connector 刷新后发现 159 工具。

## 14. 回滚触发条件

任一条件触发立即回切：

- Green readiness 失败；
- 5xx/协议错误明显增加；
- Workspace/Session revision 不连续；
- Resource/Upload 无法读取；
- idempotency 重复写；
- 双 leader；
- CI 重复领取或状态不一致；
- GitHub durable verify 异常；
- 客户端工具发现不完整；
- 旧工具回归失败；
- 无法解释的数据或 Schema 差异。

## 15. 回滚步骤

1. 停止 Green 接收新流量。
2. 原子 reload upstream 回 Blue。
3. 不删除 Green、不回滚 Git Commit、不修改用户 branch。
4. 将副作用 leader lease 交回 Blue。
5. 验证 Blue capability/read/write/Workspace/CI。
6. 保留 Green logs、metrics、DB evidence 和 image。
7. 对 Expand migration 不做破坏性逆迁移；Blue 必须本来就兼容。
8. 更新 Draft PR 和实施状态，记录根因。
9. 修复后重新走完整 Green 预检，禁止直接再次切流。

## 16. Blue 退役

只有满足以下条件才可退役：

- Green 全部验收通过；
- Connector 159 工具稳定；
- 活动请求全部离开 Blue；
- 无回滚触发；
- Blue image/config/DB backup 已归档；
- 至少完成一次真实回切演练；
- 用户或总控明确授权。

退役仅停止旧实例，不立即删除镜像、配置、日志和备份。Contract migration 不在本次执行。

## 17. 最终签署

发布结论必须记录：

```text
repository
release commit/tree
version/build SHA
manifest hash/tool count
green image digest
blue image digest
schema version
private CI job IDs
canary commit/PR
cutover time
leader handoff evidence
post-cutover smoke
rollback rehearsal
approver
```

任何空项或无法验证项都不得签署“完成”。
