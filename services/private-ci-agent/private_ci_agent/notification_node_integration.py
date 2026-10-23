"""Mandatory notification simulator integration in a separate Go+Node CI image."""

import os
import re

SXT_REPOSITORY = "frankichen/sxt"
NODE_INTEGRATION_IMAGE = "localhost/sxt-notification-go-node:1.26.4-22"
SIMULATOR_TEST = "TestPreferenceDNDSimulatorFullMigrationIntegration"
GO_ONLY_TEST_COMMAND = (
    "go test -p 6 -count=1 -skip '^TestPreferenceDNDSimulatorFullMigrationIntegration$' ./... 2>&1"
)
NODE_INTEGRATION_COMMAND = (
    "set -eu; command -v go >/dev/null; command -v node >/dev/null; "
    'test -n "$(printenv DATABASE_URL)"; '
    "test -f internal/modules/notification/preference_simulator_test.go; "
    "test -f h5/lenshub-app-simulator/tests/notification-preferences-real.mjs; "
    'output="$(go test -p 2 -count=1 -v -tags=ci_node_integration '
    "-run '^TestPreferenceDNDSimulatorFullMigrationIntegration$' "
    './internal/modules/notification 2>&1)" || { printf "%s\n" "$output"; exit 1; }; '
    'printf "%s\n" "$output"; '
    'printf "%s\n" "$output" | grep -F -- '
    "'--- PASS: TestPreferenceDNDSimulatorFullMigrationIntegration' >/dev/null "
    '|| { echo "CI_NODE_INTEGRATION_MISSING_PASS" >&2; exit 1; }; '
    'if printf "%s\n" "$output" | grep -F -- '
    "'--- SKIP: TestPreferenceDNDSimulatorFullMigrationIntegration' >/dev/null; "
    'then echo "CI_NODE_INTEGRATION_SKIPPED" >&2; exit 1; fi'
)

def notification_node_integration_required(repository: str, profile: str) -> bool:
    # The legacy local App simulator has been retired from SXT CI.
    # Re-enable the dedicated Go+Node integration only by explicit operator opt-in.
    return (
        repository == SXT_REPOSITORY
        and profile == "repo-auto-check"
        and os.getenv("SXT_LEGACY_SIMULATOR_CI_ENABLED", "0") == "1"
    )

def sxt_go_only_image_allowed(repository: str, stack: str, image: str) -> bool:
    """SXT Go checks must never inherit the dedicated Go+Node test image."""
    if repository != SXT_REPOSITORY or stack != "go":
        return True
    return re.fullmatch(
        r"(?:docker\.io/library/)?golang:\d+\.\d+(?:\.\d+)?"
        r"(?:-(?:alpine(?:[0-9.]+)?|bookworm|bullseye))?",
        image,
    ) is not None
