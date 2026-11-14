import unittest
from private_ci_agent.notification_node_integration import GO_ONLY_TEST_COMMAND, SXT_REPOSITORY, NODE_INTEGRATION_COMMAND

class SXTGoParallelismTests(unittest.TestCase):
    def test_sxt_full_go_tests_still_run_all_modules(self):
        self.assertEqual(SXT_REPOSITORY, 'frankichen/sxt')
        self.assertIn('go test -p 2 -count=1', GO_ONLY_TEST_COMMAND)
        self.assertIn('./... 2>&1', GO_ONLY_TEST_COMMAND)
        self.assertEqual(GO_ONLY_TEST_COMMAND.count('-skip'), 1)
        self.assertIn('TestPreferenceDNDSimulatorFullMigrationIntegration', GO_ONLY_TEST_COMMAND)
        self.assertIn('go test -p 2 -count=1 -v -tags=ci_node_integration', NODE_INTEGRATION_COMMAND)
