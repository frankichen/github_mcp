# DX-1 ChangeSet runtime-file ingress

MyGithut12 12.9.0 将大 ChangeSet 的传输协议从聊天 inline string 扩展为 byte-exact runtime file，并让 strict dry-run 在服务端冻结短期 prepared artifact。

## 调用协议

`apply_development_change_set` 的 Candidate 来源严格三选一：

- 小 payload：`change_set_json`；上限由 `development_change_set_inline_limit_bytes` capability 返回。
- 大或 exact-byte-sensitive payload：`change_set_file`，同时提供 `expected_change_set_size_bytes`、`expected_change_set_sha256`，可选 `expected_change_set_git_blob_sha`。
- 已通过 strict dry-run 的 write：`prepared_change_set_id`，不得再次附带 raw Candidate。

runtime file 模式严格按以下顺序处理：

```text
download raw bytes
-> byte count
-> SHA-256
-> optional Git Blob SHA
-> strict UTF-8 decode
-> JSON parse
-> ChangeSet validation/canonical hash
-> Session/Workspace/HEAD/blob CAS
```

原始 bytes 不经过 CRLF/LF、Unicode、BOM、trim、strip 或末尾换行规范化。Git Blob SHA 的计算为 `SHA1(b"blob " + decimal_size + b"\0" + raw_bytes)`。

## prepared flow

raw source 的 `dry_run=true` 成功后返回 `prepared_change_set_id`、raw identities、canonical hash、Session/Workspace revisions、HEAD 和 `expires_at`。artifact 默认 TTL 为 1800 秒，metadata 存于 MyGithut12 SQLite，原始 bytes 存于服务数据目录下的 0600 随机文件。

`dry_run=false` 使用 `prepared_change_set_id` 时，服务端重新验证 artifact TTL/state、repository、branch、Session、Workspace、revisions、HEAD、affected paths 和 blob identities，再原子 claim。真实写入继续使用现有 GitHub non-force CAS、atomic Commit、durable read-back、Workspace finalize 和 Session advance。消费、失败或过期后原始 artifact 删除；同一 prepared ID 只能产生一次 Commit。

相同 idempotency key 与相同 write request 可回放已验证结果；同 key 不同 request 返回 `IDEMPOTENCY_CONFLICT`；并发 claim、过期、wrong scope 和重复消费均 fail-stop。

## 下载安全

`change_set_file` 与 `put_generated_files(bundle_file)` 共用同一 runtime-file ingress primitive：只允许 HTTPS/443 和 `*.oaiusercontent.com`，每次 redirect 前重新做 allowlist、DNS 与公网 IP 校验，拒绝 response content encoding，并执行连接/总 timeout、Content-Length 与 streaming byte limit。signed URL 和 Candidate 内容不得进入日志或持久结果。

## capabilities

- `supports_development_change_set_file_ingress=true`
- `supports_prepared_change_set=true`
- `development_change_set_inline_limit_bytes`
- `development_change_set_file_limit_bytes`
- `prepared_change_set_ttl_seconds`

`max_patch_bytes` 仍是业务 patch validation limit，不是 transport limit。
