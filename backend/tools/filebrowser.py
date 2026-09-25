"""File manager backend: browse, create, rename, trash/restore, copy/move with
explicit conflict resolution, upload and signed downloads - always confined to
the configured FILE_SEARCH_ROOTS and never following symlink escapes.

Conventions
-----------
* Every operation re-validates authentication (`require_user`) and path
  containment with the same allowed-roots checks as the file_search tool.
* Symlinks are shown in listings but never dereferenced for operations:
  transfers ignore them and sensitive ops refuse them.
* Copy/move run as cancellable background jobs with progress; name conflicts
  are resolved explicitly with replace / skip / keep-both. Nothing is ever
  overwritten silently and nothing is ever deleted permanently - "delete"
  moves items into a per-user trash folder that can be restored.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import stat
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from public_urls import public_url
from tools.file_search import (
    LINK_SECONDS,
    allowed_roots,
    fingerprint,
    permitted,
    signer as file_signer,
)

FM_SALT = "apex-filemanager-v1"
JOB_TTL_SECONDS = 3600
FM_FILE_TTL_SECONDS = 3600
UPLOAD_MAX_FILES = 1000

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_TRASH_LOCK = threading.Lock()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
    ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".sql",
    ".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".htm", ".xml",
}


class _FmError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _data_root(config):
    return Path(
        getattr(config, "DATA_DIR", Path(__file__).resolve().parents[1] / "data")
    ).resolve()


def _generated_fm_dir(config, user_id: str) -> Path:
    path = (_data_root(config) / "generated" / "fm" / str(user_id)).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _trash_root(config, user_id: str) -> Path:
    return (_data_root(config) / "trash" / str(user_id)).resolve()


def _name_ok(name) -> bool:
    name = str(name or "")
    if not name or len(name) > 255:
        return False
    if name in (".", ".."):
        return False
    if any(char in name for char in ("/", "\\", "\x00", "\n", "\r")):
        return False
    if name.startswith(" ") or name.endswith(" "):
        return False
    return True


def _resolve(config, raw, reject_symlink=False):
    """Resolve an absolute client path and verify it stays inside the roots."""
    text = str(raw or "").strip()
    if not text:
        raise _FmError("A path is required.", 400)
    path = Path(text)
    if not path.is_absolute():
        raise _FmError("Absolute paths only.", 400)
    if ".." in path.parts or "\x00" in text:
        raise _FmError("Path traversal is not allowed.", 400)
    if reject_symlink and (path.exists() or path.is_symlink()) and path.is_symlink():
        raise _FmError("Symlinks cannot be operated on.", 403)
    resolved = path.resolve()
    if not permitted(resolved, config):
        raise _FmError("Path is outside the configured file roots.", 403)
    return resolved


def _kind_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in {".docx", ".docm"}:
        return "docx"
    if ext in {".xlsx", ".xlsm"}:
        return "xlsx"
    if ext in {".pptx", ".pptm"}:
        return "pptx"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "other"


def _entry(parent: Path, name: str):
    path = parent / name
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {
            "name": name, "path": str(path), "is_dir": False, "is_symlink": True,
            "size_bytes": None, "mtime_ns": info.st_mtime_ns, "kind": "link",
        }
    is_dir = stat.S_ISDIR(info.st_mode)
    return {
        "name": name, "path": str(path), "is_dir": is_dir, "is_symlink": False,
        "size_bytes": None if is_dir else info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "kind": "folder" if is_dir else _kind_for(name),
    }


def _dir_size(path: Path):
    try:
        return sum(
            item.stat().st_size
            for root_dir, _, files in os.walk(path, followlinks=False)
            for item in (Path(root_dir) / name for name in files)
        )
    except OSError:
        return None


def _file_ticket(config, user_id: str, path: Path) -> str:
    info = path.stat()
    token = file_signer(config).dumps(
        {"user": str(user_id), "path": str(path), "fingerprint": fingerprint(info)}
    )
    return public_url(config, f"/api/files/download/{token}")


def _fm_signer(config):
    key = getattr(config, "SECRET_KEY", "dev-change-me")
    return URLSafeTimedSerializer(key, salt=FM_SALT)


def _cleanup_old(directory: Path, ttl_seconds: int):
    if not directory.exists():
        return
    cutoff = time.time() - ttl_seconds
    for item in directory.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass


def _unique_dest(dest: Path) -> Path:
    if not dest.exists() and not dest.is_symlink():
        return dest
    stem, suffix = dest.name, ""
    dot = dest.name.rfind(".")
    if dot > 0 and not dest.is_dir():
        stem, suffix = dest.name[:dot], dest.name[dot:]
    index = 1
    while True:
        candidate = dest.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        index += 1


def _item_total(sources) -> int:
    total = 0
    for source in sources:
        total += 1
        if source.is_dir():
            for _, dirnames, filenames in os.walk(source, followlinks=False):
                total += len(dirnames) + len(filenames)
    return total


def _remove_path(path: Path):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_tree(src: Path, dst: Path, job: dict, replace: bool):
    if replace and (dst.exists() or dst.is_symlink()):
        _remove_path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    job["done"] += 1
    with os.scandir(src) as entries:
        for entry in entries:
            if job["cancel"]:
                return False
            child = Path(entry.path)
            if entry.is_symlink():
                continue
            child_dest = dst / entry.name
            if entry.is_dir(follow_symlinks=False):
                if not _copy_tree(child, child_dest, job, False):
                    return False
            else:
                child_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, child_dest)
                job["done"] += 1
    return True


def _run_job(job: dict, action: str, sources, destination: Path, conflicts: dict, config):
    try:
        for src in sources:
            if job["cancel"]:
                break
            job["current"] = str(src)
            rule = (conflicts or {}).get(str(src))
            dest = destination / src.name
            try:
                if dest.exists() or dest.is_symlink():
                    if rule == "skip":
                        job["results"].append({"path": str(src), "ok": True, "skipped": True})
                        continue
                    if rule == "replace":
                        _remove_path(dest)
                    elif rule == "keep_both":
                        dest = _unique_dest(dest)
                elif rule == "replace":
                    pass
                if action == "move":
                    if src.resolve() == dest.resolve():
                        job["errors"].append({"path": str(src), "error": "Cannot move an item into itself."})
                        continue
                    try:
                        os.rename(src, dest)
                        job["done"] += 1
                    except OSError:
                        parent = dest.parent
                        parent.mkdir(parents=True, exist_ok=True)
                        copied = _copy_tree(src, dest, job, True)
                        if copied and not job["cancel"]:
                            _remove_path(src)
                else:
                    if src.is_dir():
                        _copy_tree(src, dest, job, rule == "replace")
                    else:
                        if (dest.exists() or dest.is_symlink()) and rule == "replace":
                            _remove_path(dest)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        job["done"] += 1
                job["results"].append({"path": str(src), "ok": True})
            except Exception as exc:
                job["errors"].append({"path": str(src), "error": str(exc)})
        if job["cancel"]:
            job["status"] = "cancelled"
        else:
            job["status"] = "done"
    except Exception as exc:
        job["status"] = "failed"
        job["message"] = str(exc)
    finally:
        job["finished"] = time.time()


def _start_job(owner: str, action: str, sources, destination, conflicts: dict, config) -> dict:
    cutoff_seconds = getattr(config, "FM_JOB_TTL_SECONDS", JOB_TTL_SECONDS)
    job = {
        "id": uuid.uuid4().hex, "owner": str(owner), "action": action,
        "status": "running", "message": "", "total": _item_total(sources),
        "done": 0, "current": "", "cancel": False, "errors": [], "results": [],
        "started": time.time(), "finished": None,
    }
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
        cutoff = time.time() - cutoff_seconds
        for key in [key for key, value in _JOBS.items()
                    if value["finished"] and value["finished"] < cutoff]:
            _JOBS.pop(key, None)
    thread = threading.Thread(
        target=_run_job, args=(job, action, sources, destination, conflicts, config),
        daemon=True,
    )
    thread.start()
    return job


def register_filebrowser_routes(app, require_user, config):
    fm_file_ttl = getattr(config, "FM_FILE_TTL_SECONDS", FM_FILE_TTL_SECONDS)
    upload_max_files = getattr(config, "FM_UPLOAD_MAX_FILES", UPLOAD_MAX_FILES)

    def me():
        user = require_user()
        if not user:
            return None
        return user

    @app.get("/api/fm/roots")
    def fm_roots():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to browse files."}), 401
        roots = allowed_roots(config)
        home = Path.home()
        home_entry = {"path": str(home), "name": "Home"} if permitted(home, config) else None
        return jsonify({
            "roots": [{"path": str(root), "name": root.name or root.anchor, "is_root": True}
                      for root in roots],
            "home": home_entry,
        })

    @app.get("/api/fm/list")
    def fm_list():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to browse files."}), 401
        raw = request.args.get("path") or ""
        try:
            target = _resolve(config, raw)
        except _FmError as exc:
            return jsonify({"error": exc.message}), exc.status
        if not target.exists():
            return jsonify({"error": "This directory does not exist."}), 404
        if target.is_symlink() or not target.is_dir():
            return jsonify({"error": "This is not a directory."}), 400
        if not os.access(target, os.R_OK | os.X_OK):
            return jsonify({"error": "This directory is not readable."}), 403
        entries, skipped = [], 0
        try:
            with os.scandir(target) as iterator:
                for entry in iterator:
                    try:
                        entries.append(_entry(target, entry.name))
                    except (OSError, ValueError):
                        skipped += 1
        except OSError as exc:
            return jsonify({"error": f"Could not read the directory: {exc}"}), 500
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
        parent = str(target.parent) if target != Path(target.anchor) else None
        return jsonify({
            "path": str(target), "name": target.name or target.anchor,
            "parent": parent, "entries": entries, "skipped_inaccessible": skipped,
        })

    @app.get("/api/fm/stat")
    def fm_stat():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to browse files."}), 401
        raw = request.args.get("path") or ""
        try:
            target = _resolve(config, raw, reject_symlink=True)
        except _FmError as exc:
            return jsonify({"error": exc.message}), exc.status
        if not target.exists():
            return jsonify({"error": "This item does not exist."}), 404
        info = target.lstat()
        entry = {
            "name": target.name, "path": str(target), "is_dir": target.is_dir(),
            "is_symlink": False, "size_bytes": None if target.is_dir() else info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "kind": "folder" if target.is_dir() else _kind_for(target.name),
        }
        return jsonify({"entry": entry, "permissions": oct(info.st_mode & 0o7777)})

    @app.post("/api/fm/mkdir")
    def fm_mkdir():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to create folders."}), 401
        body = request.get_json(silent=True) or {}
        try:
            target = _resolve(config, body.get("path"), reject_symlink=True)
        except _FmError as exc:
            return jsonify({"error": exc.message}), exc.status
        if target.exists() or target.is_symlink():
            return jsonify({"error": "A file or folder with this name already exists."}), 409
        if not _name_ok(target.name):
            return jsonify({"error": "Invalid folder name."}), 400
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            return jsonify({"error": "The parent folder does not exist."}), 404
        try:
            parent.mkdir(parents=True, exist_ok=True)
            target.mkdir()
        except OSError as exc:
            return jsonify({"error": f"Could not create the folder: {exc}"}), 500
        return jsonify({"entry": _entry(parent, target.name)})

    @app.post("/api/fm/rename")
    def fm_rename():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to rename items."}), 401
        body = request.get_json(silent=True) or {}
        new_name = str(body.get("new_name") or "")
        if not _name_ok(new_name):
            return jsonify({"error": "Invalid name."}), 400
        try:
            target = _resolve(config, body.get("path"), reject_symlink=True)
        except _FmError as exc:
            return jsonify({"error": exc.message}), exc.status
        if not target.exists():
            return jsonify({"error": "This item does not exist."}), 404
        dest = target.parent / new_name
        if dest.exists() or dest.is_symlink():
            return jsonify({"error": "A file or folder with this name already exists."}), 409
        try:
            os.rename(target, dest)
        except OSError as exc:
            return jsonify({"error": f"Could not rename: {exc}"}), 500
        return jsonify({"entry": _entry(dest.parent, dest.name)})

    @app.post("/api/fm/trash")
    def fm_trash():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to delete items."}), 401
        body = request.get_json(silent=True) or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or len(paths) > upload_max_files:
            return jsonify({"error": "Provide a list of paths to delete."}), 400
        trash_root = _trash_root(config, str(user["id"]))
        trash_root.mkdir(parents=True, exist_ok=True)
        results = []
        with _TRASH_LOCK:
            manifest_path = trash_root / "manifest.json"
            manifest = []
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except (OSError, ValueError):
                    manifest = []
            known = {entry["id"] for entry in manifest}
            for raw in paths:
                try:
                    target = _resolve(config, raw, reject_symlink=True)
                except _FmError as exc:
                    results.append({"path": raw, "ok": False, "error": exc.message})
                    continue
                if not target.exists():
                    results.append({"path": raw, "ok": False, "error": "This item does not exist."})
                    continue
                slot = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
                while slot in known:
                    slot = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
                parked = trash_root / slot
                try:
                    shutil.move(str(target), str(parked))
                except OSError as exc:
                    results.append({"path": raw, "ok": False, "error": str(exc)})
                    continue
                known.add(slot)
                manifest.append({
                    "id": slot, "original": str(target), "name": target.name,
                    "at": int(time.time()),
                })
                results.append({"path": raw, "ok": True, "parked": str(parked)})
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
        return jsonify({"ok": True, "results": results})

    @app.get("/api/fm/trash")
    def fm_list_trash():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to view the trash."}), 401
        trash_root = _trash_root(config, str(user["id"]))
        manifest_path = trash_root / "manifest.json"
        entries = []
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, ValueError):
                manifest = []
            for item in manifest:
                parked = trash_root / item["id"]
                size = None
                if parked.is_dir() and not parked.is_symlink():
                    size = _dir_size(parked)
                elif parked.is_file() and not parked.is_symlink():
                    try:
                        size = parked.stat().st_size
                    except OSError:
                        size = None
                entries.append({
                    "id": item["id"], "original": item["original"],
                    "name": item["name"], "at": item.get("at", 0),
                    "exists": parked.exists(), "size_bytes": size,
                })
        return jsonify({"trash": entries})

    @app.post("/api/fm/restore")
    def fm_restore():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to restore items."}), 401
        body = request.get_json(silent=True) or {}
        ids = body.get("ids") or []
        trash_root = _trash_root(config, str(user["id"]))
        results = []
        with _TRASH_LOCK:
            manifest_path = trash_root / "manifest.json"
            manifest = []
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except (OSError, ValueError):
                    manifest = []
            kept = []
            for item in manifest:
                if item["id"] in ids:
                    parked = trash_root / item["id"]
                    original = Path(item["original"])
                    try:
                        if original.exists() or original.is_symlink():
                            results.append({"id": item["id"], "ok": False,
                                            "error": "Something already exists at the original location."})
                            kept.append(item)
                            continue
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(parked), str(original))
                        results.append({"id": item["id"], "ok": True, "path": str(original)})
                    except OSError as exc:
                        results.append({"id": item["id"], "ok": False, "error": str(exc)})
                        kept.append(item)
                else:
                    kept.append(item)
            manifest_path.write_text(json.dumps(kept, ensure_ascii=False))
        return jsonify({"ok": True, "results": results})

    @app.post("/api/fm/trash/cleanup")
    def fm_trash_cleanup():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to empty the trash."}), 401
        body = request.get_json(silent=True) or {}
        ids = set(body.get("ids") or [])
        clear_all = bool(body.get("all"))
        trash_root = _trash_root(config, str(user["id"]))
        results = []
        with _TRASH_LOCK:
            manifest_path = trash_root / "manifest.json"
            manifest = []
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except (OSError, ValueError):
                    manifest = []
            kept = []
            for item in manifest:
                if clear_all or item["id"] in ids:
                    parked = trash_root / item["id"]
                    try:
                        _remove_path(parked)
                        results.append({"id": item["id"], "ok": True})
                    except OSError as exc:
                        results.append({"id": item["id"], "ok": False, "error": str(exc)})
                        kept.append(item)
                else:
                    kept.append(item)
            manifest_path.write_text(json.dumps(kept, ensure_ascii=False))
        return jsonify({"ok": True, "results": results})

    @app.post("/api/fm/jobs")
    def fm_jobs_create():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to copy or move files."}), 401
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("copy", "move"):
            return jsonify({"error": "action must be copy or move."}), 400
        raw_sources = body.get("sources") or []
        raw_destination = body.get("destination")
        conflicts = body.get("conflicts") or {}
        if not isinstance(raw_sources, list) or not raw_sources:
            return jsonify({"error": "Provide at least one source path."}), 400
        try:
            destination = _resolve(config, raw_destination, reject_symlink=True)
        except _FmError as exc:
            return jsonify({"error": exc.message}), exc.status
        if not destination.is_dir() or destination.is_symlink():
            return jsonify({"error": "The destination must be an existing folder."}), 400
        sources = []
        for raw in raw_sources:
            try:
                source = _resolve(config, raw, reject_symlink=True)
            except _FmError as exc:
                return jsonify({"error": exc.message}), exc.status
            if not source.exists():
                return jsonify({"error": f"The source no longer exists: {raw}"}), 404
            sources.append(source)
        source_set = [str(source) for source in sources]
        if len(set(source_set)) != len(source_set):
            return jsonify({"error": "The same source appears more than once."}), 400
        for source in sources:
            if source == destination or destination.is_relative_to(source):
                return jsonify({"error": "Cannot copy/move a folder into itself or its own subfolder."}), 400
        for a in sources:
            for b in sources:
                if a != b and (a.is_relative_to(b) or b.is_relative_to(a)):
                    return jsonify({"error": "Overlapping sources are not supported."}), 400
        unresolved = []
        plan = []
        for source in sources:
            dest = destination / source.name
            exists = dest.exists() or dest.is_symlink()
            rule = (conflicts or {}).get(str(source))
            plan.append((source, dest))
            if exists and rule not in ("replace", "skip", "keep_both"):
                unresolved.append({
                    "source": str(source), "target": str(dest),
                    "is_dir": source.is_dir(),
                })
        if unresolved:
            return jsonify({"conflicts": unresolved,
                            "error": "Some names already exist in the destination. Choose how to handle them."}), 409
        job = _start_job(str(user["id"]), action, sources, destination, conflicts or {}, config)
        return jsonify({"job_id": job["id"], "total": job["total"]})

    def _job_for(user_id: str, job_id: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job or job["owner"] != str(user_id):
            return None
        return job

    @app.get("/api/fm/jobs/<job_id>")
    def fm_jobs_status(job_id: str):
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to track jobs."}), 401
        job = _job_for(str(user["id"]), job_id)
        if not job:
            return jsonify({"error": "This job does not exist."}), 404
        return jsonify({
            "id": job["id"], "action": job["action"], "status": job["status"],
            "message": job["message"], "total": job["total"], "done": job["done"],
            "current": job["current"], "cancelled": job["cancel"],
            "errors": job["errors"], "results": job["results"],
        })

    @app.post("/api/fm/jobs/<job_id>/cancel")
    def fm_jobs_cancel(job_id: str):
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to cancel jobs."}), 401
        job = _job_for(str(user["id"]), job_id)
        if not job:
            return jsonify({"error": "This job does not exist."}), 404
        if job["status"] == "running":
            job["cancel"] = True
        return jsonify({"ok": True, "status": job["status"]})

    @app.post("/api/fm/tickets")
    def fm_tickets():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to download files."}), 401
        body = request.get_json(silent=True) or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths or len(paths) > upload_max_files:
            return jsonify({"error": "Provide a list of file paths."}), 400
        tickets = {}
        for raw in paths:
            try:
                target = _resolve(config, raw, reject_symlink=True)
            except _FmError as exc:
                return jsonify({"error": exc.message}), exc.status
            if not target.is_file() or target.is_symlink():
                return jsonify({"error": f"Not a readable file: {raw}"}), 400
            tickets[str(target)] = _file_ticket(config, str(user["id"]), target)
        return jsonify({"tickets": tickets, "expires_in_seconds": LINK_SECONDS})

    @app.post("/api/fm/zip")
    def fm_zip():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to download files."}), 401
        body = request.get_json(silent=True) or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths or len(paths) > upload_max_files:
            return jsonify({"error": "Provide a list of file paths."}), 400
        files = []
        for raw in paths:
            try:
                target = _resolve(config, raw, reject_symlink=True)
            except _FmError as exc:
                return jsonify({"error": exc.message}), exc.status
            if not target.is_file() or target.is_symlink():
                return jsonify({"error": f"Not a readable file: {raw}"}), 400
            files.append(target)
        folder = _generated_fm_dir(config, str(user["id"]))
        _cleanup_old(folder, fm_file_ttl)
        name = f"files-{int(time.time() * 1000)}.zip"
        path = folder / name
        used = set()
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for index, target in enumerate(files, start=1):
                    arcname = target.name
                    if arcname in used:
                        stem, suffix = arcname.rsplit(".", 1) if "." in arcname else (arcname, "")
                        arcname = f"{stem}-{index}" + (f".{suffix}" if suffix else "")
                    used.add(arcname)
                    archive.write(target, arcname=arcname)
        except OSError as exc:
            return jsonify({"error": f"Could not build the archive: {exc}"}), 500
        info = path.stat()
        token = _fm_signer(config).dumps(
            {"user": str(user["id"]), "path": str(path), "filename": name,
             "fingerprint": fingerprint(info)}
        )
        return jsonify({"filename": name, "download_url": public_url(config, f"/api/fm/download/{token}"),
                        "expires_in_seconds": fm_file_ttl})

    @app.get("/api/fm/download/<token>")
    def fm_download(token: str):
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to download this file."}), 401
        try:
            ticket = _fm_signer(config).loads(token, max_age=fm_file_ttl)
        except SignatureExpired:
            return jsonify({"error": "Download link expired. Try again."}), 410
        except BadSignature:
            return jsonify({"error": "Invalid download link."}), 404
        if not isinstance(ticket, dict) or ticket.get("user") != str(user["id"]):
            return jsonify({"error": "This download link belongs to a different user."}), 403
        try:
            path = Path(ticket["path"]).resolve()
            base_dir = _generated_fm_dir(config, str(user["id"]))
            if not path.is_relative_to(base_dir) or not path.is_file():
                return jsonify({"error": "File is unavailable."}), 404
            info = path.stat()
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify({"error": "File is unavailable."}), 404
        if fingerprint(info) != ticket.get("fingerprint"):
            return jsonify({"error": "File changed. Try again."}), 409
        try:
            download_name = ticket.get("filename") or path.name
            mimetype = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
            response = send_file(path, as_attachment=True, download_name=download_name,
                                 mimetype=mimetype, conditional=False, max_age=0)
        except Exception:
            return jsonify({"error": "Could not send file."}), 500
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post("/api/fm/upload")
    def fm_upload():
        user = me()
        if user is None:
            return jsonify({"error": "Sign in to upload files."}), 401
        destination_raw = (request.form.get("destination") or "").strip()
        on_conflict = (request.form.get("on_conflict") or "skip").strip()
        if on_conflict not in ("replace", "skip", "keep_both"):
            on_conflict = "skip"
        try:
            destination = _resolve(config, destination_raw, reject_symlink=True)
        except _FmError as exc:
            return jsonify({"error": exc.message}), exc.status
        if not destination.is_dir() or destination.is_symlink():
            return jsonify({"error": "The destination must be an existing folder."}), 400
        files = request.files.getlist("files")
        if not files or len(files) > upload_max_files:
            return jsonify({"error": "Provide files to upload."}), 400
        results = []
        for index, uploaded in enumerate(files):
            rel = uploaded.filename or getattr(uploaded, "webkitRelativePath", None) or ""
            segments = [segment for segment in rel.replace("\\", "/").split("/") if segment]
            if not segments or not all(_name_ok(segment) for segment in segments):
                results.append({"name": rel, "ok": False, "error": "Invalid file path."})
                continue
            target = destination.joinpath(*segments)
            try:
                if not permitted(target.resolve(), config):
                    results.append({"name": rel, "ok": False,
                                    "error": "Path is outside the configured file roots."})
                    continue
            except (OSError, ValueError):
                results.append({"name": rel, "ok": False, "error": "Invalid file path."})
                continue
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    results.append({"name": rel, "ok": False, "error": "A folder with this name exists."})
                    continue
                if on_conflict == "replace":
                    target.unlink()
                elif on_conflict == "keep_both":
                    target = _unique_dest(target)
                else:
                    results.append({"name": rel, "ok": False, "error": "skipped", "skipped": True})
                    continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                uploaded.save(str(target))
                size = target.stat().st_size
            except OSError as exc:
                try:
                    if target.exists() and not target.is_dir():
                        target.unlink()
                except OSError:
                    pass
                results.append({"name": rel, "ok": False, "error": str(exc)})
                continue
            results.append({"name": rel, "ok": True, "size_bytes": size})
        return jsonify({"ok": True, "results": results})