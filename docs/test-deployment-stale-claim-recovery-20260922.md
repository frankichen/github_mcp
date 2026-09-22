# Test deployment stale-claim recovery

## Scope

This change covers the MyGithut12 test-deployment control plane, delegated
executor, and local test-environment deploy agent. It does not change SXT
business code or perform an SXT deployment.

## State machine before the fix

The persisted deployment status was treated as the ownership signal:

```text
queued --agent claim--> claimed --executor callback--> running --> passed/failed
   |                         |
   +--> canceled             +--> cancel_requested (no terminal transition)
```

The agent wrote `claimed` and the executor later selected rows by status. The
opaque lease token was not used to fence claims, there was no owner, heartbeat,
expiry, generation, or state revision, and cancellation only changed the
status to `cancel_requested`. Therefore an executor crash or polling timeout
could leave a row permanently active. `start_test_deployment` also treated all
non-terminal rows, including an old `cancel_requested` row, as an active
deployment.

## State machine after the fix

```text
queued
  --agent CAS claim / handoff lease-->
claimed
  --executor CAS claim-->
claimed (executor-owned, leased)
  --heartbeat / progress-->
running
  --verified completion-->
passed

claimed or running --cancel request--> cancel_requested
cancel_requested --executor observes cancel--> canceled

claimed or running --lease expires-->
  reconcile:
    pre-switch + cancel requested + verified different current release
      --> canceled
    pre-switch + no cancellation + verified different current release
      --> failed (DEPLOYMENT_EXECUTOR_LOST)
    release target is already verified current
      --> failed/operator-attention (DEPLOYMENT_CALLBACK_LOST_AFTER_RELEASE_SWITCH)
    release/current outcome cannot be proven safe
      --> failed/operator-attention (DEPLOYMENT_STALE_OUTCOME_UNKNOWN)
```

Every executor-owned claim has an owner identity, claim generation, hashed
claim token, state revision, claim start, heartbeat, and lease expiry. Claim,
heartbeat, progress, terminal callback, and reconciliation operations use
owner/token/generation and state-revision predicates. A stale actor cannot
advance or terminalize a newer claim.

`DEPLOYMENT_ALREADY_ACTIVE` is now based on a valid execution lease. The
controller first reconciles expired claims for the environment, then checks
for another live lease. A live heartbeat still blocks a new deployment; an
expired orphan must become terminal before the new deployment is admitted.

## Release-switch safety

Reconciliation reads the verified environment release registry and the
deployment's target release. It never marks an unknown or post-switch outcome
as passed and never rolls a release back. If the target is already current, or
the current release cannot prove a safe pre-switch cancellation, the result is
an auditable failed/operator-attention outcome.

## SQLite concurrency

The controller and agent enable WAL, bounded busy timeouts, short autocommit
transactions, guarded one-time schema initialization, and bounded
`BEGIN IMMEDIATE` retry. Claim and terminal updates are CAS operations. Log
batch writes are committed in one short transaction instead of holding a
polling transaction open.

## Recovery entry points

The controller runs a leader-only periodic stale reaper. It also exposes the
server-side, deployment-id-only MCP operation
`reconcile_stale_test_deployment`; the operation reads all release/lease/phase
facts itself, is idempotent, records an audit event, and does not accept a
caller-supplied status or release.

The delegated executor treats assignment timeout/connection reset as a
polling failure with bounded backoff. It keeps the process alive and resumes
polling after restart; callbacks are fenced with the claim credentials.
