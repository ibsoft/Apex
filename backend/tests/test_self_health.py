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

    def test_frontend_build_uses_a_disposable_snapshot(self):
        frontend = Path(self.app_module.__file__).resolve().parent.parent / 'frontend'
        original_config = (frontend / 'tsconfig.json').read_text()
        build_directories = []

        def run_check(cmd, **kwargs):
            if cmd == ['npm', 'run', 'build']:
                snapshot = kwargs['cwd']
                build_directories.append(snapshot)
                self.assertNotEqual(snapshot, frontend)
                self.assertEqual((snapshot / 'tsconfig.json').read_text(), original_config)
                self.assertFalse((snapshot / '.next').exists())
                self.assertFalse((snapshot / '.next-https').exists())
                self.assertEqual(kwargs['env']['NEXT_DIST_DIR'], '.next')
                if (frontend / 'node_modules').exists():
                    self.assertEqual((snapshot / 'node_modules').resolve(), frontend / 'node_modules')
                (snapshot / 'tsconfig.json').write_text('simulated Next build update')
            return SimpleNamespace(returncode=0, stdout='passed', stderr='')

        with patch.object(self.app_module.subprocess, 'run', side_effect=run_check):
            response = self.client.get('/api/self/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['health']['overall'], 'healthy')
        self.assertEqual(len(build_directories), 1)
        self.assertFalse(build_directories[0].exists())
        self.assertEqual((frontend / 'tsconfig.json').read_text(), original_config)

    def test_backend_tests_use_disposable_data(self):
        test_databases = []

        def run_check(cmd, **kwargs):
            if 'pytest' in cmd:
                database = Path(kwargs['env']['DB_PATH'])
                test_databases.append(database)
                self.assertEqual(database.parent, Path(kwargs['env']['DATA_DIR']))
                self.assertNotEqual(database, self.app_module.config.DB_PATH)
                self.assertEqual(kwargs['env']['DEV_AUTO_LOGIN'], 'apex-self-check')
                database.write_text('temporary test data')
            return SimpleNamespace(returncode=0, stdout='passed', stderr='')

        with patch.object(self.app_module.subprocess, 'run', side_effect=run_check):
            response = self.client.get('/api/self/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['health']['overall'], 'healthy')
        self.assertEqual(len(test_databases), 1)
        self.assertFalse(test_databases[0].parent.exists())


if __name__ == '__main__':
    unittest.main()
