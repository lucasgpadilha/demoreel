"""Outgoing webhooks: POST a JSON event to every subscribed URL when something finishes.

Events: ``match.ready``, ``match.failed``, ``render.ready``, ``render.failed``, ``job.succeeded``,
``job.failed``, ``job.canceled``. If the webhook has a secret, the body is signed with HMAC-SHA256 in the
``X-Demoreel-Signature: sha256=<hex>`` header. Delivery is best effort (3 tries) and never blocks a job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import select

from .db import Webhook, now, session_scope

log = logging.getLogger("demoreel.webhooks")

EVENTS = ["match.ready", "match.failed", "render.ready", "render.failed", "job.succeeded", "job.failed",
          "job.canceled"]


def emit(event: str, data: dict[str, Any]) -> int:
    with session_scope() as s:
        hooks = [(h.url, h.secret) for h in s.scalars(select(Webhook).where(Webhook.active == 1)).all()
                 if not h.events or event in h.events]
    if not hooks:
        return 0
    body = json.dumps({"event": event, "created_at": now().isoformat(), "data": data}, default=str).encode()
    delivered = 0
    for url, secret in hooks:
        headers = {"Content-Type": "application/json", "User-Agent": "demoreel-webhook", "X-Demoreel-Event": event}
        if secret:
            headers["X-Demoreel-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        for attempt in range(3):
            try:
                r = httpx.post(url, content=body, headers=headers, timeout=10)
                if r.status_code < 500:
                    delivered += 1
                    break
            except httpx.HTTPError as exc:
                log.warning("webhook %s attempt %d failed: %s", url, attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    return delivered
