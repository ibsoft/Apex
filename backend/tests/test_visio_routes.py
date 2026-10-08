"""VISIO settings and detection use the existing authenticated/CSRF session."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from test_auth_routes import AuthRouteTestCase
from security import CSRF_HEADER
from config import config
from tools import visio_tools


class VisioFrameRouteTests(AuthRouteTestCase):
    """A captured frame is served from disk to the person who captured it."""

    def setUp(self):
        super().setUp()
        # The frames live under DATA_DIR/visio/<user>/; a test must write its
        # snapshots into a throwaway directory, never the live data dir.
        self._frames_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._frames_tmp.cleanup)
        data_dir = patch.object(config, "DATA_DIR", Path(self._frames_tmp.name))
        data_dir.start()
        self.addCleanup(data_dir.stop)

    def _frame_url(self, user_id="alice"):
        from tools.visio_tools import preview_url
        return preview_url(config, user_id, b"\xff\xd8jpeg-bytes")

    def _stored_file(self, url, user_id="alice"):
        token = url.rsplit("/", 1)[-1]
        key = visio_tools._frame_signer(config).loads(
            token, max_age=visio_tools._FRAME_TTL_SECONDS
        )["k"]
        return Path(self._frames_tmp.name) / "visio" / user_id / f"{key}.jpg"

    def test_a_frame_needs_a_session_and_is_not_cached(self):
        client = self._app().test_client()
        url = self._frame_url()
        route = url.split("/api", 1)[1]
        self.assertEqual(client.get(f"/api{route}").status_code, 401)
        self._login(client)
        response = client.get(f"/api{route}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"\xff\xd8jpeg-bytes")
        self.assertEqual(response.mimetype, "image/jpeg")
        # The frame is released after its TTL; a proxy or the disk cache must
        # not keep a picture of the room past that.
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_another_users_frame_is_not_found(self):
        client = self._app().test_client()
        url = self._frame_url("bob")
        route = url.split("/api", 1)[1]
        self._login(client)
        # 404, not 403: a token minted for someone else has to be
        # indistinguishable from one that was never issued.
        self.assertEqual(client.get(f"/api{route}").status_code, 404)

    def test_forged_and_expired_tokens(self):
        client = self._app().test_client()
        self._login(client)
        self.assertEqual(client.get("/api/visio/frame/not-a-real-token").status_code, 404)
        # An honestly signed token whose age is past its max_age. The TTL is
        # negative for the whole exchange, signing included, so this is the
        # signature expiring (410) rather than the file having been swept off
        # disk - which is the other limit, asserted separately below.
        with patch("tools.visio_tools._FRAME_TTL_SECONDS", -1):
            stale = self._frame_url()
            self.assertEqual(client.get(f"/api{stale.split('/api', 1)[1]}").status_code, 410)

    def test_a_frame_removed_from_disk_is_not_found(self):
        client = self._app().test_client()
        self._login(client)
        url = self._frame_url()
        route = f"/api{url.split('/api', 1)[1]}"
        self._stored_file(url).unlink()
        self.assertEqual(client.get(route).status_code, 404)

    def test_locked_sessions_cannot_read_a_frame(self):
        client = self._app().test_client()
        url = self._frame_url()
        self._login(client)
        with client.session_transaction() as session:
            session["locked"] = True
        self.assertEqual(client.get(f"/api{url.split('/api', 1)[1]}").status_code, 423)


class VisioRouteTests(AuthRouteTestCase):
    def test_detection_requires_login_and_respects_lock(self):
        client = self._app().test_client()
        self.assertEqual(client.get("/api/visio/cameras").status_code, 401)
        token = self._login(client)
        with patch("tools.visio_tools.camera_devices", return_value=[]):
            result = client.get("/api/visio/cameras")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["cameras"], [])
        with client.session_transaction() as session:
            session["locked"] = True
        self.assertEqual(client.get("/api/visio/cameras").status_code, 423)

    def test_settings_roundtrip_and_validation(self):
        client = self._app().test_client()
        token = self._login(client)
        data = {"visio_enabled": True, "visio_provider": "ollama", "visio_model": "image-model", "visio_camera": "/dev/video2"}
        self.assertEqual(client.post("/api/settings", json=data).status_code, 403)
        response = client.post("/api/settings", json=data, headers={CSRF_HEADER: token})
        self.assertEqual(response.status_code, 200)
        for key, value in data.items():
            self.assertEqual(response.json["settings"][key], value)
        for invalid in ({"visio_camera": "/etc/passwd"}, {"visio_provider": []}, {"visio_model": []}):
            self.assertEqual(client.post("/api/settings", json=invalid, headers={CSRF_HEADER: token}).status_code, 400)
        result = client.post("/api/settings", json={"visio_enabled": "false"}, headers={CSRF_HEADER: token})
        self.assertFalse(result.json["settings"]["visio_enabled"])
