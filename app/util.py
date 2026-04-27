from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import string
from urllib.parse import urlparse


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_utc_aware(ts: dt.datetime) -> dt.datetime:
    """Normalize datetimes from SQLite (often naive) for arithmetic with now_utc()."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def domain_from_url(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def hostname_from_url(url: str) -> str:
    """Full hostname lowercased, single leading 'www.' stripped (subdomains preserved)."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def hostname_matches_source(hostname: str, source_domain: str) -> bool:
    """
    True if URL host is exactly the configured source domain or a subdomain of it
    (e.g. news.bbc.co.uk ↔ bbc.co.uk). Used for RSS item URL guardrails.
    """
    h = (hostname or "").strip().lower()
    s = (source_domain or "").strip().lower()
    if not h or not s:
        return False
    if h == s:
        return True
    return h.endswith("." + s)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_article_id(url: str, published_at: dt.datetime) -> str:
    # stable ID across runs; avoids cuid dependency
    base = f"{url}|{published_at.isoformat()}"
    return sha256_hex(base)[:32]


def new_id(prefix: str) -> str:
    # short, collision-resistant enough for MVP
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(20))
    return f"{prefix}_{suffix}"


def getenv_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def format_lag_seconds(seconds: object) -> str:
    """
    Human-readable duration for lag displays (seconds → s/m/h).
    """
    if seconds is None:
        return "—"
    try:
        s = int(float(seconds))  # tolerate float lags stored in DB
    except Exception:
        return "—"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def format_elapsed_since(ts: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    if ts is None:
        return "never"
    ref = now or now_utc()
    try:
        delta = ref - to_utc_aware(ts)
    except Exception:
        return "never"
    total = max(0, int(delta.total_seconds()))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}m {seconds:02d}s ago"


def format_duration_ms(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "n/a"
    total_seconds = max(0, int(round(duration_ms / 1000)))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes == 0:
        return f"{seconds}s"
    return f"{minutes}m {seconds:02d}s"
