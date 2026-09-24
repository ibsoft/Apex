"""A UI-pre-created (untitled) conversation must be named after its first post."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ChatTitleTests(unittest.TestCase):
    def _setup(self, title="New conversation", messages=None):
        import app as app_module
        db = MagicMock()
        db.get_user.return_value = {'id': 'alice', 'name': 'Alice'}
        db.all_settings.return_value = {'provider': 'openai', 'model': 'test'}
        db.get_conversation.return_value = {'id': 'conversation', 'user_id': 'alice',
                                            'skill': 'general', 'title': title}
        db.list_messages.return_value = messages if messages is not None else []
        skills = MagicMock()
        skills.select.return_value = SimpleNamespace(model='', tools=['web_search'])
        skills.build_system_prompt.return_value = 'test'
        engine = MagicMock()
        engine.stream.return_value = iter([
            {'type': 'text_delta', 'content': 'Hello.'},
            {'type': 'done', 'usage': {}},
        ])
        return app_module, db, skills, engine

    def _post(self, app_module, db, skills, engine):
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
            response = client.post('/api/chat', json={
                'message': 'find me coffee shops',
                'conversation_id': 'conversation',
            })
            self.assertEqual(response.status_code, 200)
            response.get_data(as_text=True)
            return db

    def test_empty_thread_is_titled_from_first_message(self):
        app_module, db, skills, engine = self._setup(title="New conversation")
        db = self._post(app_module, db, skills, engine)
        calls = [c for c in db.update_conversation.call_args_list]
        titled = [c.kwargs.get('title') or c.args[1]
                  for c in calls if (c.kwargs.get('title') or (len(c.args) > 1 and c.args[1]))]
        self.assertIn('find me coffee shops', titled)

    def test_empty_thread_with_empty_title_is_titled_from_first_message(self):
        app_module, db, skills, engine = self._setup(title="")
        db = self._post(app_module, db, skills, engine)
        calls = [c for c in db.update_conversation.call_args_list]
        titled = [c.kwargs.get('title') or (c.args[1] if len(c.args) > 1 else '')
                  for c in calls if c.kwargs.get('title') or (len(c.args) > 1 and c.args[1])]
        self.assertIn('find me coffee shops', titled)

    def test_thread_with_existing_messages_is_not_renamed(self):
        # MagicMock would treat the second call specially; verify via real flow
        # that a titled conversation never has its title overwritten.
        app_module, db, skills, engine = self._setup(
            title="Shops nearby",
            messages=[{'role': 'user', 'content': 'hi', 'meta': {}}],
        )
        db = self._post(app_module, db, skills, engine)
        for c in db.update_conversation.call_args_list:
            title = c.kwargs.get('title') or (c.args[1] if len(c.args) > 1 else '')
            if title:
                self.assertNotIn('find me coffee shops', title)

    def test_long_first_message_is_truncated_at_48_chars(self):
        app_module, db, skills, engine = self._setup(title="New conversation")
        long_msg = "x" * 120
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
            response = client.post('/api/chat', json={
                'message': long_msg,
                'conversation_id': 'conversation',
            })
            response.get_data(as_text=True)
        titles = []
        for c in db.update_conversation.call_args_list:
            t = c.kwargs.get('title') or (c.args[1] if len(c.args) > 1 else '')
            if t:
                titles.append(t)
        self.assertTrue(any(len(t) == 49 and t.endswith('…') for t in titles))


if __name__ == '__main__':
    unittest.main()