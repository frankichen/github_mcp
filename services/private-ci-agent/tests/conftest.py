import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def exec_capable_tmp_path():
    """Create isolated executable scratch space on the CI workspace mount."""
    workspace = Path(__file__).resolve().parents[1]
    path = Path(tempfile.mkdtemp(prefix=".pytest-exec-", dir=workspace))
    try:
        yield path
    finally:
        # Cleanup must not replace the test's original result if teardown fails.
        shutil.rmtree(path, ignore_errors=True)
