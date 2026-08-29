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
- MyGithut12 DX-2 规划总览：[`docs/MYGITHUB12_DX2规划总览.md`](docs/MYGITHUB12_DX2规划总览.md)
- MyGithut12 DX-2 需求与验收：[`docs/MYGITHUB12_DX2需求与验收标准.md`](docs/MYGITHUB12_DX2需求与验收标准.md)
- MyGithut12 DX-2 开发清单：[`docs/MYGITHUB12_DX2开发清单.md`](docs/MYGITHUB12_DX2开发清单.md)
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

MyGithut12 源码当前版本为 `12.8.0`；生产运行版本必须以 `get_mygithub_capabilities` 的实时结果为准。所有 Commit 类写入在返回成功前都必须完成 GitHub fresh read-back：目标 branch HEAD、新 Commit、Commit Tree 和 changed-path Blob 必须与本次写入严格一致；只有 durable verify 通过后才允许推进 Workspace CAS 与 `success_verified` 幂等状态。AI 日常生成 UTF-8 文本文件继续只有一个推荐入口 `put_generated_files`：普通内容使用 `files[{path,content}]`，超过 inline transport budget 时仍调用同一个工具，由 ChatGPT/Codex runtime 通过顶层 `bundle_file` 交付 version=1 的 JSON 文件包；服务端负责临时文件下载、JSON/UTF-8/路径/大小校验、Workspace/Session CAS、旧 blob 推断、hash、chunk staging、原子 Commit 和 durable read-back。bundle 下载只接受 `*.oaiusercontent.com` 的 HTTPS/443 临时 URL，并在首跳与每次 redirect 前重新校验域名 allowlist 和公网 DNS；窗口不管理 upload/chunk/offset/hash/candidate/expected_blob；V2 仍不支持二进制仓库文件和删除。

Workspace 写 Lease 默认 7200 秒（2 小时），最大仍为 14400 秒（4 小时）。DX2-WS-01 使用 activity-driven renew：仅受控 Development Session 动作在剩余 Lease 不超过 1800 秒时尝试自动续签，并在 fresh GitHub HEAD/Tree、Workspace/Session identity、revision CAS、drift 和有效 Lease 全部成立后，原子同步 Workspace 与 Session revision；闲置窗口不会后台无限续命。已经过期的 Workspace 不会被普通 renew 或自动续签复活，必须显式调用 `resume_development_workspace` 重新验证 branch/base/Tree 后恢复。

DX2-SESSION-01 在 Development Session 活动作业入口增加受控 stale Session recovery：只有 fresh GitHub branch HEAD/Tree 与 Workspace 完全一致、且旧 Session HEAD 能证明是 Workspace HEAD 的祖先时，才允许用 Session revision CAS 前进；真实 external drift 或 identity 无法证明时保持 fail-stop。HEAD 发生恢复时旧 fast/full CI、attestation 和 failure evidence 不再复用，并重新确认或请求 recovered exact HEAD Index；仅 Workspace revision/Lease stale 且 HEAD/Tree 未变时保留仍然 exact-head 的 CI 证据。`session_recovered`、`external_drift_detected`、`recovery_refused` 进入 Development Session event audit，审计持久化异常不会遮蔽主 drift/refusal 错误。

DX2-RESUME-01 新增 `resume_development_task`：新窗口只需提供 `repository + branch` 或 `repository + pull_number`，即可一次 fresh-read policy、main/branch/PR identity、Workspace/Session/Lease、exact HEAD Index、当前 HEAD CI/有效 Attestation、PR readiness 与 active overlap；stale Session 仅复用 DX2-SESSION 安全恢复门禁，过期 Lease 仅在显式参数 + revision CAS 下恢复，绝不自动 rebase、merge、close PR 或 delete branch。返回结果显式区分 live facts、historical evidence 与 candidate next actions。

DRIFT-RECOVERY-01 新增 `recover_drifted_development_task`：仅针对 `status=drifted + drift_reason=branch_moved_externally`，在重新验证 exact branch HEAD/Tree/base、forward-only ancestry、Workspace scope、branch ownership/overlap 与 Workspace/Session revision CAS 后，把当前真实 branch identity 原子推进到控制面；该工具不 reset、不移动 branch、不 merge/rebase、不 commit，也不写仓库文件。`resume_development_task` 遇到 drifted Workspace 仍 fail-stop，只返回正式 recovery tool 指引；恢复成功后 exact-HEAD Index readiness 与控制面恢复结果分离，Index 未 ready 时 Writer 仍不得继续。

兼容工具的低层分块上传继续使用 transport-safe 合同：`max_upload_chunk_bytes=24576`，推荐 `recommended_upload_chunk_bytes=16384`。这些 begin/append/finalize 工具不面向 AI 日常生成文件写入；AI 窗口不要直接管理 upload、offset、chunk SHA 或 finalize，应统一调用 `put_generated_files`。

MyGithut12 现在区分 canonical production Schema 与 compatibility registration：兼容层注册 173 个工具，生产默认 Schema 向 AI 暴露 163 个工具并隐藏 10 个 deprecated/compatibility-only 工具，包括旧低层 upload 和旧高层 put；旧 handler 仅保留兼容调用能力。AI 正常文件写入保留 `put_generated_files`，精确局部修改保留 `edit_github_file_ranges`、`replace_github_text_once` 和 `apply_github_patch`；`recover_drifted_development_task` 属于 canonical Schema，并且 `get_mygithub_capabilities` 显式返回 `supports_drifted_development_recovery=true`。`resume_development_workspace` 仅用于 expired Workspace；`branch_moved_externally` 的 drifted Workspace 必须走受保护的 `recover_drifted_development_task`。`get_mygithub_capabilities` 继续返回 `tool_schema_sha256`、`schema_generation_id`、可见工具数和兼容工具数；只读 `plan_private_ci_job` 会在启动 CI 前按准确 Commit、仓库固定 policy、Manifest/workspace 和固定入口判断 `applicable/reason/detected_stacks/selected_profiles/workspaces`，但不会排队执行 CI。基础设施自部署工具只接受固定仓库、固定环境、固定 scope、exact main、repo-auto-check 和 current-build CAS，不接受 host、shell、script、rollback 或 failure-mode 参数。

基础设施自部署的 `ciworker` 缓存预热通过 systemd 固定 User/Group broker 执行；Infrastructure Executor 本身继续保持 `NoNewPrivileges=true`，并在切换 Controller 前先验证 broker 能精确降权到 UID 1500，避免再次出现 Controller 已切换后才因 `runuser` 失败而把 deployment 标记失败。`get_infrastructure_deployment` 保持原工具名和 `deployment_id` 单参数兼容调用，并增加最长 55 秒的可选持久状态 long-poll、结构化阶段和显式 opt-in 的有限脱敏日志尾；默认响应仍不返回部署日志，也不新增 cancel/rollback/任意脚本能力。

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

MyGithut12 `12.8.0` 源码的 compatibility registration 为 174 个工具，canonical production Schema 为 164 个可见工具；生产 Schema 身份必须以运行时 capability 为准。本版新增 `converge_development_task`：在准确 Development Session revision 与 HEAD/Tree 门禁下，编排 exact-HEAD Index、Change Context、Change Impact、Contract Change Detection、Affected Tests 与 fast/full Private CI，并返回 Failure Pack、Worker 终态和只读 merge eligibility；分析降级时保持 full `repo-auto-check`，且工具不会 merge、deploy、rollback 或移动 branch。Repository Index 数据格式没有改变，因此 `repository_index_version` 继续为 `12.0.0-1`。

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
