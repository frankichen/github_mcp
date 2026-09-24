# MyGithut12 Base-Synced Recovery: Base-Absorbed Task Paths

Version: 12.9.20

`recover_base_synced_development_task` adopts an already-forward-synchronized GitHub branch into its existing Workspace and Development Session. It never writes repository files or moves Git refs.

## Base-absorbed classification

A path removed from the new-base Task delta may be classified as `BASE_ABSORBED` only when all of these server-verified facts hold:

1. `old_base -> new_base`, `old_session_head -> current_head`, and `new_base -> current_head` are exact forward ancestry relations.
2. `old_base -> old_session_head` is a verified ancestor compare, so the path is a historical Task delta rather than a merge-base-relative diagnostic path.
3. The exact path belongs to both the historical Task delta and the verified `old_base -> new_base` base delta.
4. Exact non-recursive Git tree reads prove `blob(old_session_head, path) == blob(new_base, path) == blob(current_head, path)`, with the same Git file mode at all three commits.

Each successful classification is included in `audit.task_path_convergence.absorbed_by_new_base` with the path, classification, blob SHAs, and modes. A caller cannot declare absorbed paths.

Missing files do not count as matching blob identities. A different blob or mode, an absent base-delta path, an unverified tree read, or an unverified ancestry leaves the path unexplained and recovery fails closed. Rename and delete behavior continues through the existing path and forward-delta proof; base absorption does not classify deletions.

## Preserved recovery gates

- `reviewed_overlap_paths_json` must still exactly equal the server-recomputed rename-aware overlap set. Absorption never skips this check.
- `reviewed_scope_expansion_paths_json` must still exactly equal every authoritative current path outside the declared Workspace scope. Absorbed historical paths do not grant scope to any other path.
- Repository/branch identity, old/new base identity, current HEAD/Tree, Workspace and Session revision CAS, branch ownership, lease, and final GitHub reread remain required.
- Recovery clears old-HEAD CI, Attestation, failure-pack, and Session Index references in the same atomic control-plane transition.

## Deterministic regression fixture

`services/github-action-service/tests/fixtures/base_sync/sxt_task_140_base_absorbed.json` records the SXT Task #140 Rev2 repository, branch, old/new base, old Session HEAD, current HEAD/Tree, reviewed overlap paths, and exact supplied blobs for `i18n/app/latest.v` and `i18n/app/manifest.json`. Tests use a deterministic in-memory Git graph and do not contact SXT or GitHub.
