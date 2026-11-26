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


# ------------------------------------------------------------------------------------------- long waits

@pytest.fixture
def queued_job():
    from demoreel import jobs
    from demoreel.db import Job, init_db, session_scope

    init_db()
    with session_scope() as s:
        job_id = jobs.enqueue(s, "test_never_runs", {}, capability="nobody-has-this").id
    yield job_id
    with session_scope() as s:
        s.delete(s.get(Job, job_id))


def test_a_long_wait_on_a_job_holds_no_thread(client, queued_job):
    """With a single thread for the blocking routes, a client waiting on a job must not stop the others."""
    import threading
    import time

    import anyio

    limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)
    tokens = limiter.total_tokens
    client.portal.call(setattr, limiter, "total_tokens", 1)
    try:
        waited = {}

        def wait():
            started = time.monotonic()
            waited["job"] = client.get(f"/api/v1/jobs/{queued_job}", params={"wait": 2}).json()
            waited["s"] = time.monotonic() - started

        t = threading.Thread(target=wait)
        t.start()
        time.sleep(0.4)
        started = time.monotonic()
        assert client.get("/api/v1/matches").status_code == 200
        other = time.monotonic() - started
        t.join()
    finally:
        client.portal.call(setattr, limiter, "total_tokens", tokens)
    assert waited["s"] >= 1.5 and waited["job"]["done"] is False  # the wait really waited...
    assert other < 1.0  # ...and the other route did not wait behind it


def test_waits_answer_at_once_when_too_many_are_waiting(client, queued_job, monkeypatch):
    import threading
    import time

    from demoreel import service

    monkeypatch.setattr(service, "MAX_TASK_WAITS", 0)
    started = time.monotonic()
    assert client.get(f"/api/v1/jobs/{queued_job}", params={"wait": 5}).json()["done"] is False
    assert time.monotonic() - started < 2

    monkeypatch.setattr(service, "_thread_waits", threading.BoundedSemaphore(1))
    assert service._thread_waits.acquire(blocking=False)  # the only slot is taken
    started = time.monotonic()
    assert service.wait_job(queued_job, 5).done is False
    assert time.monotonic() - started < 2
    service._thread_waits.release()
    started = time.monotonic()
    service.wait_job(queued_job, 1)  # a free slot: it waits
    assert time.monotonic() - started >= 0.9


def test_waiting_on_a_job_that_does_not_exist_says_so(client):
    r = client.get("/api/v1/jobs/j_nope", params={"wait": 1})
    assert r.status_code == 404 and "j_nope" in r.json()["detail"]
