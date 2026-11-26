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


# ------------------------------------------------------------------------------------------- other sites

EVIL = "https://evil.example"


def test_without_a_key_only_the_servers_own_host_names_are_answered(client):
    """DNS rebinding: a page on evil.example points its own name at 127.0.0.1 and talks to the server as if it
    were its own site. While no key is set, a request must name one of the server's hosts."""
    for path in ("/api/v1/matches", "/", "/health", "/mcp"):
        r = client.get(path, headers={"Host": "evil.example:8000"})
        assert r.status_code == 421, path
        assert "evil.example" in r.json()["detail"] and "DEMOREEL_ALLOWED_HOSTS" in r.json()["detail"]
    for host in ("localhost:8000", "127.0.0.1", "[::1]:8000", "testserver"):
        assert client.get("/api/v1/matches", headers={"Host": host}).status_code == 200, host


def test_host_names_the_server_was_told_about_are_answered(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "public_url", "https://lances.mixdavila.example")
    assert client.get("/health", headers={"Host": "lances.mixdavila.example"}).status_code == 200
    monkeypatch.setattr(settings, "allowed_hosts", ["192.168.0.10"])
    assert client.get("/health", headers={"Host": "192.168.0.10:8000"}).status_code == 200
    assert client.get("/health", headers={"Host": "testserver"}).status_code == 421
    monkeypatch.setattr(settings, "allowed_hosts", ["*"])
    assert client.get("/health", headers={"Host": "anything.example"}).status_code == 200


def test_with_a_key_any_host_name_is_answered(client, admin_key):
    """A public mirror sits behind proxies that pass their own host name; the key guards what matters."""
    assert client.get("/api/v1/matches", headers={"Host": "www.mixdavila.example"}).status_code == 200


def test_trusted_hosts_follow_what_the_server_knows_about_itself():
    from functools import partial

    from demoreel.config import Settings

    settings = partial(Settings, allowed_hosts=[])  # not the test client's name from conftest
    assert settings(host="0.0.0.0").trusted_hosts() is None  # every interface, no name: nothing to compare
    named = settings(host="0.0.0.0", public_url="http://localhost:8000").trusted_hosts()
    assert named is not None and {"localhost", "127.0.0.1", "[::1]"} <= named
    assert "lan.example" in Settings(host="0.0.0.0", allowed_hosts=["LAN.example:8000"]).trusted_hosts()
    assert "[::1]" in settings(host="::1").trusted_hosts()
    assert Settings(allowed_hosts=["*"]).trusted_hosts() is None


def test_pages_of_other_sites_change_nothing(client):
    r = client.post("/api/v1/webhooks", json={"url": "http://x.example/hook", "events": []},
                    headers={"Origin": EVIL})
    assert r.status_code == 403 and EVIL in r.json()["detail"] and "DEMOREEL_CORS_ORIGINS" in r.json()["detail"]
    assert client.delete("/api/v1/matches/m_nope", headers={"Origin": "null"}).status_code == 403
    # its own pages, and a development UI on another localhost port, still can
    for own in ("http://testserver", "http://localhost:5173", "http://127.0.0.1:8077"):
        r = client.post("/api/v1/webhooks", json={"url": "ftp://x", "events": []}, headers={"Origin": own})
        assert r.status_code == 400, own  # reached the route, which refused the url
    assert client.post("/api/v1/webhooks", json={"url": "ftp://x", "events": []}).status_code == 400  # no Origin


def test_without_a_key_other_sites_cannot_read_either(client):
    r = client.get("/api/v1/matches", headers={"Origin": EVIL})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers  # the browser hides it
    pre = client.options("/api/v1/matches", headers={"Origin": EVIL, "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 403 and "no key" in pre.json()["detail"]
    pre = client.options("/api/v1/webhooks", headers={"Origin": "http://localhost:5173",
                                                       "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in pre.headers["access-control-allow-methods"]
    assert pre.headers["access-control-allow-headers"] == "content-type"


def test_with_a_key_any_site_may_read_but_not_write(client, admin_key):
    r = client.get("/api/v1/matches", headers={"Origin": EVIL})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    pre = client.options("/api/v1/feed", headers={"Origin": EVIL, "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "*"
    pre = client.options("/api/v1/matches/m_x", headers={"Origin": EVIL, "Access-Control-Request-Method": "DELETE"})
    assert pre.status_code == 403
    r = client.delete("/api/v1/matches/m_x", headers={"Origin": EVIL, **admin_key})
    assert r.status_code == 403  # even with the key: a foreign page never changes anything


def test_listed_sites_and_embedding_parents_are_trusted(client, monkeypatch):
    import json
    import shutil

    from demoreel import api

    site = "https://mixdavila.example"
    monkeypatch.setattr(get_settings(), "cors_origins", [site])
    api._policy_cache.clear()
    r = client.post("/api/v1/webhooks", json={"url": "ftp://x", "events": []}, headers={"Origin": site})
    assert r.status_code == 400 and r.headers["access-control-allow-origin"] == site
    monkeypatch.setattr(get_settings(), "cors_origins", [])
    api._policy_cache.clear()

    kit = get_settings().data_dir / "brands" / "mixdavila"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / "brand.json").write_text(json.dumps({"name": "Mix da Vila", "ui": {"embed": {
        "parents": ["https://www.mixdavila.example/"]}}}), encoding="utf-8")
    try:
        api._policy_cache.clear()
        r = client.get("/api/v1/matches", headers={"Origin": "https://www.mixdavila.example"})
        assert r.headers["access-control-allow-origin"] == "https://www.mixdavila.example"
        assert client.get("/api/v1/matches", headers={"Origin": EVIL}).headers.get(
            "access-control-allow-origin") is None
    finally:
        shutil.rmtree(kit)
        api._policy_cache.clear()


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
