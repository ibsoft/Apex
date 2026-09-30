"""VISIO settings and detection use the existing authenticated/CSRF session."""
from unittest.mock import patch

from test_auth_routes import AuthRouteTestCase
from security import CSRF_HEADER


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
