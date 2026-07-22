# MyGithub09 GitHub 认证说明

当前 MyGithub09 使用 Classic PAT。私有仓库 Checks 读取依赖 Classic PAT 的 `repo` scope；认证能力以 `X-OAuth-Scopes` 和真实 GitHub API 探测为准，不能仅凭 Token 前缀判断。

Check Runs 与 Commit Status 分开分类：200 空列表分别为 `CHECKS_EMPTY` / `STATUSES_EMPTY`，权限拒绝不能降级为空列表或通过。Classic PAT 缺少 `repo` scope 时使用 `CLASSIC_PAT_REPO_SCOPE_REQUIRED`；已包含 `repo` 仍被拒绝时使用 `CLASSIC_PAT_REPOSITORY_ACCESS_DENIED`。Fine-grained PAT 的 Checks 403 使用 `FINE_GRAINED_PAT_CHECKS_UNAVAILABLE`。

推荐配置：

```dotenv
GITHUB_AUTH_MODE=classic_pat
GITHUB_TOKEN_FILE=/opt/github-action-service/secrets/github_classic_pat
```

Secret 文件必须是普通文件、权限不宽于 `0600`、内容非空。配置 Token File 时优先读取文件，兼容期才回退 `GITHUB_TOKEN`；Token 不进入日志、源码、命令参数或 Git。
