"""Self-health diagnostics stay authenticated, cached, and refreshable."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SelfHealthTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.db = MagicMock()
        self.db.get_user.return_value = {'id': 'alice', 'name': 'Alice'}
        self.db_patch = patch.object(app_module, 'get_db', return_value=self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.application = app_module.create_app()
        self.application.config['TESTING'] = True
        self.client = self.application.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 'alice'

    def test_caches_results_and_allows_explicit_refresh(self):
        success = SimpleNamespace(returncode=0, stdout='passed', stderr='')
        with patch.object(self.app_module.subprocess, 'run', return_value=success) as run:
            response = self.client.get('/api/self/health')
            self.assertEqual(response.status_code, 200)
            health = response.get_json()['health']
            self.assertEqual(health['overall'], 'healthy')
            self.assertEqual(len(health['checks']), 2)
            self.assertEqual(run.call_count, 2)

            cached = self.client.get('/api/self/health')
            self.assertEqual(cached.get_json()['health'], health)
            self.assertEqual(run.call_count, 2)

            refreshed = self.client.get('/api/self/health?run=1')
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(run.call_count, 4)

    def test_reports_failed_checks(self):
        failed = SimpleNamespace(returncode=1, stdout='', stderr='build failed')
        with patch.object(self.app_module.subprocess, 'run', return_value=failed):
            response = self.client.get('/api/self/health')
        self.assertEqual(response.status_code, 200)
        health = response.get_json()['health']
        self.assertEqual(health['overall'], 'needs_attention')
        self.assertFalse(health['checks'][0]['ok'])
        self.assertEqual(health['checks'][0]['stderr'], 'build failed')

    def test_requires_authenticated_user_without_running_checks(self):
        self.db.get_user.return_value = None
        with patch.object(self.app_module.subprocess, 'run') as run:
            response = self.client.get('/api/self/health')
        self.assertEqual(response.status_code, 401)
        run.assert_not_called()

    def test_timed_out_diagnostics_return_json_with_captured_output(self):
        timeout = subprocess.TimeoutExpired('check', 120, output=b'partial output', stderr=b'failed \xff')
        with patch.object(self.app_module.subprocess, 'run', side_effect=timeout):
            response = self.client.get('/api/self/health')
        self.assertEqual(response.status_code, 200)
        health = response.get_json()['health']
        self.assertEqual(health['overall'], 'needs_attention')
        self.assertEqual(health['checks'][0]['stdout'], 'partial output')
        self.assertEqual(health['checks'][0]['stderr'], 'failed \ufffd')


if __name__ == '__main__':
    unittest.main()
