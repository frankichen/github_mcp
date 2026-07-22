# Webhook 推送和 latest.json 固定入口

本功能用于把每日观察盘结果推送到外部服务器，并生成一个固定入口文件 `latest.json`。

## latest.json

每次 `autogupiao-server` 运行完成后，会写入：

```text
reports/latest.json
```

可以通过 `report.latest_file` 修改路径：

```json
"report": {
  "latest_file": "reports/latest.json"
}
```

`latest.json` 包含：

- run_id
- trade_date
- generated_at
- risk_level
- conclusion
- total_return_pct
- max_drawdown_pct
- trades
- win_rate_pct
- profit_factor
- markdown_url
- trades_url
- equity_url

如果通过 Cloudflare/CDN 访问，建议对 `latest.json` 设置不缓存，或者访问时加查询参数，例如：

```text
https://你的域名/auto_gupiao_reports/latest.json?t=20260522
```

## Webhook 推送

配置示例：

```json
"notify": {
  "webhook": {
    "enabled": true,
    "url": "https://你的服务器/api/stock-report",
    "secret": "强随机密钥"
  }
}
```

当 `notify.webhook.enabled=true` 时，日报生成后会 POST JSON 到指定 URL。

## Webhook Header

系统会发送：

```text
X-Auto-Gupiao-Timestamp
X-Auto-Gupiao-Signature
X-Auto-Gupiao-Event
X-Auto-Gupiao-Idempotency-Key
```

事件名：

```text
daily_report.generated
```

幂等 Key 格式：

```text
auto_gupiao:daily_report.generated:{trade_date}:{run_id}
```

## 签名

如果配置了 `notify.webhook.secret`，系统会计算：

```text
HMAC-SHA256(secret, timestamp + "\n" + body)
```

签名结果为 hex 字符串，放入：

```text
X-Auto-Gupiao-Signature
```

接收端建议校验：

- timestamp 是否过期，例如 5 分钟内
- signature 是否正确
- event 是否在白名单中
- idempotency key 是否已处理过

## 运行锁

为避免 systemd 定时任务和手动/MCP 触发并发运行，新增运行锁：

```json
"runtime": {
  "lock_file": "data/locks/daily.lock"
}
```

如果锁文件存在，新的运行会失败并提示已有任务正在执行。

正常运行结束后，锁文件会自动删除。

如果进程被强制杀死，可能残留锁文件。确认没有任务运行后，可以手工删除：

```bash
rm -f data/locks/daily.lock
```

## 安全边界

Webhook 只推送观察盘报告，不连接券商，不执行真实交易。

敏感配置不要提交 GitHub：

- webhook secret
- 钉钉 webhook
- Tushare token
