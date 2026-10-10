# SXT notification Go+Node real CI integration
The original notification preference integration test runs real Go Handler,
real PostgreSQL Migration and Node client. It is moved from ordinary Go-only
workspace execution to a mandatory Go+Node test-only stage, still inside the
same isolated per-job PostgreSQL network and before service cleanup. The Go
workspace runs all other tests and never requires Node.

Only frankichen/sxt repo-auto-check uses this special stage. Absent Node,
Go, DB, image/digest, test, required PASS, nonzero exit or SKIP fails the
whole job. The Node+Go image is operator-prewarmed on both CI Workers, never
built or pulled by normal CI execution. The image is NOT a production image.

Rollout requires separate infrastructure merge and deployment authorization:
review/merge the github_mcp PR; build the image on both authorized workers
using prewarm-sxt-notification-go-node.sh; deploy Worker; validate exact
SXT payment PR HEAD with full repo-auto-check, Worker idle and attestation.
No API/OpenAPI, database Migration, APP, payment or business rules change.

CI Worker sets `CI_PROFILE` on every check. The LensHub test wiring guard
only requires a dedicated Node phase for `repo-auto-check`; `go-check` and
`repo-fast-check` are ordinary feedback profiles and do not certify the real
simulator integration. An absent/unknown profile with `CI_COMMIT_SHA` remains
fail-closed until the managed Worker is active.

The offline prewarm script intentionally uses `buildah bud --network=none`
with explicit temporary directories: rootless Podman `podman build` had a
verified read-only temporary directory failure with multi-stage COPY.
