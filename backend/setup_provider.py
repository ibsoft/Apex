#!/usr/bin/env python3
"""Interactive provider configuration: python backend/setup_provider.py."""
import argparse
import curses
import io
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import webbrowser
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key

from codex_bridge import CodexClient, CodexError, forward_callback

BASE_DIR = Path(__file__).resolve().parent
PROVIDERS = {
    "codex": [],
    "openai": [("OPENAI_API_KEY", "API key", "", True),
               ("OPENAI_BASE_URL", "API base URL", "https://api.openai.com/v1", False),
               ("DEFAULT_MODEL", "Model", "gpt-4o-mini", False)],
    "kimi": [("KIMI_API_KEY", "API key", "", True),
             ("KIMI_BASE_URL", "API base URL", "https://api.moonshot.ai/v1", False),
             ("KIMI_MODEL", "Model", "kimi-k2-instruct", False)],
    "ollama": [("OLLAMA_BASE_URL", "API base URL", "http://localhost:11434/v1", False),
               ("OLLAMA_MODEL", "Model (already installed in Ollama)", "qwen2.5:7b", False)],
    "torch": [("TORCH_MODEL", "Hugging Face model ID or local path", "Qwen/Qwen2.5-7B-Instruct", False),
              ("TORCH_DEVICE", "Device (auto, cpu, cuda:0, mps)", "auto", False)],
}


class Cancelled(Exception):
    pass


def validate(key, value):
    if not value.strip():
        return "A value is required."
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "Control characters are not allowed."
    if "${" in value:
        return "Environment variable interpolation (${...}) is not supported here."
    if key.endswith("_URL") or key == "OPENAI_REDIRECT_URI":
        try:
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                return "Enter an http(s) URL without embedded credentials."
            url.port
        except ValueError:
            return "Enter a valid URL and port."
    return ""


def save_env(path, original, updates):
    """Preserve unrelated dotenv entries; replace atomically with private permissions."""
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink; choose the actual .env file.")
    current = path.read_bytes() if path.exists() else None
    if current != original:
        raise ValueError("The .env file changed during setup. Run setup again to reload it.")
    content = original.decode("utf-8") if original is not None else (BASE_DIR / ".env.example").read_text()
    fd, temporary = tempfile.mkstemp(prefix=".apex-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
        for key, value in updates.items():
            set_key(temporary, key, value, quote_mode="always")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Wizard:
    def __init__(self, screen):
        self.screen = screen
        screen.keypad(True)
        curses.noecho()
        curses.curs_set(0)
        self.styles = {
            "brand": curses.A_BOLD,
            "title": curses.A_BOLD,
            "selected": curses.A_REVERSE | curses.A_BOLD,
            "input": curses.A_BOLD,
            "error": curses.A_BOLD,
            "muted": curses.A_DIM,
        }
        if curses.has_colors() and "NO_COLOR" not in os.environ:
            try:
                curses.start_color()
                background = curses.COLOR_BLACK
                try:
                    curses.use_default_colors()
                    background = -1
                except curses.error:
                    pass
                palette = [
                    ("brand", curses.COLOR_CYAN, background),
                    ("title", curses.COLOR_YELLOW, background),
                    ("selected", curses.COLOR_BLACK, curses.COLOR_CYAN),
                    ("input", curses.COLOR_GREEN, background),
                    ("error", curses.COLOR_RED, background),
                ]
                for pair, (name, foreground, backdrop) in enumerate(palette, 1):
                    curses.init_pair(pair, foreground, backdrop)
                    self.styles[name] = curses.color_pair(pair) | curses.A_BOLD
            except curses.error:
                pass  # Retain readable attributes if color setup is unsupported.

    def draw(self, title, lines, error_line=None):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        rows = ["APEX | Provider setup", title, ""] + lines
        rows += [""] * max(0, height - len(rows) - 2)
        rows += ["Esc: cancel without saving | Up/Down: select | Enter: continue"]
        for row, line in enumerate(rows[:height - 1]):
            style = 0
            if row == 0:
                style = self.styles["brand"]
            elif row == 1:
                style = self.styles["title"]
            elif error_line is not None and row == error_line + 3:
                style = self.styles["error"]
            elif line.startswith(" > "):
                style = self.styles["selected"]
                line = line.ljust(max(0, width - 1))
            elif line.startswith("> "):
                style = self.styles["input"]
            elif line.startswith(("Current/default:", "Type a replacement,", "Esc:")):
                style = self.styles["muted"]
            self.screen.addnstr(row, 0, line, max(0, width - 1), style)
        self.screen.refresh()

    def key(self):
        key = self.screen.get_wch()
        if key in ("\x1b", "\x03"):
            raise Cancelled()
        return key

    def choose(self, title, options, selected=0, details=None):
        while True:
            self.draw(title, [(" > " if i == selected else "   ") + option
                              for i, option in enumerate(options)] + ([""] + details if details else []))
            key = self.key()
            if key in (curses.KEY_UP, "k"):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, "j", "\t"):
                selected = (selected + 1) % len(options)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                return selected

    def field(self, title, key, default, secret=False, optional=False):
        value, error = "", ""
        hint = "configured; Enter keeps it" if secret and default else default
        while True:
            shown = "*" * len(value) if secret else value
            self.draw(title, [f"Current/default: {hint or '(empty)'}", "",
                              "> " + shown[-max(1, self.screen.getmaxyx()[1] - 4):], "",
                              "Type a replacement, or press Enter to keep the current/default value.", error],
                      error_line=5)
            pressed = self.key()
            if pressed in ("\n", "\r", curses.KEY_ENTER):
                result = value or default
                error = "" if optional and not result else validate(key, result)
                if not error:
                    return result
            elif pressed in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                value = value[:-1]
            elif pressed == "\x15":
                value = ""
            elif isinstance(pressed, str) and pressed.isprintable():
                value += pressed

    def codex_login(self, updates):
        self.draw("Starting Codex login", ["Checking the local Codex installation..."])
        with CodexClient(updates["APEX_CODEX_HOME"], updates["CODEX_BINARY"]) as client:
            account = client.account()
            if account and account.get("type") == "chatgpt":
                if self.choose("Already signed in to Codex", ["Use existing login", "Sign in again"],
                               details=[account.get("email") or "ChatGPT account"]) == 0:
                    return
            login = client.request("account/login/start", {"type": "chatgpt"})
            auth_url, login_id = login["authUrl"], login["loginId"]
            # Run browser launching off the curses/UI thread.
            def open_browser():
                try:
                    webbrowser.open(auth_url)
                except webbrowser.Error:
                    pass
            threading.Thread(target=open_browser, daemon=True).start()
            deadline, error = time.monotonic() + 600, ""
            completed = False
            self.screen.timeout(100)
            try:
                while time.monotonic() < deadline:
                    try:
                        event = client.event(timeout=0.05)
                    except TimeoutError:
                        event = {}
                    if (event.get("method") == "account/login/completed"
                            and event.get("params", {}).get("loginId") == login_id):
                        if not event["params"].get("success"):
                            raise CodexError("ChatGPT login failed or expired. Run setup again.")
                        completed = True
                        return
                    width = max(20, self.screen.getmaxyx()[1] - 2)
                    lines = ["Sign in in your browser. If it did not open, use this URL:", ""]
                    lines += textwrap.wrap(auth_url, width=width)
                    lines += ["", "Waiting for login (up to 10 minutes).", "",
                              "P: paste the full localhost return URL if automatic return fails.",
                              "B: reopen browser | Esc: cancel", error]
                    self.draw("Sign in with ChatGPT", lines, error_line=len(lines) - 1)
                    try:
                        pressed = self.key()
                    except curses.error:
                        continue
                    if pressed in ("p", "P"):
                        self.screen.timeout(-1)
                        try:
                            callback = self.field("Paste full localhost callback URL (hidden)",
                                                  "CALLBACK", "", secret=True)
                            forward_callback(auth_url, callback)
                            error = "Callback sent. Waiting for Codex to finish sign-in."
                        except CodexError as exc:
                            error = str(exc)
                        finally:
                            self.screen.timeout(100)
                    elif pressed in ("b", "B"):
                        threading.Thread(target=open_browser, daemon=True).start()
                raise CodexError("Login timed out. Run setup again.")
            finally:
                self.screen.timeout(-1)
                if not completed:
                    try:
                        client.request("account/login/cancel", {"loginId": login_id}, timeout=3)
                    except (CodexError, TimeoutError):
                        pass

    def run(self, path, existing):
        providers = list(PROVIDERS)
        current = existing.get("PROVIDER_DEFAULT", "openai")
        labels = ["ChatGPT subscription (Codex browser login)" if name == "codex" else name for name in providers]
        provider = providers[self.choose("Select provider", labels,
                                        providers.index(current) if current in providers else 0)]
        updates = {"PROVIDER_DEFAULT": provider, "AGENT_ENGINE": "responses"}
        if provider == "codex":
            updates.update({
                "APEX_CODEX_HOME": str((BASE_DIR / "data" / "codex").absolute()),
                "CODEX_BINARY": existing.get("CODEX_BINARY") or shutil.which("codex") or "codex",
                "CODEX_MODEL": self.field("Codex model (blank uses your account default)", "CODEX_MODEL",
                                          existing.get("CODEX_MODEL") or "", optional=True),
                "DEV_AUTO_LOGIN": existing.get("DEV_AUTO_LOGIN") or "developer",
                "USE_OAUTH_ACCESS_KEY": "false",
            })
            if existing.get("APEX_CODEX_HOME"):
                updates["APEX_CODEX_HOME"] = str(Path(existing["APEX_CODEX_HOME"]).expanduser().absolute())
            if not existing.get("SECRET_KEY"):
                updates["SECRET_KEY"] = secrets.token_urlsafe(48)
            decision = self.choose("ChatGPT subscription setup", ["Continue to browser login", "Cancel"], details=[
                "APEX uses local auto-login on this trusted machine.",
                "Codex stores credentials separately and refreshes them automatically.",
                "Save target: " + str(path),
                "Codex credential directory: " + updates["APEX_CODEX_HOME"],
                "Model: " + (updates["CODEX_MODEL"] or "account default"),
                "Login saves Codex credentials; .env is saved only after login succeeds.",
            ])
            if decision != 0:
                raise Cancelled()
            self.codex_login(updates)
            return updates
        login = self.choose("Login mode", ["Keep existing authentication settings",
                            "Local auto-login (trusted local machine only)",
                            "Configure OAuth sign-in"])
        use_token = existing.get("USE_OAUTH_ACCESS_KEY", "").lower() in ("true", "1", "yes", "on")
        if login == 1:
            updates["DEV_AUTO_LOGIN"] = self.field("Local username", "DEV_AUTO_LOGIN",
                                                   existing.get("DEV_AUTO_LOGIN") or "developer")
            updates["USE_OAUTH_ACCESS_KEY"] = "false"
            use_token = False
        elif login == 2:
            updates["DEV_MODE"] = "false"
            updates["DEV_AUTO_LOGIN"] = ""
            fields = [
                ("OPENAI_CLIENT_ID", "OAuth client ID", "", False),
                ("OPENAI_CLIENT_SECRET", "OAuth client secret (optional for public clients)", "", True),
                ("BASE_URL", "Public backend URL", "http://localhost:5001", False),
                ("FRONTEND_URL", "Frontend URL", "http://localhost:3000", False),
                ("OPENAI_REDIRECT_URI", "Registered callback URL", "", False),
                ("OPENAI_OAUTH_SCOPE", "OAuth scopes", "openid profile email offline_access", False),
            ]
            for key, label, default, secret in fields:
                if key == "OPENAI_REDIRECT_URI":
                    default = updates["BASE_URL"].rstrip("/") + "/api/auth/callback"
                updates[key] = self.field(label, key, existing.get(key) or default, secret,
                                          optional=key == "OPENAI_CLIENT_SECRET")
            use_token = False
            if provider == "openai":
                use_token = self.choose("Credentials for model requests", [
                    "Server API key (OAuth is used for sign-in)",
                    "OAuth access token (requires API authorization for your OAuth app)",
                ], selected=int(existing.get("USE_OAUTH_ACCESS_KEY", "").lower() == "true")) == 1
            updates["USE_OAUTH_ACCESS_KEY"] = "true" if use_token else "false"
        for key, label, default, secret in PROVIDERS[provider]:
            if provider == "openai" and use_token:
                if key == "OPENAI_API_KEY":
                    continue
                if key == "DEFAULT_MODEL":
                    key, default = "CHATGPT_MODEL", "gpt-5-codex"
            updates[key] = self.field(f"{provider}: {label}", key, existing.get(key) or default, secret)
        if not existing.get("SECRET_KEY"):
            updates["SECRET_KEY"] = secrets.token_urlsafe(48)
        summary = [f"{key} = {'[hidden]' if 'KEY' in key or 'SECRET' in key else value}" for key, value in updates.items()]
        decision = self.choose(f"Save to {path}?", ["Save configuration", "Cancel"], details=summary)
        if decision != 0:
            raise Cancelled()
        return updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=BASE_DIR / ".env", help="Target dotenv file (default: backend/.env)")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(2, "Provider setup needs an interactive terminal.\n")
    path = args.env.absolute()
    try:
        original = path.read_bytes() if path.exists() else None
        existing = dotenv_values(stream=io.StringIO(original.decode("utf-8") if original else ""), interpolate=False)
        updates = curses.wrapper(lambda screen: Wizard(screen).run(path, existing))
        save_env(path, original, updates)
        if path == (BASE_DIR / ".env").absolute():
            # Provider setup defines the next startup defaults. Remove only
            # the UI selections that would otherwise mask those .env values.
            from db import get_db

            get_db().clear_settings("provider", "engine", "model")
    except (Cancelled, KeyboardInterrupt):
        print("Cancelled. No configuration was written.")
        return 0
    except (OSError, ValueError, curses.error, CodexError, TimeoutError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    print(f"Saved {path} (permissions 600). Restart the backend to load it.")
    print("Provider, engine, and model UI overrides were reset for this setup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
