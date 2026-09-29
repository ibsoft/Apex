"""Security tests: system-user auth, input sanitization, CSRF, CORS, IDOR.

The PAM call is never exercised against a real account - it needs a real
password, and a test that depended on one would be a test that could lock
somebody out. `_pam_authenticate` is patched instead, and the one place that
does talk to libpam (see TestPamPlumbing) only ever supplies a wrong password,
which proves the conversation callback works without needing to know a
credential.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import security
import system_auth
from system_auth import (AuthResult, RateLimiter, SystemAuth, SystemUser,
                          get_passwd_user, is_login_capable, login_capable_users,
                          passwd_users)

PASSWD_FIXTURE = """\
# a comment that must be ignored
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
snap_daemon:x:2:2::/var/lib/snapd:/usr/bin/false
www-data:x:33:33:www-data:/var/www:/usr/sbin/nologin
alice:x:1000:1000:Alice Example:/home/alice:/bin/bash
bob:x:1001:1001:Bob:/home/bob:/bin/zsh
carol:x:1002:1002:Carol,,,:/home/carol:/bin/false
malformed-line
dave:x:notanumber:1003:Dave:/home/dave:/bin/bash
eve:x:1004:1004:Eve:/home/eve:/bin/bash
"""


def write_passwd(tmp: Path, text: str = PASSWD_FIXTURE) -> Path:
    path = tmp / "passwd"
    path.write_text(text, encoding="utf-8")
    return path


# --- /etc/passwd parsing --------------------------------------------------
class TestPasswdParsing(unittest.TestCase):
    def setUp(self):
        self._tmp = self._useFixture = None
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_passwd(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_parses_fields(self):
        alice = get_passwd_user("alice", self.path)
        self.assertEqual(alice.username, "alice")
        self.assertEqual(alice.uid, 1000)
        self.assertEqual(alice.gid, 1000)
        self.assertEqual(alice.name, "Alice Example")
        self.assertEqual(alice.home, "/home/alice")
        self.assertEqual(alice.shell, "/bin/bash")

    def test_skips_comments_and_malformed_lines(self):
        names = [u.username for u in passwd_users(self.path)]
        self.assertNotIn("# a comment that must be ignored", names)
        self.assertNotIn("malformed-line", names)
        # A non-numeric uid must not abort the whole file.
        self.assertNotIn("dave", names)
        self.assertIn("eve", names)

    def test_lookup_is_case_sensitive(self):
        # A case-insensitive match could let "ROOT" select a different account
        # than the one the operator meant on a case-insensitive PAM config.
        self.assertIsNone(get_passwd_user("ALICE", self.path))
        self.assertIsNotNone(get_passwd_user("alice", self.path))

    def test_initials(self):
        self.assertEqual(get_passwd_user("alice", self.path).initials, "AE")
        self.assertEqual(get_passwd_user("bob", self.path).initials, "BO")

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(passwd_users("/nonexistent/passwd"), [])


# --- who may sign in ------------------------------------------------------
class TestLoginCapable(unittest.TestCase):
    def test_rejects_nologin_and_false_by_basename(self):
        # Full-path matching would miss /usr/bin/false and /sbin/nologin.
        for shell in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/nologin",
                      "/bin/false", "/usr/bin/false", "/usr/local/bin/false",
                      "/sbin/false"):
            with self.subTest(shell=shell):
                user = SystemUser("svc", 0, 0, "", "/", shell)
                self.assertFalse(is_login_capable(user))

    def test_rejects_below_uid_floor(self):
        self.assertFalse(is_login_capable(SystemUser("x", 500, 500, "", "/home/x", "/bin/bash")))

    def test_allows_root_with_real_shell(self):
        # Authentication still admits root; only the picker leaves it out.
        self.assertTrue(is_login_capable(SystemUser("root", 0, 0, "", "/root", "/bin/bash")))

    def test_rejects_relative_shell(self):
        self.assertFalse(is_login_capable(SystemUser("x", 1000, 1000, "", "/home/x", "bash")))

    def test_list_excludes_service_accounts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_passwd(Path(tmp))
            names = [u.username for u in login_capable_users(path)]
        self.assertEqual(names, ["alice", "bob", "eve"])

    def test_list_never_offers_root(self):
        """A root row is one click from a root shell in the terminal.

        is_login_capable still admits root, so the picker is the only thing
        standing between the first row of the sign-in screen and uid 0.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_passwd(Path(tmp))
            self.assertIn("root", [u.username for u in passwd_users(path)])
            names = [u.username for u in login_capable_users(path)]
        self.assertNotIn("root", names)


# --- throttle -------------------------------------------------------------
class TestRateLimiter(unittest.TestCase):
    def test_locks_after_max_attempts(self):
        rl = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        for _ in range(2):
            rl.record_failure("alice", "1.2.3.4")
            self.assertFalse(rl.is_locked("alice", "1.2.3.4"))
        rl.record_failure("alice", "1.2.3.4")
        self.assertTrue(rl.is_locked("alice", "1.2.3.4"))

    def test_lockout_is_per_user_and_per_address(self):
        rl = RateLimiter(max_attempts=1, window_seconds=60, lockout_seconds=60)
        rl.record_failure("alice", "1.1.1.1")
        self.assertTrue(rl.is_locked("alice", "1.1.1.1"))
        # A different account from the same host is not collateral damage.
        self.assertFalse(rl.is_locked("bob", "1.1.1.1"))
        # Nor the same account from another host.
        self.assertFalse(rl.is_locked("alice", "2.2.2.2"))

    def test_success_clears_history(self):
        rl = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
        rl.record_failure("alice", "1.1.1.1")
        rl.record_success("alice", "1.1.1.1")
        self.assertEqual(rl._fails, {})

    def test_expired_lockout_expires(self):
        rl = RateLimiter(max_attempts=1, window_seconds=60, lockout_seconds=0)
        rl.record_failure("alice", "1.1.1.1")
        self.assertFalse(rl.is_locked("alice", "1.1.1.1"))


# --- SystemAuth -----------------------------------------------------------
class TestSystemAuth(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Path(self.tmp.name) / "audit.log"
        self.path = write_passwd(Path(self.tmp.name))
        self.auth = SystemAuth(passwd_path=self.path, audit_path=self.audit,
                               max_attempts=3, window_seconds=60, lockout_seconds=60)

    def audit_text(self) -> str:
        return self.audit.read_text(encoding="utf-8") if self.audit.exists() else ""

    def test_successful_login_returns_user(self):
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(True, "")) as pam:
            result = self.auth.authenticate("alice", "correct horse", "1.1.1.1")
        self.assertTrue(result.ok)
        self.assertEqual(result.username, "alice")
        self.assertEqual(result.user.name, "Alice Example")
        pam.assert_called_once()

    def test_wrong_password_rejected(self):
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(False, "Incorrect username or password.")):
            result = self.auth.authenticate("alice", "nope", "1.1.1.1")
        self.assertFalse(result.ok)
        self.assertNotIn("alice", result.error.lower() + " ")  # no name echo

    def test_unknown_user_looks_like_wrong_password(self):
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(False, "Incorrect username or password.")) as pam:
            unknown = self.auth.authenticate("ghost", "nope", "1.1.1.1")
            wrong = self.auth.authenticate("alice", "nope", "2.2.2.2")
        # Identical wording, so the login page cannot be used to enumerate
        # accounts; and PAM is never consulted for a name that does not exist.
        self.assertEqual(unknown.error, wrong.error)
        self.assertEqual(pam.call_count, 1)

    def test_service_account_rejected_before_pam(self):
        with mock.patch.object(system_auth, "_pam_authenticate") as pam:
            result = self.auth.authenticate("www-data", "x", "1.1.1.1")
        pam.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("cannot sign in", result.error.lower())

    def test_throttle_locks_and_reports_retry_after(self):
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(False, "Incorrect username or password.")):
            for _ in range(3):
                self.auth.authenticate("alice", "bad", "1.1.1.1")
            blocked = self.auth.authenticate("alice", "bad", "1.1.1.1")
        self.assertTrue(blocked.locked)
        self.assertGreater(blocked.error.count("Try again"), 0)
        self.assertGreater(self.auth.retry_after("alice", "1.1.1.1"), 0)

    def test_password_never_appears_in_audit(self):
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(True, "")):
            self.auth.authenticate("alice", "sup3r-s3cret-passphrase", "1.1.1.1")
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(False, "Incorrect username or password.")):
            self.auth.authenticate("alice", "sup3r-s3cret-passphrase", "1.1.1.1")
        self.assertNotIn("sup3r-s3cret-passphrase", self.audit_text())
        self.assertIn("outcome=ok", self.audit_text())
        self.assertIn("outcome=bad-password", self.audit_text())

    def test_empty_credentials_rejected(self):
        result = self.auth.authenticate("", "", "1.1.1.1")
        self.assertFalse(result.ok)

    def test_unwritable_audit_log_is_announced(self):
        """A security log that silently vanishes is worse than no log.

        The failure used to be a bare `pass`, so a mistyped path or a directory
        the service cannot write discarded every attempt without a word.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_passwd(Path(tmp))
            auth = SystemAuth(
                passwd_path=path,
                audit_path=Path(tmp) / "no" / "such" / "dir" / "audit.log",
                allowed_groups=(),
            )
            with mock.patch.object(system_auth, "_pam_authenticate",
                                   return_value=(False, "nope")):
                with mock.patch("builtins.print") as out:
                    auth.authenticate("alice", "x", "1.1.1.1")
            printed = "\n".join(str(c) for c in out.call_args_list)
        self.assertIn("cannot write the auth audit log", printed)
        # The path has to be named, or the operator cannot tell which env var is wrong.
        self.assertIn("no/such/dir/audit.log", printed)

    def test_audit_warning_is_not_repeated_for_every_attempt(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_passwd(Path(tmp))
            auth = SystemAuth(passwd_path=path,
                              audit_path=Path(tmp) / "missing" / "audit.log")
            with mock.patch.object(system_auth, "_pam_authenticate",
                                   return_value=(False, "nope")):
                with mock.patch("builtins.print") as out:
                    for _ in range(5):
                        auth.authenticate("alice", "x", "1.1.1.1")
            printed = "\n".join(str(c) for c in out.call_args_list)
        self.assertEqual(printed.count("cannot write the auth audit log"), 1)

    def test_group_allowlist_matching_nothing_is_announced(self):
        """`SYSTEM_LOGIN_GROUPS=users,admin` on a box without either group
        refused every account with the same message as a wrong password."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_passwd(Path(tmp))
            auth = SystemAuth(passwd_path=path, audit_path=Path(tmp) / "audit.log",
                              allowed_groups=("no-such-group-a", "no-such-group-b"))
            with mock.patch.object(system_auth, "_pam_authenticate") as pam:
                with mock.patch("builtins.print") as out:
                    result = auth.authenticate("alice", "x", "1.1.1.1")
            printed = "\n".join(str(c) for c in out.call_args_list)
        pam.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("names no group on this machine", printed)
        self.assertIn("no-such-group-a", printed)

    def test_group_restriction(self):
        import grp

        # root exists everywhere and alice's gid is not 0.
        auth = SystemAuth(passwd_path=self.path, audit_path=self.audit,
                          allowed_groups=(grp.getgrgid(0).gr_name,))
        with mock.patch.object(system_auth, "_pam_authenticate") as pam:
            result = auth.authenticate("alice", "x", "1.1.1.1")
        pam.assert_not_called()
        self.assertFalse(result.ok)

    def test_audit_log_records_source_address(self):
        with mock.patch.object(system_auth, "_pam_authenticate",
                               return_value=(False, "Incorrect username or password.")):
            self.auth.authenticate("alice", "bad", "203.0.113.9")
        self.assertIn("from=203.0.113.9", self.audit_text())


# --- real libpam, wrong password only -------------------------------------
class TestPamPlumbing(unittest.TestCase):
    """Proves the ctypes conversation works, without needing a password.

    A wrong password must come back as PAM_AUTH_ERR (7) and the process must
    survive. The bugs this guards against - libpam free()ing Python-allocated
    memory, and a conversation callback that never delivers the password - both
    crash or misreport here, and both are invisible to a mocked test.
    """

    def test_wrong_password_returns_auth_error_without_crashing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_passwd(Path(tmp))
            user = login_capable_users(path)[-1]
        ok, err = system_auth._pam_authenticate("login", user.username, "not-the-password")
        self.assertFalse(ok)
        self.assertIn("incorrect", err.lower())

    def test_repeated_failures_do_not_leak_state(self):
        for _ in range(3):
            ok, _err = system_auth._pam_authenticate("login", "root", "not-the-password")
            self.assertFalse(ok)


# --- field sanitization ---------------------------------------------------
class TestSanitizers(unittest.TestCase):
    def test_clean_text_strips_control_characters(self):
        self.assertEqual(security.clean_text("a\x00b\x07c"), "abc")
        # Unicode line/paragraph separators are removed as well, so a field
        # cannot forge a second line in an audit log or a rendered list.
        self.assertEqual(security.clean_text("a\u2028b\u2029c"), "abc")
        self.assertNotIn("\n", security.clean_text("a\rb\nc", allow_newlines=False))

    def test_clean_text_caps_length(self):
        self.assertEqual(len(security.clean_text("x" * 99999)), security.MAX_FIELD)
        self.assertEqual(len(security.clean_name("x" * 99999)), security.MAX_SHORT_FIELD)

    def test_clean_name_is_single_line(self):
        self.assertEqual(security.clean_name("a\nb\rc"), "a b c")

    def test_clean_username_rejects_unsafe_shapes(self):
        for bad in ("", "  ", "a b", "a/b", "../root", "a;b", "user\x00",
                    "x" * 100, "a\nb", "-leading", "1leading", "a$b$c"):
            with self.subTest(value=bad):
                self.assertEqual(security.clean_username(bad), "")
        self.assertEqual(security.clean_username("ioannisb"), "ioannisb")
        self.assertEqual(security.clean_username("  ioannisb  "), "ioannisb")
        self.assertEqual(security.clean_username("svc_x-1.y$"), "svc_x-1.y$")

    def test_clean_path_component_strips_traversal(self):
        for bad in ("..", ".", "", "/", "a/b", "a\\b", "a\x00b", "../etc/passwd"):
            with self.subTest(value=bad):
                self.assertNotIn("/", security.clean_path_component(bad))
                self.assertNotIn("\\", security.clean_path_component(bad))
                self.assertNotEqual(security.clean_path_component(bad), "..")
        self.assertEqual(security.clean_path_component("report.pdf"), "report.pdf")

    def test_text_for_html_escapes_quotes(self):
        out = security.text_for_html('a"b\'c<d>e&f')
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)
        self.assertIn("&quot;", out)
        self.assertIn("&amp;", out)

    def test_is_safe_url_blocks_script_schemes(self):
        for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>",
                    "vbscript:msgbox", "  javascript:alert(1)"):
            with self.subTest(url=bad):
                self.assertFalse(security.is_safe_url(bad))
        self.assertTrue(security.is_safe_url("https://example.com"))
        self.assertTrue(security.is_safe_url("http://example.com"))
        self.assertTrue(security.is_safe_url("/api/files/download/abc"))


# --- CSRF -----------------------------------------------------------------
class TestCsrf(unittest.TestCase):
    def test_token_is_stable_for_a_session(self):
        a = security.csrf_token("secret", "sid-1")
        b = security.csrf_token("secret", "sid-1")
        self.assertEqual(a, b)
        self.assertTrue(a)

    def test_token_changes_with_session_and_secret(self):
        self.assertNotEqual(security.csrf_token("secret", "sid-1"),
                            security.csrf_token("secret", "sid-2"))
        self.assertNotEqual(security.csrf_token("secret", "sid-1"),
                            security.csrf_token("other", "sid-1"))

    def test_no_token_without_secret_or_sid(self):
        self.assertEqual(security.csrf_token("", "sid"), "")
        self.assertEqual(security.csrf_token("secret", ""), "")

    def test_safe_methods_always_pass(self):
        for method in security.SAFE_METHODS:
            self.assertTrue(security.csrf_protect("s", "sid", None, unsafe=False))
        self.assertFalse(security.csrf_protect("s", "sid", None, unsafe=True))
        self.assertFalse(security.csrf_protect("s", "sid", "wrong", unsafe=True))
        self.assertTrue(security.csrf_protect("s", "sid",
                                             security.csrf_token("s", "sid"), unsafe=True))

    def test_new_session_ids_are_unique(self):
        ids = {security.new_session_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)


if __name__ == "__main__":
    unittest.main()
