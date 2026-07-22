# 安全说明

## 不要提交的内容

- `.env`、生产配置和包含真实值的配置文件；
- GitHub、Tushare、MCP、Xray、钉钉等 Token、API Key、Secret；
- SSH 私钥、TLS 私钥和证书私钥；
- SQLite、行情缓存、报告、日志和 Docker 数据卷；
- `.venv`、`node_modules`、Go 编译产物和 Python 缓存。

## Secret 管理

推荐使用部署平台的 Secret、systemd 的受限环境文件或密码管理器注入环境变量。生产配置文件的权限应设置为仅服务用户可读。

## 泄漏处置

如果发现 Token 被提交：

1. 立即在发行平台撤销或轮换 Token；
2. 检查 GitHub Actions、服务器访问日志和仓库审计日志；
3. 从当前工作树删除敏感文件；
4. 不要只依赖删除提交，因为 Git 历史和缓存可能仍保留旧内容；
5. 使用新的 Secret 重新部署。
