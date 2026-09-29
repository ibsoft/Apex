"""Auth routes end to end: login, CSRF, lock, unlock, logout, object access.

The unit tests in test_security_auth.py prove the pieces (PAM plumbing, the
throttle, the sanitizers) work. These prove they are *wired together*: that a
real session cookie can log in, that the CSRF token the login hands out is the
one the server then demands, that a locked session cannot reach the API even by
ignoring the overlay, and that one user cannot read another's objects.

PAM is stubbed at `SystemAuth.authenticate`, so no test needs a password and
none of them can lock the machine's real accounts out.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config
import db as db_module
from security import CSRF_HEADER, csrf_token, new_session_id
from system_auth import AuthResult, SystemAuth, SystemUser, is_login_capable

from test_soul import isolated_db, signed_in  # same throwaway-DB guarantee


def _user(name: str = "alice") -> SystemUser:
    return SystemUser(
        username=name,
        uid=1000,
        gid=1000,
        name="Alice Example",
        home=f"/home/{name}",
        shell="/bin/bash",
    )


class AuthRouteTestCase(unittest.TestCase):
    """Shared app fixture: isolated database, no providers, CSRF on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            patch.object(config, "SYSTEM_LOGIN_ENABLED", True),
            patch.object(config, "LOCK_SCREEN_ENABLED", True),
            patch.object(config, "CSRF_ENABLED", True),
            patch.object(config, "DEV_AUTO_LOGIN", ""),
            # A test run must not append to the real auth-audit.log. The live
            # limiter reads it and it is the record of who signed in, so a
            # "user=alice" row from a fixture is indistinguishable from a real
            # attempt by another person.
            patch.object(config, "SYSTEM_LOGIN_AUDIT_LOG",
                         str(Path(self._tmp.name) / "auth-audit.log")),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self._db_ctx = isolated_db()
        self.db = self._db_ctx.__enter__()
        self.addCleanup(self._db_ctx.__exit__, None, None, None)
        import app as app_module

        self.app_module = app_module
        self._signed_in = signed_in(app_module)
        self._signed_in.__enter__()
        self.addCleanup(self._signed_in.__exit__, None, None, None)

    def _app(self):
        return self.app_module.create_app()

    def _client(self, app):
        return app.test_client()

    def _login(self, client, username: str = "alice", password: str = "correct horse"):
        """Sign in with PAM stubbed to accept, and return the CSRF token."""
        with patch.object(
            SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user(username))
        ):
            resp = client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()["csrf_token"]

    def _headers(self, token: str) -> dict:
        return {CSRF_HEADER: token}


# --------------------------------------------------------------------------
class LoginRouteTests(AuthRouteTestCase):
    def test_login_hands_back_a_token_the_server_itself_accepts(self):
        app = self._app()
        client = self._client(app)
        token = self._login(client)

        # Not just present: the server must agree it is the right token for the
        # session it just created. A token derived from anything else would make
        # every later write fail.
        with client.session_transaction() as session:
            expected = csrf_token(config.SECRET_KEY, session["sid"])
        self.assertEqual(token, expected)

    def test_a_wrong_password_is_rejected_and_creates_no_session(self):
        app = self._app()
        client = self._client(app)
        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=False, error="Incorrect username or password.")):
            resp = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})

        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("csrf_token", resp.get_json())
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_a_service_account_is_refused_by_the_account_filter(self):
        """The route must not be the only thing standing between a daemon and a
        session: a non-login-capable user can never authenticate at all."""
        for shell in ("/usr/sbin/nologin", "/bin/false", "nologin", "", "irrelevant"):
            with self.subTest(shell=shell):
                self.assertFalse(
                    is_login_capable(
                        SystemUser("svc", 999, 999, "svc", "/var/lib/svc", shell)
                    )
                )

    def test_a_locked_out_user_gets_the_wait_time(self):
        app = self._app()
        client = self._client(app)

        def refused(self, username, password, address=""):
            return AuthResult(ok=False, error="Too many attempts.", locked=True)

        with patch.object(SystemAuth, "retry_after", return_value=42), patch.object(
            SystemAuth, "authenticate", side_effect=refused
        ):
            body = client.post(
                "/api/auth/login", json={"username": "alice", "password": "nope"}
            ).get_json()

        self.assertEqual(body["retry_after"], 42)

    def test_throttling_survives_between_requests(self):
        """The whole point of the lockout: attempts accumulate.

        A per-request SystemAuth rebuild would hand every attempt a fresh
        counter, so N wrong passwords would never lock anything out and the
        throttle would be decorative.
        """
        app = self._app()
        client = self._client(app)
        client.get("/api/auth/system-users")  # first use builds the shared instance
        auth = app.extensions["apex_system_auth"]
        # Same address the test client presents: the throttle is keyed by
        # username *and* source, so failures recorded for another address must
        # not lock this one out (and this is why an attacker cannot lock a
        # victim out from a different host).
        for _ in range(config.SYSTEM_LOGIN_MAX_ATTEMPTS):
            auth.limiter.record_failure("alice", "127.0.0.1")

        # Only the PAM call is stubbed. The real `authenticate` runs, so the
        # throttle inside it is the thing under test - patching `authenticate`
        # itself would replace the very check being proven.
        with patch(
            "system_auth._pam_authenticate", side_effect=AssertionError("must not reach PAM")
        ):
            resp = client.post("/api/auth/login", json={"username": "alice", "password": "x"})

        self.assertEqual(resp.status_code, 401)
        self.assertGreater(
            resp.get_json()["retry_after"], 0, "a locked-out attempt must say how long to wait"
        )

    def test_failures_from_another_address_do_not_lock_you_out(self):
        app = self._app()
        client = self._client(app)
        client.get("/api/auth/system-users")
        auth = app.extensions["apex_system_auth"]
        for _ in range(config.SYSTEM_LOGIN_MAX_ATTEMPTS):
            auth.limiter.record_failure("alice", "203.0.113.9")

        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user())):
            resp = client.post("/api/auth/login", json={"username": "alice", "password": "x"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))


# --------------------------------------------------------------------------
class CsrfRouteTests(AuthRouteTestCase):
    def test_a_state_change_without_the_token_is_refused(self):
        app = self._app()
        client = self._client(app)
        self._login(client)
        resp = client.post("/api/settings", json={"engine": "responses"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "invalid_csrf_token")

    def test_the_token_from_login_is_accepted(self):
        app = self._app()
        client = self._client(app)
        token = self._login(client)
        resp = client.post("/api/settings", json={"engine": "responses"}, headers=self._headers(token))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_another_sessions_token_is_refused(self):
        app = self._app()
        alice = self._client(app)
        self._login(alice, "alice")

        other_sid = new_session_id()
        foreign = csrf_token(config.SECRET_KEY, other_sid)
        resp = alice.post("/api/settings", json={"engine": "responses"}, headers=self._headers(foreign))
        self.assertEqual(resp.status_code, 403)

    def test_the_guard_is_off_when_disabled(self):
        app = self._app()
        client = self._client(app)
        self._login(client)
        with patch.object(config, "CSRF_ENABLED", False):
            resp = client.post("/api/settings", json={"engine": "responses"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))


# --------------------------------------------------------------------------
class LockRouteTests(AuthRouteTestCase):
    def test_lock_returns_a_usable_token_for_the_unlock_that_follows(self):
        """Lock rotates the session id, so the old token dies with it.

        The lock screen's very next request is the unlock POST, which is itself
        CSRF-protected. If the replacement token were not returned here, unlock
        could never succeed and the screen would be a one-way door.
        """
        app = self._app()
        client = self._client(app)
        old = self._login(client)

        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user())):
            locked = client.post("/api/auth/lock", headers=self._headers(old))
            self.assertEqual(locked.status_code, 200)
            new_token = locked.get_json().get("csrf_token")
            self.assertTrue(new_token, "lock must return a replacement CSRF token")
            self.assertNotEqual(new_token, old)

            unlocked = client.post(
                "/api/auth/unlock",
                json={"password": "correct horse"},
                headers=self._headers(new_token),
            )
        self.assertEqual(unlocked.status_code, 200, unlocked.get_data(as_text=True))
        self.assertFalse(client.get("/api/auth/status").get_json()["locked"])

    def test_the_old_token_is_dead_after_locking(self):
        app = self._app()
        client = self._client(app)
        old = self._login(client)
        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user())):
            client.post("/api/auth/lock", headers=self._headers(old))
        resp = client.post("/api/settings", json={"engine": "responses"}, headers=self._headers(old))
        self.assertEqual(resp.status_code, 403)

    def test_a_locked_session_cannot_reach_the_api_by_ignoring_the_overlay(self):
        """The lock screen is client state; the server is the control.

        A request that carries a perfectly valid token and cookie still has to
        be refused while the session is locked, or anyone with a stale tab (or
        curl) simply keeps using the machine.
        """
        app = self._app()
        client = self._client(app)
        token = self._login(client)
        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user())):
            body = client.post("/api/auth/lock", headers=self._headers(token)).get_json()
        token = body["csrf_token"]

        for method, path, payload in (
            ("get", "/api/memory", None),
            ("get", "/api/conversations", None),
            ("post", "/api/settings", {"engine": "responses"}),
        ):
            with self.subTest(path=path):
                kwargs = {"headers": self._headers(token)}
                if payload is not None:
                    kwargs["json"] = payload
                resp = getattr(client, method)(path, **kwargs)
                self.assertEqual(resp.status_code, 423, resp.get_data(as_text=True))
                self.assertEqual(resp.get_json()["error"], "session_locked")

    def test_the_lock_screen_can_still_reach_what_it_needs(self):
        app = self._app()
        client = self._client(app)
        token = self._login(client)
        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user())):
            client.post("/api/auth/lock", headers=self._headers(token))
        self.assertEqual(client.get("/api/auth/status").status_code, 200)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertTrue(client.get("/api/auth/status").get_json()["locked"])

    def test_unlock_requires_the_password_again(self):
        app = self._app()
        client = self._client(app)
        token = self._login(client)
        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=True, user=_user())):
            token = client.post(
                "/api/auth/lock", headers=self._headers(token)
            ).get_json()["csrf_token"]

        with patch.object(SystemAuth, "authenticate", return_value=AuthResult(ok=False, error="Incorrect password.")):
            resp = client.post("/api/auth/unlock", json={"password": "wrong"}, headers=self._headers(token))
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(client.get("/api/auth/status").get_json()["locked"])

    def test_locking_without_a_session_is_refused(self):
        app = self._app()
        resp = self._client(app).post("/api/auth/lock")
        self.assertEqual(resp.status_code, 401)


# --------------------------------------------------------------------------
class LogoutRouteTests(AuthRouteTestCase):
    def test_logout_ends_the_session_server_side(self):
        app = self._app()
        client = self._client(app)
        token = self._login(client)
        self.assertTrue(client.get("/api/auth/status").get_json()["authenticated"])

        resp = client.post("/api/auth/logout", headers=self._headers(token))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(client.get("/api/auth/status").get_json()["authenticated"])
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_logout_without_the_token_is_refused(self):
        """Otherwise a cross-site POST could sign the operator out on demand."""
        app = self._app()
        client = self._client(app)
        self._login(client)
        resp = client.post("/api/auth/logout")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(client.get("/api/auth/status").get_json()["authenticated"])


# --------------------------------------------------------------------------
class ObjectAccessTests(AuthRouteTestCase):
    """IDOR: the session is the only source of identity, ever."""

    def test_another_users_conversation_is_not_found(self):
        app = self._app()
        conv_id = self.db.create_conversation("bob", "Bob's private thread")["id"]
        self.db.add_message(conv_id, "user", "a secret about the server")
        self.db.add_message(conv_id, "assistant", "here is the answer")

        client = self._client(app)
        token = self._login(client, "alice")

        with self.subTest(path="messages"):
            resp = client.get(f"/api/conversations/{conv_id}/messages")
            self.assertEqual(resp.status_code, 404, resp.get_data(as_text=True))
            self.assertNotIn("a secret about the server", resp.get_data(as_text=True))

        # A write that is refused must not have written anything either.
        with self.subTest(path="summarize"):
            resp = client.post(
                f"/api/conversations/{conv_id}/summarize",
                headers=self._headers(token),
            )
            self.assertEqual(resp.status_code, 404, resp.get_data(as_text=True))

    def test_a_conversation_id_cannot_be_hijacked_by_id(self):
        app = self._app()
        conv_id = self.db.create_conversation("bob", "Bob")["id"]
        self.db.add_message(conv_id, "user", "bob was here")

        client = self._client(app)
        token = self._login(client, "alice")
        # 404, never 403: a wrong owner must not even be distinguishable from a
        # conversation that does not exist. And the row must survive, or a single
        # guessed id would be a delete.
        self.assertEqual(
            client.get(f"/api/conversations/{conv_id}/messages").status_code, 404
        )
        self.assertEqual(
            client.delete(f"/api/conversations/{conv_id}", headers=self._headers(token)).status_code,
            404,
        )
        self.assertIsNotNone(self.db.get_conversation(conv_id))


# --------------------------------------------------------------------------
class DevAutoLoginTests(AuthRouteTestCase):
    """A dev convenience must never out-rank the sign-in screen.

    With DEV_AUTO_LOGIN set, `current_user()` re-authenticates any empty
    session as a fixed name. Since logout *is* an empty session, "sign out"
    silently undid itself, the lock screen asked PAM about the dev user instead
    of the person at the keyboard, and the sign-in screen was skipped entirely.
    """

    def test_dev_auto_login_is_ignored_while_system_login_is_on(self):
        with patch.object(config, "DEV_AUTO_LOGIN", "developer"), patch.object(
            config, "DEV_AUTO_LOGIN_WITH_SYSTEM_LOGIN", False
        ), patch.object(config, "SYSTEM_LOGIN_ENABLED", True):
            self.assertFalse(config.dev_auto_login)

    def test_signing_out_really_signs_out(self):
        app = self._app()
        client = self._client(app)
        token = self._login(client, "ioannisb")
        self.assertEqual(client.post("/api/auth/logout", headers=self._headers(token)).status_code, 200)
        # The regression: this used to come back 200 with a full user payload
        # because the very next request signed the dev user back in.
        self.assertEqual(client.get("/api/me").status_code, 401)
        status = client.get("/api/auth/status").get_json()
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["csrf_token"], "")

    def test_a_signed_out_session_cookie_cannot_be_replayed(self):
        """The sign-out that needed two clicks.

        Flask's session is a stateless signed cookie, so session.clear() only
        asks the browser to forget it. With SESSION_REFRESH_EACH_REQUEST on,
        any response another tab had in flight re-sent the cookie and the tab
        that just signed out was authenticated again on its next request. The
        cookie of a signed-out session must be refused outright.
        """
        app = self._app()
        client = self._client(app)
        token = self._login(client, "ioannisb")
        self.assertEqual(client.get("/api/me").status_code, 200)
        # Captured before signing out: the response deletes the cookie, which is
        # the whole point - deleting is not revoking.
        stale = client.get_cookie("apex_session")
        self.assertIsNotNone(stale)
        self.assertEqual(client.post("/api/auth/logout", headers=self._headers(token)).status_code, 200)
        client.set_cookie("apex_session", stale.value)
        self.assertEqual(
            client.get("/api/me").status_code, 401,
            "a signed-out session cookie was accepted",
        )

    def test_signing_out_does_not_disturb_another_session(self):
        app = self._app()
        one = self._client(app)
        token = self._login(one, "ioannisb")
        # A second tab: its own session, unaffected by the first tab signing out.
        two = self._client(app)
        token_two = self._login(two, "ioannisb")
        one.post("/api/auth/logout", headers=self._headers(token))
        self.assertEqual(one.get("/api/me").status_code, 401)
        self.assertEqual(two.get("/api/me").status_code, 200)

    def test_the_cookie_is_not_rewritten_on_every_response(self):
        """Rewriting it is the mechanism that resurrects a signed-out session."""
        self.assertFalse(config.SESSION_REFRESH_EACH_REQUEST)
        app = self._app()
        client = self._client(app)
        token = self._login(client, "ioannisb")
        first = client.get_cookie("apex_session").value
        client.get("/api/memory")
        self.assertEqual(client.get_cookie("apex_session").value, first)

    def test_the_user_list_never_offers_root(self):
        body = self._client(self._app()).get("/api/auth/system-users").get_json()
        self.assertTrue(body["users"])
        self.assertNotIn("root", [u["username"] for u in body["users"]])

    def test_the_lock_screen_asks_pam_about_the_signed_in_user(self):
        """Not the dev user, and not an empty session.

        The unlock path authenticates `session["user_id"]`, so whatever name the
        session carries is the one PAM is asked about. A stray auto-login name
        would make the correct password fail with "unknown user".
        """
        app = self._app()
        client = self._client(app)
        token = self._login(client, "ioannisb")
        seen = []

        def spy(self, username, password, address=""):
            seen.append(username)
            return AuthResult(ok=False, error="Incorrect password.")

        with patch.object(SystemAuth, "authenticate", spy):
            body = client.post(
                "/api/auth/lock", headers=self._headers(token)
            ).get_json()["csrf_token"]
            client.post(
                "/api/auth/unlock", json={"password": "whatever"},
                headers=self._headers(body),
            )
        self.assertEqual(seen, ["ioannisb"])


# --------------------------------------------------------------------------
class OAuthPolicyTests(AuthRouteTestCase):
    def test_oauth_is_not_a_way_around_the_system_account_list(self):
        app = self._app()
        client = self._client(app)
        with patch.object(config, "OPENAI_CLIENT_ID", "client-abc"), patch.object(
            config, "SYSTEM_LOGIN_ALLOW_OAUTH", False
        ):
            self.assertFalse(client.get("/api/auth/status").get_json()["oauth_available"])
            self.assertEqual(client.get("/api/oauth/start").status_code, 403)

    def test_oauth_can_be_turned_on_deliberately(self):
        app = self._app()
        client = self._client(app)
        with patch.object(config, "OPENAI_CLIENT_ID", "client-abc"), patch.object(
            config, "SYSTEM_LOGIN_ALLOW_OAUTH", True
        ):
            self.assertTrue(client.get("/api/auth/status").get_json()["oauth_available"])


if __name__ == "__main__":
    unittest.main()
