"""Authenticate APEX users against the host's own accounts.

APEX has no user database: who may use it is decided by the machine's
``/etc/passwd`` and ``/etc/shadow``, so one password policy governs the desktop
and the web UI and access follows the host's own account lifecycle (add a user
with ``useradd``, remove access with ``userdel``).

Two rules shaped this module:

* **The password is never seen by APEX.** ``/etc/shadow`` is ``root:shadow 0640``
  and the server does not run as root, so reading hashes is both impossible and
  wrong. Authentication goes through PAM (``pam_unix``), which is what
  ``login`` and ``sshd`` use. libpam is already loaded by every login-capable
  system, so this adds no dependency; the ctypes binding is the same call a C
  program makes.
* **A machine account is not a person.** Service accounts have ``nologin`` or
  ``false`` shells, or a UID below ``FIRST_UID`` with no real home. They exist
  to run something, not to log in, so they are refused even if a password would
  verify — the same judgement ``pam_shells``/``pam_nologin`` make.

Every attempt, successful or not, is appended to an audit log with the user name,
the source address and the outcome. The password is never written anywhere.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PASSWD_PATH = Path("/etc/passwd")

# UIDs below this are system accounts (daemon, bin, sys, sync, games, ...).
# They have no interactive shell by policy, so they must never reach the UI.
FIRST_UID = 1000
NOLOGIN_SHELLS = {"nologin", "false", "true", "logout", "sync", "shutdown", "halt", "stop"}

PAM_SUCCESS = 0
PAM_CONV_ERR = 19
PAM_AUTH_ERR = 7
PAM_CRED_INSUFFICIENT = 8
PAM_AUTHINFO_UNAVAIL = 9
PAM_USER_UNKNOWN = 10
PAM_MAXTRIES = 11
PAM_NEW_AUTHTOK_REQD = 12
PAM_AUTHTOK_EXPIRED = 13
PAM_PERM_DENIED = 6
PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2

# libpam is thread-safe per-transaction; a ctypes call needs the GIL released or
# a concurrent chat request stalls behind a password prompt.
_lib = None
_libc = None
_lib_lock = threading.Lock()
_lib_error: str = ""
_libc_error: str = ""


def _load_libpam() -> ctypes.CDLL | None:
    """Bind libpam once. Returns None (never raises) when it is unavailable."""
    global _lib, _lib_error
    with _lib_lock:
        if _lib is not None or _lib_error:
            return _lib
        name = ctypes.util.find_library("pam") or "libpam.so.0"
        try:
            lib = ctypes.CDLL(name)
            lib.pam_start.restype = ctypes.c_int
            lib.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_void_p, ctypes.c_void_p]
            lib.pam_authenticate.restype = ctypes.c_int
            lib.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.pam_acct_mgmt.restype = ctypes.c_int
            lib.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.pam_end.restype = ctypes.c_int
            lib.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.pam_strerror.restype = ctypes.c_char_p
            lib.pam_strerror.argtypes = [ctypes.c_void_p, ctypes.c_int]
            _lib = lib
        except OSError as exc:
            _lib_error = str(exc)
        return _lib


@dataclass(frozen=True)
class SystemUser:
    """A login-capable account from /etc/passwd. Never carries a hash."""
    username: str
    uid: int
    gid: int
    name: str
    home: str
    shell: str

    @property
    def display_name(self) -> str:
        return self.name or self.username

    @property
    def initials(self) -> str:
        words = [w for w in (self.name or self.username).split() if w]
        if not words:
            return "?"
        if len(words) == 1:
            return words[0][:2].upper()
        return (words[0][0] + words[-1][0]).upper()


def _parse_passwd_line(line: str) -> SystemUser | None:
    parts = line.split(":")
    if len(parts) < 7:
        return None
    name, _pw, uid, gid, gecos, home, shell = parts[:7]
    try:
        return SystemUser(username=name, uid=int(uid), gid=int(gid), name=gecos, home=home, shell=shell)
    except ValueError:
        return None


def passwd_users(path: Path | str = PASSWD_PATH) -> list[SystemUser]:
    """Every account in /etc/passwd, in file order."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    users = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        user = _parse_passwd_line(line)
        if user is not None:
            users.append(user)
    return users


def get_passwd_user(username: str, path: Path | str = PASSWD_PATH) -> SystemUser | None:
    """One account by exact name. Names are matched case-sensitively because
    that is how the system matches them; a case-insensitive login would let
    "ROOT" select a different account than "root" on some configurations."""
    username = (username or "").strip()
    if not username:
        return None
    for user in passwd_users(path):
        if user.username == username:
            return user
    return None


def is_login_capable(user: SystemUser | None, *, first_uid: int = FIRST_UID) -> bool:
    """False for service accounts, so they cannot be used to reach APEX.

    Shells are compared by their **basename**. Distributions disagree on where
    these live - nologin is /usr/sbin/nologin on Fedora and /sbin/nologin or
    /bin/nologin elsewhere, false is /bin/false or /usr/bin/false - so matching
    full paths silently lets service accounts through on the machines where the
    path differs, which is exactly where nobody would notice.
    """
    if user is None:
        return False
    if not user.shell.startswith("/"):
        return False
    if os.path.basename(user.shell) in NOLOGIN_SHELLS:
        return False
    # A root shell is legitimate for the machine owner; the UID floor only
    # applies to the non-root range.
    if user.uid != 0 and user.uid < first_uid:
        return False
    return True


def login_capable_users(path: Path | str = PASSWD_PATH) -> list[SystemUser]:
    """Accounts a human may sign in as, for the login screen's user list.

    root is left out of the list. APEX hands the signed-in user a PTY and
    arbitrary tool execution, so choosing root in a browser signs whoever is
    sitting at the keyboard into a root shell - and root sorts first, so it is
    the easiest row to hit by accident on a shared machine. is_login_capable
    still admits root, so this is a presentation rule about the picker, not
    access control; blocking the account itself is a separate decision.
    """
    return [u for u in passwd_users(path) if is_login_capable(u) and u.uid != 0]


class PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


PamConvFn = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(PamMessage)),
    ctypes.POINTER(ctypes.POINTER(PamResponse)),
    ctypes.c_void_p,
)


def _load_libc() -> ctypes.CDLL | None:
    """libc, for the two calls ctypes cannot do for us.

    The conversation response array must be allocated with malloc, because
    libpam frees it with free(). A ctypes-allocated array comes from Python's
    own allocator, so handing it over aborts the whole process with
    "free(): invalid pointer" the first time a password is checked.
    """
    global _libc, _libc_error
    with _lib_lock:
        if _libc is not None or _libc_error:
            return _libc
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.malloc.restype = ctypes.c_void_p
            libc.malloc.argtypes = [ctypes.c_size_t]
            libc.memset.restype = ctypes.c_void_p
            libc.memset.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
            _libc = libc
        except OSError as exc:
            _libc_error = str(exc)
        return _libc


def _make_conversation(password: bytes, libc: ctypes.CDLL):
    """Build the libpam conversation that answers its prompt with the password.

    libpam asks for a value through a callback rather than as an argument, so
    this is where the password is handed over.

    Ownership is the whole subtlety here. libpam's `_pam_drop_reply()` frees
    the response *array* and then each `resp` *string* with `free()`. Anything
    allocated by Python would therefore be freed by C and again by Python's
    garbage collector, which aborts the process with "invalid pointer". So both
    the array and the password copy are malloc'd, and ownership is handed over
    completely - never referenced from a ctypes field, which would keep a
    second owner alive.

    Only PAM_PROMPT_ECHO_OFF/ON is answered. A prompt asking for anything else
    is refused rather than guessed at, so this handler can never be talked into
    acting as a password oracle for some other account.
    """
    holder: dict = {}
    nbytes = len(password) + 1

    def conv(num_msg, msg, resp, _appdata):
        if num_msg <= 0:
            return 0
        if not resp:
            return PAM_CONV_ERR
        size = ctypes.sizeof(PamResponse) * num_msg
        buf = libc.malloc(size)
        if not buf:
            return PAM_CONV_ERR
        libc.memset(buf, 0, size)
        responses = ctypes.cast(buf, ctypes.POINTER(PamResponse))
        for i in range(num_msg):
            try:
                style = msg[i].contents.msg_style
            except (ValueError, AttributeError):
                responses[i].resp_retcode = PAM_CONV_ERR
                continue
            if style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON):
                strbuf = libc.malloc(nbytes)
                if not strbuf:
                    responses[i].resp_retcode = PAM_CONV_ERR
                    continue
                libc.memset(strbuf, 0, nbytes)
                ctypes.memmove(strbuf, password, len(password))
                # A c_char_p *value*, not Python bytes: ctypes stores the
                # address without copying it and without taking ownership, so
                # exactly one owner (PAM) remains.
                responses[i].resp = ctypes.cast(strbuf, ctypes.c_char_p)
                responses[i].resp_retcode = 0
            else:
                responses[i].resp_retcode = PAM_CONV_ERR
        # PAM takes ownership of buf (and of every string above) and frees it.
        resp[0] = responses
        return 0

    callback = PamConvFn(conv)
    holder["callback"] = callback
    return callback, holder


@dataclass
class AuthResult:


    ok: bool
    username: str = ""
    user: SystemUser | None = None
    error: str = ""
    locked: bool = False


def _pam_authenticate(service: str, username: str, password: str) -> tuple[bool, str]:
    """One pam_start / pam_authenticate / pam_acct_mgmt / pam_end cycle.

    The password is passed to libpam in a byte buffer, answered through the
    conversation callback and never stored, logged or kept afterwards. A wrong
    password comes back as PAM_AUTH_ERR; a conversation or configuration
    problem comes back as its own code, so a misconfiguration is never reported
    to the user as "wrong password".
    """
    lib = _load_libpam()
    libc = _load_libc()
    if lib is None or libc is None:
        return False, "system authentication is unavailable on this server"

    password_bytes = password.encode("utf-8")
    callback, holder = _make_conversation(password_bytes, libc)

    class PamConv(ctypes.Structure):
        _fields_ = [("conv", ctypes.c_void_p), ("pam_user", ctypes.c_void_p),
                    ("pam_password", ctypes.c_void_p)]

    conv = PamConv(ctypes.cast(callback, ctypes.c_void_p), None, None)
    handle = ctypes.c_void_p()
    rc = lib.pam_start(service.encode("utf-8"), username.encode("utf-8"),
                       ctypes.byref(conv), ctypes.byref(handle))
    if rc != 0 or not handle:
        return False, "could not start system authentication"

    try:
        rc = lib.pam_authenticate(handle, 0)
        if rc == PAM_SUCCESS:
            # The password may be expired or the account otherwise restricted.
            acct = lib.pam_acct_mgmt(handle, 0)
            if acct == PAM_SUCCESS:
                return True, ""
            if acct == PAM_NEW_AUTHTOK_REQD:
                return False, "this account must change its password before signing in"
            return False, "this account is not allowed to log in right now"
        if rc in (PAM_AUTH_ERR, PAM_USER_UNKNOWN, PAM_MAXTRIES):
            return False, "Incorrect username or password."
        if rc == PAM_PERM_DENIED:
            return False, "system authentication refused this login"
        if rc == PAM_AUTHTOK_EXPIRED:
            return False, "this account must change its password before signing in"
        if rc == PAM_CRED_INSUFFICIENT:
            return False, "system authentication could not verify this account"
        return False, "system authentication is not configured correctly"
    finally:
        try:
            lib.pam_end(handle, 0)
        except Exception:
            pass
        # Drop the last reference to the password bytes.
        holder.clear()
        password_bytes = b""


@dataclass
class RateLimiter:
    """Per-user, per-address throttle for password attempts.

    Keyed on both, so guessing against one account from many sources is still
    bounded, and a locked-out user cannot be used to lock out others.
    """
    max_attempts: int = 5
    window_seconds: int = 300
    lockout_seconds: int = 300
    _fails: dict[str, list[float]] = field(default_factory=dict)
    _locked: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _key(self, username: str, address: str) -> str:
        return f"{username}\x00{address}"

    def is_locked(self, username: str, address: str) -> bool:
        key = self._key(username, address)
        now = time.time()
        with self._lock:
            until = self._locked.get(key)
            if until and until > now:
                return True
            if until:
                self._locked.pop(key, None)
                self._fails.pop(key, None)
            return False

    def retry_after(self, username: str, address: str) -> int:
        key = self._key(username, address)
        with self._lock:
            until = self._locked.get(key, 0)
            return max(0, int(until - time.time()))

    def record_failure(self, username: str, address: str) -> None:
        key = self._key(username, address)
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(key, []) if now - t < self.window_seconds]
            fails.append(now)
            self._fails[key] = fails
            if len(fails) >= self.max_attempts:
                self._locked[key] = now + self.lockout_seconds
                self._fails.pop(key, None)

    def record_success(self, username: str, address: str) -> None:
        key = self._key(username, address)
        with self._lock:
            self._fails.pop(key, None)
            self._locked.pop(key, None)


class SystemAuth:
    """Login against the host's accounts, with throttling and an audit trail."""

    def __init__(self, *, service: str = "login", max_attempts: int = 5,
                 window_seconds: int = 300, lockout_seconds: int = 300,
                 passwd_path: Path | str = PASSWD_PATH,
                 audit_path: Path | str | None = None,
                 allowed_groups: tuple[str, ...] = ()) -> None:
        self.service = service
        self.passwd_path = Path(passwd_path)
        self.audit_path = Path(audit_path) if audit_path else None
        self.allowed_groups = tuple(allowed_groups)
        self.limiter = RateLimiter(max_attempts=max_attempts, window_seconds=window_seconds,
                                   lockout_seconds=lockout_seconds)

    # -- audit ------------------------------------------------------------
    def _audit(self, username: str, address: str, outcome: str, detail: str = "") -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} user={username or '-'} from={address or '-'} outcome={outcome}"
        if detail:
            line += f" detail={detail[:200]}"
        try:
            print(f"[auth] {line}", flush=True)
        except Exception:
            pass
        if not self.audit_path:
            return
        try:
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._audit_broken = False
        except OSError as exc:
            # Announced once, then kept quiet so a broken path cannot turn every
            # sign-in attempt into a log flood. It used to be a bare `pass`,
            # which meant a mistyped path (or a directory the service cannot
            # write) silently discarded the only durable record of who tried to
            # sign in. The stdout copy above still reaches the journal, so the
            # record is not lost - but nobody was told the file was not.
            if not getattr(self, "_audit_broken", False):
                self._audit_broken = True
                print(
                    f"[auth] WARNING: cannot write the auth audit log to "
                    f"{self.audit_path} ({exc.__class__.__name__}: {exc}); "
                    "sign-in attempts are only in the journal from now on",
                    flush=True,
                )

    # -- lookup -----------------------------------------------------------
    def lookup(self, username: str) -> SystemUser | None:
        return get_passwd_user(username, self.passwd_path)

    def list_users(self) -> list[SystemUser]:
        return login_capable_users(self.passwd_path)

    def _group_allowed(self, user: SystemUser) -> bool:
        """When SYSTEM_LOGIN_GROUPS is set, only those primary groups may sign in."""
        if not self.allowed_groups:
            return True
        try:
            import grp

            allowed = {g.gr_gid for g in grp.getgrall() if g.gr_name in self.allowed_groups}
        except Exception:
            return True  # cannot check; do not lock the operator out of their own box
        if not allowed:
            # Every configured name is unknown to this machine, so this allowlist
            # can never match. Refusing here looks identical to a wrong password
            # and to "the machine is broken" - the real cause is one env var away
            # and otherwise invisible, so it is announced rather than swallowed.
            print(
                "[auth] WARNING: SYSTEM_LOGIN_GROUPS names no group on this machine "
                f"({', '.join(self.allowed_groups)}); no account can sign in",
                flush=True,
            )
            return False
        return user.gid in allowed

    def retry_after(self, username: str, address: str) -> int:
        return self.limiter.retry_after(username, address)

    # -- the actual check --------------------------------------------------
    def authenticate(self, username: str, password: str, address: str = "") -> AuthResult:
        username = (username or "").strip()
        address = address or "-"
        if not username or not password:
            self._audit(username, address, "missing-credentials")
            return AuthResult(False, error="Enter your username and password.")
        if self.limiter.is_locked(username, address):
            wait = self.limiter.retry_after(username, address)
            self._audit(username, address, "throttled", f"retry_after={wait}")
            return AuthResult(False, locked=True, error=f"Too many attempts. Try again in {wait}s.")

        user = self.lookup(username)
        if user is None:
            # PAM would answer the same way; do not leak which names exist.
            self.limiter.record_failure(username, address)
            self._audit(username, address, "unknown-user")
            return AuthResult(False, error="Incorrect username or password.")
        if not is_login_capable(user):
            self.limiter.record_failure(username, address)
            self._audit(username, address, "not-login-capable", f"shell={user.shell}")
            return AuthResult(False, error="This account cannot sign in to APEX.")
        if not self._group_allowed(user):
            self.limiter.record_failure(username, address)
            self._audit(username, address, "group-not-allowed", f"gid={user.gid}")
            return AuthResult(False, error="This account cannot sign in to APEX.")

        ok, error = _pam_authenticate(self.service, username, password)
        if not ok:
            self.limiter.record_failure(username, address)
            self._audit(username, address, "bad-password", error)
            return AuthResult(False, error=error or "Incorrect username or password.")

        self.limiter.record_success(username, address)
        self._audit(username, address, "ok")
        return AuthResult(True, username=user.username, user=user)
