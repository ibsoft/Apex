"""Permission-respecting file discovery and user-bound download tickets."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
import stat
import time

from flask import jsonify, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from tools.base import Tool, ToolContext
from public_urls import public_url

# Virtual filesystems can expose streams/devices rather than ordinary files.
VIRTUAL_ROOTS = (Path('/proc'), Path('/sys'), Path('/dev'), Path('/run'))
MAX_ENTRIES = 100_000
SEARCH_SECONDS = 10
LINK_SECONDS = 3600


def resolve_search_root(value):
    """Resolve an absolute path or a named home folder using this machine's settings."""
    text = str(value).strip()
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    if not text or '/' in text or text in ('.', '..'):
        raise ValueError('Use an absolute directory or the name of a folder in your home directory.')
    home = Path.home()
    config_dir = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    try:
        for line in (config_dir / 'user-dirs.dirs').read_text().splitlines():
            match = re.fullmatch(r'\s*XDG_([A-Z_]+)_DIR="([^"\n]*)"\s*', line)
            if not match:
                continue
            directory = match.group(2).replace('${HOME}', str(home)).replace('$HOME', str(home))
            # Parse desktop settings as data, never as shell code.
            if '$' in directory or '`' in directory or not Path(directory).is_absolute():
                continue
            target = Path(directory)
            names = {match.group(1).replace('_', ' ').casefold(), target.name.casefold()}
            if text.casefold() in names:
                return target.resolve()
    except (OSError, UnicodeError):
        pass
    # Prefer exact spelling, then case-insensitive matching of actual home folders.
    direct = home / text
    if direct.is_dir():
        return direct.resolve()
    try:
        matches = [child for child in home.iterdir()
                   if child.name.casefold() == text.casefold() and child.is_dir()]
    except OSError:
        matches = []
    if len(matches) == 1:
        return matches[0].resolve()
    raise ValueError('Folder not found or ambiguous; provide its absolute path.')


def allowed_roots(config):
    configured = getattr(config, 'FILE_SEARCH_ROOTS', '/')
    return [Path(root).expanduser().resolve() for root in configured.split(os.pathsep) if root.strip()]


def permitted(path, config, roots=None):
    return any(path.is_relative_to(root) for root in (allowed_roots(config) if roots is None else roots)) and not any(
        path == root or path.is_relative_to(root) for root in VIRTUAL_ROOTS)


def open_regular(path):
    """Open without following any symlink components, including renamed parents."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('An absolute file path is required.')
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Only regular files can be downloaded.')
        handle = os.fdopen(file_descriptor, 'rb')
    except Exception:
        os.close(file_descriptor)
        raise
    return handle, info


def fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def signer(config):
    return URLSafeTimedSerializer(config.SECRET_KEY, salt='apex-file-download-v1')


def search_files(args, ctx: ToolContext, config):
    if not ctx.user_id:
        return {'error': 'Sign in before searching local files.'}
    query = str(args.get('query') or '').strip()
    if not query or len(query) > 256 or '/' in query or '\x00' in query:
        return {'error': 'Enter a filename or glob pattern (up to 256 characters); use root for directories.'}
    try:
        limit = max(1, min(int(args.get('limit', 20)), 100))
    except (TypeError, ValueError):
        return {'error': 'limit must be a number from 1 to 100.'}
    try:
        download_base = public_url(config, '/api/files/download/')
    except ValueError:
        return {'error': 'BASE_URL must be configured as a valid public http(s) server URL.'}
    root_arg = args.get('root')
    try:
        configured_roots = allowed_roots(config)
        roots = [resolve_search_root(root_arg)] if root_arg else configured_roots
        if not roots or any(not permitted(root, config, configured_roots) for root in roots):
            return {'error': 'The requested directory is outside the configured search roots or is a virtual filesystem.'}
        if any(not root.is_dir() for root in roots):
            return {'error': 'The search directory does not exist or is inaccessible.',
                    'roots': [str(root) for root in roots]}
    except (OSError, ValueError, RuntimeError, TypeError):
        return {'error': 'The requested search directory is invalid or inaccessible.'}
    pattern = query.casefold() if any(char in query for char in '*?[') else '*' + query.casefold() + '*'
    pending = list(reversed(roots))
    results, seen = [], set()
    scanned, skipped, truncated = 0, 0, False
    deadline = time.monotonic() + SEARCH_SECONDS
    while pending:
        if scanned >= MAX_ENTRIES or time.monotonic() >= deadline:
            truncated = True
            break
        directory = pending.pop()
        if directory in seen:
            continue
        seen.add(directory)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES or time.monotonic() >= deadline:
                        truncated = True
                        break
                    path = Path(entry.path)
                    try:
                        if entry.is_symlink() or not permitted(path, config, configured_roots):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False) and fnmatch.fnmatchcase(entry.name.casefold(), pattern):
                            handle, info = open_regular(path)
                            handle.close()
                            token = signer(config).dumps({'user': str(ctx.user_id), 'path': str(path),
                                                          'fingerprint': fingerprint(info)})
                            results.append({'name': entry.name, 'path': str(path), 'size_bytes': info.st_size,
                                            'download_url': download_base + token})
                            if len(results) >= limit:
                                truncated = True
                                break
                    except (OSError, ValueError):
                        skipped += 1
        except OSError:
            skipped += 1
        if truncated:
            break
    return {'files': results, 'roots': [str(root) for root in roots], 'scanned_entries': scanned,
            'skipped_inaccessible': skipped, 'truncated': truncated, 'link_expires_in_seconds': LINK_SECONDS,
            'note': 'Search covers readable regular files; symlinks and virtual filesystems are skipped. '
                    'If truncated, narrow the root directory or filename pattern.'}


def build_file_tools(config):
    return [Tool(name='file_search', description='Find readable local files by filename or glob and return download links. '
                 'root accepts an absolute path or a folder name resolved from the OS user home and desktop settings. '
                 'Otherwise search the configured system roots. Does not read file contents.',
                 parameters={'type': 'object', 'properties': {
                     'query': {'type': 'string', 'description': 'Filename fragment or glob, e.g. invoice or *.pdf.'},
                     'root': {'type': 'string', 'description': 'Directory to search recursively: an absolute path, ~/path, or a folder name from the user home or desktop settings.'},
                     'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20},
                 }, 'required': ['query'], 'additionalProperties': False},
                 handler=lambda args, ctx: json.dumps(search_files(args, ctx, config), ensure_ascii=False))]


def register_file_routes(app, require_user, config):
    @app.get('/api/files/download/<token>')
    def download_searched_file(token):
        user = require_user()
        if user is None:
            return jsonify({'error': 'Sign in to download this file.'}), 401
        try:
            ticket = signer(config).loads(token, max_age=LINK_SECONDS)
        except SignatureExpired:
            return jsonify({'error': 'Download link expired. Search for the file again.'}), 410
        except BadSignature:
            return jsonify({'error': 'Invalid download link.'}), 404
        if not isinstance(ticket, dict) or ticket.get('user') != str(user['id']):
            return jsonify({'error': 'This download link belongs to a different user.'}), 403
        try:
            path = Path(ticket['path'])
            if not permitted(path, config):
                return jsonify({'error': 'This file is no longer within the permitted search roots.'}), 403
            handle, info = open_regular(path)
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify({'error': 'File is unavailable or no longer readable.'}), 404
        if fingerprint(info) != ticket.get('fingerprint'):
            handle.close()
            return jsonify({'error': 'File changed. Search again to get a new download link.'}), 409
        try:
            name = ''.join(char for char in path.name if ord(char) >= 32 and ord(char) != 127) or 'download'
            response = send_file(handle, as_attachment=True, download_name=name,
                                 mimetype='application/octet-stream', conditional=False, max_age=0)
        except Exception:
            handle.close()
            raise
        response.content_length = info.st_size
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.call_on_close(handle.close)
        return response
