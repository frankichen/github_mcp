# MyGithut12：P2P Server 独立测试发布链（2026-11-13）

## 目标和边界

Repository = frankichen/p2p-server
Environment = gongshi-p2p-lab（宿主机为 gongshi-test，与 SXT gongshi-test/fullstack 相互隔离）
Scope = p2p-lab
Release root = /opt/p2p-server-lab/releases

现有 P2P 代码只包含 UDP 网关、实验信令和隔离中继原型；原厂 PZPP Master/Relay 尚未完成兼容。部署不得修改 SXT 业务容器、原 P2P 路由、DID/License、PZPP-0001 或现有数据库。

## 发布固定门禁

1. 目标 SHA 必须等于 frankichen/p2p-server 当前 main 完整 SHA。
2. 当前 main SHA 的私有 repo-auto-check 必须通过；不得以分支 CI、旧 CI 或 GitHub Actions 代替。
3. 目标 SHA 必须包含受审查的固定脚本 scripts/deploy_gongshi_p2p_lab.sh。不存在时，MyGithut12 在计划阶段返回 P2P_DEPLOY_SCRIPT_UNAVAILABLE，不得排队。
4. Worker 只领取固定仓库、环境与范围；独立 P2P 执行器仅操作独立源镜像和隔离工作区，不能接受任意 SSH 目标、Shell 或文件路径。
5. 发布脚本必须真实校验 release manifest、SHA-256 checksum、启动服务及本机健康检查。终态仅接受一条完整的 release_id/git_sha/current/health/checksum/manifest 结果，且发布目录固定在 /opt/p2p-server-lab/releases。
6. 脚本中禁止关闭现有防火墙或开放未经鉴权的公网 UDP；实验服务先仅使用 loopback。
7. P2P 回滚路径尚未实现，明确拒绝；不得走 SXT Goose down 或 SXT release 恢复逻辑。

## 上线顺序与未完成事项

MyGithut12 代码合并及当前 main 私有 CI → 授权升级 MyGithut12 控制器和 Worker → 独立安装 P2P 执行器 → 为 p2p-server 开发并合并受控发布脚本 → 新 SHA 的 repo-auto-check → 用户明确授权 P2P 实际发布 → 验证发布记录、进程、独立 status、UDP 测试。

在部署脚本、Runner、安全配置及端到端验收缺失时，本开发变更只是发布能力的候选，不表示已具备实际发布就绪状态。严禁使用 root SSH 临时替代经过验证的正式发布通道。


## Private source mirror fix

The P2P repository is private. Its deployment executor does NOT fetch from
GitHub, read CI credentials or reuse the SXT deploy mirror. A root-owned,
network-denied systemd one-shot exporter reads ONLY the private CI worker
mirror at /srv/private-ci/cache/git/frankichen-p2p-server.git, verifies
the canonical repository remote and exact refs/heads/main SHA, then produces
an atomic Git bundle in /var/lib/mygithub12-p2p-source/main.bundle.
The bundle is root:xiaowu (0640) in a root-owned directory (0750).
A fixed one-minute timer refreshes it as the CI mirror advances. The P2P
runner uses git clone from this local Git bundle only, verifies a clean main
branch at the requested exact SHA and fails closed if the bundle is missing
or stale. The exporter never copies .git/config or logs credentials.
