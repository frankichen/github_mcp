"""Test-only policy fixture for legacy owner/repo mocks."""
import os
from pathlib import Path

os.environ.setdefault("REPOSITORY_POLICY_FILE", str(Path(__file__).with_name("repository_policies_test.yml")))
