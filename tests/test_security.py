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


# ------------------------------------------------------------------------------------------- bodies

def _through_gate(method: str, path: str, chunks: list[bytes], headers: dict[str, str] | None = None):
    """Run one request through the gate in front of a stand-in app that reads its whole body.
    Returns ``(status, body bytes the app read, detail)``."""
    import asyncio
    import json

    from demoreel.api import _RequestGate

    read = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            read.append(len(message.get("body") or b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    pending = list(chunks)

    async def receive():
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    asyncio.run(_RequestGate(app)(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, sum(read), json.loads(body).get("detail") if status != 200 else None


def test_a_visitor_is_refused_before_the_body_is_read(admin_key):
    status, read, detail = _through_gate("POST", "/api/v1/matches", [b"x" * 65536] * 80)  # 5 MB
    assert status == 403 and read == 0 and "organiser" in detail
    status, read, _ = _through_gate("POST", "/api/v1/matches", [b"x" * 1024], admin_key)
    assert status == 200 and read == 1024


def test_with_an_api_key_nothing_is_read_without_it(monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", "reader-key-0123456789")
    status, read, detail = _through_gate("PATCH", "/api/v1/highlights/h_x", [b"{}"])
    assert status == 401 and read == 0 and "API key" in detail
    assert _through_gate("GET", "/api/v1/matches", [])[0] == 401


def test_worker_routes_need_a_token_before_their_body_is_read():
    status, read, detail = _through_gate("POST", "/api/v1/worker/claim", [b"{}"])
    assert status == 401 and read == 0 and "worker token" in detail


def test_bodies_are_held_to_a_size_declared_or_not(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_json_kb", 4)
    big = [b"x" * 1024] * 8
    status, read, detail = _through_gate("POST", "/api/v1/webhooks", big, {"Content-Length": str(8 * 1024)})
    assert status == 413 and read == 0 and "4 KB" in detail and "DEMOREEL_MAX_JSON_KB" in detail
    status, read, detail = _through_gate("POST", "/api/v1/webhooks", big)  # chunked: no length given
    assert status == 413 and read <= 5 * 1024 and "4 KB" in detail
    assert _through_gate("POST", "/api/v1/webhooks", [b"x" * 1024] * 3)[0] == 200


def test_a_json_body_over_the_limit_gets_413_from_the_api(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_json_kb", 1)
    r = client.post("/api/v1/webhooks", json={"url": "http://x.example/" + "a" * 4096, "events": []})
    assert r.status_code == 413 and "DEMOREEL_MAX_JSON_KB" in r.json()["detail"]


def test_an_upload_is_counted_while_it_is_stored(client, monkeypatch):
    import io

    from demoreel import service

    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    tmp = get_settings().data_dir / "tmp"
    before = set(tmp.glob("*")) if tmp.is_dir() else set()
    payload = service.CS2_MAGIC + b"\0" * (1024 * 1024 + 512 * 1024)  # 1.5 MB: under the multipart margin
    r = client.post("/api/v1/matches", files={"file": ("big.dem", io.BytesIO(payload))})
    assert r.status_code == 413 and "1 MB" in r.json()["detail"] and "DEMOREEL_MAX_UPLOAD_MB" in r.json()["detail"]
    assert set(tmp.glob("*")) == before  # nothing left behind
    r = client.post("/api/v1/matches", files={"file": ("bigger.dem", io.BytesIO(payload * 2))})
    assert r.status_code == 413  # refused by the gate: over the limit plus the margin


def test_a_zip_with_too_many_entries_is_refused_before_it_is_listed(client):
    import io
    import zipfile

    from demoreel import service

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("match.dem", service.CS2_MAGIC + b"zip-entries")
        for i in range(service.MAX_ZIP_ENTRIES):
            zf.writestr(f"junk/{i}.txt", b"")
    r = client.post("/api/v1/matches", files={"file": ("many.zip", io.BytesIO(buf.getvalue()))})
    assert r.status_code == 400 and f"at most {service.MAX_ZIP_ENTRIES}" in r.json()["detail"]

    small = io.BytesIO()
    with zipfile.ZipFile(small, "w") as zf:
        zf.writestr("match.dem", service.CS2_MAGIC + b"zip-few-entries")
    r = client.post("/api/v1/matches", files={"file": ("few.zip", io.BytesIO(small.getvalue()))})
    assert r.status_code == 202
    client.delete(f"/api/v1/matches/{r.json()['match']['id']}")


def test_a_zip_that_understates_its_entries_is_still_counted(monkeypatch, tmp_path):
    import struct
    import zipfile

    from demoreel import service

    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(3):
            zf.writestr(f"{i}.dem", service.CS2_MAGIC)
    data = bytearray(archive.read_bytes())
    end = data.rindex(b"PK\x05\x06")
    data[end + 8:end + 12] = struct.pack("<HH", 1, 1)  # the end record now claims a single entry
    archive.write_bytes(bytes(data))
    monkeypatch.setattr(service, "MAX_ZIP_ENTRIES", 2)
    with pytest.raises(service.ServiceError, match="lists 3 files"):
        service._open_maybe_compressed(archive)
    archive.unlink()  # nothing keeps it open (Windows would refuse)


def test_a_zip_without_a_demo_is_refused_and_removed(client):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", b"no demo here")
    r = client.post("/api/v1/matches", files={"file": ("nodemo.zip", io.BytesIO(buf.getvalue()))})
    assert r.status_code == 400 and "no .dem" in r.json()["detail"]


# ------------------------------------------------------------------------------------------- what visitors see

def test_visitors_do_not_see_internal_error_texts(client, admin_key):
    from demoreel import jobs
    from demoreel.db import Job, Match, init_db, now, session_scope

    secret = r"RenderError: HLAE at C:\Games\HLAE\hlae.exe failed; console: ...gpu-worker-1"
    init_db()
    with session_scope() as s:
        job = jobs.enqueue(s, "test_failed", {}, capability="nobody-has-this")
        job.status, job.error = "failed", secret
        m = Match(sha256=f"failed-{now().timestamp()}", filename="x.dem", size_bytes=1, demo_path="demos/x.dem",
                  status="failed", error=secret)
        s.add(m)
        s.flush()
        job_id, match_id = job.id, m.id
    try:
        for url in (f"/api/v1/jobs/{job_id}", "/api/v1/jobs", f"/api/v1/matches/{match_id}", "/api/v1/matches"):
            assert "HLAE" not in client.get(url).text, url
            assert "HLAE" in client.get(url, headers=admin_key).text, url
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"  # the state itself stays
    finally:
        with session_scope() as s:
            s.delete(s.get(Job, job_id))
            s.delete(s.get(Match, match_id))


def test_visitors_see_which_renderers_exist_but_not_why_or_where(client, admin_key):
    visitor = client.get("/api/v1/capabilities").json()["renderers"]
    organiser = client.get("/api/v1/capabilities", headers=admin_key).json()["renderers"]
    assert [r["name"] for r in visitor] == [r["name"] for r in organiser]
    assert [r["available"] for r in visitor] == [r["available"] for r in organiser]
    ingame = next(r for r in visitor if r["name"] == "ingame")
    assert ingame["details"] == {} and "DEMOREEL_" not in (ingame["reason"] or "")
    assert "DEMOREEL_INGAME_ENABLED" in next(r for r in organiser if r["name"] == "ingame")["reason"]


# ------------------------------------------------------------------------------------------- match sources

@pytest.fixture
def site(monkeypatch):
    """A match source whose site is played by a table of canned answers; records what each request carried."""
    import contextlib

    import httpx

    from demoreel import sources as src

    answers: dict[str, httpx.Response] = {}
    seen: list[tuple[str, dict]] = []

    def stream(method, url, headers=None, **kwargs):
        assert kwargs.get("follow_redirects") is False  # redirects are followed by hand
        seen.append((str(url), dict(headers or {})))
        answer = answers[str(url)]
        return contextlib.nullcontext(httpx.Response(answer.status_code, headers=answer.headers,
                                                     content=answer.content, request=httpx.Request(method, url)))

    monkeypatch.setattr(src.httpx, "stream", stream)
    monkeypatch.setattr(src, "MIN_DOWNLOAD_INTERVAL_S", 0.0)
    source = src.Source(id="mixdavila-test", name="Mix da Vila", list_url="https://mixdavila.example/api/partidas",
                        demo_url="https://mixdavila.example/api/partidas/{id}/demo",
                        headers={"X-Token": "site-token"})
    yield source, answers, seen
    src._CACHE.clear()
    src._cache_file(source.id).unlink(missing_ok=True)


def test_a_redirect_to_another_host_does_not_carry_the_sources_headers(site):
    import httpx

    from demoreel import sources as src

    source, answers, seen = site
    answers[source.list_url] = httpx.Response(302, headers={"location": "/api/v2/partidas"})
    answers["https://mixdavila.example/api/v2/partidas"] = httpx.Response(
        302, headers={"location": "https://cdn.example/lists/partidas.json"})
    answers["https://cdn.example/lists/partidas.json"] = httpx.Response(200, json=[{"id": 1, "map": "de_nuke"}])
    rows = src.list_remote(source, fresh=True)
    assert [r.remote_id for r in rows] == ["1"]
    assert [h.get("X-Token") for _, h in seen] == ["site-token", "site-token", None]  # same host, same, CDN
    assert all(h["User-Agent"] == "demoreel" for _, h in seen)


def test_endless_redirects_and_odd_lists_are_refused_with_a_reason(site, monkeypatch):
    import httpx

    from demoreel import sources as src

    source, answers, _ = site
    answers[source.list_url] = httpx.Response(302, headers={"location": source.list_url})
    with pytest.raises(src.SourceError, match="redirected more than"):
        src.list_remote(source, fresh=True)
    answers[source.list_url] = httpx.Response(200, content=b"<html>not json</html>")
    with pytest.raises(src.SourceError, match="not JSON"):
        src.list_remote(source, fresh=True)
    from demoreel.api import create_app

    saved = src.save_source(source)
    try:
        with TestClient(create_app()) as c:
            r = c.get(f"/api/v1/sources/{source.id}/matches")
        assert r.status_code == 502 and "not JSON" in r.json()["detail"]
    finally:
        saved.unlink()
    monkeypatch.setattr(src, "MAX_LIST_BYTES", 64)
    answers[source.list_url] = httpx.Response(200, json=[{"id": i} for i in range(50)])
    with pytest.raises(src.SourceError, match="larger than"):
        src.list_remote(source, fresh=True)


def test_a_demo_that_trickles_in_too_long_is_given_up(site, monkeypatch, tmp_path):
    import httpx

    from demoreel import service
    from demoreel import sources as src

    source, answers, seen = site
    answers["https://mixdavila.example/api/partidas/7/demo"] = httpx.Response(
        302, headers={"location": "https://files.example/7.dem"})
    answers["https://files.example/7.dem"] = httpx.Response(200, content=service.CS2_MAGIC + b"\0" * 1024)
    assert src.download_demo(source, "7", tmp_path / "7.dem") > 0
    assert seen[-1][1].get("X-Token") is None  # the file host never saw the token
    monkeypatch.setattr(src, "DOWNLOAD_DEADLINE_S", -1.0)
    with pytest.raises(RuntimeError, match="took more than"):
        src.download_demo(source, "7", tmp_path / "again.dem")


# ------------------------------------------------------------------------------------------- headers

@pytest.fixture
def kit():
    """A brand kit on disk (the only one, so it is the active kit); removed afterwards."""
    import json
    import shutil

    from demoreel import api

    folder = get_settings().data_dir / "brands" / "mixdavila"
    folder.mkdir(parents=True, exist_ok=True)

    def write(**fields):
        (folder / "brand.json").write_text(json.dumps({"name": "Mix da Vila", **fields}), encoding="utf-8")
        api._policy_cache.clear()
        return folder

    yield write
    shutil.rmtree(folder)
    api._policy_cache.clear()


def test_every_answer_carries_the_security_headers(client, kit, monkeypatch):
    for path in ("/", "/api/v1/matches", "/health"):
        r = client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    kit(ui={"embed": {"parents": ["https://mixdavila.example"]}})
    assert client.get("/").headers["content-security-policy"] == "frame-ancestors 'self' https://mixdavila.example"
    monkeypatch.setattr(get_settings(), "frame_ancestors", ["https://other.example", "bad value; script-src *"])
    from demoreel import api

    api._policy_cache.clear()
    assert client.get("/").headers["content-security-policy"] == (
        "frame-ancestors 'self' https://mixdavila.example https://other.example")
    monkeypatch.setattr(get_settings(), "frame_ancestors", ["*"])
    api._policy_cache.clear()
    assert client.get("/").headers["content-security-policy"] == "frame-ancestors *"


def test_svg_files_of_a_kit_are_sandboxed(client, kit):
    folder = kit(logo="logo.svg")
    (folder / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                                     encoding="utf-8")
    r = client.get("/files/brands/mixdavila/logo.svg")
    assert r.status_code == 200 and r.headers["content-security-policy"] == "sandbox"


def test_the_share_page_never_ends_its_script_early(client, kit, sample_analysis):
    from demoreel import service
    from demoreel.db import Highlight, Match, init_db, now, session_scope

    init_db()
    with session_scope() as s:
        m = Match(sha256=f"share-{now().timestamp()}", filename="x.dem", size_bytes=1, demo_path="demos/x.dem",
                  status="ready", map="de_test")
        s.add(m)
        s.flush()
        c = sample_analysis.highlights[0]
        h = Highlight(match_id=m.id, kind=c.kind, title=c.title, description=c.description, origin="auto")
        service.apply_candidate(h, c)
        s.add(h)
        s.flush()
        match_id, hid = m.id, h.id
    try:
        kit(ui={"embed": {"share_base": "https://mixdavila.example/jogadas/</script><script>alert(1)//"}})
        page = client.get(f"/p/{hid}").text
        script = page.split("<script>", 1)[1]
        assert "</script><script>alert" not in script and "<\\/script>" in script
    finally:
        with session_scope() as s:
            service.delete_match(s, match_id)


# ------------------------------------------------------------------------------------------- /mcp key

def test_the_mcp_key_is_checked_without_crashing_on_odd_headers(admin_key):
    from demoreel.api import create_app

    with TestClient(create_app()) as c:  # the gate is set up when the app is built
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        r = c.post("/mcp/", json=body)
        assert r.status_code == 401 and "organiser" in r.json()["error"]
        r = c.post("/mcp/", json=body, headers={"Authorization": b"Bearer \xff\xfe-not-utf8"})
        assert r.status_code == 401  # not a 500
        r = c.post("/mcp/", json=body, headers={**admin_key, "Accept": "application/json, text/event-stream"})
        assert r.status_code == 200 and "tools" in r.json()["result"]


# ------------------------------------------------------------------------------------------- radar images

def test_only_plain_map_names_are_looked_up():
    from demoreel.render import maps

    png = maps.cache_dir() / "de_testmap.png"
    png.write_bytes(b"\x89PNG")
    outside = get_settings().data_dir / "secret.png"
    outside.write_bytes(b"\x89PNG")
    try:
        assert maps.radar_image("de_testmap") == png
        for bad in ("../../secret", "..\\..\\secret", "de_x/../../secret", "DE_TESTMAP", "", None):
            assert maps.radar_image(bad) is None, bad
    finally:
        png.unlink()
        outside.unlink()


def test_the_radar_pack_is_used_only_with_the_expected_hash(monkeypatch):
    import hashlib
    import io
    import zipfile

    from demoreel.render import maps

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("de_hashed.png", b"\x89PNG")
    pack = buf.getvalue()

    class Fake:
        content = pack

        def raise_for_status(self):
            return None

    monkeypatch.setattr(maps.httpx, "get", lambda *a, **k: Fake())
    monkeypatch.setattr(get_settings(), "offline", False)
    target = maps.cache_dir() / "de_hashed.png"
    try:
        monkeypatch.setattr(get_settings(), "radar_images_sha256", "0" * 64)
        monkeypatch.setattr(maps, "_download_attempted", False)
        assert maps.radar_image("de_hashed") is None and not target.exists()
        monkeypatch.setattr(get_settings(), "radar_images_sha256", hashlib.sha256(pack).hexdigest())
        monkeypatch.setattr(maps, "_download_attempted", False)
        assert maps.radar_image("de_hashed") == target
    finally:
        target.unlink(missing_ok=True)


def test_the_default_radar_pack_is_a_fixed_release_with_a_hash():
    from demoreel.config import Settings

    s = Settings()
    assert "/latest/" not in s.radar_images_url and len(s.radar_images_sha256) == 64


# ------------------------------------------------------------------------------------------- keys

@pytest.fixture
def fresh_settings(monkeypatch):
    """get_settings() read again from the environment; the usual settings come back afterwards."""
    for name in ("DEMOREEL_API_KEY", "DEMOREEL_ADMIN_KEY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_weak_keys_stop_the_server_without_being_repeated(fresh_settings):
    from demoreel.config import ConfigError

    for name, value, expected in (("DEMOREEL_ADMIN_KEY", "change-me", "example value"),
                                  ("DEMOREEL_API_KEY", "abc123secret", "12 characters long")):
        fresh_settings.setenv(name, value)
        get_settings.cache_clear()
        with pytest.raises(ConfigError) as err:
            get_settings()
        message = str(err.value)
        assert name in message and expected in message and "openssl rand" in message
        assert value not in message  # messages end up in logs
        fresh_settings.delenv(name)


def test_strong_and_empty_keys_are_accepted(fresh_settings):
    fresh_settings.setenv("DEMOREEL_ADMIN_KEY", "x" * 16)
    fresh_settings.setenv("DEMOREEL_API_KEY", "")  # `DEMOREEL_API_KEY=` in a compose file: no key
    get_settings.cache_clear()
    s = get_settings()
    assert s.admin_key == "x" * 16 and s.api_key is None


# ------------------------------------------------------------------------------------------- publish

def test_a_publish_target_cannot_smuggle_an_ssh_option(monkeypatch):
    import subprocess

    from demoreel import publish

    with pytest.raises(publish.PublishError, match="starts with '-'"):
        publish.Target.parse("-oProxyCommand=calc.exe:/srv/demoreel/data")
    ran = []

    def fake_run(args, **kwargs):
        ran.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(publish.shutil, "which", lambda name: "ssh")
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    publish._ssh(publish.Target.parse("demoreel@mirror.example:/srv/demoreel/data"), "true")
    args = ran[0]
    assert args[args.index("demoreel@mirror.example") - 1] == "--"  # nothing after it is read as an option


# ------------------------------------------------------------------------------------------- docker

def test_docker_compose_publishes_the_port_on_this_machine_only():
    """`docker compose up` is the README's path: it must not open a keyless server to the network."""
    import re
    from pathlib import Path

    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"\s*$', compose, flags=re.M)
    assert published and all(p.startswith("127.0.0.1:") for p in published), published
    assert "DEMOREEL_ADMIN_KEY" in compose


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


def test_with_a_key_webhooks_cannot_point_inside_the_network(client, admin_key, monkeypatch):
    for url in ("http://127.0.0.1:9/hook", "http://localhost/hook", "http://10.0.0.5/hook", "http://[::1]/x",
                "http://169.254.169.254/latest/meta-data"):
        r = client.post("/api/v1/webhooks", json={"url": url, "events": []}, headers=admin_key)
        assert r.status_code == 400 and "private network" in r.json()["detail"], url
        assert "DEMOREEL_WEBHOOKS_ALLOW_PRIVATE" in r.json()["detail"]
    monkeypatch.setattr(get_settings(), "webhooks_allow_private", True)
    r = client.post("/api/v1/webhooks", json={"url": "http://10.0.0.5/hook", "events": []}, headers=admin_key)
    assert r.status_code == 201
    client.delete(f"/api/v1/webhooks/{r.json()['id']}", headers=admin_key)


def test_webhooks_are_delivered_without_holding_the_caller(monkeypatch):
    import threading
    import time

    from demoreel import webhooks
    from demoreel.db import Webhook, init_db, session_scope

    init_db()
    with session_scope() as s:
        w = Webhook(url="http://127.0.0.1:9/slow-hook", events=["render.ready"], secret="s")
        s.add(w)
        s.flush()
        hook_id = w.id
    delivered = threading.Event()

    def slow_post(url, **kwargs):
        time.sleep(1.0)
        delivered.set()

        class Ok:
            status_code = 200
        return Ok()

    monkeypatch.setattr(webhooks.httpx, "post", slow_post)
    try:
        started = time.monotonic()
        assert webhooks.emit("render.ready", {"render_id": "r_x"}) == 1
        assert time.monotonic() - started < 0.5  # the job that finished does not wait for the endpoint
        assert delivered.wait(10)
    finally:
        with session_scope() as s:
            s.delete(s.get(Webhook, hook_id))


def test_a_webhook_turned_private_is_not_sent_while_a_key_is_set(admin_key, monkeypatch):
    from demoreel import webhooks

    calls = []
    monkeypatch.setattr(webhooks.httpx, "post", lambda *a, **k: calls.append(a))
    assert webhooks._deliver("http://127.0.0.1:9/hook", None, "job.failed", b"{}") is False and calls == []


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
