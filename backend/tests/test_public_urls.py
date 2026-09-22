import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_urls import public_url
from tools.file_search import search_files
from tools.base import ToolContext
from tools.obsidian_tools import build_obsidian_tools


class PublicURLTests(unittest.TestCase):
    def test_ip_fqdn_and_proxy_prefix(self):
        for base in ['https://192.168.1.218', 'https://apex.example.com/', 'https://example.com/apex/', 'http://192.168.1.218:5001']:
            with self.subTest(base=base):
                config = SimpleNamespace(BASE_URL=base)
                self.assertEqual(public_url(config, '/api/files/download/test'), base.rstrip('/') + '/api/files/download/test')

    def test_invalid_base_rejected(self):
        for base in ['', 'localhost:5001', 'file:///tmp', 'https://user:secret@host', 'https://host?query=1', 'https://host#fragment']:
            with self.subTest(base=base), self.assertRaises(ValueError):
                public_url(SimpleNamespace(BASE_URL=base), '/api/files/download/test')

    def test_search_and_image_links_share_public_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'image with spaces.png').write_bytes(b'image')
            for base in ['https://192.168.1.218', 'https://apex.example.com/prefix/']:
                config = SimpleNamespace(BASE_URL=base, FILE_SEARCH_ROOTS=directory, SECRET_KEY='test', OBSIDIAN_VAULT_PATH=root)
                result = search_files({'query': '*.png'}, ToolContext(user_id='user'), config)
                self.assertTrue(result['files'][0]['download_url'].startswith(base.rstrip('/') + '/api/files/download/'))
                tools = {tool.name: tool for tool in build_obsidian_tools(config)}
                link = tools['obsidian_attachment_url'].handler({'path': 'image with spaces.png'}, None)
                self.assertEqual(link, base.rstrip('/') + '/api/obsidian/file?path=image%20with%20spaces.png')


if __name__ == '__main__':
    unittest.main()
