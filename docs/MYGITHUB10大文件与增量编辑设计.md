# MyGithub10 大文件与增量编辑开发契约

版本：`10.0.3`。本文件面向 AI 开发代理和部署审查人员。

## 能力边界

MyGithub10 通过 Git Blob API 获取精确字节，不把大文件正文塞入普通 JSON。`get_mygithub_capabilities` 明确返回当前实现能力；尚未实现的 Tree SHA Attestation 和 Artifact-only deployment 保持 `false`，不得在部署文档中虚报。

核心接口：

| 工具 | 用途 |
|---|---|
| `get_github_file_manifest` | 返回 commit、blob、大小、编码、行数和完整 SHA256，不返回正文 |
| `read_github_file_chunk` | 按 UTF-8 合法边界读取最多 65536 字节 |
| `open_github_file_resource` / `read_github_file_resource` | 打开并分页读取资源 |
| `apply_github_patch` | 严格 unified diff，默认 dry-run，支持多文件原子 commit；新增文件使用 `--- /dev/null` 与 `@@ -0,0 +1,N @@` |
| `edit_github_file_ranges` | 按 1-based inclusive 行号执行 hash 校验后的非重叠编辑；范围基于原始内容计算并按降序应用 |
| `begin/append/finalize/commit/abort_github_file_upload` | 严格 offset、chunk SHA、总大小和总 SHA 的分块上传 |

## 读取协议

先读取 manifest，保存 `resolved_commit_sha`、`blob_sha` 和 `content_sha256`。随后从 `offset_bytes=0` 开始连续读取；每次使用返回的 `next_offset`，并验证 `chunk_sha256`。只有最后一块 `eof=true`，重组后的完整字节 SHA 必须等于 manifest 的 `content_sha256`。

错误码包括：`FILE_NOT_FOUND`、`FILE_BLOB_SHA_MISMATCH`、`FILE_CHUNK_OUT_OF_RANGE`、`FILE_CHUNK_LIMIT_EXCEEDED`、`FILE_ENCODING_UNSUPPORTED`、`FILE_BINARY_UNSUPPORTED`、`FILE_UTF8_BOUNDARY_INVALID`。

## Patch 协议

真实提交前必须传入分支 HEAD SHA；修改文件时通过 `expected_blob_shas_json` 传入旧 blob SHA。上下文必须逐行精确匹配，不允许 fuzzy、3-way、自动 rebase、rename、mode change、submodule 或二进制 patch。

推荐流程：

1. manifest 读取目标文件；
2. 生成严格 unified diff；
3. `dry_run=true` 审查变更路径和 fingerprint；
4. 重新确认 branch HEAD 与 blob SHA；
5. 使用唯一 `idempotency_key` 真实提交；
6. 回读 commit/tree/blob SHA。

HEAD 改变返回 `PATCH_HEAD_CHANGED`（details.error_code=`HEAD_CHANGED`，包含 expected/actual/repository/branch/phase）；文件 blob 改变返回 `BLOB_CHANGED`。失败时不更新任何 branch ref。

## 幂等与审计

写操作使用 `mygithub10_operations` 表，记录 operation id、请求 SHA、脱敏请求摘要、工具名、仓库、分支、期望 HEAD、状态、结果 commit、错误码和脱敏 `result_json`。请求摘要覆盖工具、仓库、分支、expected HEAD、目标路径、expected blob、操作内容摘要/上传 SHA 和提交消息。相同 key 与相同请求返回原始完整结果（不重新读取 HEAD、上传临时文件或创建 Commit）；相同 key 的不同请求返回 `IDEMPOTENCY_CONFLICT`；执行中的重复请求返回 `IDEMPOTENCY_IN_PROGRESS`。

数据库不保存 Token、Authorization Header、完整 patch、完整文件正文或上传内容。上传内容只暂存在权限 `0600` 的临时文件中，成功提交后删除，过期上传由 abort/清理流程删除。

## AI 重新部署步骤

部署前由管理员配置 Secret，AI 不读取或回显 Secret：

```bash
cd services/github-action-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
export MYGITHUB10_BUILD_SHA="$(git rev-parse HEAD)"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

部署前必须执行：

```bash
python3 scripts/validate_tool_manifest.py
pytest -q services/github-action-service/tests
```

默认禁止写默认分支；生产环境需要通过 Secret 文件注入 GitHub Token，并设置最小 `ALLOWED_REPOSITORIES`。本修复不部署、不重启现网、不修改 `frankichen/sxt`，也不改变现有兼容工具字段含义。

## 回滚

回滚只切换到上一个已验证的 Controller 镜像或 Git commit；不要删除 `mygithub10_operations` 审计表，不要删除数据库文件，不要 force push。若 patch 发生并发冲突，保留当前分支 HEAD，重新读取 manifest 后生成新 patch。
