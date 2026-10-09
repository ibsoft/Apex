"""Line-by-line editing of the dotenv file the backend loads at import time.

Rewriting an env file from a dict would be wrong: it is the same file
``config.py`` loads for the OpenAI keys, the wake word and the VAPT flag, and a
partial rewrite would silently blank those. So it is edited line by line under
a lock, and only the keys the caller names are ever touched.

Three states per key, which is the whole difficulty:

* a non-empty value replaces the line;
* a key that is *absent* from ``wanted`` is left untouched, so a partial save
  cannot disturb the operator's hand-written env file;
* a key that is *present and empty* has its line removed. That is what makes
  "forget the stored password" real - writing ``SIP_PASSWORD=`` would leave
  the value pinned to "" and override the default forever instead.

Both the SIP settings mirror and the skill creator's ``set_env`` go through
here, so neither can invent its own idea of what "remove this key" means.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_ENV_LOCK = threading.Lock()


def upsert_env(env_path: Path | str, wanted: dict[str, str]) -> Path:
    """Upsert the ``wanted`` keys of ``env_path``, leaving every other line alone.

    Empty values delete the key's line; absent keys are never touched. The file
    is left mode 0600 - it holds credentials and is world-readable otherwise.
    """
    env_path = Path(env_path)
    wanted = {str(k): str(v or "") for k, v in wanted.items()}
    with _ENV_LOCK:
        existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        out: list[str] = []
        seen: set[str] = set()
        for line in existing:
            key = _env_key(line)
            if key in wanted:
                if key not in seen:
                    seen.add(key)
                    if wanted[key]:
                        out.append(f"{key}={_env_quote(wanted[key])}")
                continue
            out.append(line)
        for key, value in wanted.items():
            if key not in seen and value:
                out.append(f"{key}={_env_quote(value)}")
        env_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
    return env_path


def _env_key(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return ""
    return line.split("=", 1)[0].strip()


def _env_quote(value: str) -> str:
    if any(c in value for c in ' "#\'\n'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return value
