"""Keep actual search results in streamed and saved chat messages."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DownloadMessageTests(unittest.TestCase):
    def test_tool_downloads_saved_even_if_model_omits_links(self):
        import app as app_module
        db = MagicMock()
        db.get_user.return_value = {'id': 'alice', 'name': 'Alice'}
        db.all_settings.return_value = {'provider': 'openai', 'model': 'test'}
        db.get_conversation.return_value = {'id': 'conversation', 'user_id': 'alice', 'skill': 'general'}
        db.list_messages.return_value = [{'role': 'user', 'content': 'find my files'}]
        skills = MagicMock()
        skills.select.return_value = SimpleNamespace(model='', tools=['file_search'])
        skills.build_system_prompt.return_value = 'test'
        output = json.dumps({'files': [{'name': 'report.pdf', 'path': '/tmp/report.pdf',
                                       'download_url': 'https://192.168.1.218/api/files/download/test'}]})
        engine = MagicMock()
        engine.stream.return_value = iter([
            {'type': 'tool_result', 'name': 'file_search', 'output': output},
            {'type': 'text_delta', 'content': 'Here is your file.'},
            {'type': 'done', 'usage': {}},
        ])
        with patch.object(app_module, 'get_db', return_value=db), \
             patch.object(app_module, 'get_skill_manager', return_value=skills), \
             patch.object(app_module, 'bearer_for_api', return_value=None), \
             patch.object(app_module, 'is_subscription_access', return_value=False), \
             patch.object(app_module, 'ProviderManager'), \
             patch.object(app_module, 'make_registry'), \
             patch.object(app_module, 'build_engine', return_value=engine), \
             patch.object(app_module.config, 'MEMORY_ENABLED', False), \
             patch.object(app_module.config, 'MEMORY_SUMMARIZE', False):
            application = app_module.create_app()
            client = application.test_client()
            with client.session_transaction() as session:
                session['user_id'] = 'alice'
            response = client.post('/api/chat', json={'message': 'find my files', 'conversation_id': 'conversation'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('tool_result', response.get_data(as_text=True))
            saved = [call.args for call in db.add_message.call_args_list if call.args[1] == 'assistant'][0]
            self.assertEqual(saved[3]['tools'][0]['output'], output)


if __name__ == '__main__':
    unittest.main()
