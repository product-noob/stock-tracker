"""Shared trust boundaries for attachment/API downloads.

Binding rules implemented here (review ST-C05 / ST-D03):

* every attachment URL must be HTTPS on an allowlisted exchange host —
  a compromised upstream payload must not point our fetchers anywhere;
* downloads are size-capped before persistence so a hostile or broken
  source cannot exhaust disk or memory;
* API responses are size-capped for the same reason.
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

#: Hosts each source is allowed to serve attachment bytes from.
ATTACHMENT_HOSTS: dict[str, frozenset[str]] = {
    "nse_api": frozenset({
        "nsearchives.nseindia.com",
        "www.nseindia.com",
        "nseindia.com",
    }),
    "bse_api": frozenset({
        "www.bseindia.com",
        "bseindia.com",
    }),
}

#: Hard cap for one downloaded attachment (25 MiB — filings are far smaller;
#  anything larger is treated as a protocol failure, never persisted).
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

#: Hard cap for one API response body (20 MiB).
MAX_API_BYTES = 20 * 1024 * 1024


def attachment_url_error(source: str, url: str) -> Optional[str]:
    """Return a rejection reason for ``url``, or ``None`` when allowed."""
    hosts = ATTACHMENT_HOSTS.get(source)
    if hosts is None:
        return f"unknown source {source!r}"
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if parsed.scheme != "https":
        return f"scheme {parsed.scheme!r} is not https"
    if parsed.username or parsed.password:
        return "URL carries credentials"
    host = (parsed.hostname or "").lower()
    if host not in hosts:
        return f"host {host!r} is not an allowlisted {source} attachment host"
    return None


def download_size_error(nbytes: int) -> Optional[str]:
    """Return a rejection reason when a download exceeds the hard cap."""
    if nbytes > MAX_DOWNLOAD_BYTES:
        return (f"download of {nbytes} bytes exceeds the "
                f"{MAX_DOWNLOAD_BYTES}-byte cap")
    return None


def api_body_size_error(nbytes: int) -> Optional[str]:
    """Return a rejection reason when an API response exceeds the hard cap."""
    if nbytes > MAX_API_BYTES:
        return (f"API response of {nbytes} bytes exceeds the "
                f"{MAX_API_BYTES}-byte cap")
    return None
