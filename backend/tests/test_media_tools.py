"""Tests for the YouTube music/video players' backend: the media_player tool,
its yt-dlp search, the events it emits and the deterministic MEDIA router."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.base import ALWAYS_ON_TOOLS
from skills.manager import SkillManager, force_media_skill, route_skill
from tools.base import ToolContext, ToolRegistry, load_default_tools
from tools.youtube_tools import (
    ACTIONS,
    _parse_entry,
    build_media_tools,
    clear_search_cache,
    search_youtube,
    t_media,
)


def _config(**overrides) -> SimpleNamespace:
    base = dict(
        YOUTUBE_ENABLED=True,
        YOUTUBE_DLP="yt-dlp",
        YOUTUBE_SEARCH_LIMIT=8,
        YOUTUBE_SEARCH_TIMEOUT=20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _items(n=3) -> list[dict]:
    return [
        {
            "id": f"vid{i:013d}",
            "title": f"Track {i}",
            "channel": f"Channel {i}",
            "duration": 60 + i,
            "url": f"https://www.youtube.com/watch?v=vid{i:013d}",
            "thumbnail": f"https://i.ytimg.com/vi/vid{i:013d}/hqdefault.jpg",
        }
        for i in range(1, n + 1)
    ]


class _Ctx(ToolContext):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.events: list[dict] = []
        if self.emit is None:
            self.emit = self.events.append


def _tool(config=None, ctx=None):
    config = config or _config()
    ctx = ctx or _Ctx(user_id="u")
    tools = build_media_tools(config)
    assert len(tools) == 1
    return tools[0], ctx


class MediaToolTests(unittest.TestCase):
    def test_tool_shape(self):
        tool, _ = _tool()
        self.assertEqual(tool.name, "media_player")
        props = tool.parameters["properties"]
        self.assertEqual(props["app"]["enum"], ["music", "video"])
        self.assertEqual(set(props["action"]["enum"]), set(ACTIONS))
        self.assertEqual(tool.parameters["required"], ["app", "action"])
        self.assertFalse(tool.parameters.get("additionalProperties", True))

    def test_media_player_is_always_on_and_registered(self):
        self.assertIn("media_player", ALWAYS_ON_TOOLS)
        registry = ToolRegistry()
        load_default_tools(registry, memory=None)
        self.assertIsNotNone(registry.get("media_player"))

    def test_play_searches_and_emits_the_window_event(self):
        tool, ctx = _tool()
        with mock.patch("tools.youtube_tools.search_youtube", return_value=_items()):
            out = tool.call({"app": "music", "action": "play", "query": "jazz"}, ctx)
        self.assertIn("Playing 'Track 1'", out)
        self.assertEqual(len(ctx.events), 1)
        event = ctx.events[0]
        self.assertEqual(event["type"], "media_play")
        self.assertEqual(event["app"], "music")
        self.assertEqual(event["index"], 0)
        self.assertEqual(len(event["items"]), 3)
        self.assertTrue(event["items"][0]["url"].startswith("https://www.youtube.com/watch?v="))

    def test_play_picks_a_result_by_number_and_clamps_it(self):
        tool, ctx = _tool()
        with mock.patch("tools.youtube_tools.search_youtube", return_value=_items()):
            out = tool.call({"app": "video", "action": "play", "query": "live", "pick": 2}, ctx)
        self.assertEqual(ctx.events[0]["index"], 1)
        self.assertIn("result 2 of 3", out)
        ctx.events.clear()
        with mock.patch("tools.youtube_tools.search_youtube", return_value=_items()):
            tool.call({"app": "video", "action": "play", "query": "live", "pick": 99}, ctx)
        self.assertEqual(ctx.events[0]["index"], 2)

    def test_search_lists_results_without_emitting(self):
        tool, ctx = _tool()
        with mock.patch("tools.youtube_tools.search_youtube", return_value=_items()):
            out = tool.call({"app": "music", "action": "search", "query": "jazz"}, ctx)
        self.assertIn("1. Track 1 — Channel 1 [vid0000000000001]", out)
        self.assertIn('action="play" and pick=<number>', out)
        self.assertEqual(ctx.events, [])

    def test_control_actions_emit_and_status_does_not(self):
        for action in ("stop", "pause", "resume", "next", "previous", "close",
                       "maximize", "minimize", "restore"):
            tool, ctx = _tool()
            out = tool.call({"app": "video", "action": action}, ctx)
            self.assertEqual(
                ctx.events,
                [{"type": "media_control", "app": "video", "action": action}],
                action,
            )
            self.assertIn("Video player", out)
        tool, ctx = _tool()
        out = tool.call({"app": "music", "action": "status"}, ctx)
        self.assertEqual(ctx.events, [])
        self.assertIn("media context", out)

    def test_missing_query_unknown_app_and_unknown_action_are_messages(self):
        tool, ctx = _tool()
        self.assertIn("needs a query", tool.call({"app": "music", "action": "play"}, ctx))
        self.assertIn("app=", tool.call({"app": "radio", "action": "play", "query": "x"}, ctx))
        self.assertIn("app=", tool.call({"action": "play", "query": "x"}, ctx))
        self.assertIn("Unknown action", tool.call({"app": "music", "action": "teleport"}, ctx))
        self.assertEqual(ctx.events, [])

    def test_disabled_playback_reports_itself(self):
        tool, ctx = _tool(config=_config(YOUTUBE_ENABLED=False))
        out = tool.call({"app": "music", "action": "play", "query": "jazz"}, ctx)
        self.assertIn("turned off", out)
        self.assertEqual(ctx.events, [])

    def test_search_failures_are_plain_messages(self):
        tool, ctx = _tool()
        with mock.patch("tools.youtube_tools.search_youtube",
                        side_effect=subprocess.TimeoutExpired("yt-dlp", 20)):
            self.assertIn("timed out", tool.call({"app": "music", "action": "play", "query": "x"}, ctx))
        with mock.patch("tools.youtube_tools.search_youtube",
                        side_effect=RuntimeError("yt-dlp is not installed (looked for `yt-dlp`).")):
            self.assertIn("not installed", tool.call({"app": "music", "action": "play", "query": "x"}, ctx))
        with mock.patch("tools.youtube_tools.search_youtube",
                        side_effect=RuntimeError("no results (nothing found)")):
            self.assertIn("no results", tool.call({"app": "music", "action": "search", "query": "x"}, ctx))
        self.assertEqual(ctx.events, [])

    def test_missing_binary_is_reported_before_running_it(self):
        tool, ctx = _tool(config=_config(YOUTUBE_DLP="definitely-not-installed"))
        out = tool.call({"app": "music", "action": "play", "query": "jazz"}, ctx)
        self.assertIn("not installed", out)
        self.assertEqual(ctx.events, [])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "yt-dlp"
        # The search cache is module state: without this a test that searches
        # the same query would be served the previous test's results.
        clear_search_cache()
        self.addCleanup(clear_search_cache)

    def _fake_ytdlp(self, stdout: str, stderr: str = ""):
        self.binary.write_text(
            f"#!/bin/sh\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\nexit 0\n",
            encoding="utf-8",
        )
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        return _config(YOUTUBE_DLP=str(self.binary))

    def test_search_parses_flat_playlist_json_lines(self):
        entries = "\n".join(
            json.dumps({"id": f"abcdefghijk{i}", "title": f"Song {i}",
                        "channel": f"Chan {i}", "duration": 100 + i})
            for i in range(1, 5)
        )
        config = self._fake_ytdlp(entries + "\n")
        items = search_youtube("jazz", config)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["id"], "abcdefghijk1")
        self.assertEqual(items[0]["title"], "Song 1")
        self.assertEqual(items[0]["duration"], 101)
        self.assertEqual(items[0]["url"], "https://www.youtube.com/watch?v=abcdefghijk1")
        self.assertTrue(items[0]["thumbnail"].endswith("/hqdefault.jpg"))

    def test_search_tolerates_junk_lines_and_dedupes(self):
        entries = "\n".join([
            "not json at all",
            "[]",
            json.dumps({"id": "short", "title": "no id shape"}),
            json.dumps({"id": "abcdefghijk1", "title": "First", "duration": "not-a-number"}),
            json.dumps({"id": "abcdefghijk1", "title": "Duplicate"}),
            json.dumps({"id": "abcdefghijk2", "title": "Second", "channel": "C"}),
        ])
        config = self._fake_ytdlp(entries + "\n")
        items = search_youtube("jazz", config)
        self.assertEqual([i["id"] for i in items], ["abcdefghijk1", "abcdefghijk2"])
        self.assertEqual(items[0]["duration"], 0)

    def test_search_respects_the_configured_limit(self):
        entries = "\n".join(
            json.dumps({"id": f"abcdefghijk{i}", "title": f"S{i}", "duration": i})
            for i in range(1, 21)
        )
        config = self._fake_ytdlp(entries + "\n")
        config.YOUTUBE_SEARCH_LIMIT = 3
        self.assertEqual(len(search_youtube("x", config)), 3)

    def test_search_with_no_usable_results_raises_a_message(self):
        config = self._fake_ytdlp("", "ERROR: [youtube:search] nothing here\n")
        with self.assertRaises(RuntimeError) as caught:
            search_youtube("x", config)
        self.assertIn("no results", str(caught.exception))
        self.assertIn("nothing here", str(caught.exception))

    def test_a_repeated_search_reuses_the_result_instead_of_rerunning_ytdlp(self):
        calls = self.root / "calls"
        entries = json.dumps({"id": "abcdefghijk1", "title": "Song", "duration": 3}) + "\n"
        self.binary.write_text(
            f"#!/bin/sh\nprintf 'x' >> '{calls}'\nprintf '%s' '{entries}'\n",
            encoding="utf-8",
        )
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        config = _config(YOUTUBE_DLP=str(self.binary), YOUTUBE_SEARCH_CACHE_SECONDS=300)
        first = search_youtube("Jazz Music", config)
        # Case and spacing are normalized, so the same request is a hit.
        second = search_youtube("  jazz   music ", config)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(len(calls.read_text(encoding="utf-8")), 1)
        # A copy, not the cached list: a caller mutating it must not leak.
        second[0]["title"] = "changed"
        self.assertEqual(search_youtube("jazz music", config)[0]["title"], "Song")
        # Expiry is the only reason to run yt-dlp again, and a zero TTL disables
        # the cache outright - the off switch for a deployment that wants it.
        config.YOUTUBE_SEARCH_CACHE_SECONDS = 0
        search_youtube("jazz music", config)
        self.assertEqual(len(calls.read_text(encoding="utf-8")), 2)

    def test_a_failed_search_is_never_cached(self):
        calls = self.root / "calls"
        self.binary.write_text(
            f"#!/bin/sh\nprintf 'x' >> '{calls}'\necho 'ERROR: nope' >&2\nexit 1\n",
            encoding="utf-8",
        )
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        config = _config(YOUTUBE_DLP=str(self.binary))
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                search_youtube("x", config)
        self.assertEqual(len(calls.read_text(encoding="utf-8")), 2)

    def test_a_binary_that_is_not_executable_is_reported(self):
        self.binary.write_text("x", encoding="utf-8")  # no exec bit
        with self.assertRaises(RuntimeError) as caught:
            search_youtube("x", _config(YOUTUBE_DLP=str(self.binary)))
        self.assertIn("not installed", str(caught.exception))

    def test_disabled_config_never_touches_the_subprocess(self):
        config = _config(YOUTUBE_ENABLED=False, YOUTUBE_DLP=str(self.binary))
        with self.assertRaises(RuntimeError) as caught:
            search_youtube("x", config)
        self.assertIn("disabled", str(caught.exception))


class ParseEntryTests(unittest.TestCase):
    def test_non_object_and_malformed_lines_are_skipped(self):
        self.assertIsNone(_parse_entry(""))
        self.assertIsNone(_parse_entry("nope"))
        self.assertIsNone(_parse_entry("[1, 2]"))
        self.assertIsNone(_parse_entry("{not json"))

    def test_entry_without_a_usable_id_is_skipped(self):
        self.assertIsNone(_parse_entry(json.dumps({"title": "x"})))
        self.assertIsNone(_parse_entry(json.dumps({"id": "../../etc", "title": "x"})))

    def test_missing_fields_fall_back_safely(self):
        item = _parse_entry(json.dumps({"id": "abcdefghijk9"}))
        self.assertEqual(item["title"], "Untitled")
        self.assertEqual(item["channel"], "")
        self.assertEqual(item["duration"], 0)


class MediaRoutingTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillManager().all()
        self.assertTrue(any(s.name == "MEDIA" for s in self.skills),
                        "the MEDIA skill definition must be loaded")

    def test_media_definition_is_wellformed(self):
        media = [s for s in self.skills if s.name == "MEDIA"]
        self.assertEqual(len(media), 1)
        self.assertIn("media_player", media[0].tools)

    def test_playback_requests_reach_the_media_skill(self):
        for text in (
            "play some jazz",
            "Play the lofi stream please",
            "can you play some music?",
            "just put on something quiet",
            "put on some music",
            "watch that live set",
            "listen to some piano",
            "pause the music",
            "stop the music",
            "skip to the next song",
            "change the video",
            "next",
            "stop",
            "previous",
            "what can you play for me next?",
            "Σταματα τη μουσικη",
            "βαλε μουσικη",
            "επομενο τραγουδι",
            "κλεισε το βιντεο",
            "αλλαξε τραγουδι",
            "τι μπορεις να παιξεις;",
            "Δείξε μου το επόμενο τραγούδι",
        ):
            self.assertEqual(force_media_skill(text, self.skills), "MEDIA", text)

    def test_accented_greek_reaches_the_skill(self):
        self.assertEqual(force_media_skill("βάλε μουσική", self.skills), "MEDIA")

    def test_final_sigma_forms_still_match(self):
        # `_strip_accents` does not fold ς to σ, so a Greek stem written with a
        # final sigma must match by prefix rather than by exact spelling.
        for text in ("σταματα τη μουσικη", "σταματα τη μουσικη".replace("σ", "ς")):
            self.assertEqual(force_media_skill(text, self.skills), "MEDIA", text)

    def test_non_media_requests_are_not_routed(self):
        for text in (
            "",
            "hello there",
            "run df -h",
            "what is the disk usage?",
            "next steps for the migration",
            "stop the timer",
            "remind me to watch the video later",
            "I like that song a lot",
            "play chess with me",
            "play a game of poker",
            "watch out for that file",
            "open the settings tab",
            "τι ώρα είναι;",
        ):
            self.assertIsNone(force_media_skill(text, self.skills), text)

    def test_code_shaped_text_is_never_routed(self):
        self.assertIsNone(force_media_skill("write a python function that plays music",
                                            self.skills))

    def test_without_the_media_skill_there_is_no_route(self):
        others = [s for s in self.skills if s.name != "MEDIA"]
        self.assertIsNone(force_media_skill("play some jazz", others))

    def test_route_skill_prefers_media_over_the_host_guard(self):
        # The compound request is the reason media sits above the host guard:
        # the shell skill owns the terminal and cannot play anything.
        routed = route_skill("run df -h and put on some music", self.skills, provider=None)
        self.assertEqual(routed, "MEDIA")


if __name__ == "__main__":
    unittest.main()
