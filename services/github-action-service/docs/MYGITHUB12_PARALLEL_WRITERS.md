# MyGithut12: independent-branch concurrent writers

This policy is shared by all MyGithut12 repositories, including frankichen/xyzl
(Android/Java) and frankichen/sxt (Go). It is **not** a per-project exception.

## Rule: first commit progresses, later integrator resolves conflicts

- Distinct legal Development Workspaces on **different branches** may edit the
  same Git filename at the same time; including different or identical hunks.
- workspace_overlap reports shared changed_paths as advisory medium
  evidence. A path match alone does not block an unrelated branch's Workspace
  or Session recovery. development_base_sync_recovery._current_task_overlap
  preserves that classification after filtering imported base paths.
- Source changes remain isolated in each branch. The writer integrating
  *later* must fetch the current target, handle any actual content conflict
  and run the impacted tests before seeking merge approval. Branch-local
  writes continue using HEAD/Blob SHA and Workspace/Session revision CAS.
- The existence of shared-path evidence is retained in overlap responses and
  recovery audits; it is not erased or disguised as none.

## Still prohibited

- Two Workspaces claiming the **same branch**, unauthorized Writer identity,
  scope expansion without exact review, stale/mismatched HEAD/Tree/base,
  expired leases, unverified comparisons, or reused stale CI/attestation.
- Conflicting explicitly declared apis, tables, or migrations stay
  high because they affect contracts/shared state beyond a filename.
- Actual Git merge conflicts are **not** bypassed. Merge/Deploy/Rollback
  require separately authorized release gates.

## Concrete cases

- XYZL: Task #604 wearable functions and Task #605 account functions in
  RestApiClient.java may progress on their *separate branches*.
- SXT: account service.go touched by multiple PRs may be developed
  independently; later base synchronization or integration deals with
  the actual base delta and content conflicts.
- This does **not** authorize out-of-scope SqlLit.java writes or grant
  a debug Manifest file to a different Task. Scope review remains separate.

The behavior is tested in tests/test_parallel_workspace_overlap.py and
tests/test_base_sync_recovery.py. The change does not create, renew, or
close Workspaces on behalf of any project.
