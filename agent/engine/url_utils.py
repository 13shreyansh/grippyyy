"""URL normalization helpers for demo/operator-entered form URLs."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse, urlunparse

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_PREFIX_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_HOSTLIKE_RE = re.compile(
    r"^(localhost(?::\d+)?|"
    r"(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?|"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:\:\d+)?)(?:[/?#].*)?$"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ERROR = "Invalid URL. Use http(s):// or enter a bare domain like httpbin.org"


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_valid_host(host: str) -> bool:
    if not host:
        return False
    lowered = host.lower()
    if lowered in _LOCAL_HOSTS or _is_ip_address(lowered):
        return True
    return "." in lowered and all(part for part in lowered.split("."))


def normalize_demo_url(raw: str) -> str:
    """Normalize a URL entered in the demo form UI."""
    value = raw.strip()
    if not value:
        raise ValueError(_ERROR)

    if value.startswith("//"):
        value = f"https:{value}"
    elif value.lower().startswith("localhost:"):
        value = f"http://{value}"
    elif not _SCHEME_RE.match(value):
        scheme_like = _PREFIX_SCHEME_RE.match(value)
        if scheme_like and scheme_like.group(1).lower() not in _LOCAL_HOSTS:
            raise ValueError(_ERROR)
        if not _HOSTLIKE_RE.match(value):
            raise ValueError(_ERROR)
        host = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
        scheme = "http" if host.lower() in _LOCAL_HOSTS or _is_ip_address(host) else "https"
        value = f"{scheme}://{value}"

    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(_ERROR)
    if not parsed.netloc or not _is_valid_host(parsed.hostname or ""):
        raise ValueError(_ERROR)

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "",
            parsed.params or "",
            parsed.query or "",
            parsed.fragment or "",
        )
    )


def validate_demo_url(raw: str) -> str:
    """Validate and return a normalized URL."""
    return normalize_demo_url(raw)
