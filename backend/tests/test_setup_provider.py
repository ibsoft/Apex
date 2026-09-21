import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from setup_provider import Cancelled, PROVIDERS, Wizard, save_env, validate


class SetupTests(unittest.TestCase):
    def test_preserves_settings_and_quotes_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            original = b'# keep comment\nOTHER=value\nOPENAI_API_KEY=old\n'
            path.write_bytes(original)
            secret = "a'quoted\\secret # value"
            save_env(path, original, {'OPENAI_API_KEY': secret})
            self.assertEqual(dotenv_values(path)['OPENAI_API_KEY'], secret)
            self.assertEqual(dotenv_values(path)['OTHER'], 'value')
            self.assertIn('# keep comment', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_new_file_uses_template(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            save_env(path, None, {'PROVIDER_DEFAULT': 'ollama'})
            values = dotenv_values(path)
            self.assertEqual(values['PROVIDER_DEFAULT'], 'ollama')
            self.assertEqual(values['PORT'], '5001')

    def test_concurrent_change_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('OTHER=changed\n')
            with self.assertRaises(ValueError):
                save_env(path, b'OTHER=old\n', {'PROVIDER_DEFAULT': 'torch'})
            self.assertEqual(path.read_text(), 'OTHER=changed\n')

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.write_text('OTHER=value\n')
            path = Path(directory) / '.env'
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                save_env(path, target.read_bytes(), {})

    def test_validation(self):
        for value in ['', 'ftp://host', 'https://host:bad', 'https://user:pass@host']:
            self.assertTrue(validate('OPENAI_BASE_URL', value))
        self.assertFalse(validate('OLLAMA_BASE_URL', 'http://localhost:11434/v1'))
        self.assertTrue(validate('OPENAI_API_KEY', '${SECRET}'))
        self.assertTrue(validate('DEFAULT_MODEL', 'model\nINJECT=value'))

    def test_oauth_setup_credentials_and_masking(self):
        for token_mode in (False, True):
            wizard = object.__new__(Wizard)
            selections = iter([list(PROVIDERS).index("openai"), 2, int(token_mode), 0])
            reviews, prompted = [], []

            def choose(*args, **kwargs):
                if "details" in kwargs:
                    reviews.extend(kwargs["details"])
                return next(selections)

            def field(title, key, default, secret=False, optional=False):
                prompted.append(key)
                return {"OPENAI_CLIENT_ID": "client-id",
                        "OPENAI_CLIENT_SECRET": "private-secret",
                        "BASE_URL": "https://backend.example",
                        "OPENAI_API_KEY": "private-key"}.get(key, default)

            wizard.choose, wizard.field = choose, field
            result = wizard.run(Path('/tmp/example'), {"DEV_AUTO_LOGIN": "developer"})
            self.assertEqual(result["DEV_AUTO_LOGIN"], "")
            self.assertEqual(result["DEV_MODE"], "false")
            self.assertEqual(result["USE_OAUTH_ACCESS_KEY"], str(token_mode).lower())
            self.assertEqual(result["OPENAI_REDIRECT_URI"], "https://backend.example/api/auth/callback")
            self.assertEqual("OPENAI_API_KEY" in prompted, not token_mode)
            self.assertIn("CHATGPT_MODEL" if token_mode else "DEFAULT_MODEL", prompted)
            self.assertNotIn("private-secret", str(reviews))
            self.assertNotIn("private-key", str(reviews))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / '.env'
                save_env(path, None, result)
                self.assertEqual(dotenv_values(path)["OPENAI_CLIENT_SECRET"], "private-secret")

    def test_wizard_provider_values_and_cancel(self):
        for provider, model_key in [('openai', 'DEFAULT_MODEL'), ('kimi', 'KIMI_MODEL'),
                                    ('ollama', 'OLLAMA_MODEL'), ('torch', 'TORCH_MODEL')]:
            wizard = object.__new__(Wizard)
            selections = iter([list(PROVIDERS).index(provider), 0, 0])
            wizard.choose = lambda *args, **kwargs: next(selections)
            wizard.field = lambda title, key, default, secret=False: default or 'test-key'
            result = wizard.run(Path('/tmp/example'), {'SECRET_KEY': 'existing'})
            self.assertEqual(result['PROVIDER_DEFAULT'], provider)
            self.assertIn(model_key, result)
            self.assertNotIn('SECRET_KEY', result)
            self.assertNotIn('DEV_AUTO_LOGIN', result)
        selections = iter([list(PROVIDERS).index("ollama"), 0, 1])
        wizard.choose = lambda *args, **kwargs: next(selections)
        with self.assertRaises(Cancelled):
            wizard.run(Path('/tmp/example'), {})


if __name__ == '__main__':
    unittest.main()
