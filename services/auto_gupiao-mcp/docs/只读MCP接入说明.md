# 只读 MCP 接入说明

本功能提供一个只读 MCP HTTP 服务，用于查询观察盘结果。

## 安全边界

只读 MCP 不会：

- 触发日报运行
- 重新拉取行情数据
- 发送钉钉消息
- 修改 SQLite
- 连接券商或同花顺
- 执行真实交易

第一版只支持查询。

## 命令

```bash
./bin/autogupiao-mcp -config configs/server.json -addr 127.0.0.1:8090
```

也可以通过环境变量设置访问令牌：

```bash
export AUTO_GUPIAO_MCP_TOKEN='change-me'
./bin/autogupiao-mcp -config configs/server.json -addr 127.0.0.1:8090
```

如果传入 `-token`，会覆盖环境变量：

```bash
./bin/autogupiao-mcp -config configs/server.json -addr 127.0.0.1:8090 -token 'change-me'
```

## HTTP 入口

健康检查：

```bash
curl http://127.0.0.1:8090/healthz
```

MCP JSON-RPC：

```bash
curl -s http://127.0.0.1:8090/mcp \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

ChatGPT 连接器兼容入口：

```bash
curl -N http://127.0.0.1:8090/sse \
  -H 'Authorization: Bearer change-me'
```

`/sse` 会返回 `endpoint` 事件，格式为 `/messages?sessionId=<id>`。`/messages` 同时兼容带 `sessionId` 和不带 `sessionId` 的 JSON-RPC 请求：

```bash
curl -s 'http://127.0.0.1:8090/messages?sessionId=test' \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"chatgpt","version":"1.0"}}}'
```

服务端兼容 `2024-11-05`、`2025-03-26`、`2025-06-18` 三个 MCP protocolVersion；未知版本会降级到当前默认版本。

## 工具

### get_latest_report

读取 `reports/latest.json`。

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_latest_report","arguments":{}}}
```

### list_recent_runs

读取 SQLite 最近 N 次运行记录。

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_recent_runs","arguments":{"limit":10}}}
```

### get_run_detail

读取指定 `run_id` 的详情。

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_run_detail","arguments":{"run_id":5}}}
```

### get_run_note

读取指定 `run_id` 的人工备注。

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_run_note","arguments":{"run_id":5}}}
```

## systemd 示例

`/etc/systemd/system/auto-gupiao-mcp.service`：

```ini
[Unit]
Description=Auto Gupiao read-only MCP server
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/dly/auto_gupiao
Environment=TZ=Asia/Shanghai
Environment=AUTO_GUPIAO_MCP_TOKEN=change-me
ExecStart=/home/dly/auto_gupiao/bin/autogupiao-mcp -config /home/dly/auto_gupiao/configs/server.json -addr 127.0.0.1:8090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
systemctl daemon-reload
systemctl enable --now auto-gupiao-mcp.service
systemctl status auto-gupiao-mcp.service --no-pager
```

## Nginx 示例

```nginx
location /auto_gupiao_mcp/ {
    proxy_pass http://127.0.0.1:8090/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

外部地址示例：

```text
https://example.com/auto_gupiao_mcp/mcp
```

## 建议

- 必须设置强随机 `AUTO_GUPIAO_MCP_TOKEN`。
- 不要暴露无鉴权 MCP。
- 初期只开放内网或 Cloudflare 保护后的地址。
- 写入类能力以后单独评估，不放在这个只读服务里。
