"""Persistent blocklist for egress IPs whose upstream quota is exhausted.

Cloudflare WARP hands out a small pool of addresses, and it will happily
re-assign an IP whose daily upstream budget has already been burned.  Rotating
onto such an IP looks successful (the address did change) but every request
still comes back as HTTP 429 with a multi-hour ``Retry-After``.

This module keeps those IPs in a small SQLite table inside the shared metrics
database (``METRICS_DB_PATH``) until the upstream quota window resets, so both
the proxy and the rotator can refuse them.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("ip_blocklist")

DB_PATH = Path(os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db"))

#: A Retry-After at or above this many seconds means "this IP's daily budget is
#: gone", not a transient burst limit.  Transient limits are usually seconds to
#: a few minutes; exhausted quotas report hours.
EXHAUSTION_RETRY_AFTER_SECONDS = int(
    os.environ.get("IP_EXHAUSTION_RETRY_AFTER", "300")
)

#: Fallback block duration when the upstream does not tell us when it resets.
DEFAULT_BLOCK_SECONDS = int(os.environ.get("IP_EXHAUSTION_BLOCK_SECONDS", "86400"))


def quota_identity(ip: Optional[str]) -> Optional[str]:
    """Return the key the upstream actually uses for its free-tier quota.

    OpenCode's ``ipRateLimiter`` truncates an IPv6 client address to its first
    four hextets (a /64 prefix) before building the Redis counter key; IPv4 is
    used verbatim.  Blocking a full IPv6 address therefore misses every sibling
    address in the same /64, and rotating within that /64 cannot restore quota.
    """
    if not ip:
        return None
    value = ip.strip()
    if not value:
        return None
    if ":" in value:
        return ":".join(value.split(":")[:4])
    return value


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """Create the shared table.  Safe to call from both containers."""
    try:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_egress_ips (
                    ip TEXT PRIMARY KEY,
                    blocked_until REAL NOT NULL,
                    reason TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            _migrate_to_quota_identities(conn)
    except Exception as exc:  # never crash a service because of bookkeeping
        log.warning("Unable to initialise the egress blocklist: %s", exc)


def _migrate_to_quota_identities(conn: sqlite3.Connection) -> None:
    """Collapse old full-IPv6 rows into their /64 quota identities."""
    rows = conn.execute(
        "SELECT ip, blocked_until, reason, created_at FROM blocked_egress_ips"
    ).fetchall()
    for ip, blocked_until_value, reason, created_at in rows:
        key = quota_identity(ip)
        if not key or key == ip:
            continue
        conn.execute("DELETE FROM blocked_egress_ips WHERE ip = ?", (ip,))
        conn.execute(
            """
            INSERT INTO blocked_egress_ips (ip, blocked_until, reason, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                blocked_until = MAX(blocked_egress_ips.blocked_until, excluded.blocked_until),
                reason = excluded.reason
            """,
            (key, blocked_until_value, reason, created_at),
        )


def block_ip(
    ip: Optional[str],
    until: Optional[float] = None,
    reason: str = "",
) -> Optional[float]:
    """Record ``ip`` as unusable until ``until`` (epoch seconds).

    Returns the expiry timestamp, or ``None`` when the IP was invalid or the
    database could not be written.
    """
    if not ip:
        return None
    ip = quota_identity(ip)
    if not ip:
        return None
    expiry = until if until is not None else time.time() + DEFAULT_BLOCK_SECONDS
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO blocked_egress_ips (ip, blocked_until, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET
                    blocked_until = MAX(blocked_egress_ips.blocked_until, excluded.blocked_until),
                    reason = excluded.reason
                """,
                (ip, expiry, reason, time.time()),
            )
        log.info(
            "Egress IP %s blocked until %s (%s)",
            ip,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry)),
            reason,
        )
        return expiry
    except Exception as exc:
        log.warning("Unable to add %s to the egress blocklist: %s", ip, exc)
        return None


def blocked_until(ip: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Return the expiry timestamp while ``ip``'s quota identity is blocked."""
    if not ip:
        return None
    key = quota_identity(ip)
    if not key:
        return None
    current = time.time() if now is None else now
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT blocked_until FROM blocked_egress_ips WHERE ip = ?",
                (key,),
            ).fetchone()
        if row and row[0] > current:
            return float(row[0])
    except Exception as exc:
        log.debug("Unable to read the egress blocklist: %s", exc)
    return None


def is_blocked(ip: Optional[str], now: Optional[float] = None) -> bool:
    return blocked_until(ip, now=now) is not None


def prune(now: Optional[float] = None) -> int:
    """Drop expired entries and return how many were removed."""
    current = time.time() if now is None else now
    try:
        with _connect() as conn:
            cursor = conn.execute(
                "DELETE FROM blocked_egress_ips WHERE blocked_until <= ?",
                (current,),
            )
            return cursor.rowcount or 0
    except Exception as exc:
        log.debug("Unable to prune the egress blocklist: %s", exc)
        return 0


def list_blocked(now: Optional[float] = None) -> List[Dict[str, object]]:
    """Return the active blocklist, earliest expiry first."""
    current = time.time() if now is None else now
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT ip, blocked_until, reason, created_at
                FROM blocked_egress_ips
                WHERE blocked_until > ?
                ORDER BY blocked_until ASC
                """,
                (current,),
            ).fetchall()
        return [
            {
                "ip": row[0],
                "blocked_until": row[1],
                "blocked_until_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(row[1])
                ),
                "reason": row[2],
                "created_at": row[3],
            }
            for row in rows
        ]
    except Exception as exc:
        log.debug("Unable to list the egress blocklist: %s", exc)
        return []
