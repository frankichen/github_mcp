import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from private_ci_agent.services import ServiceManager, ServiceSetupError


def output(code=0, stdout='', stderr=''):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class ServiceCreateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.manager = ServiceManager('podman', config={'worker_id': 'wsl-ci-01'})
        self.pod_name = 'ci-svc-wsl-ci-01-job123'
        self.pod_args = ['pod', 'create', '--name', self.pod_name,
                         *self.manager.resources.label_args('job123', 'pod')]
        self.pg_name = 'ci-wsl-ci-01-job123-postgres-global'
        self.pg_args = ['run', '-d', '--name', self.pg_name,
                        *self.manager.resources.label_args('job123', 'postgres-global')]

    def test_pod_timeout_created_then_confirmed_without_duplicate_retry(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[:3] == ['podman', 'pod', 'create']:
                self.assertEqual(kwargs['timeout'], 30)
                raise subprocess.TimeoutExpired(command, 30)
            if command[:3] == ['podman', 'pod', 'exists']:
                return output()
            if command[:3] == ['podman', 'pod', 'inspect']:
                return output(stdout=json.dumps({
                    'private-ci.worker': 'wsl-ci-01', 'private-ci.job': 'job123', 'private-ci.resource': 'pod'}))
            raise AssertionError(command)

        with patch('private_ci_agent.services.subprocess.run', side_effect=run):
            self.manager._run(self.pod_args, 'SERVICE_NETWORK_UNAVAILABLE',
                              resource_type='pod', resource_name=self.pod_name)
        self.assertEqual(sum(c[1:3] == ['pod', 'create'] for c in calls), 1)

    def test_pod_ownership_mismatch_is_denied_without_retry(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[:3] == ['podman', 'pod', 'create']:
                return output(code=125, stderr='name in use')
            if command[:3] == ['podman', 'pod', 'exists']:
                return output()
            if command[:3] == ['podman', 'pod', 'inspect']:
                return output(stdout=json.dumps({
                    'private-ci.worker': 'wsl-ci-02', 'private-ci.job': 'another-job', 'private-ci.resource': 'pod'}))
            raise AssertionError(command)

        with patch('private_ci_agent.services.subprocess.run', side_effect=run):
            with self.assertRaises(ServiceSetupError) as caught:
                self.manager._run(self.pod_args, 'SERVICE_NETWORK_UNAVAILABLE',
                                  resource_type='pod', resource_name=self.pod_name)
        self.assertEqual(caught.exception.code, 'SERVICE_NETWORK_UNAVAILABLE')
        self.assertEqual(sum(c[1:3] == ['pod', 'create'] for c in calls), 1)

    def test_pod_missing_after_timeout_may_retry(self):
        calls = []
        attempt = 0

        def run(command, **kwargs):
            nonlocal attempt
            calls.append(command)
            if command[:3] == ['podman', 'pod', 'create']:
                attempt += 1
                if attempt == 1:
                    raise subprocess.TimeoutExpired(command, 30)
                return output()
            if command[:3] == ['podman', 'pod', 'exists']:
                return output(code=1)
            raise AssertionError(command)

        with patch('private_ci_agent.services.subprocess.run', side_effect=run):
            with patch('private_ci_agent.services.time.sleep', return_value=None):
                self.manager._run(self.pod_args, 'SERVICE_NETWORK_UNAVAILABLE',
                                  resource_type='pod', resource_name=self.pod_name)
        self.assertEqual(attempt, 2)

    def test_detached_container_timeout_verified_running(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[:2] == ['podman', 'run']:
                self.assertEqual(kwargs['timeout'], 45)
                raise subprocess.TimeoutExpired(command, 45)
            if command[:3] == ['podman', 'container', 'exists']:
                return output()
            if command[:2] == ['podman', 'inspect'] and '{{json .Config.Labels}}' in command:
                return output(stdout=json.dumps({
                    'private-ci.worker': 'wsl-ci-01', 'private-ci.job': 'job123', 'private-ci.resource': 'postgres-global'}))
            if command[:2] == ['podman', 'inspect'] and '.State.Status' in ' '.join(command):
                return output(stdout='running|0||unknown')
            raise AssertionError(command)

        with patch('private_ci_agent.services.subprocess.run', side_effect=run):
            self.manager._run(self.pg_args, 'CI_INFRA_POSTGRES_GLOBAL_UNAVAILABLE',
                              resource_type='postgres-global', resource_name=self.pg_name)
        self.assertEqual(sum(c[1] == 'run' for c in calls), 1)

    def test_detached_container_not_running_never_accepted(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[:2] == ['podman', 'run']:
                raise subprocess.TimeoutExpired(command, 45)
            if command[:3] == ['podman', 'container', 'exists']:
                return output()
            if command[:2] == ['podman', 'inspect'] and '{{json .Config.Labels}}' in command:
                return output(stdout=json.dumps({
                    'private-ci.worker': 'wsl-ci-01', 'private-ci.job': 'job123', 'private-ci.resource': 'postgres-global'}))
            if command[:2] == ['podman', 'inspect'] and '.State.Status' in ' '.join(command):
                return output(stdout='created|0||unknown')
            raise AssertionError(command)

        with patch('private_ci_agent.services.subprocess.run', side_effect=run):
            with self.assertRaises(ServiceSetupError):
                self.manager._run(self.pg_args, 'CI_INFRA_POSTGRES_GLOBAL_UNAVAILABLE',
                                  resource_type='postgres-global', resource_name=self.pg_name)
        self.assertEqual(sum(c[1] == 'run' for c in calls), 1)


if __name__ == '__main__':
    unittest.main()
