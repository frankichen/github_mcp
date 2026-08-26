# MCP 与 CI 部署服务

本仓库包含四个相互独立的服务边界：

- `services/github-action-service`：现网 MyGithub09 Controller 源码，负责 GitHub API、MCP 工具、CI 查询和部署意图编排。
- `services/private-ci-agent`：Private CI 执行 Agent，负责受控容器任务、日志和源码镜像。
- `services/private-deploy-agent`：服务器 `root@de` 上运行的私有部署 Worker，负责领取部署队列、记录状态并按受控策略执行测试环境部署。
- `services/private-ci-deploy-executor`：WSL/发布 Executor 的脚本、测试和 systemd 模板。

股票选股和股票 MCP 服务已经从仓库移除。

## 两端关系

```text
AI / MCP 客户端
        │
        ▼
github-action-service
  GitHub API / MCP / CI / 部署编排
        │ 受控部署队列
        ▼
private-ci-agent（CI 执行端）
        │ 私有 CI 结果
        ▼
private-deploy-agent（服务器端）
  claim_only 或受控执行
        │
        ▼
测试环境 / 发布工作区
```

本仓库不包含生产环境 Secret、`.env`、数据库、私有 SSH 密钥、Xray 配置和运行数据。部署时必须从 Secret 管理系统或服务器受限环境文件注入。

## 快速入口

- AI 重新部署：[`docs/AI重新部署指南.md`](docs/AI重新部署指南.md)
- 迁移与部署说明：[`docs/迁移与部署说明.md`](docs/迁移与部署说明.md)
- MyGithut12 规划总览：[`docs/MYGITHUB12规划总览.md`](docs/MYGITHUB12规划总览.md)
- MyGithut12 需求文档：[`docs/MYGITHUB12需求文档.md`](docs/MYGITHUB12需求文档.md)
- MyGithut12 开发设计：[`docs/MYGITHUB12开发设计.md`](docs/MYGITHUB12开发设计.md)
- MyGithut12 开发清单：[`docs/MYGITHUB12开发清单.md`](docs/MYGITHUB12开发清单.md)
- MyGithut12 验收清单：[`docs/MYGITHUB12验收清单.md`](docs/MYGITHUB12验收清单.md)
- MyGithut12 DX-1 规划总览：[`docs/MYGITHUB12_DX规划总览.md`](docs/MYGITHUB12_DX规划总览.md)
- MyGithut12 DX-1 需求文档：[`docs/MYGITHUB12_DX需求文档.md`](docs/MYGITHUB12_DX需求文档.md)
- MyGithut12 DX-1 开发设计：[`docs/MYGITHUB12_DX开发设计.md`](docs/MYGITHUB12_DX开发设计.md)
- MyGithut12 DX-1 接口变更：[`docs/MYGITHUB12_DX接口变更清单.md`](docs/MYGITHUB12_DX接口变更清单.md)
- MyGithut12 DX-1 开发清单：[`docs/MYGITHUB12_DX开发清单.md`](docs/MYGITHUB12_DX开发清单.md)
- MyGithut12 DX-1 验收与蓝绿发布：[`docs/MYGITHUB12_DX验收与蓝绿发布.md`](docs/MYGITHUB12_DX验收与蓝绿发布.md)
- 安全说明：[`SECURITY.md`](SECURITY.md)
- GitHub/MCP 服务：[`services/github-action-service`](services/github-action-service)
- Private CI Agent：[`services/private-ci-agent`](services/private-ci-agent)
- 私有部署 Worker：[`services/private-deploy-agent`](services/private-deploy-agent)
- WSL Executor：[`services/private-ci-deploy-executor`](services/private-ci-deploy-executor)

## GitHub Action Service

MyGithut12 当前版本为 `12.2.1`。所有 Commit 类写入在返回成功前都必须完成 GitHub fresh read-back：目标 branch HEAD、新 Commit、Commit Tree 和 changed-path Blob 必须与本次写入严格一致；只有 durable verify 通过后才允许推进 Workspace CAS 与 `success_verified` 幂等状态。小范围唯一文本替换优先使用 `replace_github_text_once`，大文件仍使用 manifest、chunk read/upload 和 finalize/commit 流程，不退化为普通全文提交。

ChatGPT/MCP 分块上传使用 transport-safe 合同：`max_upload_chunk_bytes=24576`，推荐 `recommended_upload_chunk_bytes=16384`。UTF-8 Patch、源码和文档优先使用 `text` 字段，`content_base64` 仅用于必须按二进制传输的内容；非法 Base64、空 payload 或同时传两种编码会返回稳定错误码，不再降级成泛化 `INTERNAL_ERROR`。多文件 `finalize -> apply_development_change_set` 的原子 Commit 语义保持不变。

MyGithut12 现在区分 canonical production Schema 与 compatibility registration：兼容层注册 163 个工具，生产默认 Schema 向 AI 暴露 160 个工具，并隐藏 `get_github_file`、`commit_github_files`、`get_test_deployment_logs` 三个 deprecated 工具；旧 handler 保留兼容调用能力。`get_mygithub_capabilities` 会基于当前实际可见 Schema 返回 `tool_schema_sha256`、`schema_generation_id`、可见工具数和兼容工具数，从而可以直接识别 Connector Schema 是否同步。新增只读 `plan_private_ci_job` 会在启动 CI 前按准确 Commit、仓库固定 policy、Manifest/workspace 和固定入口判断 `applicable/reason/detected_stacks/selected_profiles/workspaces`，但不会排队执行 CI。

MCP tool result 默认以真正的 `structuredContent` 对象返回；安全 inline budget 为 32 KiB，超过预算的完整 payload 会保存为短期 `mygithub12://response/...` Resource，并在小型 inline summary 的 `response_meta` 中返回 `inline_bytes`、`total_bytes`、`truncated`、`resource_uri`、`has_more` 和 SHA-256。`get_private_ci_job` 默认 `detail_level=summary`，只返回门禁所需状态；`detail_level=full` 保留 command、changed files、evidence 和 step offsets，但仍受统一 resource fallback 保护。

GitHub 认证支持 PAT Secret 文件和 GitHub App installation token。GitHub App 模式会在内存中缓存短期 token，并在到期前自动刷新；状态工具只返回认证类型、installation ID 和过期时间，不返回凭据。服务默认应只监听 `127.0.0.1`，通过 HTTPS 反向代理或安全隧道提供 MCP 访问。

```bash
cd services/github-action-service
python3 -m venv .venv
source .venv/bin/activate
pip install -c constraints.txt -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

生产镜像同时使用 `constraints.txt` 固定传递依赖；更新直接依赖后必须重建镜像并重新生成约束文件。

Docker：

```bash
cd services/github-action-service
cp .env.example .env
docker compose up -d --build
```

必须配置 PAT（优先 `GITHUB_TOKEN_FILE`）或 GitHub App 三项配置、`ACTION_API_KEY`，并按最小权限设置 `ALLOWED_REPOSITORIES` 和 `ALLOW_DEFAULT_BRANCH_WRITE`。
`ALLOWED_REPOSITORIES` 默认拒绝全部仓库；只有显式设置为逗号分隔仓库列表或明确设置为 `*` 才会放行。`/health` 仅表示进程存活，`/ready` 检查 GitHub 配置和 Controller 数据库，受 API Key 保护的 `/metrics` 提供低基数请求计数与累计耗时。

## MyGithut12 运行状态

MyGithut12 `12.2.1` 的 compatibility registration 为 163 个工具，canonical production Schema 为 160 个可见工具。除 Schema 降噪和 CI Profile 预检外，MyGithut12 自身成功合并 PR 后还会立即为新的 base/main exact SHA 请求 Repository Index identity；现有 same-tree cache 可以在 Tree 未变化时 100% 复用索引数据，同时仍保留新 Commit 独立合法的 index identity。Repository Index 数据格式没有改变，因此 `repository_index_version` 继续为 `12.0.0-1`。

## Private Deploy Agent

该服务从 `root@de` 的 `/opt/private-deploy-agent/app` 提取了非敏感源码。服务器当前 systemd 服务名为 `private-deploy-agent.service`，实际环境文件在服务器的 `/etc/private-ci/deploy-worker.env`，不会提交到 Git。

Worker 支持 `claim_only` 模式：只领取部署任务并把执行交给受控的 WSL 流程；生产部署时必须明确审核执行模式、工作区、目标环境和回滚策略。

详细部署步骤、变量表、systemd 模板、健康检查和故障处理请阅读 [AI 重新部署指南](docs/AI重新部署指南.md)。

## 安全底线

- 不提交 `.env`、Token、API Key、密码、Webhook Secret、SSH 私钥或 TLS 私钥；
- 不把任意 SSH、Shell、主机或脚本路径暴露为 MCP 参数；
- 默认禁止直接写默认分支；
- 部署前执行计划检查、变更范围检查和人工确认；
- 任何 Token 一旦出现在日志、备份或聊天记录中，立即撤销并轮换。
