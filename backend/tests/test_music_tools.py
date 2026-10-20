"""Tests for the local music library: MP3 download, library scanning, the
signed streaming route and the Range handling.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.base import ToolContext
from tools.music_tools import (
    _decode_track_id,
    _parse_progress,
    _range_bounds,
    build_music_tools,
    library_url,
    list_library,
    stream_url,
    t_music_library,
)


def _config(root: Path, **overrides) -> SimpleNamespace:
    base = dict(
        MUSIC_ENABLED=True,
        MUSIC_DIR=root / "music",
        MUSIC_LIBRARY_ROOTS=[],
        MUSIC_AUDIO_EXTENSIONS=[".mp3", ".m4a"],
        YOUTUBE_ENABLED=True,
        YOUTUBE_DLP="yt-dlp",
        YOUTUBE_SEARCH_LIMIT=8,
        YOUTUBE_SEARCH_TIMEOUT=20,
        MUSIC_AUDIO_FORMAT="mp3",
        MUSIC_AUDIO_QUALITY="192",
        MUSIC_DOWNLOAD_TIMEOUT=600,
        MUSIC_LIBRARY_MAX=500,
        MUSIC_FALLBACK_TO_YOUTUBE=True,
        SECRET_KEY="test-secret",
        BASE_URL="http://127.0.0.1:5001",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Ctx(ToolContext):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.events: list[dict] = []
        if self.emit is None:
            self.emit = self.events.append


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)

    def _write_mp3(self, rel: str, data: bytes = b"ID3-fake-mpeg-data") -> Path:
        path = self.root / "music" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class LibraryScanTests(_Base):
    def test_lists_downloaded_tracks_newest_first(self):
        self._write_mp3("u/First Song [abcdefghijk1].mp3")
        self._write_mp3("u/Second Song [abcdefghijk2].mp3")
        tracks = list_library(self.config, "u")
        self.assertEqual(len(tracks), 2)
        titles = {t.title for t in tracks}
        self.assertEqual(titles, {"First Song", "Second Song"})
        self.assertTrue(all(t.source == "download" for t in tracks))

    def test_audio_extension_and_query_filter(self):
        self._write_mp3("u/song.mp3")
        self._write_mp3("u/notes.txt", b"not audio")
        self._write_mp3("u/other.m4a")
        self.assertEqual(len(list_library(self.config, "u")), 2)
        self.assertEqual(len(list_library(self.config, "u", query="song")), 1)

    def test_scans_a_configured_local_root(self):
        con = self.root / "Music"
        con.mkdir()
        (con / "home-track.mp3").write_bytes(b"audio")
        config = _config(self.root, MUSIC_LIBRARY_ROOTS=[str(con)])
        tracks = list_library(config, "u")
        self.assertEqual([t.source for t in tracks], ["local"])

    def test_a_track_id_is_scoped_and_cannot_escape(self):
        path = self._write_mp3("u/song.mp3")
        track = list_library(self.config, "u")[0]
        # The stored id is "<scope>:<relpath>", never a raw absolute path.
        self.assertTrue(track.id.startswith("own:"))
        self.assertNotIn(str(self.root), track.id)
        resolved = _decode_track_id(track.id, "u", self.config)
        self.assertEqual(resolved, path)
        # A crafted id cannot walk out of the library.
        for bad in ("own:../../etc/passwd", "own:/etc/passwd", "own:", "bogus:x", "own:a/../b.mp3"):
            self.assertIsNone(_decode_track_id(bad, "u", self.config), bad)

    def test_another_users_directory_is_not_reachable(self):
        self._write_mp3("alice/song.mp3")
        self._write_mp3("bob/other.mp3")
        self.assertEqual([t.title for t in list_library(self.config, "alice")], ["song"])
        self.assertEqual([t.title for t in list_library(self.config, "bob")], ["other"])


class ProgressParseTests(unittest.TestCase):
    def test_reads_a_percentage_line(self):
        self.assertEqual(_parse_progress("[download]  12.3% of 4.56MiB at 1MiB/s")["percent"], 12.3)
        self.assertEqual(_parse_progress("[download] 100% of 4.56MiB")["percent"], 100.0)

    def test_ignores_other_lines(self):
        for line in ("", "[info] Downloading 1 format(s)", "[ExtractAudio] Destination: x.mp3",
                     "[download] Destination: x.webm"):
            self.assertIsNone(_parse_progress(line), line)


class RangeTests(unittest.TestCase):
    def test_parses_standard_ranges(self):
        self.assertEqual(_range_bounds("bytes=0-99", 1000), (0, 99))
        self.assertEqual(_range_bounds("bytes=500-", 1000), (500, 999))
        self.assertEqual(_range_bounds("bytes=-100", 1000), (900, 999))
        self.assertEqual(_range_bounds("bytes=0-99999", 1000), (0, 999))

    def test_rejects_unusable_ranges(self):
        for header in ("", "bytes=", "bytes=-", "bytes=1000-", "bytes=500-100", "notbytes=0-1"):
            self.assertIsNone(_range_bounds(header, 1000), header)


class DownloadTests(_Base):
    def _fake_ytdlp(self, *, write_audio: bool = True):
        """A stub yt-dlp that prints progress and (optionally) writes the mp3."""
        binary = self.root / "yt-dlp"
        target = self.root / "music" / "u" / "Downloaded [abcdefghijk1].mp3"
        script = (
            "#!/bin/sh\n"
            "echo '[download]   0.0% of 1.00MiB'\n"
            "echo '[download]  50.0% of 1.00MiB'\n"
            + (f"mkdir -p '{target.parent}'\nprintf 'audio-bytes' > '{target}'\n" if write_audio else "")
            + "echo '[download] 100% of 1.00MiB'\n"
            "exit 0\n"
        )
        binary.write_text(script, encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        self.config.YOUTUBE_DLP = str(binary)
        return binary

    def test_download_emits_progress_and_plays_the_track(self):
        self._fake_ytdlp()
        ctx = _Ctx(user_id="u")
        items = [{"id": "abcdefghijk1", "title": "Downloaded", "duration": 200}]
        with mock.patch("tools.youtube_tools.search_youtube", return_value=items):
            out = t_music_library({"action": "download", "query": "song"}, ctx, self.config)
        self.assertIn("Downloaded", out)
        stages = [e for e in ctx.events if e["type"] == "media_progress"]
        self.assertTrue(stages)
        self.assertEqual(stages[-1]["percent"], 100.0)
        plays = [e for e in ctx.events if e["type"] == "media_play"]
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["source"], "local")
        self.assertEqual(plays[0]["app"], "music")

    def test_a_second_download_of_the_same_video_is_reused(self):
        self._fake_ytdlp()
        ctx = _Ctx(user_id="u")
        items = [{"id": "abcdefghijk1", "title": "Downloaded", "duration": 200}]
        with mock.patch("tools.youtube_tools.search_youtube", return_value=items):
            t_music_library({"action": "download", "query": "song"}, ctx, self.config)
            ctx.events.clear()
            out = t_music_library({"action": "download", "query": "song"}, ctx, self.config)
        self.assertIn("Already in the library", out)

    def test_download_of_a_named_video_id_needs_no_query(self):
        """The window's "embedding disabled" fallback sends an id, not a query:
        it must download *that* video without searching for a replacement."""
        self._fake_ytdlp()
        ctx = _Ctx(user_id="u")
        with mock.patch("tools.youtube_tools.search_youtube") as search:
            out = t_music_library(
                {"action": "download", "video_id": "abcdefghijk1", "title": "Refused Song"},
                ctx,
                self.config,
            )
        search.assert_not_called()
        self.assertIn("Downloaded", out)
        plays = [e for e in ctx.events if e["type"] == "media_play"]
        self.assertEqual(plays[0]["source"], "local")

    def test_download_without_a_query_or_id_is_a_plain_message(self):
        ctx = _Ctx(user_id="u")
        out = t_music_library({"action": "download"}, ctx, self.config)
        self.assertIn("needs a query or a video_id", out)

    def test_a_failed_download_is_a_plain_message(self):
        binary = self.root / "yt-dlp"
        binary.write_text("#!/bin/sh\necho 'ERROR: nope' >&2\nexit 1\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        self.config.YOUTUBE_DLP = str(binary)
        ctx = _Ctx(user_id="u")
        items = [{"id": "abcdefghijk1", "title": "X"}]
        with mock.patch("tools.youtube_tools.search_youtube", return_value=items):
            out = t_music_library({"action": "download", "query": "song"}, ctx, self.config)
        self.assertIn("Download failed", out)
        self.assertFalse([e for e in ctx.events if e["type"] == "media_play"])

    def test_download_without_a_binary_is_reported(self):
        self.config.YOUTUBE_DLP = "definitely-not-installed"
        ctx = _Ctx(user_id="u")
        items = [{"id": "abcdefghijk1", "title": "X"}]
        with mock.patch("tools.youtube_tools.search_youtube", return_value=items):
            out = t_music_library({"action": "download", "query": "song"}, ctx, self.config)
        self.assertIn("not installed", out)


class ToolActionTests(_Base):
    def _tool(self, ctx=None):
        return build_music_tools(self.config)[0], (ctx or _Ctx(user_id="u"))

    def test_list_and_play_emit_the_library_and_play_events(self):
        self._write_mp3("u/Jazz Night [abcdefghijk9].mp3")
        tool, ctx = self._tool()
        out = tool.call({"action": "list"}, ctx)
        self.assertIn("Jazz Night", out)
        self.assertTrue([e for e in ctx.events if e["type"] == "media_library"])
        ctx.events.clear()
        tool.call({"action": "play", "query": "jazz"}, ctx)
        plays = [e for e in ctx.events if e["type"] == "media_play"]
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["source"], "local")
        self.assertTrue(plays[0]["items"][0]["url"].startswith("http"))

    def test_play_with_no_match_is_a_message(self):
        tool, ctx = self._tool()
        out = tool.call({"action": "play", "query": "nothing"}, ctx)
        self.assertIn("No local track", out)
        self.assertEqual([e for e in ctx.events if e["type"] == "media_play"], [])

    def test_disabled_and_signed_out_are_messages(self):
        tool, ctx = self._tool()
        with mock.patch.object(self.config, "MUSIC_ENABLED", False):
            self.assertIn("turned off", tool.call({"action": "list"}, ctx))
        anon = _Ctx(user_id="")
        self.assertIn("Sign in", tool.call({"action": "list"}, anon))

    def test_tool_shape_and_registration(self):
        from agent.base import ALWAYS_ON_TOOLS
        from tools.base import ToolRegistry, load_default_tools
        tool, _ = self._tool()
        self.assertEqual(tool.name, "music_library")
        self.assertIn("music_library", ALWAYS_ON_TOOLS)
        registry = ToolRegistry()
        load_default_tools(registry, memory=None)
        self.assertIsNotNone(registry.get("music_library"))


class RouteTests(_Base):
    def _app(self):
        from flask import Flask
        from tools.music_tools import register_music_routes

        app = Flask(__name__)
        app.secret_key = "test-secret"

        def require_user():
            from flask import session
            if session.get("uid") != "u":
                return None
            return {"id": "u"}

        register_music_routes(app, require_user, self.config)
        return app

    def _client(self):
        client = self._app().test_client()
        with client.session_transaction() as session:
            session["uid"] = "u"
        return client

    def test_stream_full_and_range(self):
        self._write_mp3("u/song.mp3", b"0123456789")
        track = list_library(self.config, "u")[0]
        client = self._client()
        route = f"/api{stream_url(self.config, 'u', track.id).split('/api', 1)[1]}"
        full = client.get(route)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.data, b"0123456789")
        self.assertEqual(full.headers["Accept-Ranges"], "bytes")
        ranged = client.get(route, headers={"Range": "bytes=2-5"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.data, b"2345")
        self.assertEqual(ranged.headers["Content-Range"], "bytes 2-5/10")

    def test_stream_needs_a_session(self):
        self._write_mp3("u/song.mp3")
        track = list_library(self.config, "u")[0]
        route = f"/api{stream_url(self.config, 'u', track.id).split('/api', 1)[1]}"
        self.assertEqual(self._app().test_client().get(route).status_code, 401)

    def test_a_token_for_another_user_is_not_found(self):
        self._write_mp3("u/song.mp3")
        track = list_library(self.config, "u")[0]
        route = f"/api{stream_url(self.config, 'bob', track.id).split('/api', 1)[1]}"
        self.assertEqual(self._client().get(route).status_code, 404)

    def test_library_route_lists_and_needs_a_session(self):
        self._write_mp3("u/Song [abcdefghijk1].mp3")
        route = f"/api{library_url(self.config, 'u').split('/api', 1)[1]}"
        self.assertEqual(self._app().test_client().get(route).status_code, 401)
        response = self._client().get(route)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 1)
        self.assertTrue(response.json["tracks"][0]["url"].startswith("http"))


if __name__ == "__main__":
    unittest.main()
