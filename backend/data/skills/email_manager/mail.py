#!/usr/bin/env python3
"""email_manager pack - check / read / send mail over POP3 and SMTP.

All configuration comes from the .env file next to this script (one source of
truth; environment variables are ignored on purpose). Secret values are never
printed by any command in this script.

Commands:
  --check              validate .env + probe POP3 and SMTP (real login, no mail)
  check [--last N] [--all]
                       inbox summary; already-read messages are hidden unless --all
  read <n> [--raw] [--full] [--body]
                       show one message (marks it read)
  unread <n>           mark a message unread again so `check` lists it
  send --to A [--to B] --subject S [--body TEXT | --body-file F | stdin]
       [--html] [--attach FILE]
  sent [--last N]      list messages this script has sent (newest first)
  sent-read <id> [--full]
                       show one stored sent message and its content
Exit codes: 0 ok, 1 failure (reason on stderr), 2 usage.

Mailbox state (which messages are read, and every sent message's content) is
kept in mail.sqlite3 next to this script, mode 0600. It never holds passwords.
"""
import argparse
import base64
import hashlib
import html as html_mod
import mimetypes
import os
import re
import socket
import sqlite3
import ssl
import sys
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import poplib
import smtplib

ENV_PATH = Path(__file__).resolve().with_name(".env")
DB_PATH = Path(__file__).resolve().with_name("mail.sqlite3")
BODY_CAP = 3000        # keep read output (headers + body) inside run_shell's 4000-char result
LIST_BUDGET = 3800     # same reason, for the inbox listing


class MailError(Exception):
    pass


# --------------------------------------------------------------------------- config

def load_env(path: Path) -> dict:
    if not path.exists():
        raise MailError(f"missing config file {path} - create it with the "
                        f"required variables (see the skill prompt)")
    cfg = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        key, sep, value = s.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        cfg[key] = value
    return cfg


def _flag(cfg, name):
    raw = (cfg.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    raise MailError(f"{name} must be true or false, got {raw!r}")


def _bool_default_true(cfg, name):
    raw = (cfg.get(name) or "").strip().lower()
    if raw == "":
        return True
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise MailError(f"{name} must be true or false, got {raw!r}")


def _starttls_mode(cfg):
    raw = (cfg.get("SMTP_STARTTLS") or "auto").strip().lower()
    if raw in ("auto", ""):
        return "auto"
    if raw in ("1", "true", "yes", "on"):
        return "true"
    if raw in ("0", "false", "no", "off"):
        return "false"
    raise MailError(f"SMTP_STARTTLS must be auto, true or false, got {raw!r}")


def tls_context(verify: bool):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _port(cfg, name, default):
    raw = (cfg.get(name) or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        raise MailError(f"{name} must be a number, got {raw!r}")
    if not 1 <= port <= 65535:
        raise MailError(f"{name} must be 1-65535, got {port}")
    return port


def resolve(cfg: dict) -> dict:
    required = ["POP3_SERVER", "POP3_EMAIL", "POP3_PASSWORD", "SMTP_SERVER"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise MailError(
            "missing required variable(s) in " + str(ENV_PATH) + ": "
            + ", ".join(missing)
            + "\nEdit that file directly to add them. Never paste a password "
              "into chat.")
    pop3_port = _port(cfg, "POP3_PORT", 110)
    smtp_port = _port(cfg, "SMTP_PORT", 25)
    pop3_ssl = pop3_port == 995 if "POP3_SSL" not in cfg else _flag(cfg, "POP3_SSL")
    smtp_ssl = smtp_port == 465 if "SMTP_SSL" not in cfg else _flag(cfg, "SMTP_SSL")
    return {
        "pop3_server": cfg["POP3_SERVER"],
        "pop3_port": pop3_port,
        "pop3_ssl": pop3_ssl,
        "pop3_email": cfg["POP3_EMAIL"],
        "pop3_password": cfg["POP3_PASSWORD"],
        "smtp_server": cfg["SMTP_SERVER"],
        "smtp_port": smtp_port,
        "smtp_ssl": smtp_ssl,
        "smtp_starttls": _starttls_mode(cfg),
        "smtp_verify": _bool_default_true(cfg, "SMTP_TLS_VERIFY"),
        "pop3_verify": _bool_default_true(cfg, "POP3_TLS_VERIFY"),
        "smtp_from": cfg.get("SMTP_FROM") or cfg["POP3_EMAIL"],
        "helo": cfg.get("SMTP_HELO_HOST") or None,
        "smtp_user": cfg.get("SMTP_USERNAME") or None,
        "smtp_password": cfg.get("SMTP_PASSWORD") or None,
    }


# --------------------------------------------------------------------------- helpers

def _decode(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _line_bytes(lines) -> bytes:
    return b"\n".join(lines) + b"\n\n"


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        payload = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# Signature and quoted-reply trimming is for the `--body` mode (voice reading):
# an assistant that speaks an email must not read a legal footer, a signature
# block or a quoted thread. Heuristic and deliberately conservative - when in
# doubt it keeps the line, because a summariser can ignore extra text but
# cannot recover what was cut.
_SIG_MARKERS = (
    re.compile(r"^--\s*$"),
    re.compile(r"^_{5,}\s*$"),
    re.compile(r"^-{5,}\s*$"),
    re.compile(r"^={5,}\s*$"),
    re.compile(r"^sent from my\b", re.I),
    re.compile(r"^get outlook for\b", re.I),
    re.compile(r"^on .{0,140}\bwrote:\s*$", re.I),
    re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.I),
)


def trim_body(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue  # quoted reply line
        if any(m.match(s) for m in _SIG_MARKERS):
            break  # signature / quoted block starts here
        lines.append(ln)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip() or text.strip()


def body_of(msg) -> str:
    plain, rich = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        ct = part.get_content_type()
        if ct == "text/plain":
            plain.append(_decode_part(part))
        elif ct == "text/html":
            rich.append(_decode_part(part))
    if plain:
        return "\n".join(plain)
    if rich:
        return strip_html("\n".join(rich))
    return "(no text body)"


def attachments_of(msg):
    found = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            data = part.get_payload(decode=True) or b""
            found.append((_decode(name) or "unnamed", len(data)))
    return found


# --------------------------------------------------------------------------- state
# Which messages have been read (so `check` stops listing them) and every
# message this script has sent. A POP3 UIDL is the stable identity when the
# server supports it; otherwise a hash of the headers + size is used, so the
# state survives a server that predates UIDL or a mailbox that renumbers.

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def account_key(cfg) -> str:
    return cfg["pop3_email"]


def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        " account TEXT NOT NULL, key TEXT NOT NULL, number INTEGER,"
        " subject TEXT, sender TEXT, msg_date TEXT, read_at TEXT,"
        " PRIMARY KEY (account, key))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sent ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, sent_at TEXT,"
        " recipients TEXT, subject TEXT, body TEXT, attachments TEXT)")
    return conn


def seen_keys(conn, account) -> set:
    return {r[0] for r in conn.execute(
        "SELECT key FROM seen WHERE account=?", (account,)).fetchall()}


def mark_seen(conn, account, key, number, head):
    conn.execute(
        "INSERT INTO seen(account, key, number, subject, sender, msg_date, read_at)"
        " VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(account, key) DO UPDATE SET read_at=excluded.read_at",
        (account, key, number, _decode(head.get("Subject")),
         _decode(head.get("From")), (head.get("Date") or "").strip(), _now()))
    conn.commit()


def mark_unread(conn, account, key):
    conn.execute("DELETE FROM seen WHERE account=? AND key=?", (account, key))
    conn.commit()


def record_sent(conn, account, recipients, subject, body, attachments):
    conn.execute(
        "INSERT INTO sent(account, sent_at, recipients, subject, body, attachments)"
        " VALUES(?,?,?,?,?,?)",
        (account, _now(), ", ".join(recipients), subject, body,
         ", ".join(attachments)))
    conn.commit()


def message_key(num, head, size, uidls) -> str:
    uid = uidls.get(num)
    if uid:
        return "uidl:" + uid
    basis = "|".join([
        _decode(head.get("From")),
        _decode(head.get("Subject")),
        (head.get("Date") or "").strip(),
        str(size) if size is not None else "",
    ])
    return "h:" + hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()


def _uidl_map(conn) -> dict:
    try:
        _, lines, _ = conn.uidl()
    except poplib.error_proto:
        return {}
    out = {}
    for ln in lines:
        parts = ln.split()
        if len(parts) >= 2:
            try:
                out[int(parts[0])] = parts[1].decode("utf-8", "replace")
            except (ValueError, AttributeError):
                continue
    return out


def _sizes(conn) -> dict:
    try:
        return {int(p[0]): int(p[1])
                for p in (ln.split() for ln in conn.list()[1])}
    except Exception:
        return {}


def _head_of(conn, num):
    try:
        _, lines, _ = conn.top(num, 0)
    except poplib.error_proto:
        _, lines, _ = conn.retr(num)
    return message_from_bytes(_line_bytes(lines))


# --------------------------------------------------------------------------- POP3

def pop3_connect(cfg):
    try:
        if cfg["pop3_ssl"]:
            conn = poplib.POP3_SSL(cfg["pop3_server"], cfg["pop3_port"],
                                   timeout=20, context=tls_context(cfg["pop3_verify"]))
        else:
            conn = poplib.POP3(cfg["pop3_server"], cfg["pop3_port"], timeout=20)
    except (OSError, ssl.SSLError) as e:
        raise MailError(f"POP3 connect to {cfg['pop3_server']}:{cfg['pop3_port']} "
                        f"failed: {e}")
    try:
        conn.user(cfg["pop3_email"])
        conn.pass_(cfg["pop3_password"])
    except poplib.error_proto as e:
        try:
            conn.quit()
        except Exception:
            pass
        raise MailError(f"POP3 login failed for {cfg['pop3_email']}: {e}")
    return conn


def cmd_check_inbox(cfg, last, show_all):
    conn = pop3_connect(cfg)
    try:
        count, size = conn.stat()
        print(f"POP3 {cfg['pop3_server']}:{cfg['pop3_port']} "
              f"{'(SSL) ' if cfg['pop3_ssl'] else ''}- {count} message(s), "
              f"{size} bytes total")
        if count == 0:
            print("  mailbox empty")
            return
        sizes = _sizes(conn)
        uidls = _uidl_map(conn)
        dbc = db()
        try:
            seen = seen_keys(dbc, account_key(cfg))
        finally:
            dbc.close()
        used_top = True
        entries = []          # newest first; dropped from the end if too long
        read_hidden = 0
        for num in range(count, max(0, count - last), -1):
            try:
                _, lines, _ = conn.top(num, 0)
            except poplib.error_proto:
                _, lines, _ = conn.retr(num)
                used_top = False
            head = message_from_bytes(_line_bytes(lines))
            is_read = message_key(num, head, sizes.get(num), uidls) in seen
            if is_read and not show_all:
                read_hidden += 1
                continue
            when = (head.get("Date") or "?").strip()
            frm = _decode(head.get("From")) or "?"
            subj = _decode(head.get("Subject")) or "(no subject)"
            bsize = sizes.get(num)
            kb = f"{bsize / 1024:.1f} KB" if isinstance(bsize, int) else "?"
            tag = "[read] " if is_read else "       "
            entries.append(f"  #{num:<4} {tag}{when:<31} {frm}  [{kb}]\n"
                           f"        {subj}")
        if not entries:
            if read_hidden:
                print(f"  no unread messages ({read_hidden} already read; "
                      f"run `check --all` to list them)")
            else:
                print("  no messages in the requested range")
            return
        omitted = 0
        while entries and sum(len(e) + 1 for e in entries) > LIST_BUDGET:
            entries.pop()
            omitted += 1
        if read_hidden:
            print(f"  ({read_hidden} read message(s) hidden - `check --all` "
                  f"to list them)")
        if omitted:
            print(f"  (oldest {omitted} not shown - run `check --last N` "
                  f"or `read <n>` for a specific message)")
        print("\n".join(entries))
        if not used_top:
            print("  note: server does not support TOP; full messages were "
                  "fetched for headers")
        print(f"  read one with: mail.py read <n>   (numbers above)")
    finally:
        _quit(conn)


def cmd_read(cfg, number, raw, full, body_only):
    conn = pop3_connect(cfg)
    try:
        try:
            _, lines, _ = conn.retr(number)
        except poplib.error_proto as e:
            raise MailError(f"cannot read message #{number}: {e}")
        uidls = _uidl_map(conn)
        sizes = _sizes(conn)
    finally:
        _quit(conn)
    raw_bytes = _line_bytes(lines)
    msg = message_from_bytes(raw_bytes)
    dbc = db()
    try:
        mark_seen(dbc, account_key(cfg),
                  message_key(number, msg, sizes.get(number), uidls),
                  number, msg)
    finally:
        dbc.close()
    if raw:
        print(raw_bytes.decode("utf-8", "replace"))
        return
    body = body_of(msg).replace("\r\n", "\n").rstrip()
    if body_only:
        body = trim_body(body)
        if not full and len(body) > BODY_CAP:
            body = body[:BODY_CAP] + f"\n... [truncated, {len(body) - BODY_CAP} more chars; use --full]"
        print(body)
        return
    print(f"Message #{number}")
    for hdr in ("From", "To", "Cc", "Date", "Subject"):
        val = _decode(msg.get(hdr))
        if val:
            print(f"  {hdr}: {val}")
    atts = attachments_of(msg)
    if atts:
        print("  attachments (not displayed):")
        for name, size in atts:
            print(f"    - {name} ({size} bytes)")
    if not full and len(body) > BODY_CAP:
        body = body[:BODY_CAP] + f"\n... [truncated, {len(body) - BODY_CAP} more chars; use --full]"
    print()
    print(body)
    print(f"\n  [saved: read - it will no longer be listed by `check`]")


def cmd_unread(cfg, number):
    conn = pop3_connect(cfg)
    try:
        try:
            head = _head_of(conn, number)
        except poplib.error_proto as e:
            raise MailError(f"cannot mark #{number} unread: {e}")
        uidls = _uidl_map(conn)
        sizes = _sizes(conn)
    finally:
        _quit(conn)
    dbc = db()
    try:
        mark_unread(dbc, account_key(cfg),
                    message_key(number, head, sizes.get(number), uidls))
    finally:
        dbc.close()
    print(f"message #{number} is unread again - `check` will list it")


def _quit(conn):
    try:
        conn.quit()
    except Exception:
        pass


# --------------------------------------------------------------------------- SMTP

def smtp_connect(cfg):
    try:
        if cfg["smtp_ssl"]:
            s = smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"],
                                 timeout=20, context=tls_context(cfg["smtp_verify"]))
        else:
            s = smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=20)
    except (OSError, ssl.SSLError) as e:
        raise MailError(f"SMTP connect to {cfg['smtp_server']}:{cfg['smtp_port']} "
                        f"failed: {e}")
    try:
        try:
            s.ehlo(cfg["helo"] or "")
        except smtplib.SMTPException:
            s.helo(cfg["helo"] or "")
        mode = cfg["smtp_starttls"]
        advertised = s.has_extn("starttls")
        used_starttls = False
        if not cfg["smtp_ssl"] and mode != "false":
            if mode == "true" and not advertised:
                s.quit()
                raise MailError("SMTP_STARTTLS=true but the server does not "
                                "advertise STARTTLS")
            if advertised and (mode == "true" or mode == "auto"):
                try:
                    s.starttls(context=tls_context(cfg["smtp_verify"]))
                except ssl.SSLCertVerificationError as e:
                    s.quit()
                    raise MailError(
                        f"STARTTLS certificate verification failed for "
                        f"{cfg['smtp_server']}: {e}. If this is a trusted "
                        f"internal relay with a self-signed certificate, set "
                        f"SMTP_TLS_VERIFY=false in the .env; to send without "
                        f"encryption, set SMTP_STARTTLS=false instead.")
                except ssl.SSLError as e:
                    s.quit()
                    raise MailError(f"STARTTLS handshake failed: {e}")
                s.ehlo(cfg["helo"] or "")
                used_starttls = True
        s._apex_starttls_used = used_starttls
        if cfg["smtp_user"]:
            if not s.has_extn("auth"):
                s.quit()
                raise MailError("SMTP server does not advertise AUTH but "
                                "SMTP_USERNAME is set in .env")
            if not cfg["smtp_password"]:
                s.quit()
                raise MailError("SMTP_USERNAME is set but SMTP_PASSWORD is not - "
                                "edit the .env directly")
            try:
                s.login(cfg["smtp_user"], cfg["smtp_password"])
            except smtplib.SMTPAuthenticationError as e:
                s.quit()
                raise MailError(f"SMTP authentication failed for "
                                f"{cfg['smtp_user']}: {e.smtp_error!r}")
    except Exception:
        try:
            s.quit()
        except Exception:
            pass
        raise
    return s


def cmd_send(cfg, to, subject, body, body_file, html, attach):
    if body is not None:
        text = body
    elif body_file:
        path = Path(body_file).expanduser()
        if not path.is_file():
            raise MailError(f"--body-file not found: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise MailError("no message body - pipe text, or use --body / --body-file")
    if not text.strip():
        raise MailError("refusing to send an empty body")

    recipients = []
    for item in to:
        for addr in item.split(","):
            addr = addr.strip()
            if addr:
                recipients.append(addr)
    if not recipients:
        raise MailError("no recipients - use --to name@host")
    bad = [a for a in recipients if "@" not in a]
    if bad:
        raise MailError("suspicious recipient address(es): " + ", ".join(bad))

    msg = EmailMessage()
    msg["From"] = cfg["smtp_from"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if html:
        msg.set_content(strip_html(text))
        msg.add_alternative(text, subtype="html")
    else:
        msg.set_content(text)
    total = len(text.encode())
    for item in attach:
        path = Path(item).expanduser()
        if not path.is_file():
            raise MailError(f"--attach not found: {path}")
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(path.name)
        if ctype and "/" in ctype:
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype,
                               filename=path.name)
        else:
            msg.add_attachment(data, maintype="application",
                               subtype="octet-stream", filename=path.name)
        total += len(data)

    s = smtp_connect(cfg)
    try:
        refused = s.send_message(msg, from_addr=cfg["smtp_from"],
                                 to_addrs=recipients)
    except smtplib.SMTPException as e:
        raise MailError(f"SMTP send failed: {e}")
    finally:
        try:
            s.quit()
        except Exception:
            pass
    if refused:
        raise MailError("rejected recipient(s): " + ", ".join(sorted(refused)))
    dbc = db()
    try:
        record_sent(dbc, account_key(cfg), recipients, subject, text,
                    [Path(a).expanduser().name for a in attach])
    finally:
        dbc.close()
    print(f"sent 1 message to {len(recipients)} recipient(s) "
          f"({total} bytes body+attachments)")
    for addr in recipients:
        print(f"  -> {addr}")


def cmd_sent(cfg, last):
    dbc = db()
    try:
        rows = dbc.execute(
            "SELECT id, sent_at, recipients, subject FROM sent "
            "WHERE account=? ORDER BY id DESC LIMIT ?",
            (account_key(cfg), last)).fetchall()
    finally:
        dbc.close()
    if not rows:
        print("no sent messages recorded yet")
        return
    header = f"{len(rows)} sent message(s), newest first:\n"
    entries = []
    for id_, when, recips, subj in rows:
        entries.append(f"  #{id_:<4} {when:<20} {recips}\n"
                       f"        {subj or '(no subject)'}")
    omitted = 0
    while entries and len(header) + sum(len(e) + 1 for e in entries) > LIST_BUDGET:
        entries.pop()
        omitted += 1
    out = header
    if omitted:
        out += f"  (oldest {omitted} not shown - raise --last)\n"
    out += "\n".join(entries)
    print(out)
    print("  show one with: mail.py sent-read <id>   (ids above)")


def cmd_sent_read(cfg, sent_id, full):
    dbc = db()
    try:
        row = dbc.execute(
            "SELECT id, sent_at, recipients, subject, body, attachments "
            "FROM sent WHERE id=? AND account=?",
            (sent_id, account_key(cfg))).fetchone()
    finally:
        dbc.close()
    if row is None:
        raise MailError(f"no sent message #{sent_id} recorded for this account")
    _, when, recips, subj, body, atts = row
    print(f"Sent #{sent_id}")
    print(f"  Date: {when}")
    print(f"  To: {recips}")
    print(f"  Subject: {subj or '(no subject)'}")
    if atts:
        print(f"  Attachments: {atts}")
    text = body or ""
    if not full and len(text) > BODY_CAP:
        text = text[:BODY_CAP] + f"\n... [truncated, {len(text) - BODY_CAP} more chars; use --full]"
    print()
    print(text)


# --------------------------------------------------------------------------- --check

def cmd_check_env(cfg_all, probe):
    ok = True
    print(f"config file: {ENV_PATH}")
    try:
        mode = ENV_PATH.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"  warning: file mode {oct(mode)} is group/world accessible; "
                  f"run: chmod 600 {ENV_PATH}")
        else:
            print(f"  permissions: {oct(mode)} ok")
    except OSError as e:
        print(f"  cannot stat config: {e}")
        ok = False

    required = ["POP3_SERVER", "POP3_EMAIL", "POP3_PASSWORD", "SMTP_SERVER"]
    optional = ["POP3_PORT", "SMTP_PORT", "SMTP_FROM", "SMTP_HELO_HOST",
                "SMTP_USERNAME", "SMTP_PASSWORD", "POP3_SSL", "SMTP_SSL",
                "SMTP_STARTTLS", "SMTP_TLS_VERIFY", "POP3_TLS_VERIFY"]
    for key in required:
        set_ = bool(cfg_all.get(key))
        shown = cfg_all.get(key, "")
        if key.endswith("PASSWORD"):
            shown_disp = "set (hidden)" if set_ else "MISSING"
        else:
            shown_disp = shown if set_ else "MISSING"
        if not set_:
            ok = False
        print(f"  {key:<16} {shown_disp:<16} (required)")
    for key in optional:
        val = cfg_all.get(key)
        if key.endswith("PASSWORD"):
            shown = "set (hidden)" if val else "-"
        else:
            shown = val if val else "-"
        print(f"  {key:<16} {shown}")
    print(f"  defaults: POP3_PORT=110, SMTP_PORT=25, "
          f"SMTP_FROM={cfg_all.get('SMTP_FROM') or cfg_all.get('POP3_EMAIL') or '?'}")
    if not ok:
        print("\nRESULT: config incomplete - fix the MISSING line(s) above in "
              "the .env, then rerun. Probes skipped.")
        return 1

    cfg = resolve(cfg_all)
    print()
    if not probe:
        return 0

    print(f"probe POP3 {cfg['pop3_server']}:{cfg['pop3_port']}"
          f"{' (SSL)' if cfg['pop3_ssl'] else ''} ...")
    try:
        conn = pop3_connect(cfg)
        try:
            count, size = conn.stat()
            print(f"  ok - login accepted, {count} message(s), {size} bytes")
        finally:
            _quit(conn)
    except MailError as e:
        print(f"  FAILED - {e}")
        ok = False

    print(f"probe SMTP {cfg['smtp_server']}:{cfg['smtp_port']}"
          f"{' (SSL)' if cfg['smtp_ssl'] else ''} ...")
    try:
        s = smtp_connect(cfg)
        try:
            auth = "none"
            if cfg["smtp_user"]:
                auth = "login ok"
            elif s.has_extn("auth"):
                auth = f"available ({s.esmtp_features.get('auth', '?')}), not configured"
            print(f"  ok - EHLO accepted"
                  f"{' via HELO ' + cfg['helo'] if cfg['helo'] else ''}"
                  f", STARTTLS {'used' if getattr(s, '_apex_starttls_used', False) else 'no'}"
                  f" (verify {'on' if cfg['smtp_verify'] else 'off'})"
                  f", AUTH {auth}")
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except MailError as e:
        print(f"  FAILED - {e}")
        ok = False

    print(f"\nRESULT: {'all checks passed' if ok else 'one or more checks failed'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="mail.py",
        description="POP3/SMTP client driven by the .env next to this script.")
    p.add_argument("--check", action="store_true",
                   help="validate .env and probe POP3 + SMTP, then exit")
    sub = p.add_subparsers(dest="cmd")
    pc = sub.add_parser("check", help="inbox summary with message numbers")
    pc.add_argument("--last", type=int, default=10, metavar="N")
    pc.add_argument("--all", action="store_true", dest="show_all",
                    help="include messages already marked read")
    pr = sub.add_parser("read", help="show one message (marks it read)")
    pr.add_argument("number", type=int)
    pr.add_argument("--raw", action="store_true", help="print raw RFC822")
    pr.add_argument("--full", action="store_true", help="do not truncate body")
    pr.add_argument("--body", action="store_true", dest="body_only",
                    help="body only, signatures and quoted replies removed "
                         "(use for voice reading / summarising)")
    pu = sub.add_parser("unread", help="mark a message unread again")
    pu.add_argument("number", type=int)
    ps = sub.add_parser("send", help="send one message")
    ps.add_argument("--to", action="append", required=True, metavar="ADDR")
    ps.add_argument("--subject", required=True)
    ps.add_argument("--body", default=None, metavar="TEXT")
    ps.add_argument("--body-file", default=None, metavar="FILE")
    ps.add_argument("--html", action="store_true")
    ps.add_argument("--attach", action="append", default=[], metavar="FILE")
    pst = sub.add_parser("sent", help="list messages this script sent")
    pst.add_argument("--last", type=int, default=10, metavar="N")
    psr = sub.add_parser("sent-read", help="show a stored sent message")
    psr.add_argument("id", type=int)
    psr.add_argument("--full", action="store_true", help="do not truncate body")
    args = p.parse_args(argv)

    try:
        cfg_all = load_env(ENV_PATH)
        if args.check:
            return cmd_check_env(cfg_all, probe=True)
        if args.cmd == "check":
            cmd_check_inbox(resolve(cfg_all), args.last, args.show_all)
            return 0
        if args.cmd == "read":
            cmd_read(resolve(cfg_all), args.number, args.raw, args.full,
                     args.body_only)
            return 0
        if args.cmd == "unread":
            cmd_unread(resolve(cfg_all), args.number)
            return 0
        if args.cmd == "send":
            cmd_send(resolve(cfg_all), args.to, args.subject, args.body,
                     args.body_file, args.html, args.attach)
            return 0
        if args.cmd == "sent":
            cmd_sent(resolve(cfg_all), args.last)
            return 0
        if args.cmd == "sent-read":
            cmd_sent_read(resolve(cfg_all), args.id, args.full)
            return 0
        p.print_help()
        return 2
    except MailError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (OSError, socket.timeout) as e:
        print(f"error: network problem: {e}", file=sys.stderr)
        return 1
    except poplib.error_proto as e:
        print(f"error: POP3 server rejected the command: {e}", file=sys.stderr)
        return 1
    except smtplib.SMTPException as e:
        print(f"error: SMTP server error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
