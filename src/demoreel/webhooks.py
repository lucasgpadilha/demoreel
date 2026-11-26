"""Outgoing webhooks: POST a JSON event to every subscribed URL when something finishes.

Events: ``match.ready``, ``match.failed``, ``render.ready``, ``render.failed``, ``job.succeeded``,
``job.failed``, ``job.canceled``. If the webhook has a secret, the body is signed with HMAC-SHA256 in the
``X-Demoreel-Signature: sha256=<hex>`` header. Delivery is best effort (3 tries) and runs in background
threads: a slow or dead endpoint never holds the job (or the remote worker's ``/finish``) that finished.

While a key is set - the server is meant to be reached by others - webhooks only go to public addresses, so
a webhook cannot become a way into the server's own network (``DEMOREEL_WEBHOOKS_ALLOW_PRIVATE=1`` allows it).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .config import get_settings
from .db import Webhook, now, session_scope

log = logging.getLogger("demoreel.webhooks")

EVENTS = ["match.ready", "match.failed", "render.ready", "render.failed", "job.succeeded", "job.failed",
          "job.canceled"]

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    # Not daemon threads: a process that exits right after a job still sends what it queued.
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="demoreel-webhook")
        return _pool


def private_address(url: str) -> str | None:
    """The first private, loopback, link-local or otherwise internal address the host of ``url`` resolves
    to, or None (also when it does not resolve: then the delivery fails on its own)."""
    u = urlparse(url)
    if not u.hostname:
        return None
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return None
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return str(ip)
    return None


def blocked_reason(url: str) -> str | None:
    """Why this server will not POST to ``url`` (said to the organiser), or None."""
    settings = get_settings()
    if not settings.keyed or settings.webhooks_allow_private:
        return None
    address = private_address(url)
    if address is None:
        return None
    return (f"{url} points to {address}, an address inside a private network. This server has a key, so its "
            "webhooks go only to public addresses; set DEMOREEL_WEBHOOKS_ALLOW_PRIVATE=1 to allow this one.")


def _deliver(url: str, secret: str | None, event: str, body: bytes) -> bool:
    reason = blocked_reason(url)  # again at delivery: the name may point elsewhere by now
    if reason:
        log.warning("webhook not sent: %s", reason)
        return False
    headers = {"Content-Type": "application/json", "User-Agent": "demoreel-webhook", "X-Demoreel-Event": event}
    if secret:
        headers["X-Demoreel-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for attempt in range(3):
        try:
            r = httpx.post(url, content=body, headers=headers, timeout=10)
            if r.status_code < 500:
                return True
        except httpx.HTTPError as exc:
            log.warning("webhook %s attempt %d failed: %s", url, attempt + 1, exc)
        time.sleep(1.5 * (attempt + 1))
    return False


def emit(event: str, data: dict[str, Any]) -> int:
    """Queue ``event`` for every webhook subscribed to it; returns how many were queued."""
    with session_scope() as s:
        hooks = [(h.url, h.secret) for h in s.scalars(select(Webhook).where(Webhook.active == 1)).all()
                 if not h.events or event in h.events]
    if not hooks:
        return 0
    body = json.dumps({"event": event, "created_at": now().isoformat(), "data": data}, default=str).encode()
    for url, secret in hooks:
        _executor().submit(_deliver, url, secret, event, body)
    return len(hooks)
