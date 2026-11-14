from pathlib import Path
import re
import unittest


class SharedPodmanRunrootPreflightTests(unittest.TestCase):
    def test_worker_preflight_never_mutates_shared_runroot(self):
        script = (Path(__file__).resolve().parents[1] / 'bin' / 'private-ci-preflight').read_text()
        executable_lines = [line.split('#', 1)[0].strip() for line in script.splitlines()
                            if line.strip() and not line.lstrip().startswith('#')]
        executable = '\n'.join(executable_lines)
        self.assertNotRegex(executable, r'\bpkill\b')
        self.assertNotRegex(executable, r'\brm\s+-rf\b')
        self.assertNotRegex(executable, r'\bpodman_run_as_ciworker\s+system\s+migrate\b')
        self.assertNotRegex(executable, r'\bpodman_run_as_ciworker\s+unshare\b')
        self.assertIn("podman_run_as_ciworker ps --format '{{.ID}}'", script)


if __name__ == '__main__':
    unittest.main()
