# MCP 服务集合

这是个人 MCP 服务代码仓库，当前包含两个可以独立部署的服务：

- `services/github-action-service`：Python 服务，提供 GitHub 文件、分支、提交、Pull Request 和 CI 工作流管理能力。
- `services/auto_gupiao-mcp`：Go 服务，提供 A 股观察盘报告和运行记录的只读 MCP 查询能力。

仓库只保存源代码、示例配置、测试和部署说明。真实 Token、API Key、Webhook、数据库、行情缓存、报告和编译产物不会提交到仓库。

## 安全声明

所有服务都应启用鉴权，并通过环境变量或外部 Secret 管理敏感信息。不要把 `.env`、生产配置、私钥、数据库文件或真实行情报告提交到 Git。

如果某个 Token 曾经出现在日志、备份或聊天记录中，应立即在对应平台撤销并重新生成。

## 仓库结构

```text
.
├── README.md
├── SECURITY.md
├── docs/
└── services/
    ├── github-action-service/
    └── auto_gupiao-mcp/
```

## GitHub Action Service

该服务基于 FastAPI 和 MCP Python SDK，主要能力包括：

- 读取 GitHub 文件内容和目录列表；
- 创建分支；
- 以单次提交方式新增、修改或删除多个文件；
- 创建 Pull Request；
- 查询 CI Worker、CI Profile、Job 和日志；
- 触发或取消 CI 工作流；
- 对大文本提交参数做长度、哈希和边界标记诊断。

### 本地运行

```bash
cd services/github-action-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```dotenv
GITHUB_TOKEN=替换为细粒度 GitHub Token
ACTION_API_KEY=替换为随机生成的服务访问密钥
GITHUB_API_URL=https://api.github.com
ALLOWED_REPOSITORIES=owner/repo-a,owner/repo-b
ALLOW_DEFAULT_BRANCH_WRITE=false
```

启动：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Docker 运行：

```bash
cd services/github-action-service
cp .env.example .env
docker compose up -d --build
```

Compose 默认只把宿主机 `127.0.0.1:8765` 映射到容器的 8000 端口。生产环境不要直接绑定公网地址。

### 权限建议

GitHub Token 应使用细粒度 Token，并只授予确实需要的仓库和权限。只读使用时不要授予写入权限；需要提交或创建 PR 时，再逐项授予 Contents、Pull requests 和 Actions 的最小权限。

## auto_gupiao-mcp

该 Go 服务提供只读 MCP HTTP 服务，用于查询观察盘结果：

- `get_latest_report`：读取最新报告；
- `list_recent_runs`：查询最近运行记录；
- `get_run_detail`：读取指定运行详情；
- `get_run_note`：读取人工备注。

只读 MCP 不触发日报、不重新拉取行情、不发送钉钉消息、不修改 SQLite、不连接券商，也不执行真实交易。

### 构建和测试

```bash
cd services/auto_gupiao-mcp
go test ./...
go build -o ./bin/autogupiao-mcp ./cmd/autogupiao-mcp
```

### 启动

```bash
cd services/auto_gupiao-mcp
export AUTO_GUPIAO_MCP_TOKEN='请替换为强随机密钥'
./bin/autogupiao-mcp \
  -config configs/server.example.json \
  -addr 127.0.0.1:8090
```

生产配置应复制示例配置后在本机保存为 `configs/server.json`；该文件已被 `.gitignore` 排除。数据库、报告和行情缓存也应放在仓库之外。

### HTTP/MCP 入口

健康检查：

```bash
curl http://127.0.0.1:8090/healthz
```

工具列表：

```bash
curl -s http://127.0.0.1:8090/mcp \
  -H 'Authorization: Bearer 请替换为强随机密钥' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

SSE 入口：

```bash
curl -N http://127.0.0.1:8090/sse \
  -H 'Authorization: Bearer 请替换为强随机密钥'
```

完整的协议、反向代理和 systemd 说明见 `services/auto_gupiao-mcp/docs/只读MCP接入说明.md`。

## 部署原则

1. 复制示例配置，在服务器上填写 Secret；
2. MCP 服务默认监听 `127.0.0.1`；
3. 对外提供服务时使用 HTTPS、反向代理和访问控制；
4. 数据库、报告、行情缓存和日志放在仓库之外；
5. 发布前执行测试和 Secret 扫描；
6. 升级前备份数据库和服务配置。

## 测试清单

Python 服务：

```bash
cd services/github-action-service
pip install -r requirements-dev.txt
pytest
```

Go 服务：

```bash
cd services/auto_gupiao-mcp
go test ./...
```

## 免责声明

`auto_gupiao-mcp` 仅用于策略研究、工程开发、观察盘和模拟验证，不构成投资建议，也不承诺收益。任何真实交易接入都需要单独的权限、风控和人工复核流程。
