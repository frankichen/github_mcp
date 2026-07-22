# AI 开发协作约定

本文件是本仓库的 AI / Codex / ChatGPT 协作入口。新的会话在修改代码前必须先阅读本文件。

## 固定开发流程

除非用户明确要求紧急直接修改 `main`，否则所有代码改动必须走 Pull Request 流程：

1. 从最新 `main` 创建功能分支。
2. 在功能分支提交代码，提交备注使用中文。
3. 创建 Pull Request 到 `main`。
4. 等待 GitHub Actions `Go CI` 跑完。
5. 使用 GitHub 工具读取 PR 对应的 workflow run、job、step 状态。
6. 如果 CI 失败，读取日志、修复、再次提交，并重复等待 CI。
7. 只有 CI 结论为 `success` 后，才允许合并到 `main`。
8. 合并后再次确认 `main` 最新提交。

## 禁止事项

- 不要在未经过 CI 的情况下宣布开发完成。
- 不要把真实 token、服务器私钥、账号密码写入仓库。
- 不要提交 `data/cache/`、`data/bars/`、`data/account/`、`reports/` 下的本地生成文件。
- 不要把临时测试 CSV 提交进仓库。
- 不要绕过 `go test ./...`、`go build` 和 CLI smoke tests。

## 必跑检查

本仓库的 GitHub Actions 会执行：

- `gofmt` 检查。
- `go test ./...`。
- `go build -o /tmp/autogupiao ./cmd/autogupiao`。
- CLI smoke tests：`select`、`backtest`、`simulate`、`plan`、`paper`、`report`、`daily`。

本地修复后也建议先运行：

```bash
gofmt -w .
go test ./...
go build ./cmd/autogupiao
```

## 当前系统定位

当前系统是中国 A 股自动选股研究系统，主要能力包括：

- CSV 选股。
- AKShare 免费数据源。
- AKShare 缓存和指标增强。
- Tushare 可选数据源。
- basic / full 策略档位。
- 历史回测。
- 单次模拟盘。
- 连续模拟盘 paper 模式。
- 数据质量校验。
- 每日任务计划 plan 模式。
- paper 报告导出 report 模式。
- 一键 paper + 报告 daily 模式。

## 推荐后续开发方向

优先级从高到低：

1. 长周期真实数据集生成和连续模拟验证。
2. 定时 runner / cron 运行说明。
3. 报告图表化。
4. 策略参数配置文件化。
5. 服务器部署 workflow。
6. 实盘交易接口。实盘前必须先完成更严格风控和长期模拟验证。
