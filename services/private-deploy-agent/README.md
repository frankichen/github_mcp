# Private Deploy Agent

这是服务器端的私有 CI/部署 Worker。当前在 `root@de` 上以 `private-deploy-agent.service` 运行，工作目录为 `/opt/private-deploy-agent`。

## 设计边界

- 只处理代码内固定契约中的 `frankichen/sxt` 和 `frankichen/auto_gupiao`；
- 环境、scope、工作区和脚本均来自对应的固定契约；
- 部署脚本路径由代码和受控工作区决定，不接受 MCP 参数传入任意路径；
- `claim_only` 模式只领取任务并交给受控 WSL 流程；
- 运行状态写入受控的 `/var/lib/private-ci`；
- 日志应脱敏，不得输出 Token、密码或 Authorization Header。

## 部署

请从仓库根目录阅读 [`docs/AI重新部署指南.md`](../../docs/AI重新部署指南.md)，再使用 `deploy/` 下的 systemd 和环境变量示例。真实环境文件不应提交 Git。

`deploy-contracts/sxt/` 保存 Worker 依赖的最小发布契约：测试环境 Compose、Nginx 模板、`deploy_gongshi_test.sh` 和 `sync_test_env.sh`。完整的 `frankichen/sxt` 应用源码仍需从它自己的仓库获取；本目录不替代业务应用仓库。

## 依赖

Worker 运行时只使用 Python 标准库；保留空的 `requirements.txt` 作为统一部署入口。

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 验证

```bash
systemctl is-active private-deploy-agent.service
journalctl -u private-deploy-agent.service -n 100 --no-pager
```
