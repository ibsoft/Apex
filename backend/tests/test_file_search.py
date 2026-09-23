"""Search and real HTTP download behavior using an isolated filesystem."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, session
from skills.manager import SkillManager
from tools.base import ToolContext
from tools.file_search import build_file_tools, register_file_routes, search_files, LINK_SECONDS


class FileSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = SimpleNamespace(FILE_SEARCH_ROOTS=str(self.root), SECRET_KEY='test-secret', BASE_URL='http://localhost')
        self.ctx = ToolContext(user_id='alice')
        self.document = self.root / 'Quarterly invoice [final].PDF'
        self.document.write_bytes(b'file content\x00\xff')
        self.app = Flask(__name__)
        self.app.secret_key = self.config.SECRET_KEY
        register_file_routes(self.app, lambda: {'id': session['user_id']} if 'user_id' in session else None, self.config)
        self.client = self.app.test_client()
        self.login('alice')

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess['user_id'] = user

    def search(self, **kwargs):
        return search_files({'query': '*.pdf', **kwargs}, self.ctx, self.config)

    def link(self):
        return self.search()['files'][0]['download_url']

    def test_search_and_download_original_bytes(self):
        result = self.search()
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['files'][0]['path'], str(self.document))
        self.assertFalse(result['truncated'])
        with self.client.get(result['files'][0]['download_url']) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, self.document.read_bytes())
            self.assertIn('attachment;', response.headers['Content-Disposition'])
            self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_authentication_ownership_and_tampering(self):
        link = self.link()
        self.login(None)
        self.assertEqual(self.client.get(link).status_code, 401)
        self.login('bob')
        self.assertEqual(self.client.get(link).status_code, 403)
        self.login('alice')
        self.assertEqual(self.client.get(link + 'tampered').status_code, 404)

    def test_expired_link(self):
        link = self.link()
        with patch('itsdangerous.timed.time.time', return_value=time.time() + LINK_SECONDS + 2):
            self.assertEqual(self.client.get(link).status_code, 410)

    def test_changed_or_deleted_file(self):
        link = self.link()
        self.document.write_bytes(b'changed')
        self.assertEqual(self.client.get(link).status_code, 409)
        self.document.unlink()
        self.assertEqual(self.client.get(link).status_code, 404)

    def test_symlinks_and_special_files_skipped(self):
        (self.root / 'alias.pdf').symlink_to(self.document)
        os.mkfifo(self.root / 'pipe.pdf')
        result = self.search()
        self.assertEqual([item['name'] for item in result['files']], [self.document.name])

    def test_parent_symlink_swap_rejected(self):
        folder = self.root / 'folder'
        folder.mkdir()
        target = folder / 'target.txt'
        target.write_text('original')
        link = self.search(query='target.txt')['files'][0]['download_url']
        folder.rename(self.root / 'original-folder')
        folder.symlink_to(self.root / 'original-folder', target_is_directory=True)
        self.assertEqual(self.client.get(link).status_code, 404)

    def test_scope_checked_on_search_and_download(self):
        self.assertIn('error', self.search(root='/'))
        link = self.link()
        self.config.FILE_SEARCH_ROOTS = str(self.root / 'restricted')
        self.assertEqual(self.client.get(link).status_code, 403)

    def test_unreadable_files_skipped(self):
        with patch('tools.file_search.open_regular', side_effect=PermissionError):
            result = self.search()
        self.assertEqual(result['files'], [])
        self.assertEqual(result['skipped_inaccessible'], 1)

    def test_search_bounds_and_fragments(self):
        (self.root / 'another invoice.pdf').write_text('second')
        result = self.search(query='INVOICE', limit=1)
        self.assertEqual(len(result['files']), 1)
        self.assertTrue(result['truncated'])
        with patch('tools.file_search.MAX_ENTRIES', 0):
            result = self.search()
        self.assertEqual(result['files'], [])
        self.assertTrue(result['truncated'])

    def test_arbitrary_filename_and_named_home_folder(self):
        folder = self.root / 'Design Assets'
        folder.mkdir()
        names = ['orchid-sketch.svg', 'Budget_2027.xlsx', 'holiday photograph.jpg']
        for name in names:
            (folder / name).write_text(name)
        with patch('tools.file_search.Path.home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}):
            for name in names:
                result = self.search(query=name, root='design assets')
                self.assertEqual([item['name'] for item in result['files']], [name])
                with self.client.get(result['files'][0]['download_url']) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data.decode(), name)

    def test_xdg_directory_resolution(self):
        folder = self.root / 'Localized Folder'
        folder.mkdir()
        (folder / 'arbitrary-name.txt').write_text('found')
        config_dir = self.root / 'config'
        config_dir.mkdir()
        (config_dir / 'user-dirs.dirs').write_text('XDG_DOCUMENTS_DIR="$HOME/Localized Folder"\n')
        with patch('tools.file_search.Path.home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config_dir)}):
            result = self.search(query='arbitrary-name', root='Documents')
        self.assertEqual(result['roots'], [str(folder)])
        self.assertEqual(len(result['files']), 1)

    def test_greek_folder_aliases_follow_xdg_settings_and_preserve_filenames(self):
        cases = {
            'DESKTOP': ['Επιφάνεια εργασίας', 'ΕΠΙΦΑΝΕΙΑ ΕΡΓΑΣΙΑΣ'],
            'DOCUMENTS': ['Έγγραφα', 'ΕΓΓΡΑΦΑ', 'εγγραφα'],
            'DOWNLOAD': ['Λήψεις', 'ΛΗΨΕΙΣ', 'ληψεις', 'Κατεβάσματα'],
            'PICTURES': ['Εικόνες', 'εικονες', 'Φωτογραφίες'],
            'MUSIC': ['Μουσική', 'ΜΟΥΣΙΚΗ'],
            'VIDEOS': ['Βίντεο', 'βιντεο'],
            'TEMPLATES': ['Πρότυπα', 'προτυπα'],
            'PUBLICSHARE': ['Κοινόχρηστα', 'Δημόσια'],
        }
        config_dir = self.root / 'config'
        config_dir.mkdir()
        settings = []
        for kind in cases:
            folder = self.root / f'Configured {kind}'
            folder.mkdir()
            (folder / 'Αναφορά [Τελική] 2027.txt').write_text('Ακριβές όνομα')
            settings.append(f'XDG_{kind}_DIR="$HOME/{folder.name}"\n')
        (config_dir / 'user-dirs.dirs').write_text(''.join(settings))
        with patch('tools.file_search.Path.home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config_dir)}):
            for kind, aliases in cases.items():
                for alias in aliases:
                    with self.subTest(alias=alias):
                        result = self.search(query='Αναφορά', root=alias)
                        self.assertEqual(result['roots'], [str(self.root / f'Configured {kind}')])
                        self.assertEqual([item['name'] for item in result['files']], ['Αναφορά [Τελική] 2027.txt'])

    def test_greek_folder_aliases_use_existing_conventional_folders_without_xdg(self):
        cases = [('Έγγραφα', 'Documents'), ('Λήψεις', 'Downloads'),
                 ('Επιφάνεια εργασίας', 'Desktop'), ('Εικόνες', 'Pictures'),
                 ('Μουσική', 'Music'), ('Βίντεο', 'Videos'),
                 ('Πρότυπα', 'Templates'), ('Κοινόχρηστα', 'Public')]
        for _, name in cases:
            (self.root / name).mkdir()
            (self.root / name / 'found.txt').write_text(name)
        with patch('tools.file_search.Path.home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'missing-config')}):
            for alias, name in cases:
                with self.subTest(alias=alias):
                    result = self.search(query='found.txt', root=alias)
                    self.assertEqual(result['roots'], [str(self.root / name)])
                    self.assertEqual(len(result['files']), 1)

    def test_literal_greek_folder_and_absolute_path_win_over_alias(self):
        literal = self.root / 'Έγγραφα'
        literal.mkdir()
        (literal / 'literal.txt').write_text('literal')
        translated = self.root / 'Documents'
        translated.mkdir()
        (translated / 'other.txt').write_text('other')
        config_dir = self.root / 'config'
        config_dir.mkdir()
        (config_dir / 'user-dirs.dirs').write_text('XDG_DOCUMENTS_DIR="$HOME/Documents"\n')
        with patch('tools.file_search.Path.home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config_dir)}):
            for root in ['Έγγραφα', str(literal)]:
                with self.subTest(root=root):
                    result = self.search(query='literal', root=root)
                    self.assertEqual(result['roots'], [str(literal)])
                    self.assertEqual([item['name'] for item in result['files']], ['literal.txt'])

    def test_unknown_named_folder_does_not_search_working_directory(self):
        with patch('tools.file_search.Path.home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}):
            self.assertIn('error', self.search(query='anything', root='absent-folder'))

    def test_general_chat_exposes_file_search(self):
        from agent.base import AgentContext
        from tools.base import ToolRegistry
        manager = SkillManager(self.root / 'skills')
        general = manager.get('general')
        registry = ToolRegistry()
        registry.register(build_file_tools(self.config)[0])
        context = AgentContext(user_id='alice', conversation_id='test', system_prompt='',
                               history=[], provider=None, provider_kind='openai', engine_name='responses',
                               tools=registry, skill_tools=general.tools)
        self.assertIn('file_search', [item['function']['name'] for item in context.tool_schemas()])

    def test_skill_and_tool_discovery(self):
        manager = SkillManager(self.root / 'skills')
        skill = manager.get('FILE_SEARCH')
        self.assertIsNotNone(skill)
        self.assertEqual(skill.tools, ['file_search'])
        tool = build_file_tools(self.config)[0]
        result = json.loads(tool.call({'query': 'invoice'}, self.ctx))
        self.assertEqual(len(result['files']), 1)
        self.assertTrue(result['files'][0]['download_url'].startswith('http://localhost/api/files/download/'))


if __name__ == '__main__':
    unittest.main()
