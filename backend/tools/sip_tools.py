"""SIP skill: place an outbound phone call and hold a two-way voice conversation.

Transport
---------
``baresip`` is the SIP user agent. It runs as a long-lived child process driven
over stdin, because a conversation needs the *same* call to survive across
several spoken turns and ``baresip -e`` only runs commands at startup.

Injecting a new utterance into a call that is already up is the hard part, and
stock ``baresip`` cannot do it. ``audio_source`` is read once when the audio
layer starts, and the ``/ausrc`` and ``/auplay`` commands swap *modules*, not
the parameter a module was started with. A file handed to ``aufile`` is
therefore fixed for the lifetime of the process, which caps a bare baresip at
one announcement per call and is why the obvious port of a dial-a-message
script cannot become a conversation.

So the microphone is not a file. Each call gets a *pair* of PulseAudio null
sinks:

    <call>_rx        baresip audio_player  - audio arriving from the phone
    <call>_tx        APEX plays utterances in here
    <call>_tx.monitor  baresip audio_source - audio leaving to the phone

Two sinks rather than one, deliberately. With a single sink the monitor would
carry back everything baresip had just played, and the caller would hear their
own voice echoed at them for the length of the call. Splitting the directions
means each monitor sees only one side: ``<call>_tx.monitor`` carries APEX's
voice and nothing else, so ``parec`` records the far end without picking up
what APEX is saying.

Gating
------
Dialling is consequential and is not the model's decision, so ``action=plan``
is the default and the only thing a bare request can reach: it validates the
account, normalises the destination and prints a redacted plan without opening
a socket. ``action=call`` additionally requires ``confirm=true`` in the same
request, so a call cannot happen by omission.

Secrets
-------
SIP_PASSWORD lives in the settings DB and is mirrored into the env file. It is
never echoed: everything headed for the model or the log passes through
``redact`` first, and the plan shows a masked account line. baresip prints its
whole account line on startup, which is exactly why that helper exists.
"""
from __future__ import annotations

import json
import os
import pty
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tty
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from tools.base import Tool
from tools.envfile import upsert_env

REQUIRED_FIELDS = ("sip_server", "sip_user", "sip_password")
ALLOWED_TRANSPORTS = ("udp", "tcp", "tls")
ALLOWED_TTS_ENGINES = ("espeak", "edge")

#: Settings-DB key -> env var. The settings tab writes the DB row and
#: ``write_env_file`` mirrors the same values into the env file, so a
#: shell-started backend and a systemd-started one agree on the account.
ENV_KEYS = {
    "sip_enabled": "SIP_ENABLED",
    "sip_server": "SIP_SERVER",
    "sip_user": "SIP_USER",
    "sip_password": "SIP_PASSWORD",
    "sip_transport": "SIP_TRANSPORT",
    "sip_port": "SIP_PORT",
    "sip_display_name": "SIP_DISPLAY_NAME",
    "sip_domain": "SIP_DOMAIN",
    "sip_outbound_proxy": "SIP_OUTBOUND_PROXY",
    "sip_notify_to": "SIP_NOTIFY_TO",
    "sip_tts_engine": "SIP_TTS_ENGINE",
    "sip_tts_voice": "SIP_TTS_VOICE",
}

#: Characters a SIP destination can legitimately be built from. The check runs
#: even on input that already looks like a URI: the destination goes to baresip
#: on stdin, and baresip tokenises commands on spaces, so an unfiltered value
#: could append a second command to the same line.
_DEST_ALLOWED = re.compile(r"[+0-9*#,A-Za-z._@-]{1,120}")

#: A complete `sip:`/`sips:` URI the caller already has. Only the scheme, the
#: user part, an optional host/port and optional parameters - no whitespace, so
#: it cannot carry a second command.
_SIP_URI_RE = re.compile(
    r"sips?:[0-9A-Za-z._~%!$&'()*+,;=@-]+(?::[0-9]+)?(?:;[0-9A-Za-z._~%!$&'()*+,;=:@-]*)?",
    re.IGNORECASE,
)

#: A number the way people actually write and dictate it. Speech recognition
#: and habits both insert spaces, dashes and brackets ("+30 6977 456030"), and a
#: model asked to reformat a number the operator already gave is a worse answer
#: than dialling it. Every one of these characters is a visual separator: none of
#: them can appear in a real SIP destination, so removing them cannot make a
#: string *more* dangerous, and the result is re-validated by ``_DEST_ALLOWED``
#: anyway.
_NUMERIC_SEPARATORS = re.compile(r"[\s()\[\]\u2010-\u2015.\-]+")

#: After stripping separators a destination must still be a bare number. This is
#: the belt to the allowlist's braces: the strip above never *introduces* a
#: character, but a destination like "1234 ext 567" or "555 /dial" must survive
#: as-is into the allowlist check, which rejects it, rather than being silently
#: rewritten into something that dialled.
_BARE_NUMBER_RE = re.compile(r"\+?[0-9]{3,20}")

#: baresip's own rejection wording. Verified against the binary: a refused
#: INVITE closes the session with one of these, and waiting for the ring timeout
#: instead turns a wrong number into a minute of silence.
_REJECTED_RE = re.compile(
    r"(session closed|not found|forbidden|unauthorized|proxy authentication|"
    r"temporarily unavailable|no common audio codecs|rejected)",
    re.IGNORECASE,
)

#: One call at a time. A second would need a second baresip and a second sink
#: pair, and the failure mode of getting that wrong is two people hearing each
#: other's conversation.
MAX_CALLS = 1

#: How long to wait for the far end to pick up before giving up. Distinct from
#: the call duration: this is the ring time.
RING_TIMEOUT_SECONDS = 60

_SESSIONS: dict[str, "SipSession"] = {}
_SESSION_LOCK = threading.Lock()
_SWEEPER: threading.Thread | None = None


# --------------------------------------------------------------------------
# the pending plan
# --------------------------------------------------------------------------
# A call is a two-turn exchange, and the second turn is the dangerous one. The
# model plans, shows the plan, and the operator answers "yes" - a message that
# names no skill, no tool and no destination. Routing is per-turn, so that reply
# otherwise lands in the general skill, which has no `sip_call`, and the model
# truthfully reports that it cannot place phone calls while a plan to do exactly
# that sits in the previous message. Recording the destination here is what lets
# the next turn be routed back to the SIP skill.
#
# Keyed by user and conversation: two people on two tabs each get their own
# pending plan, and neither can confirm the other's.
_PENDING: dict[tuple[str, str], dict] = {}
_PENDING_LOCK = threading.Lock()

#: How long a plan stays confirmable. Long enough to read a plan and answer it,
#: short enough that a plan quoted an hour later is not dialled on a stale
#: "yes" from a different conversation entirely.
PENDING_TTL_SECONDS = 300

#: What counts as agreeing to the plan. Anchored to the whole utterance, and
#: deliberately short: these are read as answers to the question that was just
#: asked. A refusal clears the plan instead, so a declined call cannot be
#: revived by a later, unrelated "ok" (which is what would happen if only the
#: positive side were recognised).
_CONFIRM_RE = re.compile(
    r"^\s*(y|yes|yeah|yep|sure|ok|okay|kk|k|"
    r"ναι|οκ|χμ|μμμ|"
    r"yes please|go ahead|go on|do it|make it so|proceed|confirm|confirmed|"
    r"προχωρα|καν το|καν' το|κανε το|τι θελεις να κανω|"
    r"dial it|call it|ring me|επιβεβαιωσε)\b[\s!.]*$",
    re.IGNORECASE,
)

#: Greek letters that render identically to Latin ones, folded to Latin before
#: the confirmation words are matched. A Greek omicron next to a Latin "K" is
#: "ΟK" rather than "OK", and it is what a Greek keyboard or a recogniser that
#: transcribed "ok" in the operator's own language produces. Same trap the voice
#: command normaliser documents for the final sigma.
#:
#: Only omicron is folded. Folding the other lookalikes would be actively wrong:
#: nu→v turns the Greek "ναι" (yes) into "vαι", and rho→p turns "προχωρα" into
#: a misspelling, so both would *lose* confirmations rather than gain them.
#: Each candidate is matched in both spellings, so folding cannot cost the
#: Greek words that contain an omicron ("οκ", "οχι").
_GREEK_LOOKALIKE = str.maketrans({"ο": "o", "Ο": "O"})


def _fold_reply(text: str) -> tuple[str, str]:
    """The two spellings a short confirmation must be matched in.

    Accents are stripped, and Greek lookalikes are folded to Latin, so the
    confirmation words can be written once, unaccented, in the patterns above.
    Matching the accented and unaccented forms by hand does not scale: the same
    word ends up spelled two ways in one alternation and only the other spelling
    matches, so "κάν το" silently stops working while "οκ" keeps working.

    Both the folded *and* the unfolded spelling are returned. Folding is only a
    net gain if it is additive: "ΟK" needs folding to become "OK", but Greek
    "οχι" becomes "oxi" under the same fold and only the unfolded spelling
    recognises it.
    """
    import unicodedata

    raw = str(text or "")
    try:
        unaccented = "".join(
            c for c in unicodedata.normalize("NFD", raw)
            if unicodedata.category(c) != "Mn"
        )
    except (TypeError, ValueError):
        unaccented = raw
    folded = unaccented.translate(_GREEK_LOOKALIKE)
    return (unaccented, folded) if folded != unaccented else (unaccented,)

_DECLINE_RE = re.compile(
    r"^\s*(n|no|nope|nah|don'?t|do not|stop|cancel|"
    r"οχι|μην|ασε|σταματα|ακυρο|ακυρω)\b[\s!.]*$",
    re.IGNORECASE,
)


def _pending_key(ctx) -> tuple[str, str] | None:
    user = str(getattr(ctx, "user_id", "") or "")
    if not user:
        return None
    return (user, str(getattr(ctx, "conversation_id", "") or ""))


def _key_of(user_id, conversation_id) -> tuple[str, str] | None:
    user = str(user_id or "")
    if not user:
        return None
    return (user, str(conversation_id or ""))


def _sweep_pending(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for key, entry in list(_PENDING.items()):
        if now - float(entry.get("at") or 0) > PENDING_TTL_SECONDS:
            _PENDING.pop(key, None)


def note_pending(ctx, cfg: dict, dest: str) -> None:
    """Remember a planned destination so the confirmation turn can find it."""
    key = _pending_key(ctx)
    if not key:
        return
    with _PENDING_LOCK:
        _sweep_pending()
        _PENDING[key] = {"dest": str(dest or ""), "at": time.time(),
                         "server": cfg.get("sip_server", "")}


def pending_plan(user_id, conversation_id) -> dict | None:
    """The plan awaiting confirmation for this conversation, if any."""
    key = _key_of(user_id, conversation_id)
    if not key:
        return None
    with _PENDING_LOCK:
        _sweep_pending()
        return _PENDING.get(key)


def clear_pending(key_or_ctx) -> None:
    key = (_pending_key(key_or_ctx) if not isinstance(key_or_ctx, tuple)
           else (str(key_or_ctx[0] or ""), str(key_or_ctx[1] or "")))
    if not key[0]:
        return
    with _PENDING_LOCK:
        _PENDING.pop(key, None)


def pending_reply(text: str, user_id, conversation_id) -> str | None:
    """Whether ``text`` answers a pending plan, and how.

    ``"confirm"`` means the operator agreed and the SIP skill should handle the
    turn; ``"decline"`` means the plan is dropped; ``None`` means the message is
    not about a pending plan at all and should be routed normally.

    Takes the two ids rather than a ``ToolContext``: this runs in the chat route
    while deciding which skill handles the turn, before any tool context is
    built.
    """
    key = _key_of(user_id, conversation_id)
    if not key:
        return None
    with _PENDING_LOCK:
        _sweep_pending()
        if _PENDING.get(key) is None:
            return None
    spellings = _fold_reply(str(text or "").strip())
    if any(_DECLINE_RE.fullmatch(s) for s in spellings):
        clear_pending(key)
        return "decline"
    if any(_CONFIRM_RE.fullmatch(s) for s in spellings):
        return "confirm"
    # Anything else is a new topic, not an answer to the plan. Drop it, so the
    # plan cannot be dialled by a later stray "ok".
    clear_pending(key)
    return None


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def effective_settings(config, runtime=None) -> dict:
    """Settings-DB overrides merged over env defaults.

    Transport is normalised rather than trusted, because the env file is
    editable by hand: an operator who writes SIP_TRANSPORT=sctp should get a
    working call on udp and a clear reading of the field, not a baresip config
    that silently never registers.
    """
    if runtime is None:
        from db import get_db
        runtime = get_db().all_settings()

    def pick(key: str, attr: str, default: str = "") -> str:
        if key in runtime and runtime[key] is not None:
            return str(runtime[key])
        return str(getattr(config, attr, "") or default)

    transport = pick("sip_transport", "SIP_TRANSPORT", "udp").strip().lower()
    if transport not in ALLOWED_TRANSPORTS:
        transport = "udp"
    port = pick("sip_port", "SIP_PORT").strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        port = ""

    cfg = {
        "sip_enabled": _truthy(runtime.get("sip_enabled", getattr(config, "SIP_ENABLED", False))),
        "sip_server": pick("sip_server", "SIP_SERVER").strip(),
        "sip_user": pick("sip_user", "SIP_USER").strip(),
        "sip_password": str(runtime.get("sip_password") or getattr(config, "SIP_PASSWORD", "") or ""),
        "sip_transport": transport,
        "sip_port": port,
        "sip_display_name": pick("sip_display_name", "SIP_DISPLAY_NAME", "APEX").strip() or "APEX",
        "sip_domain": pick("sip_domain", "SIP_DOMAIN").strip(),
        "sip_outbound_proxy": pick("sip_outbound_proxy", "SIP_OUTBOUND_PROXY").strip(),
        "sip_notify_to": pick("sip_notify_to", "SIP_NOTIFY_TO").strip(),
        "sip_tts_engine": pick("sip_tts_engine", "SIP_TTS_ENGINE", "espeak").strip().lower(),
        "sip_tts_voice": pick("sip_tts_voice", "SIP_TTS_VOICE").strip(),
        "response_language": str(runtime.get(
            "response_language", getattr(config, "RESPONSE_LANGUAGE", "en")
        ) or "en").strip().lower(),
    }
    if cfg["sip_tts_engine"] not in ALLOWED_TTS_ENGINES:
        cfg["sip_tts_engine"] = "espeak"
    # An empty domain means "the server", which is what almost every hosted
    # account wants and what a PBX behind a hostname wants too.
    cfg["sip_domain"] = cfg["sip_domain"] or cfg["sip_server"]
    cfg["configured"] = all(cfg[k] for k in REQUIRED_FIELDS)
    cfg["missing"] = [k for k in REQUIRED_FIELDS if not cfg[k]]
    return cfg


def env_file_path(config) -> Path:
    fallback = Path(__file__).resolve().parents[1] / ".env"
    return Path(getattr(config, "SIP_ENV_FILE", "") or fallback)


def write_env_file(cfg: dict, env_path: Path) -> Path:
    """Upsert the SIP_* lines of ``env_path`` and leave every other line alone.

    The three-state semantics (replace / untouched / delete-when-empty) live in
    ``tools.envfile.upsert_env`` so the skill creator's ``set_env`` cannot
    invent a different meaning for "remove this key". Only SIP_* keys derived
    from ``cfg`` are ever passed through from here.
    """
    wanted = {ENV_KEYS[k]: str(cfg.get(k) or "") for k in ENV_KEYS if k in cfg}
    return upsert_env(env_path, wanted)


def mirror_to_env(config) -> tuple[bool, str]:
    """Write the effective SIP settings into the env file.

    The *stored* settings rows, not the merged values: the env file mirrors the
    Settings tab, and merging would write every documented default into it on
    first save and pin them there forever. It also means a cleared password
    really disappears - mirroring the merged value would read the password back
    out of the very file it was just removed from and put it straight back.

    Best-effort by design: the settings row is already saved and is what the
    tool actually reads, so a read-only or full env file must not turn a
    successful save into an error. The failure is returned rather than raised so
    the caller can log it - the symptom otherwise shows up much later as "the
    helper script still has the old number".

    Returns ``(ok, error)``.
    """
    try:
        from db import get_db

        stored = {k: v for k, v in get_db().all_settings().items() if k in ENV_KEYS}
        write_env_file(stored, env_file_path(config))
        return True, ""
    except OSError as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------
def redact(text: str, cfg: dict | None = None) -> str:
    """Strip the SIP password from anything headed for the model or the log."""
    out = str(text)
    password = str((cfg or {}).get("sip_password") or "")
    if password:
        out = out.replace(password, "***")
    # Independent of the value: baresip echoes the whole account line at
    # startup, so the parameter has to be masked even when the secret was
    # already substituted elsewhere.
    return re.sub(r"(auth_pass\s*=\s*)\S+", r"\1***", out)


# --------------------------------------------------------------------------
# the call plan
# --------------------------------------------------------------------------
def normalize_destination(dest: str, cfg: dict) -> str:
    """Turn an extension, E.164 number or SIP URI into something dialable."""
    dest = str(dest or "").strip()
    if not dest:
        raise ValueError("No destination given. Ask the operator for a number or extension.")
    # A complete URI is passed through, so this is checked before the plain
    # allowlist: "sip:" carries a colon, which that allowlist rightly refuses.
    # Whitespace is still excluded by the character class, so a URI cannot smuggle
    # a second baresip command onto the same stdin line.
    if _SIP_URI_RE.fullmatch(dest):
        return dest
    # A number dictated or written by hand arrives grouped: "+30 6977 456030",
    # "697-745-6030", "(697) 745-6030". Strip the visual separators first, so the
    # operator is not asked to reformat a number they already gave. Only a string
    # that is *entirely* number characters is rewritten; anything with other
    # characters ("555 ext 9", "555 /dial") is left intact so the allowlist below
    # rejects it rather than this quietly turning it into a dialable number.
    squeezed = _NUMERIC_SEPARATORS.sub("", dest)
    if squeezed != dest and _BARE_NUMBER_RE.fullmatch(squeezed):
        dest = squeezed
    if not _DEST_ALLOWED.fullmatch(dest):
        raise ValueError(
            "Destination contains characters that are not part of a number, extension or "
            "SIP URI. Allowed: digits, + * # , letters, . _ - @"
        )
    return f"sip:{dest}@{cfg['sip_domain'] or cfg['sip_server']}"


def account_line(cfg: dict) -> str:
    """The single ``accounts`` line baresip registers with.

    Only PCMA/PCMU are offered. Anything else depends on a codec the far end
    may not have, and a call that negotiates opus when the other side only
    speaks G.711 connects, shows as established, and carries silence.
    """
    hostport = f"{cfg['sip_domain']}:{cfg['sip_port']}" if cfg["sip_port"] else cfg["sip_domain"]
    params = [
        f"auth_user={cfg['sip_user']}",
        f"auth_pass={cfg['sip_password']}",
        "audio_codecs=PCMA,PCMU",
        "regint=60",
    ]
    if cfg.get("sip_outbound_proxy"):
        params.append(f'outbound="{cfg["sip_outbound_proxy"]}"')
    return (
        f'"{cfg["sip_display_name"]}" <sip:{cfg["sip_user"]}@{hostport};'
        f'transport={cfg["sip_transport"]}>;' + ";".join(params)
    )


def build_plan(cfg: dict, dest: str, text: str = "", duration: int = 60) -> dict:
    """Everything a dry run reports, and nothing that touches the network."""
    return {
        "mode": "dry-run",
        "dialled": False,
        "to": normalize_destination(dest, cfg),
        "server": cfg["sip_server"],
        "user": cfg["sip_user"],
        "transport": cfg["sip_transport"],
        "port": int(cfg["sip_port"]) if cfg["sip_port"] else None,
        "domain": cfg["sip_domain"],
        "display_name": cfg["sip_display_name"],
        "outbound_proxy": cfg["sip_outbound_proxy"] or None,
        "account": redact(account_line(cfg), cfg),
        "speak": (text or "").strip()[:2000] or None,
        "duration_seconds": duration,
        "missing_on_host": missing_binaries(),
    }


# --------------------------------------------------------------------------
# host dependencies
# --------------------------------------------------------------------------
def missing_binaries() -> list[str]:
    """What has to be installed before a call can connect.

    Reported rather than assumed. A plan that hides a missing baresip is worse
    than no plan, because the operator approves it in good faith.
    """
    missing = []
    if not shutil.which("baresip"):
        missing.append("baresip (sudo apt-get install -y baresip)")
    return missing


def tts_command() -> list[str] | None:
    """espeak-ng preferred, espeak accepted; both take the same ``-w file`` form.

    espeak-ng is the maintained fork and the one with usable non-English voices,
    but plenty of hosts only carry espeak 1.48, so demanding a reinstall would
    exclude them for no gain.
    """
    for name in ("espeak-ng", "espeak"):
        path = shutil.which(name)
        if path:
            return [path]
    return None


def edge_tts_command() -> str | None:
    path = shutil.which("edge-tts")
    if path:
        return path
    venv_script = Path(sys.executable).parent / "edge-tts"
    return str(venv_script) if venv_script.is_file() else None


def tts_engine_name(config, cfg: dict | None = None) -> str:
    """Return the selected engine, or the local fallback if Edge is absent."""
    cfg = cfg or effective_settings(config)
    if cfg["sip_tts_engine"] == "edge":
        if edge_tts_command():
            return "edge-tts"
        local = tts_command()
        return f"{Path(local[0]).name} (Edge TTS unavailable)" if local else "none"
    return Path((tts_command() or ["none"])[0]).name


def _module_path() -> str:
    """Where baresip keeps its modules.

    Debian ships them in /usr/lib/baresip/modules and a source install in
    /usr/local/lib/baresip/modules. Guessing wrong makes every ``module`` line
    a load error, the account never registers, and the call fails with a SIP
    timeout that looks like the server is down.
    """
    for candidate in ("/usr/lib/baresip/modules", "/usr/local/lib/baresip/modules"):
        if Path(candidate).is_dir():
            return candidate
    return "/usr/lib/baresip/modules"


# --------------------------------------------------------------------------
# the PulseAudio loopback
# --------------------------------------------------------------------------
def pulse_env() -> dict:
    """Environment that reaches the operator's PulseAudio/PipeWire session.

    A systemd service has no ``XDG_RUNTIME_DIR`` even when it runs as the right
    account, so ``pactl`` would hunt for the socket in /run and fail. Deriving
    it from the process uid is right for the shipped unit; when that directory
    is absent nothing is faked, and ``pulse_ready`` turns the absence into an
    actionable sentence.
    """
    env = dict(os.environ)
    uid = os.getuid() if hasattr(os, "getuid") else os.geteuid()
    runtime_dir = f"/run/user/{uid}"
    if Path(runtime_dir).is_dir():
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return env


def pulse_ready() -> tuple[bool, str]:
    if not shutil.which("pactl"):
        return False, "pactl is not installed (package: pulseaudio-utils)."
    env = pulse_env()
    if not env.get("XDG_RUNTIME_DIR"):
        return False, (
            f"No XDG_RUNTIME_DIR for uid {os.geteuid()}, so the PulseAudio socket cannot "
            "be found. Run the backend as the same account as the operator's desktop session."
        )
    probe = subprocess.run([shutil.which("pactl"), "info"], env=env,
                           capture_output=True, text=True, timeout=20)
    if probe.returncode:
        return False, (
            "Cannot reach the PulseAudio/PipeWire session for this user. The backend "
            "service must run as the same account that owns the operator's desktop audio."
        )
    return True, ""


def _pactl(*args: str, timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(["pactl", *args], env=pulse_env(),
                          capture_output=True, text=True, timeout=timeout)


def _wait_for_device(name: str, module: int, deadline: float) -> bool:
    """Wait for a sink and its monitor to both appear in the graph.

    They show up asynchronously. Dialling into a monitor that does not exist
    yet yields a call with no audio in either direction and no error message
    at all, which is the worst possible failure to debug from a SIP log.
    """
    while time.time() < deadline:
        sinks = _pactl("list", "short", "sinks").stdout
        sources = _pactl("list", "short", "sources").stdout
        if name in sinks and f"{name}.monitor" in sources:
            return True
        time.sleep(0.2)
    _pactl("unload-module", str(module))
    return False


def _create_one_sink(name: str, description: str) -> int:
    load = _pactl(
        "load-module", "module-null-sink", f"sink_name={name}",
        f"sink_properties=device.description={description}",
    )
    if load.returncode or not load.stdout.strip().isdigit():
        raise RuntimeError(
            f"Could not create the '{name}' audio loopback: "
            + (load.stderr or load.stdout).strip()
        )
    return int(load.stdout.strip())


def create_null_sinks(prefix: str) -> tuple[list[int], str, str]:
    """Create the rx/tx null-sink pair for one call.

    Two sinks, not one: a monitor belongs to its sink, so a single sink used
    both as baresip's speaker and as APEX's playback device would carry the
    phone's own audio straight back out to the phone. Splitting the directions
    means ``<tx>.monitor`` sees APEX's voice and nothing else.

    Both are torn down on failure, because a half-built pair leaves a
    half-open loopback in the operator's audio graph.
    """
    rx, tx = f"{prefix}_rx", f"{prefix}_tx"
    modules: list[int] = []
    for name, description in ((rx, "APEX\\ SIP\\ receive"), (tx, "APEX\\ SIP\\ transmit")):
        module = _create_one_sink(name, description)
        modules.append(module)
        if not _wait_for_device(name, module, time.time() + 10):
            drop_null_sinks(modules)
            raise RuntimeError(f"The '{name}' audio loopback did not come up within 10 seconds.")
    return modules, rx, tx


def drop_null_sinks(modules: list[int]) -> None:
    for module in modules:
        if module:
            try:
                _pactl("unload-module", str(module))
            except Exception:
                pass


# --------------------------------------------------------------------------
# speech out (eSpeak or Edge TTS) and speech back (faster-whisper)
# --------------------------------------------------------------------------
def synthesize(
    text: str,
    out_wav: Path,
    language: str = "",
    voice: str = "",
    engine: str = "espeak",
) -> Path:
    engine = str(engine or "espeak").strip().lower()
    if engine not in ALLOWED_TTS_ENGINES:
        raise ValueError(f"Unsupported SIP TTS engine: {engine}")
    if engine == "edge":
        edge = edge_tts_command()
        if edge:
            output = Path(out_wav).with_suffix(".mp3")
            args = [edge, "--text", str(text)]
            if voice:
                args += ["--voice", voice]
            args += ["--write-media", str(output)]
            proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
            if proc.returncode or not output.exists() or output.stat().st_size <= 44:
                raise ValueError("Edge TTS failed: " + (proc.stderr or proc.stdout).strip())
            return output
        if not tts_command():
            raise ValueError(
                "Edge TTS is selected but edge-tts is not installed, and no eSpeak fallback is available."
            )
        engine = "espeak"
        language = ""
        voice = ""

    cmd = tts_command()
    if not cmd:
        raise ValueError(
            "No text-to-speech engine is installed. Install espeak-ng "
            "(sudo apt-get install -y espeak-ng) to speak into calls."
        )
    out_wav = Path(out_wav)
    args = list(cmd)
    if language:
        args += ["-v", language]
    if voice:
        args += ["-v", voice]
    proc = subprocess.run(args + ["-w", str(out_wav), str(text)],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode or not out_wav.exists() or out_wav.stat().st_size <= 44:
        raise ValueError("Speech synthesis failed: " + (proc.stderr or proc.stdout).strip())
    return out_wav


def edge_voice_for_language(configured_voice: str, language: str) -> str:
    if configured_voice:
        return configured_voice
    if str(language).strip().lower().startswith("el"):
        return "el-GR-AthinaNeural"
    return ""


def resolve_call_destination(cfg: dict, destination: str) -> str:
    if str(destination or "").strip().lower() in {
        "me", "myself", "call me", "my phone", "operator", "the operator",
        "user", "the user", "owner", "the owner",
    }:
        target = str(cfg.get("sip_notify_to") or "").strip()
        if not target:
            raise ValueError(
                "No call-me number is configured. Set it in SIP settings before scheduling calls to yourself."
            )
        return target
    return str(destination or "").strip()


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise ValueError("ffmpeg is not installed, so call audio cannot be converted.")
    return path


def to_wav(src: Path, dst: Path, rate: int, trim_silence: bool = False) -> Path:
    """Resample to mono signed-16 at ``rate``.

    8000 Hz mono is what G.711 wants; 16000 mono is what faster-whisper wants,
    so the two directions are deliberately not the same file.

    ``trim_silence`` strips the long quiet a phone line always carries at both
    ends, which otherwise costs whisper accuracy rather than just latency: it
    spends its budget on silence and is likelier to emit something for it.
    """
    dst = Path(dst)
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if trim_silence:
        cmd += ["-af", (
            "silenceremove=start_periods=1:start_silence=0.35:start_threshold=-45dB:detection=peak,"
            "areverse,"
            "silenceremove=start_periods=1:start_silence=0.35:start_threshold=-45dB:detection=peak,"
            "areverse"
        )]
    cmd += ["-ac", "1", "-ar", str(rate), "-sample_fmt", "s16", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode or not dst.exists() or dst.stat().st_size <= 44:
        raise ValueError("Audio conversion failed: " + (proc.stderr or "").strip())
    return dst


def play_into_sink(wav: Path, sink: str) -> None:
    """Play an utterance where the call's microphone can hear it.

    Blocking on purpose. The next action in a conversation is to listen, and
    overlapping the two would put the agent's own voice into the transcript it
    is about to ask for.
    """
    if not shutil.which("paplay"):
        raise ValueError("paplay is not installed (package: pulseaudio-utils).")
    proc = subprocess.run(["paplay", "--device", sink, str(wav)], env=pulse_env(),
                          capture_output=True, text=True, timeout=300)
    if proc.returncode:
        raise ValueError("Could not play audio into the call: " + (proc.stderr or "").strip())


def record_from_sink(wav: Path, sink: str, seconds: float,
                     stop_event: threading.Event | None = None) -> Path | None:
    """Capture ``seconds`` of what the far end sent into a real WAV file.

    Recording the operator's own audio graph rather than relying on baresip's
    ``sndfile`` dumps is what makes per-turn boundaries exact: a dump file is
    named after the call, not after the turn, so the model would otherwise have
    to guess where one reply ended and the next began.

    Returns ``None`` when nothing at all arrived, which is a normal result of
    asking a question someone is still thinking about and must not be reported
    as a failure.
    """
    if not shutil.which("parec"):
        raise ValueError("parec is not installed (package: pulseaudio-utils).")
    proc = subprocess.Popen(
        ["parec", "--device", f"{sink}.monitor", "--rate", "16000",
         "--channels", "1", "--format", "s16le"],
        env=pulse_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + max(0.5, float(seconds))
    chunks: list[bytes] = []
    try:
        while time.time() < deadline and proc.poll() is None:
            # Sampling once per 0.1 s keeps the loop responsive to the deadline
            # without spinning a core for a twenty-second wait.
            time.sleep(0.1)
            # Somebody hanging up ends the turn immediately. Waiting out the
            # full window would answer the model "heard nothing" for a call
            # that no longer exists, which reads as the person not answering
            # rather than as the person having gone.
            if stop_event is not None and stop_event.is_set():
                break
            chunk = proc.stdout.read1(65536) if proc.stdout else b""
            if chunk:
                chunks.append(chunk)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.stdout:
            proc.stdout.close()

    pcm = b"".join(chunks)
    if not pcm:
        return None
    Path(wav).write_bytes(_wav_container(pcm, rate=16000, channels=1))
    return Path(wav)


def _wav_container(pcm: bytes, rate: int, channels: int) -> bytes:
    """Wrap raw little-endian PCM in a canonical 44-byte RIFF header."""
    bits = 16
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVEfmt "
    header += (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
    header += channels.to_bytes(2, "little") + rate.to_bytes(4, "little")
    header += byte_rate.to_bytes(4, "little") + block_align.to_bytes(2, "little")
    header += bits.to_bytes(2, "little") + b"data" + len(pcm).to_bytes(4, "little")
    return header + pcm


def transcribe(audio: Path, config, model: str = "", language: str = "", timeout: int = 900) -> str:
    """Transcribe captured call audio with faster-whisper.

    faster-whisper pulls torch, which does not belong in the backend's own
    dependency set, so it runs from a separate virtualenv via a helper that
    imports nothing from APEX. An absent interpreter is reported as a sentence
    the model can relay, not as an exception: the caller spoke, and failing to
    hear them is a degradation, not a reason to lose the call.
    """
    if not audio.exists() or audio.stat().st_size <= 44:
        # Silence is not speech. Checked before the setup guard so a caller that
        # recorded nothing gets nothing back rather than a setup complaint, and
        # so the model is never told the far end said something.
        return ""
    raw = str(getattr(config, "SIP_WHISPER_PYTHON", "") or "").strip()
    # `Path("")` is `.`, which exists - so an unset interpreter would sail past
    # the existence check and get executed as a directory.
    interpreter = Path(raw) if raw else None
    helper = Path(__file__).resolve().parent / "sip_whisper.py"
    if interpreter is None or not interpreter.exists():
        return "(Speech recognition is not set up, so this reply could not be read back. " \
               "Install it with: python3 -m venv .venv/sip-whisper " \
               "&& .venv/sip-whisper/bin/pip install faster-whisper)"
    if not helper.exists():
        return "(The speech recognition helper is missing from this installation.)"

    cmd = [str(interpreter), str(helper), str(audio),
           "--model", model or str(getattr(config, "SIP_WHISPER_MODEL", "tiny") or "tiny")]
    if language:
        cmd += ["--language", language]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "(Transcription timed out; the recording was longer than expected.)"
    if proc.returncode:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "(Could not transcribe the reply: " + (lines[-1] if lines else "unknown error") + ")"
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------
# baresip configuration
# --------------------------------------------------------------------------
def _transport_modules(module_dir: str, transport: str) -> list[str]:
    """The module that carries ``transport``, if this host has one.

    baresip registers a transport per loaded module, and an account naming a
    transport nothing can carry fails to register with "Destination address
    required" - a message that points at the network rather than at a missing
    ``tls.so``. Reporting this from the status is the difference between "check
    your firewall" and "install baresip's TLS module".
    """
    if transport not in ("tls", "sips"):
        return []
    for name in ("tls.so", "gnutls.so"):
        if Path(module_dir, name).exists():
            return [name]
    return []


def available_transports(module_dir: str = "") -> list[str]:
    """Transports this host can actually carry.

    udp and tcp are in baresip's core; tls needs a module that is frequently
    absent from a distribution package.
    """
    module_dir = module_dir or _module_path()
    out = ["udp", "tcp"]
    for name in ("tls.so", "gnutls.so"):
        if Path(module_dir, name).exists():
            out.append("tls")
            break
    return out


def unsupported_transport(cfg: dict) -> str:
    """Why this account cannot be dialled on this host, or "" if it can.

    Worth its own check: an account that names a transport no loaded module
    carries fails to register with "Destination address required", which reads
    like a network fault and sends the operator to look at their firewall.
    """
    transport = str(cfg.get("sip_transport") or "udp")
    if transport in available_transports():
        return ""
    return (f"This host has no baresip transport module for {transport.upper()}, so the "
            f"account would never register (baresip reports 'Destination address required'). "
            f"Install the baresip TLS module, or set the transport to "
            f"{' or '.join(available_transports())} in the Settings tab.")


def write_baresip_config(cfg: dict, config_dir: Path, rx_sink: str, tx_sink: str) -> None:
    """Generate a per-call baresip config.

    No sound device of its own and no ``aufile``: the microphone is the
    transmit monitor and the speaker is the receive sink, so this file only has
    to get SIP signalling and G.711 right.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    accounts = config_dir / "accounts"
    accounts.write_text(account_line(cfg) + "\n", encoding="utf-8")
    os.chmod(accounts, 0o600)

    module_dir = _module_path()
    # Order is load-bearing, twice over:
    #   * g711/pulse register the audio codecs, and account.so parses the
    #     `accounts` file the moment it loads. With account.so first, the
    #     account binds no codec and the call dies with "no common audio
    #     codecs" - or worse, an established call carrying silence;
    #   * the menu is loaded as `module_app` and *not* also as `module`. Both
    #     lines try to load it, the second one reports "module already loaded /
    #     Operation already in progress", and the app never starts - which means
    #     /dial is not registered at all.
    modules = ["stdio.so", "g711.so", "pulse.so", "account.so", "contact.so"]
    modules += _transport_modules(module_dir, cfg["sip_transport"])
    lines = [
        # select, not epoll: this box's container refuses epoll_ctl on stdin,
        # and the difference is a few hundred calls an hour at most.
        "poll_method\tselect",
        f"module_path\t{module_dir}",
        "call_local_timeout\t60",
        "call_max_calls\t1",
        f"audio_player\tpulse,{rx_sink}",
        f"audio_source\tpulse,{tx_sink}.monitor",
        "audio_level\tno",
        "ausrc_format\ts16",
        "auplay_format\ts16",
        "auenc_format\ts16",
        "audec_format\ts16",
        "rtcp_mux\tno",
    ]
    lines += [f"module\t{m}" for m in modules if Path(module_dir, m).exists()]
    lines += ["module_app\tmenu.so"]
    (config_dir / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(config_dir / "config", 0o600)
    (config_dir / "contacts").write_text("", encoding="utf-8")


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------
@dataclass
class SipSession:
    id: str
    user_id: str
    destination: str
    workdir: Path
    rx_sink: str
    tx_sink: str
    modules: list[int]
    proc: subprocess.Popen
    master: int = -1
    started_at: float = 0.0
    deadline: float = 0.0
    duration: int = 0
    output: list[str] = field(default_factory=list)
    #: Set when the far end puts the call down. baresip is a long-lived process
    #: that survives the call, so ``proc.poll()`` cannot see a hangup and the
    #: session would otherwise look live until its deadline - every later tool
    #: call would report a call that no longer exists as still up.
    closed: threading.Event = field(default_factory=threading.Event)
    close_reason: str = ""
    #: When the call was answered. A "session closed" line before this point is a
    #: failed INVITE (a 404, a 407, a refused codec) and is handled as a
    #: rejection, not as somebody hanging up on us.
    established_at: float = 0.0
    #: Where the log stood when /dial went out. Read from here rather than from
    #: "now": a phone that answers inside the 2s registration settle has already
    #: written "Call established" by the time the reader starts, and a reader
    #: that began at the current end would miss it and wait out the ring timeout
    #: on a call that is up.
    dial_mark: int = 0
    transcript: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, line: str) -> None:
        with self.lock:
            self.output.append(str(line))
            # Bounded: a long call logs every RTP timeout, and an unbounded list
            # in a long-lived worker is a slow leak.
            if len(self.output) > 2000:
                del self.output[:1000]

    def tail(self, limit: int = 4000) -> str:
        with self.lock:
            return "\n".join(self.output)[-limit:]

    def expired(self) -> bool:
        return (
            time.time() > self.deadline
            or self.proc.poll() is not None
            or self.closed.is_set()
        )

    def end_reason(self) -> str:
        """Why the call is over, in words the model can pass on."""
        if self.close_reason:
            return self.close_reason
        if self.proc.poll() is not None:
            return "baresip exited."
        if time.time() > self.deadline:
            return f"The call reached its {int(self.duration)}s time limit."
        return "The call ended."

    def remaining(self) -> int:
        return max(0, int(self.deadline - time.time()))


def _all_sessions() -> list[SipSession]:
    with _SESSION_LOCK:
        return list(_SESSIONS.values())


def sessions_for(user_id: str) -> list[SipSession]:
    with _SESSION_LOCK:
        return [s for s in _SESSIONS.values() if s.user_id == user_id]


def lookup_session(user_id: str, session_id: str) -> SipSession | None:
    """Sessions are addressed by id *and* owner.

    The owner check is the whole point: a session id is a bearer reference to a
    live phone call, so answering someone else's would put one operator's
    conversation in another operator's transcript. Unknown ids and other
    people's ids are both "no such call".
    """
    with _SESSION_LOCK:
        sess = _SESSIONS.get(session_id)
    if sess is None or sess.user_id != user_id:
        return None
    return sess


def close_session(sess: SipSession) -> None:
    """Hang up, unload both sinks and drop the work directory."""
    try:
        if sess.proc.poll() is None:
            try:
                send_baresip(sess.proc, sess.master, "/hangup")
            except (OSError, ValueError):
                pass
            try:
                sess.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sess.proc.send_signal(signal.SIGTERM)
                try:
                    sess.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sess.proc.kill()
    finally:
        if sess.master >= 0:
            try:
                os.close(sess.master)
            except OSError:
                pass
            sess.master = -1
        drop_null_sinks(sess.modules)
        shutil.rmtree(sess.workdir, ignore_errors=True)
        with _SESSION_LOCK:
            _SESSIONS.pop(sess.id, None)


def spawn_baresip(config_dir: Path):
    """Start baresip on a **pty**, not a pipe, and return the process.

    This is the single non-obvious requirement in the whole feature. The commands
    that place a call (``/dial``, ``/hangup``) are registered by the *menu
    module*, and the menu is only instantiated when the app starts - which needs
    a terminal. Handed a pipe, baresip boots happily, says "baresip is ready",
    and answers every command with ``command not found (dial)``: no call, no
    error, nothing to notice. Verified on this box - with a pipe ``/dial`` is
    unknown, on a pty the same command reaches
    ``call: connecting to 'sip:...'``.

    The slave is put in raw mode so the line discipline does not echo what we
    type back at us: the log is read to decide whether the far end answered, and
    an echo of the command is indistinguishable from baresip talking.
    """
    master, slave = pty.openpty()
    tty.setraw(slave)
    try:
        proc = subprocess.Popen(
            ["baresip", "-f", str(config_dir)],
            stdin=slave, stdout=slave, stderr=slave,
            # System services lack the desktop runtime directory. Use the
            # same audio server as pactl, paplay and parec; otherwise SIP can
            # connect even though pulse.so failed and neither direction works.
            env=pulse_env(),
            start_new_session=True, close_fds=True,
        )
    except Exception:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


def send_baresip(proc: subprocess.Popen, master: int, line: str) -> None:
    """Write one command to baresip's pty."""
    os.write(master, line.encode("utf-8", "replace") + b"\n")


def read_baresip(master: int, limit: int = 8192) -> str:
    """Whatever baresip has said so far, decoded and stripped of control bytes."""
    try:
        data = os.read(master, limit)
    except (OSError, BlockingIOError):
        return ""
    if not data:
        return ""
    return data.decode("utf-8", "replace").replace("\r", "\n").strip()


#: What baresip prints when an established call is torn down by the far end.
#: Verified against baresip 1.0.0 driving a BYE from a local UAS:
#:   "sip:1001@127.0.0.1;transport=udp: session closed: Connection reset by peer"
#:   "sip:1001@127.0.0.1: Call with sip:... terminated (duration: 2 secs)"
#: The reason text varies with how the peer tore it down, so the marker is the
#: stable part and everything after it is kept as the explanation.
_CALL_ENDED_RE = re.compile(
    r"(session closed|terminated \(duration|call (?:with .* )?terminated)",
    re.IGNORECASE,
)


def _note_call_end(sess: SipSession, line: str) -> None:
    """Recognize a remote hangup only after the call has been answered."""
    if sess.closed.is_set() or not sess.established_at:
        return
    if _CALL_ENDED_RE.search(line):
        why = re.search(r"session closed:\s*(.+)$", line, re.IGNORECASE)
        sess.close_reason = (
            f"The caller hung up ({why.group(1).strip()})."
            if why else "The caller hung up."
        )
        sess.closed.set()


def _pump(proc: subprocess.Popen, master: int, log, sess: "SipSession | None" = None) -> None:
    """Drain baresip's pty on a thread.

    Non-optional: the buffer is finite, and a call that logs an RTP timeout every
    few seconds fills it, after which baresip blocks on write and stops answering
    commands entirely.

    Also the only place a remote hangup can be noticed. baresip outlives the
    call, so a BYE leaves no trace on the process and nothing else in the
    session is watching its log.
    """

    def reader() -> None:
        while True:
            text = read_baresip(master)
            if text:
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    line = line.strip()
                    log(line)
                    if sess is not None:
                        _note_call_end(sess, line)
            if proc.poll() is not None:
                return
            time.sleep(0.2)

    threading.Thread(target=reader, daemon=True).start()


def _wait_for_answer(sess: SipSession, timeout: float = RING_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Wait for the far end to answer.

    The markers are baresip's own: it prints ``Call established: <peer>`` on the
    way up, and the session is closed again on a rejection, which is a 404, a
    407, a TLS failure or a bad number. A rejection is detected rather than
    waited out, because a number that does not exist should say so in a second
    instead of ringing for the full minute.

    Failure returns the log tail, because "not answered" is unactionable on its
    own: each of those causes needs a different fix.
    """
    started = sess.dial_mark
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sess.proc.poll() is not None:
            return False, f"baresip exited early: {sess.tail(1200)}"
        with sess.lock:
            fresh = "\n".join(sess.output[started:]).lower()
        if "call established" in fresh or "sip session established" in fresh:
            sess.established_at = time.time()
            return True, ""
        if _REJECTED_RE.search(fresh):
            return False, f"The call was rejected. baresip said:\n{sess.tail(2000)}"
        time.sleep(0.4)
    return False, f"No answer within {int(timeout)}s. baresip said:\n{sess.tail(2000)}"


def _sweep_expired() -> None:
    for sess in _all_sessions():
        if sess.expired():
            close_session(sess)


def _ensure_sweeper() -> None:
    global _SWEEPER
    with _SESSION_LOCK:
        if _SWEEPER is not None and _SWEEPER.is_alive():
            return
        _SWEEPER = threading.Thread(target=_sweep_loop, daemon=True,
                                    name="apex-sip-sweeper")
        _SWEEPER.start()


def _sweep_loop() -> None:
    while True:
        time.sleep(5)
        try:
            _sweep_expired()
        except Exception:
            pass


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def _disabled(config, runtime) -> str | None:
    """The message for every action that must not touch the network yet."""
    cfg = effective_settings(config, runtime)
    if not cfg["sip_enabled"]:
        return ("SIP calls are turned off. Turn on 'SIP · phone calls' in the Settings tab "
                "and fill in the server, user and password first.")
    if not cfg["configured"]:
        return (f"SIP is not configured yet. Still missing {', '.join(cfg['missing'])} - "
                "set them in the Settings tab.")
    return None


def status_report(config, ctx) -> str:
    cfg = effective_settings(config)
    ready, why = pulse_ready()
    if not cfg["sip_enabled"]:
        why = "SIP is turned off in Settings."
    elif not cfg["configured"]:
        why = f"Missing {', '.join(cfg['missing'])} in Settings."
    elif missing_binaries():
        why = "Missing on this host: " + ", ".join(missing_binaries())
    return json.dumps({
        "sip_enabled": cfg["sip_enabled"],
        "configured": cfg["configured"],
        "missing_settings": cfg["missing"],
        "missing_on_host": missing_binaries(),
        "tts_engine": tts_engine_name(config, cfg),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "whisper_ready": bool(str(getattr(config, "SIP_WHISPER_PYTHON", "") or "")
                              and Path(config.SIP_WHISPER_PYTHON).exists()),
        "audio_loopback": "ok" if ready else why,
        "transport": cfg["sip_transport"],
        "server": cfg["sip_server"],
        "user": cfg["sip_user"],
        "domain": cfg["sip_domain"],
        "account": redact(account_line(cfg), cfg) if cfg["configured"] else None,
        "live_calls": len(sessions_for(ctx.user_id)),
        "max_concurrent_calls": MAX_CALLS,
    }, indent=2)


def plan_call(config, ctx, dest: str, text: str, duration: int) -> str:
    blocked = _disabled(config, None)
    if blocked:
        return blocked
    cfg = effective_settings(config)
    try:
        dest = resolve_call_destination(cfg, dest)
        plan = build_plan(cfg, dest, text, duration)
    except ValueError as exc:
        return f"That destination cannot be dialled: {exc}"
    plan["next_step"] = (
        "Nothing has been dialled. To place this call, call this tool again with the "
        "same destination and confirm=true. Show this plan to the operator and get "
        "their agreement first."
    )
    # The plan is only half of a two-turn exchange, and the reply to it - "yes",
    # "go ahead" - names no skill and no tool. Record the destination so the
    # next turn can be routed back here instead of falling out to the general
    # skill, which has no sip_call and would answer that it cannot place calls.
    note_pending(ctx, cfg, dest)
    return json.dumps(plan, indent=2)


def start_call(config, ctx, dest: str, text: str, duration: int) -> str:
    blocked = _disabled(config, None)
    if blocked:
        return blocked
    cfg = effective_settings(config)
    try:
        dest = resolve_call_destination(cfg, dest)
    except ValueError as exc:
        return f"Nothing was dialled. {exc}"

    missing = missing_binaries()
    if missing:
        return "Nothing was dialled. This host is missing: " + ", ".join(missing)
    why_not = unsupported_transport(cfg)
    if why_not:
        return "Nothing was dialled. " + why_not
    ready, why = pulse_ready()
    if not ready:
        return "Nothing was dialled. " + why

    live = sessions_for(ctx.user_id)
    if len(live) >= MAX_CALLS:
        return (f"A call is already up on this session (to {live[0].destination}). "
                "Finish it with action=hangup before dialling again.")

    cap = int(getattr(config, "SIP_MAX_DURATION_SECONDS", 600))
    duration = max(10, min(int(duration or 60), cap))
    try:
        uri = normalize_destination(dest, cfg)
    except ValueError as exc:
        return f"Nothing was dialled. {exc}"

    workdir = Path(tempfile.mkdtemp(prefix="APEX_sip_"))
    prefix = f"apex_sip_{uuid.uuid4().hex[:10]}"
    modules: list[int] = []
    try:
        modules, rx_sink, tx_sink = create_null_sinks(prefix)
        cfg_dir = workdir / "baresip"
        write_baresip_config(cfg, cfg_dir, rx_sink, tx_sink)
        proc, master = spawn_baresip(cfg_dir)
    except Exception as exc:
        drop_null_sinks(modules)
        shutil.rmtree(workdir, ignore_errors=True)
        return f"The call was not placed: {redact(str(exc), cfg)}"

    session_id = uuid.uuid4().hex[:12]
    sess = SipSession(
        id=session_id, user_id=ctx.user_id, destination=uri, workdir=workdir,
        rx_sink=rx_sink, tx_sink=tx_sink, modules=modules, proc=proc, master=master,
        started_at=time.time(),
        duration=int(duration),
        # Provisional, so an unanswered call cannot live forever; replaced once
        # the call is answered.
        deadline=time.time() + duration + RING_TIMEOUT_SECONDS,
    )
    with _SESSION_LOCK:
        _SESSIONS[session_id] = sess
    _ensure_sweeper()
    _pump(proc, master, sess.log, sess)

    # REGISTER has to reach the server before an INVITE will be routed, and a
    # /dial issued in the first second is answered with 503 and never rings.
    time.sleep(2.0)
    sess.log(f"DIAL {uri}")
    with sess.lock:
        sess.dial_mark = len(sess.output)
    try:
        send_baresip(proc, master, f"/dial {uri}")
    except (OSError, ValueError) as exc:
        close_session(sess)
        return f"The call was not placed: {redact(str(exc), cfg)}"

    answered, detail = _wait_for_answer(sess)
    if not answered:
        close_session(sess)
        return (f"The call to {uri} was not answered, so nothing was said. Check the number "
                f"and that the account may make outbound calls. baresip reported:\n"
                f"{redact(detail, cfg)}")

    emit = getattr(ctx, "emit", None)
    if emit:
        emit({"type": "sip_call_started", "sip_session_id": session_id, "sip_to": uri})

    # The duration is the conversation budget, so it starts when the far end
    # picks up. Arming it at the /dial would spend the whole of it on ringing:
    # a phone that takes twenty seconds to answer would be hung up the instant
    # it said hello.
    sess.deadline = time.time() + duration

    result = {
        "mode": "call",
        "answered": True,
        "session": session_id,
        "to": uri,
        "duration_seconds": duration,
        "remaining_seconds": sess.remaining(),
        "transcript": [],
    }
    if str(text or "").strip():
        spoken = speak_turn(config, sess, cfg, text)
        if spoken.get("error"):
            result["error"] = spoken["error"]
        else:
            result["spoken"] = spoken["spoken"]
            result["remaining_seconds"] = spoken["remaining_seconds"]
            if getattr(ctx, "autonomous_call_authorized", False):
                close_session(sess)
                result["session"] = None
                result["remaining_seconds"] = 0
                result["completed"] = True
                result["note"] = "Scheduled notification delivered; the call was ended."
    else:
        result["note"] = (
            "The call is up and they are listening. Speak your opening line with "
            f"action=say and session={session_id}, then action=listen to hear the reply."
        )
    return json.dumps(result, indent=2)


def speak_turn(config, sess: SipSession, cfg: dict, text: str) -> dict:
    """Say one utterance into a live call."""
    if sess.expired():
        close_session(sess)
        return {"error": f"{sess.end_reason()} The call to {sess.destination} is closed."}
    text = str(text or "").strip()
    if not text:
        return {"error": "No text to speak."}
    try:
        synth = synthesize(
            text,
            sess.workdir / "utterance.wav",
            voice=(
                edge_voice_for_language(cfg["sip_tts_voice"], cfg["response_language"])
                if cfg["sip_tts_engine"] == "edge" else ""
            ),
            engine=cfg["sip_tts_engine"],
        )
        sip_wav = to_wav(synth, sess.workdir / "utterance_8k.wav", 8000)
        play_into_sink(sip_wav, sess.tx_sink)
        if sess.closed.is_set():
            reason = sess.end_reason()
            close_session(sess)
            return {"error": f"{reason} The call to {sess.destination} ended while speaking."}
    except ValueError as exc:
        return {"error": str(exc)}
    with sess.lock:
        sess.transcript.append({"role": "apex", "text": text})
    return {"spoken": text, "remaining_seconds": sess.remaining()}


def say_into_call(config, ctx, session_id: str, text: str) -> str:
    sess = lookup_session(ctx.user_id, session_id)
    if sess is None:
        return "No such call is up on this session. Dial with action=call first."
    cfg = effective_settings(config)
    out = speak_turn(config, sess, cfg, text)
    if out.get("error"):
        return out["error"]
    with sess.lock:
        transcript = list(sess.transcript)
    return json.dumps({
        "session": sess.id,
        "spoken": out["spoken"],
        "remaining_seconds": out["remaining_seconds"],
        "transcript": transcript,
    }, indent=2)


def listen_for_reply(config, ctx, session_id: str, seconds: int) -> str:
    sess = lookup_session(ctx.user_id, session_id)
    if sess is None:
        return "No such call is up on this session. Dial with action=call first."
    if sess.expired():
        close_session(sess)
        return f"The call to {sess.destination} has ended and was closed."

    cap = int(getattr(config, "SIP_LISTEN_TIMEOUT_SECONDS", 20))
    seconds = max(2, min(int(seconds or cap), cap, max(2, sess.remaining())))
    try:
        recorded = record_from_sink(
            sess.workdir / f"reply_{int(time.time())}.wav", sess.rx_sink, seconds,
            stop_event=sess.closed,
        )
        if sess.closed.is_set():
            close_session(sess)
            return (f"{sess.end_reason()} Nothing further was heard on the call "
                    f"to {sess.destination}, which is now closed.")
        if recorded is None:
            heard = ""
        else:
            trimmed = to_wav(recorded, recorded.with_name(recorded.stem + "_trim.wav"),
                             16000, trim_silence=True)
            heard = transcribe(trimmed, config)
    except ValueError as exc:
        return str(exc)

    if heard:
        with sess.lock:
            sess.transcript.append({"role": "caller", "text": heard})
    with sess.lock:
        transcript = list(sess.transcript)
    return json.dumps({
        "session": sess.id,
        "listened_seconds": seconds,
        "heard": heard,
        "heard_nothing": not heard,
        "transcript": transcript,
        "remaining_seconds": sess.remaining(),
    }, indent=2)


def hang_up(config, ctx, session_id: str) -> str:
    sess = lookup_session(ctx.user_id, session_id)
    if sess is None:
        return "No such call is up on this session."
    with sess.lock:
        transcript = list(sess.transcript)
    seconds = round(time.time() - sess.started_at, 1)
    to = sess.destination
    close_session(sess)
    return json.dumps({
        "session": session_id, "to": to, "hung_up": True,
        "duration_seconds": seconds, "transcript": transcript,
    }, indent=2)


ACTIONS = ("status", "plan", "call", "say", "listen", "hangup")


def build_sip_tools(config) -> list[Tool]:
    """The SIP tool, bound to this deployment's config.

    No ``enabled`` flag: the tool stays registered and every action re-reads the
    settings, so turning SIP on in the tab takes effect on the next turn
    instead of needing a restart.
    """

    def t_sip_call(args: dict, ctx) -> str:
        if not ctx.user_id:
            return "Sign in to use SIP."
        action = str(args.get("action") or "status").strip().lower()
        if action not in ACTIONS:
            return f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}."
        try:
            if action == "status":
                return status_report(config, ctx)
            if action == "plan":
                if getattr(ctx, "autonomous_call_authorized", False):
                    default_duration = int(getattr(config, "SIP_MAX_DURATION_SECONDS", 600) or 600)
                    return start_call(config, ctx, args.get("to", ""), args.get("text", ""),
                                      int(args.get("duration") or default_duration))
                default_duration = int(getattr(config, "SIP_MAX_DURATION_SECONDS", 600) or 600)
                return plan_call(config, ctx, args.get("to", ""), args.get("text", ""),
                                 int(args.get("duration") or default_duration))
            if action == "call":
                # The dry run is the gate. Without an explicit confirmation in
                # the same request there is no interactive dial. A scheduled
                # task has its own explicit, persisted authorization.
                if not args.get("confirm") and not getattr(ctx, "autonomous_call_authorized", False):
                    return ("Nothing was dialled. Call this tool again with the same 'to' "
                            "and confirm=true to place the call - get the operator's "
                            "agreement first.")
                default_duration = int(getattr(config, "SIP_MAX_DURATION_SECONDS", 600) or 600)
                return start_call(config, ctx, args.get("to", ""), args.get("text", ""),
                                  int(args.get("duration") or default_duration))
            if action == "say":
                return say_into_call(config, ctx, str(args.get("session") or ""),
                                     str(args.get("text") or ""))
            if action == "listen":
                return listen_for_reply(config, ctx, str(args.get("session") or ""),
                                        int(args.get("seconds") or 0))
            return hang_up(config, ctx, str(args.get("session") or ""))
        except Exception as exc:  # a tool must not take the turn down with it
            return f"SIP error: {redact(str(exc), None)}"

    return [Tool(
        "sip_call",
        "Place an outbound phone call on the operator's own SIP account and hold a "
        "two-way spoken conversation with whoever answers. Actions: status (what is "
        "configured and installed; never dials), plan (validate a destination and print a "
        "redacted call plan; never dials), call (dial, then hold the call open), say "
        "(speak one utterance into the live call), listen (record the caller's reply and "
        "transcribe it), hangup. Interactive calls need confirm=true and the operator's "
        "explicit agreement; a scheduled task explicitly requesting a call is already "
        "authorized. A scheduled notification ends after speaking. For 'call me', use "
        "the configured call-me number. Never invent another phone number.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS),
                       "description": "status | plan | call | say | listen | hangup"},
            "to": {"type": "string",
                   "description": "For plan and call: an extension, an E.164 number, or a "
                                  "full sip:user@domain URI. Required to dial."},
            "text": {"type": "string",
                     "description": "What to say. For call it is the opening line; for say "
                                    "it is the next utterance."},
            "confirm": {"type": "boolean",
                        "description": "Required for interactive calls; a call explicitly requested by a scheduled task is pre-authorized."},
            "duration": {"type": "integer", "minimum": 10, "maximum": 600,
                         "description": "Total call length in seconds from answer. Defaults to the configured SIP maximum."},
            "session": {"type": "string",
                        "description": "Session id returned by call. Required for say, listen and hangup."},
            "seconds": {"type": "integer", "minimum": 2, "maximum": 120,
                        "description": "How long to listen for the reply. Defaults to the server setting."},
        }, "required": ["action"], "additionalProperties": False},
        t_sip_call,
    )]
