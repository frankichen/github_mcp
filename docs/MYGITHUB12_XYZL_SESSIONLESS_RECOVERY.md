# MyGithut12 12.9.25+ legacy Workspace first-session recovery

## Why this exists

XYZL A #527, B #531 and C #532 each own an original, valid DevHub Writer Lease and a unique existing MyGithut12 Workspace. A/B Workspaces are drifted after independently verified forward GitHub commits; C's Workspace remains active. Read-only production SQLite queries on 2026-10-17 showed **zero** Development Session rows for those three Workspace IDs and branches, even though other `frankichen/xyzl` sessions exist. Consequently, the old pair-recovery tool cannot operate: there is no original Session ID to recover. An operator must **not invent an original ID** or create another Workspace or branch.

## New explicit capability

`recover_sessionless_development_workspace` is added alongside the existing pair-recovery API, without weakening its contract. The new capability initializes the **first canonical Development Session** bound to the original Workspace only when *no prior session for that Workspace or branch exists*. It preserves repository, branch, Workspace ID, original owner, pinned base and exact Git HEAD/Tree. Existing DevHub Task/Lease authorization remains required for subsequent Worker operations; this tool does not modify DevHub.

1. Default `confirm=false` is a read-only preview (apart from standard access diagnostics). For drifted Workspace, `expected_previous_head_sha` must match the durable `index_commit_sha`; the indexed commit must be an ancestor of the exact GitHub HEAD, with a bounded rename-aware GitHub comparison. An exact active Workspace needs the same previous/current SHA.
2. Base branch/pinned SHA, owner, Workspace revision, HEAD, Tree, scope and overlap must be freshly checked. No ambiguous or force-pushed histories, other sessions, duplicate branch owners, or high-overlap Workspaces may proceed.
3. Preview reports exact `required_scope_expansion_paths`. Operator inspects actual diff and changes no arbitrary file scope; `confirm=true` requires the exact reviewed list and a stable idempotency key.
4. On confirm, a single SQLite transaction rechecks all durable CAS identities and fresh GitHub identity, then activates the same Workspace, persists exact reviewed scope and creates the first Session with the same owner and lease. Failed audit/session insert rolls back Workspace changes. Idempotent replays cannot advance revisions or renew leases.
5. Index readiness is separate; no device, UI, authentication, test, Reviewer, or Manager acceptance is implied. No branch creation, Git write, merge, deployment of XYZL, or stolen Writer Lease occurs.

## Operator-only alternate entrypoint

Existing MCP OAuth authorization remains unchanged. To avoid reading or reusing tokens, an authorized local Controller administrator can use the same validated function with:

```sh
docker exec -i github-action-service python -m app.maintenance_legacy_workspace < request.json
# Inspect exact preview paths and authoritative DevHub lease/scope first.
docker exec -i github-action-service python -m app.maintenance_legacy_workspace --confirm < reviewed-request.json
```

The request JSON contains only the tool's exact CAS/identity fields, **not credentials**. The CLI has the same safety gates and prints bounded audit-friendly metadata. It does not override security refusals on login/Android tests, binary ingress, network credentials, physical devices or deployments.

## Production guardrails

- Deploy only a pushed exact commit with immutable build SHA, using the documented local Controller deployment and de Nginx edge unchanged.
- Before any production DB mutation, require a **PASSED** official `mygithub-sqlite-backup@<unique-id>.service` backup with a verified immutable output. Never use an ad hoc unbounded SQLite backup or delete an active partial output.
- Run focused tests and regression, verify the public MCP route, current capabilities, private CI Worker and current GitHub/Workspace identities. Save the previous image/config/mounts. Roll back *image only*, never restore an older DB snapshot over successful Writer advances.
- Recovery may be performed only after fresh DevHub canonical Task/Revision/Writer Lease and owner verification. If a Worker is actively writing, coordinate the CAS window rather than forcibly rotating the Session.
- Maintain HA#93 / HA#98 as unresolved until each exact workspace and first Session is read back, DevHub session activities verify, and the Worker demonstrably can resume. Do not claim 47-screen Android acceptance.

## 2026-10-17 candidate identities (MUST fresh-read, never blindly replay)

| Slot | Task | Workspace | Initial state | Initial CAS |
|---|---|---|---|---|
| WORKER-A | 527:1 | `ws_7e45dd3c379b4185` | drifted | 41 |
| WORKER-B | 531:1 | `ws_e947d2443ad4475c` | drifted | 29 |
| WORKER-C | 532:1 | `ws_677d3386456a4fb0` | active | 28 |

A/B exact HEAD is respectively `bf6bc0fd0b0c2e90f3ceebda54c656e2b3c03afc` and `f7fb2398369cce2b82efbe473c39d180c9bd9d06`; C `eac90361b63fcde4f0540254d8fdbf9486f6f325`. Those are historic inputs only, not executable authorization.
