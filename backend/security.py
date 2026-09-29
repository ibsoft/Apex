"""Shared input validation, output encoding and request-integrity helpers.

Everything that crosses a trust boundary in APEX passes through here:

* **Field sanitization** - every string that arrives from a client is
  length-capped, control characters are stripped and the result is trimmed
  before it is used. Capping matters more than filtering: an unvalidated field
  is a denial-of-service vector, and a regex that tries to "remove bad
  characters" always misses something.
* **Output encoding** - `text_for_html` is what goes into an HTML context.
  Callers that build markup by hand must use it; callers that build React
  elements do not need to, because React escapes children for them.
* **CSRF** - APEX authenticates with a session cookie, so a state-changing
  request from another origin would otherwise be indistinguishable from the
  user's own. `csrf_protect` requires a token that only same-origin code can
  read, which defeats the request entirely.
"""
from __future__ import annotations

import hmac
import re
import secrets
import unicodedata

# Long enough for any real field, short enough that a 10MB "title" cannot be
# smuggled through a form field that gets stored and later re-rendered.
MAX_FIELD = 4096
MAX_SHORT_FIELD = 256
MAX_USERNAME = 64

# Control characters (C0) and DEL, plus the Unicode line/paragraph separators.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029]")


def clean_text(value: object, *, max_length: int = MAX_FIELD, allow_newlines: bool = True) -> str:
    """Normalize one untrusted text field.

    Strips control characters, applies NFC so visually identical strings
    compare equal, and caps the length. Returns "" for None.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if not allow_newlines:
        value = value.replace("\r", " ").replace("\n", " ")
    value = _CONTROL.sub("", value)
    value = unicodedata.normalize("NFC", value)
    value = value.strip()
    if len(value) > max_length:
        value = value[:max_length]
    return value


def clean_name(value: object, *, max_length: int = MAX_SHORT_FIELD) -> str:
    """A single-line identifier: name, title, label, filename stem."""
    return clean_text(value, max_length=max_length, allow_newlines=False)


def clean_username(value: object) -> str:
    """A system user name, or "" when the input is not one.

    Unlike the other cleaners this never truncates. A username is an identifier
    that must match /etc/passwd exactly, so quietly shortening a bad value would
    be worse than refusing it: "user\\x00evil" would become "user" and sign in
    as a different account than the caller asked for. Anything that is not
    already a valid name is rejected outright.

    The permitted shape is the POSIX portable username: a letter or underscore,
    then letters, digits, underscore, dot or dash, optionally with one trailing
    "$" for machine accounts. That is deliberately stricter than Linux itself,
    because these values are also written into audit lines and compared against
    PAM, and a narrower accepted set means less to get wrong downstream.
    """
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > MAX_USERNAME:
        return ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?", name):
        return ""
    return name


def valid_username(value: object) -> bool:
    return bool(clean_username(value))


def clean_path_component(value: object) -> str:
    """One path segment. Strips separators and traversal, so a caller can join
    the result onto a root and rely on the containment check."""
    part = clean_name(value, max_length=MAX_SHORT_FIELD)
    part = part.replace("\\", "").replace("/", "")
    part = part.replace("\x00", "")
    if part in {"", ".", ".."}:
        return ""
    return part


def text_for_html(value: object) -> str:
    """Encode a value for interpolation into HTML text or a quoted attribute."""
    import html as _html

    return _html.escape("" if value is None else str(value), quote=True)


def is_safe_url(value: object, *, schemes: tuple[str, ...] = ("http", "https")) -> bool:
    """True for URLs whose scheme is explicitly allowed.

    Used for anything that ends up in an href/src. ``javascript:`` and ``data:``
    are rejected because they turn a stored string into script execution.
    """
    url = clean_text(value, max_length=2048, allow_newlines=False)
    if not url:
        return False
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", url)
    if not match:
        # Relative URLs have no scheme and cannot execute.
        return not url.lower().startswith("javascript:") and not url.lower().startswith("data:")
    return match.group(1).lower() in schemes


# --- CSRF -----------------------------------------------------------------
# The token is derived from the session secret and the session id, so it is
# stable for a session and unforgeable without the server's SECRET_KEY.
CSRF_HEADER = "X-APEX-CSRF"


def csrf_token(secret: str, session_id: str) -> str:
    import hashlib

    if not secret or not session_id:
        return ""
    mac = hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def csrf_protect(secret: str, session_id: str, header_value: str | None,
                 *, unsafe: bool) -> bool:
    """True when the request may proceed.

    Safe methods pass. Unsafe methods need a header equal to the expected token,
    compared in constant time.
    """
    if not unsafe:
        return True
    expected = csrf_token(secret, session_id)
    if not expected or not header_value:
        return False
    return hmac.compare_digest(expected, header_value)
