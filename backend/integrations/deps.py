"""Automatic provisioning of the integration manager's optional dependencies.

The MCP tab's SQL kinds need a Python driver per engine and - for SQL Server -
Microsoft's "ODBC Driver 18 for SQL Server", a *system* library. Installing the
system library normally needs root, so instead of asking the operator to run
apt this module fetches the driver from Microsoft's own ``.deb`` and unpacks it
into a user-owned directory, then hands pyodbc the absolute ``.so`` path to load
(``DRIVER={/abs/path/libmsodbcsql-18.x.so}``; unixODBC accepts a path in place
of a registered driver name).

Everything here is best-effort: a network failure, a missing tool or an
unexpected package layout returns ``None`` and the caller keeps reporting the
ordinary "driver not installed" reason. Nothing raises on I/O.

``INTEGRATIONS_AUTO_INSTALL`` (default on) turns the whole thing off, in which
case both the pip drivers and the ODBC library are left to the operator.
"""
from __future__ import annotations

import gzip
import importlib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# Python driver package per kind, as (import name, pip spec).
_PIP = {
    "postgres": ("psycopg2", "psycopg2-binary>=2.9"),
    "mysql": ("pymysql", "pymysql>=1.1"),
    "sqlserver": ("pyodbc", "pyodbc>=5.1"),
    "oracle": ("oracledb", "oracledb>=2.4"),
}

# Microsoft publishes an ODBC driver per Ubuntu release under /ubuntu/<version>.
_MS_BASE = "https://packages.microsoft.com/ubuntu/{version}/prod"
_ODBC_PACKAGE = "msodbcsql18"
_DRIVER_LIB_GLOB = "libmsodbcsql-*.so*"

_LOCK = threading.Lock()
_CACHE: dict[str, str | None] = {}


# ---------------------------------------------------------------------------
# config access
# ---------------------------------------------------------------------------

def _cfg():
    from config import config  # imported lazily so tests can monkeypatch flags
    return config


def auto_install_enabled() -> bool:
    return bool(getattr(_cfg(), "INTEGRATIONS_AUTO_INSTALL", True))


def _odbc_dir() -> Path:
    configured = getattr(_cfg(), "INTEGRATIONS_ODBC_DIR", None)
    if configured:
        return Path(configured)
    base = getattr(_cfg(), "DATA_DIR", None) or tempfile.gettempdir()
    return Path(base) / "odbc"


def _deb_url_override() -> str:
    return str(getattr(_cfg(), "INTEGRATIONS_ODBC_DEB_URL", "") or "").strip()


# ---------------------------------------------------------------------------
# Python drivers
# ---------------------------------------------------------------------------

def load_driver_module(kind: str):
    """Import the driver for ``kind``, pip-installing it first if the operator
    allowed auto-install and it is missing. Raises ImportError when the driver
    cannot be provided (the caller turns that into a message)."""
    import_name, spec = _PIP[kind]
    try:
        return importlib.import_module(import_name)
    except ImportError:
        if not auto_install_enabled():
            raise
        if not _pip_install(spec):
            raise
        importlib.invalidate_caches()
        return importlib.import_module(import_name)


def _pip_install(spec: str) -> bool:
    """Best-effort ``pip install`` into the running interpreter's environment."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "-q", spec],
            capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001 - any failure is a normal "no driver"
        log.warning("auto pip install of %s failed: %s", spec, exc)
        return False
    if result.returncode != 0:
        log.warning("auto pip install of %s failed: %s", spec,
                    (result.stderr or "").strip()[-300:])
        return False
    return True


# ---------------------------------------------------------------------------
# SQL Server ODBC library
# ---------------------------------------------------------------------------

def _odbc_drivers() -> list:
    """Registered ODBC drivers, via pyodbc. Empty list if pyodbc is unavailable."""
    try:
        import pyodbc  # type: ignore
        return list(pyodbc.drivers())
    except Exception:
        return []


def _pick_microsoft(drivers) -> str | None:
    for name in drivers:
        low = name.lower()
        if "odbc driver" in low and "sql server" in low:
            return name
    return None


def local_driver_path() -> Path | None:
    """The auto-provisioned driver library, if one has already been unpacked."""
    return _find_local_driver(_odbc_dir())


def resolve_sqlserver_driver(configured_driver: str = "", auto_install: bool | None = None) -> str | None:
    """Return a usable ``DRIVER`` value for a SQL Server connection, or None.

    Order of preference: the operator's named driver if it is registered, an
    already-unpacked local library, a registered Microsoft driver, then a fresh
    download+unpack (when allowed)."""
    configured = (configured_driver or "").strip()
    drivers = _odbc_drivers()
    if configured and configured in drivers:
        return configured
    local = local_driver_path()
    if local:
        return str(local)
    if not configured:
        picked = _pick_microsoft(drivers) or (drivers[0] if drivers else None)
        if picked:
            return picked
    return provision_sqlserver_driver(auto_install)


def provision_sqlserver_driver(auto_install: bool | None = None) -> str | None:
    """Download and unpack Microsoft's ODBC Driver 18 for SQL Server, if needed.

    Returns the absolute ``.so`` path, or None when auto-install is off or the
    download/unpack failed. The result (including a failure) is cached for the
    process so a broken network is not retried on every request."""
    enabled = auto_install_enabled() if auto_install is None else bool(auto_install)
    if not enabled:
        return None
    with _LOCK:
        if "sqlserver" in _CACHE:
            return _CACHE["sqlserver"]
        try:
            path = _provision_locked()
        except Exception as exc:  # noqa: BLE001 - provisioning never breaks a turn
            log.warning("ODBC driver auto-provision failed: %s", exc)
            path = None
        _CACHE["sqlserver"] = path
        return path


def _provision_locked() -> str | None:
    target = _odbc_dir()
    existing = _find_local_driver(target)
    if existing:
        return str(existing)
    url = _deb_url_override() or _latest_deb_url()
    if not url:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="msodbc-", dir=str(target.parent)))
    try:
        deb = work / "pkg.deb"
        deb.write_bytes(_download_bytes(url, timeout=120))
        root = work / "root"
        root.mkdir()
        _extract_deb(deb, root)
        if not _find_local_driver(root):
            raise RuntimeError("no driver library inside the downloaded package")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        root.rename(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    found = _find_local_driver(target)
    return str(found) if found else None


def _find_local_driver(root: Path) -> Path | None:
    if not root or not root.exists():
        return None
    candidates = [p for p in root.rglob(_DRIVER_LIB_GLOB)
                  if p.is_file() and not p.is_symlink()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: _version_key(p.name))[-1]


def _extract_deb(deb: Path, dest: Path) -> None:
    if shutil.which("dpkg-deb"):
        _run(["dpkg-deb", "-x", str(deb), str(dest)])
        return
    if shutil.which("ar"):
        _run(["ar", "x", str(deb)], cwd=dest)
        for data in sorted(dest.glob("data.tar.*")):
            _run(["tar", "xf", str(data)], cwd=dest)
            return
        raise RuntimeError("no data.tar in package")
    raise RuntimeError("neither dpkg-deb nor ar is available to unpack the package")


def _run(cmd, cwd=None) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(result.stderr or '').strip()[-300:]}")


def _latest_deb_url() -> str | None:
    rel = _os_release()
    version = rel.get("VERSION_ID", "")
    codename = rel.get("VERSION_CODENAME", "")
    if rel.get("ID") != "ubuntu" or not version or not codename:
        return None
    base = _MS_BASE.format(version=version)
    index = f"{base}/dists/{codename}/main/binary-amd64/Packages.gz"
    text = gzip.decompress(_download_bytes(index, timeout=30)).decode("utf-8", "replace")
    best: tuple[str, str] | None = None
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ": " in line:
                key, _, value = line.partition(": ")
                fields[key] = value
        if fields.get("Package") != _ODBC_PACKAGE:
            continue
        filename = fields.get("Filename")
        version = fields.get("Version")
        if not filename:
            continue
        if best is None or _version_key(version) > _version_key(best[0]):
            best = (version, filename)
    return f"{base}/{best[1]}" if best else None


def _download_bytes(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "APEX-integrations/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _os_release() -> dict:
    out = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return out


def _version_key(value: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", value or ""))


def main(argv=None) -> int:
    """CLI: provision the SQL Server ODBC driver if it is missing.

    Exposed as ``python -m integrations.deps`` and via
    ``scripts/install_sql_drivers.py`` so setup can pre-warm it. Idempotent;
    exits 0 when a driver is ready, 1 when it could not be provided."""
    import sys as _sys
    _ = argv if argv is not None else _sys.argv[1:]
    if not auto_install_enabled():
        print("INTEGRATIONS_AUTO_INSTALL is off; nothing to do (no changes made)")
        return 0
    path = resolve_sqlserver_driver("")
    if path:
        print(f"SQL Server ODBC driver ready: {path}")
        return 0
    print("could not provision a SQL Server ODBC driver; install `msodbcsql18` "
          "manually or set INTEGRATIONS_ODBC_DEB_URL", file=_sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

