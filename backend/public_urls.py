"""URLs for files and media served by this APEX backend."""
from urllib.parse import urlsplit


def public_url(config, route):
    base = config.BASE_URL.strip().rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('BASE_URL must be an http(s) server URL without credentials, query, or fragment.')
    parsed.port  # Validate malformed ports too.
    return base + '/' + route.lstrip('/')
