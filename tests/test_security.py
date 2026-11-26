"""What the server refuses, and to whom: visitors, other sites, oversized bodies, unknown host names."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from demoreel.config import get_settings

ORGANISER_KEY = "organiser-key-0123456789"
ORGANISER = {"Authorization": f"Bearer {ORGANISER_KEY}"}


@pytest.fixture(scope="module")
def client():
    from demoreel.api import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def admin_key(monkeypatch):
    """A public viewer: reading stays open, changing anything needs the organiser's key."""
    monkeypatch.setattr(get_settings(), "admin_key", ORGANISER_KEY)
    return ORGANISER


# ------------------------------------------------------------------------------------------- webhooks

def test_webhook_addresses_are_shown_to_the_organiser_only(client, admin_key):
    made = client.post("/api/v1/webhooks", json={"url": "https://hooks.mixdavila.example/secret-path",
                                                  "events": []}, headers=admin_key)
    assert made.status_code == 201
    try:
        r = client.get("/api/v1/webhooks")
        assert r.status_code == 403 and "organiser" in r.json()["detail"] and "secret-path" not in r.text
        rows = client.get("/api/v1/webhooks", headers=admin_key).json()
        assert any(w["url"].endswith("/secret-path") for w in rows)
    finally:
        client.delete(f"/api/v1/webhooks/{made.json()['id']}", headers=admin_key)


def test_without_an_organiser_key_the_local_user_sees_the_webhooks(client):
    assert client.get("/api/v1/webhooks").status_code == 200
