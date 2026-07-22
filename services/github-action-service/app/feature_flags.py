"""Hard server-side feature gates; capability reporting is not an authorization gate."""

import os


def enabled(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {"1", "true", "yes", "on"}


ARTIFACT_BUILD = "MYGITHUB10_ARTIFACT_BUILD_ENABLED"
ARTIFACT_DEPLOY = "MYGITHUB10_ARTIFACT_DEPLOY_ENABLED"
ATTESTATION_REUSE = "MYGITHUB10_ATTESTATION_REUSE_ENABLED"
