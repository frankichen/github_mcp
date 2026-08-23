# MyGithut12 Index Build Fast Path V1 - Development Task and Acceptance Plan

Date: 2026-08-23
Repository: frankichen/github_mcp
Baseline: main@926c2a3f613fb0e628e4c82c3d3a1382b36564b3
Scope: MyGithut12 rebuildable exact-commit code index only

## 1. Problem statement

Production measurements on frankichen/sxt show that incremental indexing is still too slow for multi-window development. The repository has roughly 2,150 eligible tree entries while a normal branch update changes only 1-10 text files. A production force-incremental rebuild of commit 4aafb54305aed33a7b3ba777f63cd9b6270e52c5 from base e9a004dc02c13e0de7302b400ac10a97f4751ac9 took 81.17 seconds even though 2,114 files were reused and only 4 text files were reindexed.

Read-only profiling established:

- Parsing all ~2,125 indexed text files / ~14,324 symbols: ~0.33s.
- Reading all files rows: ~0.03s.
- Reading all symbol rows: ~0.05s.
- GitHub recursive tree request: ~2.1s.
- Normal single GitHub blob request: ~0.7-0.9s.
- 30 PNG files and 2 Gradle wrapper JARs are repeatedly downloaded on every incremental build, fail UTF-8 decoding, and are discarded because binary negatives are not retained.
- Current worker persists progress and polls cancellation once per tree entry.
- Unchanged blobs still run symbol extraction again.
- Ready indexes with an identical tree_sha are not reused across commit identities.

## 2. Goals

1. Preserve exact commit/tree identity and GitHub as source of truth.
2. Reduce typical sxt incremental build latency from ~81s to <=15s.
3. Reduce an identical-tree rebuild to <=3s.
4. Remove repeated network fetches for known binary formats before blob download.
5. Batch progress/cancellation persistence without weakening cancellation correctness.
6. Reuse unchanged symbol metadata while regenerating commit-scoped symbol IDs.
7. Fetch changed blobs with bounded concurrency (default 4, max 8).
8. Persist phase timing evidence in the immutable index manifest for future profiling.
9. Preserve bounded LRU retention and workspace/job pins introduced in prior releases.

## 3. Non-goals

- No local Git bare mirror/object cache in V1.
- No SQLite schema migration.
- No change to search APIs, symbol semantics, or authoritative GitHub identity checks.
- No indexing of binary assets.
- No Worker/private CI runtime change.
- No unbounded network concurrency.

## 4. Design

### 4.1 Known binary path prefilter

Introduce an explicit binary suffix denylist for formats that the current UTF-8 index never usefully stores: common raster images, archives, Java/Android binaries, fonts, media, office binary/zip formats, native objects and SQLite files. These entries are excluded before get_git_blob.

Unknown extensions retain the existing UTF-8 decode probe. A decode failure remains a safe skip.

### 4.2 Progress/cancel pulse

Replace per-file progress/cancel DB activity with one combined pulse. Defaults:

- every 32 processed files, OR
- every 250ms,
- plus forced pulses at boundaries/completion.

A pulse reads cancel_requested and persists progress in one SQLite connection/transaction. Configuration is bounded and invalid values fall back safely.

### 4.3 Unchanged symbol reuse

A current-version ready base snapshot is loaded once. For a path whose blob_sha is unchanged:

- reuse content/digest/line count,
- reuse symbol metadata,
- regenerate symbol_id using the target commit SHA,
- do not rerun AST/regex symbol extraction.

### 4.4 Bounded concurrent changed-blob fetch

Changed blob SHAs are de-duplicated and submitted to a ThreadPoolExecutor. Default worker count is 4, bounded to 1..8. The main deterministic assembly loop consumes the futures in tree order; exact output ordering/identity is unchanged.

### 4.5 Same-tree fast path

For auto/incremental builds, before requesting the recursive tree, search for a ready index in the same repository with identical tree_sha and INDEX_VERSION but a different commit SHA.

If found:

- clone file rows from the ready source,
- clone symbol metadata while regenerating target commit-scoped symbol IDs,
- write a new immutable index manifest with build_strategy=tree_reuse,
- perform no recursive-tree or blob requests in the worker.

strategy=full deliberately bypasses this fast path.

### 4.6 Telemetry

New index manifests record bounded non-secret telemetry:

- tree_lookup_ms
- tree_fetch_ms
- base_load_ms
- blob_fetch_wait_ms
- assemble_ms
- db_write_ms
- build_total_ms
- retention_ms (added after retention when possible)
- changed_blob_entries
- blob_fetch_requests
- binary_path_skipped_count
- decode_skipped_count

No URLs, tokens, file contents or credentials are recorded.

## 5. Safety and compatibility

- INDEX_VERSION remains unchanged because indexed text/symbol semantics do not change; known binary formats were already discarded after failed UTF-8 decode.
- Existing ready indexes remain readable.
- full strategy remains a true full build and bypasses tree reuse/base symbol reuse.
- Quotas remain enforced.
- Exact repository/commit/tree identity is still produced by resolve_identity before a job is created.
- Same-tree reuse requires repository + tree_sha + current INDEX_VERSION + ready status.
- Any tree/blob/API failure still fails the job; no stale source is silently substituted.
- Retention still runs only after successful index creation.

## 6. Acceptance criteria

AC-01 Binary prefilter: .png and .jar entries are excluded before _decode_blob; unknown non-UTF8 content is still safely skipped.

AC-02 Progress batching: a 2,000-file synthetic build does not persist one progress transaction per file; forced final progress equals progress_total.

AC-03 Cancellation: cancel_requested is detected by the next pulse and the job reaches cancelled without publishing a ready target index.

AC-04 Base validity: incremental reuse is allowed only from a current INDEX_VERSION ready base snapshot.

AC-05 Symbol reuse: unchanged file symbols preserve metadata but target symbol_id is derived from the target commit SHA; no _symbols call is required for unchanged blobs.

AC-06 Changed fetch concurrency: default is 4, bounded 1..8, duplicate blob SHAs generate only one GitHub blob request.

AC-07 Tree reuse: same tree_sha/current INDEX_VERSION produces build_strategy=tree_reuse, correct file/symbol counts/content, target-commit symbol IDs, and no recursive-tree/blob call.

AC-08 Full strategy: strategy=full bypasses tree reuse and incremental base reuse.

AC-09 Telemetry: successful new manifests expose non-secret timings_ms plus binary/fetch counters through get_repository_index_status.

AC-10 Regression: github-action-service Ruff, compileall and full pytest pass; private-ci-agent and private-deploy-agent full gates pass in Private CI.

AC-11 Performance production: sxt representative incremental (~2.1k files, <=10 changed text blobs) completes in <=15 seconds under normal GitHub connectivity.

AC-12 Performance tree reuse: a real existing sxt commit whose tree matches a ready retained snapshot completes in <=3 seconds after request acceptance, excluding the caller's initial resolve_identity network latency.

AC-13 Production integrity: Controller healthy, MyGithut12 ready, SQLite quick_check=ok, active index jobs=0 after verification, Private CI worker online+idle.

AC-14 Search correctness: text and symbol search on the newly built exact commit returns the expected target commit/blob evidence.

AC-15 Release gate: exact-main Private CI passes and its server-side attestation validates ok=true/reusable=true.

AC-16 Rollback: previous Controller image/container remains stopped with restart=no until post-release verification completes.

## 7. Rollback

No database schema change is introduced. Rollback is Controller-only: stop the V1 Controller and restore the immediately previous production Controller image/container. Index rows produced by V1 use the existing schema/version and remain backward-readable. If desired they are rebuildable cache and can be force rebuilt.
