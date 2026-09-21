"""Local Codex app-server transport. Codex owns OAuth credentials and refreshes."""
from collections import deque
import http.client
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit


class CodexError(RuntimeError):
    pass


class CodexClient:
    def __init__(self, home, binary="codex"):
        executable = shutil.which(binary)
        if not executable:
            raise CodexError("Codex CLI not found. Install it, or set CODEX_BINARY to its executable path.")
        self.home = Path(home).expanduser().absolute()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.events = deque()
        self.incoming = queue.Queue()
        self.counter = 0
        self.workspace = tempfile.TemporaryDirectory(prefix="apex-codex-work-")
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY")}
        env["CODEX_HOME"] = str(self.home)
        command = [executable, "app-server", "--listen", "stdio://",
                   "-c", 'cli_auth_credentials_store="file"',
                   "-c", 'forced_login_method="chatgpt"',
                   "-c", 'model_provider="openai"',
                   "-c", 'web_search="disabled"']
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks",
                        "multi_agent", "browser_use", "computer_use", "code_mode"):
            command += ["-c", f"features.{feature}=false"]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                            cwd=self.workspace.name, env=env, bufsize=1)
        except OSError:
            self.workspace.cleanup()
            raise CodexError("Could not start Codex CLI.") from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "apex", "version": "1.0.0"},
                                        "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.incoming.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.incoming.put(None)

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise CodexError("Codex app-server disconnected.") from None

    def receive(self, timeout=120):
        try:
            message = self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Timed out waiting for Codex.") from None
        if message is None:
            raise CodexError("Codex app-server stopped. Check the CLI installation and configuration.")
        return message

    def request(self, method, params=None, timeout=30):
        self.counter += 1
        request_id = f"apex-{self.counter}"
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(max(0, deadline - time.monotonic()))
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise CodexError(f"Codex rejected {method}. Check your login and CLI version.")
                return message.get("result", {})
            self.events.append(message)

    def event(self, timeout=120):
        return self.events.popleft() if self.events else self.receive(timeout)

    def account(self):
        return self.request("account/read", {"refreshToken": False}).get("account")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()
        self.workspace.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def forward_callback(auth_url, callback_url):
    """Replay only this login's callback to its exact local listener, never arbitrary URLs."""
    try:
        auth = parse_qs(urlsplit(auth_url).query)
        redirect = urlsplit(auth["redirect_uri"][0])
        returned = urlsplit(callback_url.strip())
        query = parse_qs(returned.query)
        if (redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1", "::1")
                or (returned.scheme, returned.netloc, returned.path) !=
                   (redirect.scheme, redirect.netloc, redirect.path)
                or returned.username or returned.password or returned.fragment
                or len(query.get("state", [])) != 1 or len(query.get("code", [])) != 1
                or not secrets.compare_digest(query["state"][0], auth["state"][0])):
            raise ValueError
        port = redirect.port or 80
    except (ValueError, KeyError, IndexError):
        raise CodexError("Paste the full localhost callback URL from this login attempt (including code and state).") from None
    connection = http.client.HTTPConnection(redirect.hostname, port, timeout=15)
    try:
        connection.request("GET", returned.path + "?" + returned.query)
        response = connection.getresponse()
        if response.status >= 400:
            raise CodexError("Codex rejected the callback. Start a new login attempt.")
    except (OSError, http.client.HTTPException):
        raise CodexError("Cannot reach the local Codex callback listener. Start a new login attempt.") from None
    finally:
        connection.close()
