import os
import tempfile
from pathlib import Path

import pytest

from tools.obsidian_tools import build_obsidian_tools


class _Cfg:
    OBSIDIAN_VAULT_PATH = ""
    BASE_URL = "http://localhost:5001"
    OBSIDIAN_DAILY_NOTES_FOLDER = ""
    OBSIDIAN_DAILY_NOTES_FORMAT = "%Y-%m-%d"


@pytest.fixture
def vault():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Ideas").mkdir()
        (root / "Projects").mkdir()
        (root / "Ideas" / "black-horse.md").write_text("# Black horse\nA beautiful black horse.\n")
        (root / "Projects" / "apex.md").write_text("# Apex\nBuild an assistant named Apex.\n")
        (root / "todo.md").write_text("# Todo\n- buy milk\n- call Apex\n")
        cfg = _Cfg()
        cfg.OBSIDIAN_VAULT_PATH = str(root)
        yield cfg


def test_list_notes(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_list_notes"].handler({"limit": 10}, None)
    assert "black-horse.md" in out
    assert "apex.md" in out
    assert "todo.md" in out


def test_list_notes_subfolder(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_list_notes"].handler({"path": "Ideas"}, None)
    assert "black-horse.md" in out
    assert "apex.md" not in out


def test_read_note(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_read_note"].handler({"path": "Projects/apex.md"}, None)
    assert "Build an assistant named Apex" in out


def test_search_notes(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_search_notes"].handler({"query": "black horse"}, None)
    assert "black-horse.md" in out
    assert "beautiful black horse" in out


def test_path_traversal_blocked(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_read_note"].handler({"path": "../secret.md"}, None)
    assert "escapes" in out.lower()


def test_unconfigured():
    cfg = _Cfg()
    cfg.OBSIDIAN_VAULT_PATH = ""
    tools = {t.name: t for t in build_obsidian_tools(cfg)}
    out = tools["obsidian_list_notes"].handler({}, None)
    assert "not configured" in out.lower()


def test_create_and_update_note(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_create_note"].handler({"path": "New/Idea.md", "content": "# Idea\nInitial."}, None)
    assert "Created" in out
    updated = tools["obsidian_update_note"].handler({"path": "New/Idea.md", "content": "# Idea\nUpdated."}, None)
    assert "Updated" in updated
    content = tools["obsidian_read_note"].handler({"path": "New/Idea.md"}, None)
    assert "Updated" in content


def test_delete_note(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_delete_note"].handler({"path": "todo.md"}, None)
    assert "Deleted" in out
    assert "todo.md" not in tools["obsidian_list_notes"].handler({"limit": 10}, None)


def test_create_and_delete_folder(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    assert "Created folder Archive" in tools["obsidian_create_folder"].handler({"path": "Archive"}, None)
    assert "Deleted folder Archive" in tools["obsidian_delete_folder"].handler({"path": "Archive"}, None)


def test_outgoing_links(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    (Path(vault.OBSIDIAN_VAULT_PATH) / "links.md").write_text("See [[black-horse]] and [[Projects/apex|Apex project]].")
    out = tools["obsidian_get_outgoing_links"].handler({"path": "links.md"}, None)
    assert "black-horse" in out
    assert "Apex project" in out


def test_backlinks(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    (Path(vault.OBSIDIAN_VAULT_PATH) / "links.md").write_text("See [[black-horse]].")
    out = tools["obsidian_get_backlinks"].handler({"path": "Ideas/black-horse.md"}, None)
    assert "links.md" in out


def test_follow_link(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_follow_link"].handler({"link": "black-horse"}, None)
    assert "Black horse" in out


def test_note_metadata(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    (Path(vault.OBSIDIAN_VAULT_PATH) / "meta.md").write_text("---\ntags:\n  - idea\n  - apex\n---\n# Meta\n#inline\n")
    out = tools["obsidian_get_note_metadata"].handler({"path": "meta.md"}, None)
    assert "idea" in out
    assert "inline" in out


def test_search_by_tag(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    (Path(vault.OBSIDIAN_VAULT_PATH) / "tagged.md").write_text("# Tagged\n#important task\n")
    out = tools["obsidian_search_by_tag"].handler({"tag": "important"}, None)
    assert "tagged.md" in out


def test_daily_note(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    out = tools["obsidian_daily_note"].handler({}, None)
    assert "Created" in out or "Opened" in out


def test_list_attachments(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    (Path(vault.OBSIDIAN_VAULT_PATH) / "img.png").write_bytes(b"\x89PNG")
    out = tools["obsidian_list_attachments"].handler({}, None)
    assert "img.png" in out


def test_attachment_url(vault):
    tools = {t.name: t for t in build_obsidian_tools(vault)}
    (Path(vault.OBSIDIAN_VAULT_PATH) / "img.png").write_bytes(b"\x89PNG")
    out = tools["obsidian_attachment_url"].handler({"path": "img.png"}, None)
    assert "http://localhost:5001/api/obsidian/file?path=img.png" in out
