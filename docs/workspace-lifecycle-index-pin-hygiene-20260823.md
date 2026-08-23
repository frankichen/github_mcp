# MyGithut12 Multi-window Workspace Lifecycle & Index Pin Hygiene

Date: 2026-08-23
Repository: `frankichen/github_mcp`
Baseline: `main@587d880636f20835e341cedced6c00fd966cd658`
Development branch: `ai/workspace-lifecycle-pin-hygiene-20260823`

## 1. Problem statement

MyGithut12 supports multiple ChatGPT windows working on different `ai/*` branches through independent development workspaces. The existing write path is safe: a workspace owns one repository/branch pair, writes require a valid lease, workspace revision CAS, and exact GitHub branch HEAD verification.

The index retention system introduced in PR #48 protects `index_commit_sha`, `base_commit_sha`, and `head_sha` for every workspace whose status is `active` or `drifted`. This is intentionally conservative, but it has one lifecycle leak: a workspace can remain `active`/`drifted` indefinitely after its write lease expires, so its rebuildable index snapshots remain permanently protected from LRU GC even when the development window has been abandoned.

Production evidence on 2026-08-23 showed 15 `frankichen/sxt` workspaces with status `active` and expired leases. Their Git branches and workspace history must be preserved, but they must not create permanent index-cache pins.

## 2. Goals

1. Preserve safe multi-window, multi-branch parallel development.
2. Keep write authorization semantics unchanged: an expired lease is never writable.
3. Decouple write lease validity from rebuildable index-cache retention.
4. Give temporarily idle windows a bounded index-pin grace period to avoid unnecessary rebuild churn.
5. Stop expired abandoned workspaces from permanently bypassing per-repository LRU retention.
6. Preserve workspace branch, base/head identity, scope, revision history, PR association, and audit history.
7. Make index metadata reporting use the exact same pin semantics as GC.
8. Allow an expired workspace to resume through the existing lease-renew flow without creating a second workspace for the same branch.

## 3. Non-goals / safety boundaries

This change MUST NOT:

- delete, rename, merge, rebase, reset, or otherwise move any Git branch;
- automatically close a workspace merely because its lease expired;
- clear `base_commit_sha`, `head_sha`, `tree_sha`, `scope_json`, `pr_number`, or historical workspace records;
- weaken `workspace_write_preflight()` lease, revision-CAS, or exact-HEAD checks;
- allow two active/drifted workspaces to own the same repository/branch;
- change the current per-repository index-retention default of 50 snapshots;
- delete `jobs` or `workspaces` rows during index GC;
- require an index to exist before a workspace lease can be renewed;
- make GitHub source data depend on SQLite cache availability.

## 4. Lifecycle model

### 4.1 Write lease

`lease_valid` remains:

- `status == active`; and
- `lease_expires_at > now`.

Only a valid write lease permits AI writes. No grace period applies to writes.

### 4.2 Index-pin lease

Add a separately configurable grace period:

`MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS`

Default: `86400` seconds (24 hours).

A workspace contributes index-cache pins when:

- status is `active` or `drifted`; and
- `lease_expires_at + pin_grace_seconds > now`.

This keeps recently idle/drifted development windows warm without allowing abandoned workspaces to pin cache indefinitely.

`0` means no post-expiry grace. Values are bounded to a safe maximum of 7 days.

### 4.3 What remains pinned

For a workspace whose index-pin lease is active, protect all non-null:

- `index_commit_sha`;
- `base_commit_sha`;
- `head_sha`.

Queued/running index jobs continue to independently protect their target/base commits.

### 4.4 Expired beyond grace

Once the write lease has expired beyond the pin grace:

- workspace row remains unchanged;
- branch remains unchanged;
- scope/history remain unchanged;
- writes remain rejected;
- workspace no longer protects rebuildable index snapshots from LRU GC;
- an index may therefore be evicted if it falls outside normal retention.

### 4.5 Resume behavior

When the user returns to an expired `active` workspace:

1. resolve the existing workspace rather than creating a duplicate owner for the same branch;
2. renew lease with existing revision CAS;
3. write safety becomes valid again only after renewal succeeds;
4. index pin becomes active immediately;
5. if the referenced index was already evicted, normal index status returns `not_found` and the index is rebuilt on demand;
6. no Git branch movement occurs merely because the workspace resumed.

A `drifted` workspace remains non-writable and must use the existing refresh/recovery flow; pin grace does not hide branch drift.

## 5. Implementation plan

### Phase A - Single source of truth for workspace index-pin eligibility

- Add bounded config parser for `MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS`.
- Add helper that evaluates workspace cache-pin activity from status, lease expiry, current time, and grace.
- Keep `lease_valid` semantics unchanged.
- Expose derived `index_pin_active` and `index_pin_grace_expires_at` in workspace responses for observability.

### Phase B - Retention consistency

- Split workspace pin collection from active index-job pin collection.
- Make `_protected_index_commits()` combine both sets.
- Require workspace lease/grace eligibility before `index/base/head` are protected.
- Re-evaluate eligibility immediately before each deletion, preserving the existing race-safety check.
- Make `list_repository_indexes().pinned_by_workspace` use the same workspace-pin helper, not a separate status-only query.

### Phase C - Resume and concurrency regression coverage

- Verify lease renewal after expiry makes `index_pin_active=true` without changing branch/head/base/scope.
- Verify an expired workspace remains non-writable until renewed.
- Verify multiple different branches remain independently supported.
- Verify same repository/branch uniqueness remains enforced while workspace status is active/drifted.
- Verify drifted workspace pin grace does not make it writable.

### Phase D - Production rollout

- Run full Private CI for exact branch SHA.
- Require GitHub checks + Private CI merge readiness.
- Squash merge to `main`.
- Build exact-main production Controller image using the established offline overlay path if dependency identity is unchanged.
- Canary on a copy/synthetic database covering expired-within-grace, expired-beyond-grace, renew-after-expiry, and write rejection.
- Preserve current Controller as rollback.
- Deploy Controller only; Worker code is not changed.
- Validate health, workspace API output, retention preview, exact-main Private CI, worker idle state, and Attestation.

## 6. Acceptance criteria

### AC-1 Multi-window isolation remains intact

Given two active workspaces for the same repository on different `ai/*` branches, both remain valid independently and neither changes the other's revision, lease, head, or pin state.

### AC-2 Same-branch ownership remains exclusive

Two `active`/`drifted` workspace rows cannot own the same `(repository, branch)` pair. Existing uniqueness protection remains in force.

### AC-3 Expired lease cannot write

For an `active` workspace with `lease_expires_at <= now`, `workspace_write_preflight()` returns `WORKSPACE_LEASE_REQUIRED`, including during the index-pin grace period.

### AC-4 Pin grace is independent from write lease

With default 24-hour grace:

- lease valid -> `index_pin_active=true`;
- lease expired by less than 24 hours -> write invalid, `index_pin_active=true`;
- lease expired by more than 24 hours -> write invalid, `index_pin_active=false`.

### AC-5 Expired workspace no longer pins forever

A workspace beyond grace does not add `index_commit_sha`, `base_commit_sha`, or `head_sha` to the retention-protected commit set. A snapshot outside normal LRU retention can be pruned.

### AC-6 Recently expired / drift-recovery window remains warm

An `active` or `drifted` workspace still inside pin grace protects its index/base/head snapshots.

### AC-7 Renewal restores pin without moving Git

Renewing an expired active workspace through revision CAS:

- increments revision;
- renews `lease_expires_at`;
- returns `lease_valid=true` and `index_pin_active=true`;
- preserves repository, branch, base commit, head commit, tree, scope, and PR fields.

### AC-8 Evicted index is rebuildable

If an expired workspace's historical index was evicted before renewal, renewal itself succeeds. The index may report `not_found`; a normal index build can recreate it from GitHub.

### AC-9 Reporting matches retention

`list_repository_indexes().pinned_by_workspace` uses the same grace-aware workspace pin eligibility as GC. There is no status-only reporting path that says an indefinitely expired workspace is pinned.

### AC-10 Index-job safety is unchanged

Queued/running index job target/base commits remain protected regardless of workspace lease state.

### AC-11 No destructive workspace migration

Deployment does not delete or auto-close existing workspace rows and does not modify Git branches. Existing expired workspace metadata remains queryable after deployment.

### AC-12 Full regression gates

The exact PR SHA must pass:

- Controller Ruff;
- Controller compileall;
- Controller full pytest;
- Private CI Agent Ruff/compileall/full pytest;
- Private Deploy Agent Ruff/compileall/full pytest;
- GitHub checks;
- `git diff --check`.

### AC-13 Production runtime verification

After deployment:

- `/health` is `ok` on exact merged main SHA;
- Private CI worker is online/idle;
- a production workspace sample demonstrates grace-aware `index_pin_active` behavior;
- retention preview shows stale expired workspaces no longer add permanent pins;
- exact-main Private CI passes;
- final Attestation validates `ok=true` and `reusable=true`.

## 7. Rollback

Controller-only rollback is sufficient because this change adds no schema migration and does not mutate workspace rows during startup. Reverting the Controller immediately restores the previous conservative status-only pin behavior. Git branches, workspace history, and index data already retained are unaffected.
