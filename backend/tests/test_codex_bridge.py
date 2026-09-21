import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import CodexError, forward_callback
from models.codex_provider import CodexProvider
from setup_provider import PROVIDERS, Wizard

AUTH_URL = 'https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=expected'
CALLBACK = 'http://localhost:1455/auth/callback?state=expected&code=secret-code'


class CallbackTests(unittest.TestCase):
    @patch('codex_bridge.http.client.HTTPConnection')
    def test_forward_only_matching_callback_without_redirects(self, connection):
        connection.return_value.getresponse.return_value.status = 302
        forward_callback(AUTH_URL, CALLBACK)
        connection.assert_called_once_with('localhost', 1455, timeout=15)
        connection.return_value.request.assert_called_once_with('GET', '/auth/callback?state=expected&code=secret-code')
        connection.return_value.close.assert_called_once()

    @patch('codex_bridge.http.client.HTTPConnection')
    def test_invalid_urls_never_sent(self, connection):
        for url in [CALLBACK.replace('expected', 'wrong'), CALLBACK.replace('localhost', 'example.com'),
                    CALLBACK.replace('1455', '8000'), CALLBACK.replace('/auth/callback', '/other'),
                    CALLBACK + '&state=duplicate', CALLBACK.replace('code=', 'missing='),
                    CALLBACK.replace('http:', 'https:'), CALLBACK + '#fragment']:
            with self.subTest(url=url), self.assertRaises(CodexError) as error:
                forward_callback(AUTH_URL, url)
            self.assertNotIn('secret-code', str(error.exception))
        connection.assert_not_called()

    @patch('codex_bridge.http.client.HTTPConnection')
    def test_callback_http_error(self, connection):
        connection.return_value.getresponse.return_value.status = 400
        with self.assertRaises(CodexError):
            forward_callback(AUTH_URL, CALLBACK)
        connection.return_value.close.assert_called_once()


class ProviderTests(unittest.TestCase):
    @patch('models.codex_provider.CodexClient')
    def test_stream_and_tool_result_roundtrip(self, factory):
        client = factory.return_value
        client.account.return_value = {'type': 'chatgpt'}
        client.workspace.name = '/tmp/test'
        client.request.side_effect = [{'thread': {'id': 'thread-1'}}, {}]
        client.event.side_effect = [
            {'method': 'item/agentMessage/delta', 'params': {'delta': 'Checking.'}},
            {'id': 88, 'method': 'item/tool/call', 'params': {'tool': 'calculate', 'callId': 'call-1', 'arguments': {'expression': '2+2'}}},
            {'method': 'item/agentMessage/delta', 'params': {'delta': '4'}},
            {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}},
        ]
        provider = CodexProvider(MagicMock(model='test-model'))
        messages = [{'role': 'system', 'content': 'APEX'}, {'role': 'user', 'content': '2+2?'}]
        tools = [{'type': 'function', 'function': {'name': 'calculate', 'parameters': {'type': 'object'}}}]
        chunks = list(provider.chat_stream(messages, tools))
        self.assertEqual(chunks[0]['content'], 'Checking.')
        self.assertEqual(chunks[1]['calls'][0]['id'], 'call-1')
        messages.append({'role': 'tool', 'tool_call_id': 'call-1', 'content': '4'})
        self.assertEqual(list(provider.chat_stream(messages, tools)), [{'type': 'text', 'content': '4'}])
        client.send.assert_called_with({'id': 88, 'result': {'success': True, 'contentItems': [{'type': 'inputText', 'text': '4'}]}})
        thread_params = client.request.call_args_list[0].args[1]
        self.assertEqual(thread_params['sandbox'], 'read-only')
        self.assertEqual(thread_params['dynamicTools'][0]['name'], 'calculate')
        provider.close()
        client.close.assert_called_once()

    @patch('models.codex_provider.CodexClient')
    def test_requires_chatgpt_login(self, factory):
        factory.return_value.account.return_value = {'type': 'apiKey'}
        provider = CodexProvider(MagicMock(model=''))
        with self.assertRaises(CodexError):
            list(provider.chat_stream([]))
        provider.close()

    def test_setup_uses_codex_and_does_not_request_client_id(self):
        wizard = object.__new__(Wizard)
        choices = iter([list(PROVIDERS).index('codex'), 0])
        wizard.choose = lambda *args, **kwargs: next(choices)
        wizard.field = MagicMock(return_value='')
        wizard.codex_login = MagicMock()
        values = wizard.run(Path('/tmp/test.env'), {})
        self.assertEqual(values['PROVIDER_DEFAULT'], 'codex')
        self.assertEqual(values['USE_OAUTH_ACCESS_KEY'], 'false')
        self.assertEqual(values['DEV_AUTO_LOGIN'], 'developer')
        self.assertNotIn('OPENAI_CLIENT_ID', values)
        self.assertNotIn('OPENAI_API_KEY', values)
        wizard.codex_login.assert_called_once_with(values)


if __name__ == '__main__':
    unittest.main()
