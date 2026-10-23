# MyGithut12 Base-Synced Recovery: Base-Absorbed Task Paths

Version: 12.9.23

`recover_base_synced_development_task` adopts an already-forward-synchronized GitHub branch into its existing Workspace and Development Session. It never writes repository files or moves Git refs.

## Base-absorbed classification

A path removed from the new-base Task delta may be classified as `BASE_ABSORBED` only when all of these server-verified facts hold:

1. `old_base -> new_base`, `old_session_head -> current_head`, and `new_base -> current_head` are exact forward ancestry relations.
2. `old_base -> old_session_head` is a verified ancestor compare, so the path is a historical Task delta rather than a merge-base-relative diagnostic path.
3. The exact path belongs to both the historical Task delta and the verified `old_base -> new_base` base delta.
4. Exact path reads at the three pinned commits prove `blob(old_session_head, path) == blob(new_base, path) == blob(current_head, path)`.

Each successful classification is included in `audit.task_path_convergence.absorbed_by_new_base` with the path, classification, blob SHAs, and modes. A caller cannot declare absorbed paths.

Missing files do not count as matching blob identities. A different blob, an absent base-delta path, an unavailable path lookup, or an unverified ancestry leaves the path unexplained and recovery fails closed. Rename and delete behavior continues through the existing path and forward-delta proof; base absorption does not classify deletions.

## Preserved recovery gates

- `reviewed_overlap_paths_json` must still exactly equal the server-recomputed rename-aware overlap set. Absorption never skips this check.

## Explicit concurrent-file review (opt-in)

A base-synchronized Workspace may share *files* with an independently owned, still-active Writer. This must not be confused with imported-base overlap:

- `reviewed_overlap_paths_json` applies **only** to changed paths shared by the imported `old_base -> new_base` delta and the task. It remains exact-match and fail-closed.
- The new optional `reviewed_active_overlap_paths_json` applies **only** to other active Workspaces' `changed_paths` intersecting this task's authoritative current paths. Defaults to `[]`; without exact explicit review, any high active-Writer overlap continues to fail.
- The server recomputes the full active path set at each recovery, compares it exactly (no missing, extra or duplicate paths), and fails when overlap evidence cannot be verified or involves an unreviewable API/table/Migration conflict. Active Writers are never closed, replaced or re-leased by this recovery.
- Explicit review allows only *metadata adoption of an already-forward-merged Git branch*. It does not write source files, merge another Writer's changes, waive Git Blob/HEAD or Workspace/Session CAS, alter a branch's Owner, or grant permission to release/deploy/merge a PR. Later commits still require the normal exact-blob checks, and the later Writer resolves same-file conflicts.
- Recovery records both the independent base overlap review and the active-Workspace reviewed paths/identities in durable audit data. The review set becomes part of the idempotency identity when nonempty.

Example: imported-base overlap is `[]`, independently active shared file is `internal/modules/account/service.go`. Then supply `reviewed_overlap_paths_json="[]"` and `reviewed_active_overlap_paths_json='["internal/modules/account/service.go"]'`. Mixing the two sets is rejected.

- `reviewed_scope_expansion_paths_json` must still exactly equal every authoritative current path outside the declared Workspace scope. Absorbed historical paths do not grant scope to any other path.
- Repository/branch identity, old/new base identity, current HEAD/Tree, Workspace and Session revision CAS, branch ownership, lease, and final GitHub reread remain required.
- Recovery clears old-HEAD CI, Attestation, failure-pack, and Session Index references in the same atomic control-plane transition.

## Deterministic regression fixture

`services/github-action-service/tests/fixtures/base_sync/sxt_task_140_base_absorbed.json` records the SXT Task #140 Rev2 repository, branch, old/new base, old Session HEAD, current HEAD/Tree, reviewed overlap paths, and exact supplied blobs for `i18n/app/latest.v` and `i18n/app/manifest.json`. Tests use a deterministic in-memory Git graph and do not contact SXT or GitHub.
