from __future__ import annotations

import ipaddress
import os
from typing import Optional
from urllib.parse import urlparse

from fastapi import Header, HTTPException


def verify_bearer_secret(authorization: Optional[str] = Header(default=None)) -> None:
    """Protect operator-only endpoints when CRON_SECRET is configured."""
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return
    if authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def validate_public_https_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL must be absolute HTTPS")

    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="URL host is not allowed")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        raise HTTPException(status_code=400, detail="URL host is not allowed")
    return url
