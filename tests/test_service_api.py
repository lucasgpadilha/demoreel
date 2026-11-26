"""Service, job-queue and HTTP API tests on a synthetic match (no real demo or renderer needed)."""

from __future__ import annotations

import io

import pytest
from conftest import real_demos
from fastapi.testclient import TestClient

from demoreel import jobs, service
from demoreel.config import get_settings
from demoreel.db import Highlight, Job, Match, init_db, now, session_scope
from demoreel.schemas import EditRequest

FAKE_DEMO = service.CS2_MAGIC + b"\x00" * 64


@pytest.fixture(scope="module")
def client():
    from demoreel.api import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def match_id(sample_analysis) -> str:
    """A 'ready' match seeded straight into the database, with its highlights."""
    init_db()
    settings = get_settings()
    with session_scope() as s:
        m = Match(sha256=f"test-{now().timestamp()}", filename="synthetic.dem", size_bytes=len(FAKE_DEMO),
                  demo_path="demos/x.dem", status="ready", map="de_test", tickrate=64,
                  total_ticks=sample_analysis.total_ticks, rounds=2, team_a_name="A", team_b_name="B",
                  team_a_score=1, team_b_score=1)
        s.add(m)
        s.flush()
        m.demo_path = f"demos/{m.id}.dem"
        m.analysis_path = f"matches/{m.id}/analysis.json"
        settings.path("demos", f"{m.id}.dem").write_bytes(FAKE_DEMO)
        settings.path("matches", m.id, "analysis.json").write_text(sample_analysis.model_dump_json(), encoding="utf-8")
        for c in sample_analysis.highlights:
            h = Highlight(match_id=m.id, kind=c.kind, title=c.title, description=c.description, origin="auto")
            service.apply_candidate(h, c)
            s.add(h)
        return m.id


def first_highlight(match_id: str) -> str:
    with session_scope() as s:
        return service.list_highlights(s, match_id)[0].id


def edit(hid: str, *ops: dict):
    with session_scope() as s:
        h = service.edit_highlight(s, hid, EditRequest.model_validate({"ops": list(ops)}))
        return service.highlight_out(s, h)


# ------------------------------------------------------------------------------------------- editing

def test_trim_split_speed_caption(match_id):
    hid = first_highlight(match_id)
    before = edit(hid, {"op": "set_meta", "title": "My ace"}, {"op": "remove_segment", "segment": 1})  # drop the replay
    seg = before.segments[0]
    mid = (seg.start_tick + seg.end_tick) // 2
    out = edit(hid,
               {"op": "trim", "segment": 0, "start_delta_s": 1.0, "end_delta_s": -0.5},
               {"op": "split_segment", "segment": 0, "at_tick": mid},
               {"op": "set_speed", "segment": 1, "speed": 0.5},
               {"op": "set_caption", "segment": 1, "caption": "slow"})
    assert out.title == "My ace" and out.version == before.version + 1
    assert len(out.segments) == 2
    assert out.segments[0].start_tick == seg.start_tick + 64 and out.segments[0].end_tick == mid
    assert out.segments[1].end_tick == seg.end_tick - 32
    assert out.segments[1].speed == 0.5 and out.segments[1].caption == "slow"
    # slow motion makes the clip longer than its tick span
    assert out.duration_s > (out.segments[1].end_tick - out.segments[0].start_tick) / 64


def test_edit_is_all_or_nothing(match_id):
    hid = first_highlight(match_id)
    with session_scope() as s:
        original = service.highlight_out(s, service.get_highlight(s, hid))
    with pytest.raises(service.ServiceError) as err:
        edit(hid, {"op": "set_caption", "segment": 0, "caption": "kept?"}, {"op": "remove_segment", "segment": 9})
    assert "Operation #1" in err.value.message
    with session_scope() as s:
        after = service.highlight_out(s, service.get_highlight(s, hid))
    assert after.segments == original.segments and after.version == original.version


@pytest.mark.parametrize("op, fragment", [
    ({"op": "set_range", "segment": 0, "start_tick": -5, "end_tick": 100}, "within 0.."),
    ({"op": "set_range", "segment": 0, "start_tick": 500, "end_tick": 500}, "at least 0.25s"),
    ({"op": "set_pov", "segment": None, "steamid": "nobody"}, "not a player"),
    ({"op": "split_segment", "segment": 0, "at_tick": 1}, "inside the segment"),
])
def test_invalid_edits_are_rejected(match_id, op, fragment):
    with pytest.raises(service.ServiceError) as err:
        edit(first_highlight(match_id), op)
    assert fragment in err.value.message


def test_pov_move_and_remove(match_id):
    hid = first_highlight(match_id)
    edit(hid, {"op": "remove_segment", "segment": 1})  # the automatic replay from the victim's eyes
    out = edit(hid, {"op": "add_segment", "start_tick": 8000, "end_tick": 8200, "pov_steamid": "t1"},
               {"op": "move_segment", "segment": 1, "to": 0}, {"op": "set_pov", "segment": None, "steamid": None})
    assert out.segments[0].start_tick == 8000 and all(s.pov_steamid is None for s in out.segments)
    out = edit(hid, {"op": "remove_segment", "segment": 0})
    assert len(out.segments) == 1
    with pytest.raises(service.ServiceError):
        edit(hid, {"op": "remove_segment", "segment": 0})  # a highlight cannot end up empty


# ------------------------------------------------------------------------------------------- queue

def test_job_queue_claim_and_run():
    init_db()
    seen = {}

    @jobs.handler("test_echo")
    def _echo(ctx: jobs.JobContext):
        ctx.progress(0.5, "half")
        seen["payload"] = ctx.payload
        return {"ok": True}

    @jobs.handler("test_boom")
    def _boom(ctx: jobs.JobContext):
        raise RuntimeError("boom")

    with session_scope() as s:
        a = jobs.enqueue(s, "test_echo", {"x": 1}, capability="testcap").id
        b = jobs.enqueue(s, "test_boom", {}, capability="testcap").id
        c = jobs.enqueue(s, "test_echo", {}, capability="other").id
    assert jobs.claim("w", ["nothing"]) is None  # capability mismatch: nothing to do
    assert jobs.run_one("w", ["testcap"]) and jobs.run_one("w", ["testcap"])
    assert not jobs.run_one("w", ["testcap"])
    with session_scope() as s:
        ja, jb, jc = s.get(Job, a), s.get(Job, b), s.get(Job, c)
        assert ja.status == "succeeded" and ja.result == {"ok": True} and ja.progress == 1.0
        assert jb.status == "failed" and "boom" in jb.error
        assert jc.status == "queued"
        jobs.request_cancel(s, jc)
        assert jc.status == "canceled"
    assert seen["payload"] == {"x": 1}


def test_work_this_machine_cannot_start_now_waits_in_the_queue_in_its_order(monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta

    init_db()
    ran: list = []
    state = {"taken": True, "defer": 0, "held": 0}

    @contextmanager
    def take_game():
        if state["taken"]:
            raise jobs.Taken()
        state["held"] += 1
        try:
            yield
        finally:
            state["held"] -= 1

    @jobs.handler("test_game")
    def _game(ctx: jobs.JobContext):
        assert state["held"] == 1  # the worker took the game before the job, and keeps it while it runs
        if state["defer"]:
            state["defer"] -= 1
            raise jobs.JobDeferred("Waiting for the game")
        ran.append(ctx.payload["n"])

    @jobs.handler("test_edit")
    def _edit(ctx: jobs.JobContext):
        assert state["held"] == 0  # work that needs no game does not keep it from the others
        ran.append("edit")

    @jobs.handler("test_cancelled_meanwhile")
    def _cancelled_meanwhile(ctx: jobs.JobContext):
        with session_scope() as s:
            jobs.request_cancel(s, s.get(Job, ctx.job_id))
        raise jobs.JobDeferred("Waiting for the game")

    monkeypatch.setitem(jobs.RESERVE, "testgame", take_game)
    with session_scope() as s:
        first = jobs.enqueue(s, "test_game", {"n": 1}, capability="testgame", priority=10)
        second = jobs.enqueue(s, "test_game", {"n": 2}, capability="testgame", priority=10)
        first.created_at = second.created_at - timedelta(seconds=30)
        jobs.enqueue(s, "test_edit", {}, capability="testcpu", priority=50)
        first, second = first.id, second.id
    caps = ["testcpu", "testgame"]
    assert jobs.run_one("w", caps) and ran == ["edit"]  # the game is taken: the worker edits instead...
    assert not jobs.run_one("w", caps)  # ...and leaves the recordings where they are
    with session_scope() as s:
        assert s.get(Job, first).status == "queued" and s.get(Job, first).attempts == 0

    state.update(taken=False, defer=1)  # free again, but the handler still finds it cannot record right now
    assert not jobs.run_one("w", caps) and state["held"] == 0
    with session_scope() as s:
        j = s.get(Job, first)
        assert (j.status, j.attempts, j.worker_id, j.message) == ("queued", 0, None, "Waiting for the game")
        assert s.get(Job, second).status == "queued"
    assert jobs.run_one("w", caps) and jobs.run_one("w", caps)
    assert ran == ["edit", 1, 2]  # the one that had to step back was still the first to record

    with session_scope() as s:
        gone = jobs.enqueue(s, "test_cancelled_meanwhile", {}, capability="testgame").id
    assert jobs.run_one("w", caps)
    with session_scope() as s:
        assert s.get(Job, gone).status == "canceled"  # not put back in the queue to run later


def test_a_recording_that_finds_the_game_taken_goes_back_to_the_queue(match_id, monkeypatch):
    """A recording job does not wait for the game on a worker - it used to give up after half an hour there,
    and whoever arrived first was not the first served. It returns to the queue untouched; nothing failed."""
    from contextlib import contextmanager

    from demoreel import tasks
    from demoreel.db import Render
    from demoreel.render.base import Availability, RecorderBusy

    class TakenGame:
        name, description = "ingame", "fake"
        asked: list[float] = []

        def availability(self):
            return Availability(ok=True)

        @contextmanager
        def recorder(self, wait_s=0.0, cancelled=None, on_wait=None):
            self.asked.append(wait_s)
            raise RecorderBusy("Another in-game recording is running on this machine.")
            yield

        def render(self, job):
            raise AssertionError("the game was taken: nothing may be recorded")

    game, events = TakenGame(), []
    monkeypatch.setattr(tasks, "get_renderer", lambda name: game)
    monkeypatch.setattr(tasks, "emit", lambda name, data: events.append(name))
    monkeypatch.setattr(get_settings(), "ingame_enabled", True)
    hid = first_highlight(match_id)
    with session_scope() as s:
        r, job = service.create_render(s, match_id, [hid], renderer="radar")
        r.renderer, job.capability = "ingame", "testrecord"  # first person, and the footage does not exist yet
        rid, jid = r.id, job.id

    assert not jobs.run_one("w", ["testrecord"])
    with session_scope() as s:
        j = s.get(Job, jid)
        assert (j.status, j.attempts, j.worker_id, j.error) == ("queued", 0, None, None)
        assert j.message == tasks.WAITING_FOR_GAME
        assert s.get(Render, rid).status == "queued" and s.get(Render, rid).error is None
    assert events == [] and game.asked == [0.0]  # no "render.failed" went out, and it did not sit waiting

    with pytest.raises(jobs.JobDeferred):  # the recording of a whole match steps back the same way
        tasks.capture_match(jobs.JobContext(job_id="j_none", payload={"match_id": match_id, "highlight_ids": [hid]}))
    with pytest.raises(jobs.JobDeferred):  # a job may ask to wait a little first; it is still not a failure
        tasks.capture_match(jobs.JobContext(job_id="j_none", payload={
            "match_id": match_id, "highlight_ids": [hid], "extra": {"ingame_lock_wait_s": 5}}))
    assert game.asked == [0.0, 0.0, 5.0]


def test_a_worker_takes_the_game_before_it_takes_a_recording_job():
    """Only the worker that has the game looks at recording jobs, so two workers can never pick one each and
    then let the later job record first."""
    from demoreel import tasks
    from demoreel.render.ingame import launcher
    from demoreel.render.ingame.settings import default_lock_path

    assert jobs.RESERVE["ingame"] is tasks._take_game
    other = launcher.RecordingLock(default_lock_path())  # another process recording on this machine
    assert other.try_acquire()
    try:
        with pytest.raises(jobs.Taken), tasks._take_game():
            pass
    finally:
        other.release()
    with tasks._take_game():
        assert other.is_held_elsewhere()  # kept for as long as the job runs
        with tasks._recorder(jobs.JobContext(job_id="j_none", payload={}), {}):  # the job's own use is free
            pass
        assert other.is_held_elsewhere()
    assert not other.is_held_elsewhere()


# ------------------------------------------------------------------------------------------- HTTP

def test_health_and_capabilities(client):
    from demoreel import __version__

    health = client.get("/health").json()
    assert health["status"] == "ok" and health["version"] == __version__  # the skill tells agents to check it
    caps = client.get("/api/v1/capabilities").json()
    assert {r["name"] for r in caps["renderers"]} == {"radar", "ingame"}
    ingame = next(r for r in caps["renderers"] if r["name"] == "ingame")
    # switched off by default: reported as unavailable with the setting to flip, whatever the machine has
    assert ingame["available"] is False and "DEMOREEL_INGAME_ENABLED" in ingame["reason"]
    assert "/api/v1/matches" in client.get("/openapi.json").json()["paths"]


def test_upload_rejects_non_demos(client):
    r = client.post("/api/v1/matches", files={"file": ("x.dem", io.BytesIO(b"hello world, not a demo"))})
    assert r.status_code == 400 and "PBDEMS2" in r.json()["detail"]
    r = client.post("/api/v1/matches", files={"file": ("old.dem", io.BytesIO(service.CSGO_MAGIC + b"\0" * 32))})
    assert r.status_code == 400 and "CS:GO" in r.json()["detail"]


def test_upload_queues_analysis_and_dedupes(client):
    payload = service.CS2_MAGIC + b"unique-content-1"
    r = client.post("/api/v1/matches", files={"file": ("a.dem", io.BytesIO(payload))})
    assert r.status_code == 202
    body = r.json()
    assert body["created"] and body["match"]["status"] == "queued" and body["job"]["type"] == "analyze_match"
    again = client.post("/api/v1/matches", files={"file": ("b.dem", io.BytesIO(payload))}).json()
    assert not again["created"] and again["match"]["id"] == body["match"]["id"]
    assert client.get(f"/api/v1/jobs/{body['job']['id']}").json()["done"] is False
    assert client.delete(f"/api/v1/matches/{body['match']['id']}").status_code == 204
    assert client.get(f"/api/v1/matches/{body['match']['id']}").status_code == 404


def test_match_read_endpoints(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    assert client.get(base).json()["highlight_count"] >= 1
    board = client.get(f"{base}/scoreboard").json()
    assert [t["score"] for t in board["teams"]] == [1, 1] and len(board["teams"][0]["players"]) == 5
    assert len(client.get(f"{base}/rounds").json()) == 2
    assert client.get(f"{base}/rounds/9").status_code == 404
    assert client.get(f"{base}/players/ct1").json()["kills"] == 5
    kills = client.get(f"{base}/kills", params={"attacker": "ct1", "headshot": True}).json()
    assert len(kills) == 3 and all(k["headshot"] for k in kills)
    assert client.get("/api/v1/matches/m_missing/analysis").status_code == 404


def test_highlight_http_flow(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    hs = client.get(f"{base}/highlights").json()
    assert hs and hs[0]["score"] >= hs[-1]["score"]
    created = client.post(f"{base}/highlights", json={"round": 2, "player_steamid": "t2", "title": "t2 triple"})
    assert created.status_code == 201
    h = created.json()
    assert h["origin"] == "manual" and len(h["kill_ticks"]) == 3 and h["player"]["steamid"] == "t2"
    bad = client.post(f"{base}/highlights", json={"round": 1, "player_steamid": "t5"})
    assert bad.status_code == 400 and "no kills" in bad.json()["detail"]
    edited = client.post(f"/api/v1/highlights/{h['id']}/edit",
                         json={"ops": [{"op": "set_speed", "segment": None, "speed": 2}], "origin": "agent"}).json()
    assert edited["segments"][0]["speed"] == 2 and edited["origin"] == "agent" and edited["version"] == 2
    assert client.post(f"/api/v1/highlights/{h['id']}/edit", json={"ops": [{"op": "nope"}]}).status_code == 422

    # rendering is asynchronous: the API answers 202 with a job that a worker will pick up
    r = client.post(f"/api/v1/highlights/{h['id']}/renders", json={"renderer": "radar"})
    assert r.status_code == 202
    render, job = r.json()["render"], r.json()["job"]
    assert render["status"] == "queued" and render["renderer"] == "radar" and job["type"] == "render"
    assert client.get(f"/api/v1/renders/{render['id']}").json()["video_url"] is None
    # editing after requesting a render flags it as stale
    client.post(f"/api/v1/highlights/{h['id']}/edit", json={"ops": [{"op": "set_caption", "segment": 0, "caption": "x"}]})
    assert client.get(f"/api/v1/renders/{render['id']}").json()["stale"] is True
    assert client.post(f"/api/v1/jobs/{job['id']}/cancel").json()["status"] == "canceled"
    # the in-game renderer is opt-in per machine
    r = client.post(f"/api/v1/highlights/{h['id']}/renders", json={"renderer": "ingame"})
    assert r.status_code == 409 and "DEMOREEL_INGAME_ENABLED" in r.json()["detail"]
    assert client.post(f"/api/v1/highlights/{h['id']}/renders", json={"renderer": "vhs"}).status_code == 400

    reel = client.post(f"{base}/reels", json={"top": 2, "title": "Best of"})
    assert reel.status_code == 202 and reel.json()["render"]["kind"] == "reel"
    assert len(reel.json()["render"]["highlight_ids"]) == 2
    assert client.delete(f"/api/v1/highlights/{h['id']}").status_code == 204
    assert client.get(f"/api/v1/highlights/{h['id']}").status_code == 404


def test_files_endpoint_only_serves_renders(client, match_id):
    assert client.get("/files/demoreel.db").status_code == 404
    assert client.get(f"/files/demos/{match_id}.dem").status_code == 404
    assert client.get("/files/renders/../demoreel.db").status_code == 404
    get_settings().path("renders", "r_test.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    assert client.get("/files/renders/r_test.mp4").status_code == 200


def test_webhooks_crud(client):
    assert client.post("/api/v1/webhooks", json={"url": "ftp://x", "events": []}).status_code == 400
    assert client.post("/api/v1/webhooks", json={"url": "http://localhost:1/x", "events": ["nope"]}).status_code == 400
    w = client.post("/api/v1/webhooks", json={"url": "http://localhost:1/hook", "events": ["render.ready"]}).json()
    assert any(x["id"] == w["id"] for x in client.get("/api/v1/webhooks").json())
    assert client.delete(f"/api/v1/webhooks/{w['id']}").status_code == 204


@pytest.mark.demo
def test_real_demo_end_to_end_radar(tmp_path):
    """Upload-less pipeline on a real demo: ingest -> analyze job -> render job -> playable mp4."""
    demos = real_demos()
    if not demos:
        pytest.skip("no demos/*.dem available")
    from demoreel import tasks  # noqa: F401 - registers job handlers

    init_db()
    demo = min(demos, key=lambda p: p.stat().st_size)
    with session_scope() as s:
        m, job, _created = service.ingest_demo(s, demo, demo.name, auto_render_top=0)
        mid = m.id
    if job is not None:
        while jobs.run_one("test", ["cpu"]):
            pass
    with session_scope() as s:
        m = service.get_match(s, mid)
        assert m.status == "ready", m.error
        h = service.list_highlights(s, mid)[0]
        seg = dict(h.segments[0])
        seg["end_tick"] = min(seg["end_tick"], seg["start_tick"] + 64 * 2)  # 2 seconds is enough for a test
        h.segments = [seg]
        r, _job = service.create_render(s, mid, [h.id], renderer="radar")
        rid = r.id
    while jobs.run_one("test", ["cpu"]):
        pass
    with session_scope() as s:
        out = service.render_out(s, service.get_render(s, rid))
        assert out.status == "ready", out.error
        assert out.video_url and out.size_bytes > 10_000 and 1.5 <= out.duration_s <= 3.0
        video = service.abs_path(service.get_render(s, rid).video_path)
        assert video.read_bytes()[4:8] == b"ftyp"


def test_render_deleted_midway_is_a_cancellation(match_id, monkeypatch):
    """Deleting a video while it is being rendered must end as 'canceled' and leave no partial file behind."""
    from demoreel import tasks
    from demoreel.render import RenderResult

    with session_scope() as s:
        r, job = service.create_render(s, match_id, [first_highlight(match_id)], renderer="radar")
        rid, jid = r.id, job.id
    produced = {}

    class FakeRenderer:
        name, description = "radar", "fake"

        def render(self, job):
            job.out_path.write_bytes(b"\x00" * 64)
            produced["path"] = job.out_path
            with session_scope() as s:
                service.delete_render(s, rid)  # the user deletes the render while the worker is busy
            return RenderResult(duration_s=1.0, width=2, height=2, fps=30)

    monkeypatch.setattr(tasks, "get_renderer", lambda name: FakeRenderer())
    with pytest.raises(jobs.JobCancelled):
        tasks.render(jobs.JobContext(job_id=jid, payload={"render_id": rid}))
    assert not produced["path"].exists()


def test_renderer_cancellation_is_not_a_failure(match_id, monkeypatch):
    from demoreel import tasks
    from demoreel.render.base import RenderCancelled

    with session_scope() as s:
        r, job = service.create_render(s, match_id, [first_highlight(match_id)], renderer="radar")
        rid, jid = r.id, job.id

    class Cancelling:
        name, description = "radar", "fake"

        def render(self, job):
            job.out_path.write_bytes(b"partial")
            raise RenderCancelled("Render canceled.")

    monkeypatch.setattr(tasks, "get_renderer", lambda name: Cancelling())
    with pytest.raises(jobs.JobCancelled):
        tasks.render(jobs.JobContext(job_id=jid, payload={"render_id": rid}))
    assert not service.abs_path(f"renders/{rid}.mp4").exists()


# ------------------------------------------------------------------------------------------- brand, feed, sources

REMOTE = [
    {"id": 7, "mapa": "nuke", "data": "2026-11-14T01:13:21.000Z", "placarA": 13, "placarB": 3, "demo": True},
    {"id": 8, "mapa": "inferno", "data": "2026-11-14T02:00:00.000Z", "placarA": 2, "placarB": 13, "demo": False},
    {"id": 9, "mapa": "anubis", "data": "2026-11-15T03:00:00.000Z", "placarA": 13, "placarB": 11, "demo": True},
]


@pytest.fixture
def source(monkeypatch):
    from demoreel import sources as src

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return REMOTE

    monkeypatch.setattr(src.httpx, "get", lambda *a, **k: FakeResponse())
    s = src.Source(id="testsite", name="Test Site", url="https://example.test/partidas", brand=None,
                   list_url="https://example.test/api/partidas", demo_url="https://example.test/api/partidas/{id}/demo",
                   page_url="https://example.test/partidas/{id}",
                   fields=src.SourceFields(id="id", map="mapa", played_at="data", score_a="placarA",
                                           score_b="placarB", has_demo="demo"))
    path = src.save_source(s)
    yield s
    path.unlink(missing_ok=True)
    src._CACHE.clear()


def test_brand_endpoint_has_a_neutral_default(client):
    b = client.get("/api/v1/brand").json()
    assert b["slug"] == "demoreel" and b["logo_url"] is None and set(b["colors"]) >= {"background", "accent", "ct", "t"}


def test_source_lists_remote_matches_newest_first(client, source):
    listed = next(x for x in client.get("/api/v1/sources").json() if x["id"] == "testsite")
    assert (listed["name"], listed["url"], listed["paused_until"]) == ("Test Site", "https://example.test/partidas", None)
    rows = client.get("/api/v1/sources/testsite/matches").json()
    assert [r["remote_id"] for r in rows] == ["9", "8", "7"]
    assert rows[0]["played_at"] == "2026-11-15T03:00:00Z" and rows[0]["page_url"] == "https://example.test/partidas/9"
    assert rows[1]["has_demo"] is False and rows[2]["score_a"] == 13 and rows[2]["match_id"] is None
    assert client.get("/api/v1/sources/nope/matches").status_code == 404


def test_import_from_source_downloads_and_links_the_match(client, source, monkeypatch):
    from demoreel import sources as src
    from demoreel import tasks

    r = client.post("/api/v1/sources/testsite/import", json={"remote_ids": ["7", "8", "7"]})
    assert r.status_code == 202
    body = r.json()
    assert len(body["jobs"]) == 1 and body["skipped"] == ["8"]  # no demo on the site; duplicates collapse
    job = body["jobs"][0]
    assert job["type"] == "import_remote"
    assert client.post("/api/v1/sources/testsite/import", json={"remote_ids": ["7"]}).json()["skipped"] == ["7"]
    assert client.post("/api/v1/sources/testsite/import", json={"remote_ids": ["404"]}).status_code == 404
    status = {r["remote_id"]: r["status"] for r in client.get("/api/v1/sources/testsite/matches").json()}
    assert status["7"] == "importing" and status["9"] is None

    def fake_download(_source, remote_id, dest, progress=lambda f, m: None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(service.CS2_MAGIC + f"remote-{remote_id}".encode())
        return dest.stat().st_size

    monkeypatch.setattr(src, "download_demo", fake_download)
    result = tasks.import_remote(jobs.JobContext(job_id=job["id"], payload={"source_id": "testsite", "remote_id": "7"}))
    m = client.get(f"/api/v1/matches/{result['match_id']}").json()
    assert m["source_id"] == "testsite" and m["remote_id"] == "7" and m["played_at"] == "2026-11-14T01:13:21Z"
    assert m["page_url"] == "https://example.test/partidas/7" and result["analyze_job_id"]
    with session_scope() as s:  # the import job is done in real life; mark it so the listing reflects the match
        s.get(Job, job["id"]).status = "succeeded"
    row = next(r for r in client.get("/api/v1/sources/testsite/matches").json() if r["remote_id"] == "7")
    assert row["match_id"] == result["match_id"] and row["status"] == "queued"
    client.delete(f"/api/v1/matches/{result['match_id']}")


def test_feed_lists_plays_that_have_a_video_and_prefers_real_footage(client, match_id):
    from demoreel.db import Render

    hid = first_highlight(match_id)
    assert all(i["match"]["id"] != match_id for i in client.get("/api/v1/feed").json())  # nothing rendered yet
    with session_scope() as s:
        s.add(Render(id="r_feed_radar", match_id=match_id, highlight_ids=[hid], renderer="radar", status="ready",
                     video_path="renders/r_feed_radar.mp4"))
        s.add(Render(id="r_feed_game", match_id=match_id, highlight_ids=[hid], renderer="ingame", status="ready",
                     video_path="renders/r_feed_game.mp4"))
        s.add(Render(id="r_feed_reel", match_id=match_id, kind="reel", highlight_ids=[hid], renderer="radar",
                     status="ready", video_path="renders/r_feed_reel.mp4"))
    items = [i for i in client.get("/api/v1/feed", params={"limit": 200}).json() if i["match"]["id"] == match_id]
    assert len(items) == 1
    item = items[0]
    assert item["render"]["id"] == "r_feed_game" and item["highlight"]["id"] == hid  # reels are not plays
    assert item["highlight"]["segments"] and item["highlight"]["kill_ticks"] and item["match"]["tickrate"] == 64
    assert client.get("/api/v1/feed", params={"player": "nobody"}).json() == []


def test_overloaded_source_is_paused_and_left_alone(client, source, monkeypatch):
    """A site that answers 'usage exceeded' must not be asked again: imports stop, the last list is reused."""
    from demoreel import sources as src

    assert len(client.get("/api/v1/sources/testsite/matches").json()) == 3  # a good list is saved to disk
    calls = {"n": 0}

    class Overloaded:
        status_code = 503
        text = '{"error":"usage_exceeded","message":"Usage exceeded"}'

        def raise_for_status(self):
            raise AssertionError("must not get this far")

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def stream(*a, **k):
        calls["n"] += 1
        return Overloaded()

    monkeypatch.setattr(src.httpx, "stream", stream)
    monkeypatch.setattr(src, "MIN_DOWNLOAD_INTERVAL_S", 0.0)
    dest = get_settings().path("tmp", "never.dem")
    try:
        with pytest.raises(src.SourceUnavailable) as err:
            src.download_demo(source, "7", dest)
        assert "paused" in str(err.value) and src.paused("testsite")["reason"].startswith("HTTP 503")
        with pytest.raises(src.SourceUnavailable):
            src.download_demo(source, "9", dest)
        assert calls["n"] == 1  # the second match was never requested
        src._CACHE.clear()
        monkeypatch.setattr(src.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request while paused")))
        assert len(client.get("/api/v1/sources/testsite/matches").json()) == 3  # served from the saved copy
        listed = next(x for x in client.get("/api/v1/sources").json() if x["id"] == "testsite")
        assert listed["paused_until"] and "usage_exceeded" in listed["paused_reason"]
        r = client.post("/api/v1/sources/testsite/import", json={"remote_ids": ["9"]})
        assert r.status_code == 503 and "paused" in r.json()["detail"]
    finally:
        src.resume("testsite")
        src._cache_file("testsite").unlink(missing_ok=True)
    assert src.paused("testsite") is None


# ------------------------------------------------------------------------------------------- produce

def test_produce_queues_every_shape_and_skips_what_exists(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    n = len(client.get(f"{base}/highlights").json())
    r = client.post(f"{base}/produce", json={"renderers": ["radar"], "formats": ["16:9", "9:16"]})
    assert r.status_code == 202
    body = r.json()
    assert body["render_jobs"] == 2 * n and body["capture_job"] is None and body["skipped"] == 0
    renders = client.get(f"{base}/renders").json()
    assert {x["format"] for x in renders} == {"16:9", "9:16"} and all(x["renderer"] == "radar" for x in renders)
    again = client.post(f"{base}/produce", json={"renderers": ["radar"], "formats": ["16:9", "9:16"]}).json()
    assert again["render_jobs"] == 0 and again["skipped"] == 2 * n  # nothing is rendered twice
    forced = client.post(f"{base}/produce", json={"renderers": ["radar"], "formats": ["16:9"], "force": True}).json()
    assert forced["render_jobs"] == n


def test_produce_first_person_without_the_game_says_what_is_missing(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    n = len(client.get(f"{base}/highlights").json())
    body = client.post(f"{base}/produce", json={"renderers": ["ingame"], "formats": ["16:9"]}).json()
    # in-game recording is off in tests and no footage exists: nothing can be queued, and the answer says why
    assert body["render_jobs"] == 0 and body["capture_job"] is None
    assert str(n) in body["detail"] and "DEMOREEL_INGAME_ENABLED" in body["detail"]


def test_a_hand_made_clip_can_be_banter_and_what_the_analysis_found_cannot_be_moved(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    plays = client.get(f"{base}/highlights").json()
    cut = {"start_tick": plays[0]["segments"][0]["start_tick"], "end_tick": plays[0]["segments"][0]["end_tick"]}
    made = client.post(f"{base}/highlights", json={**cut, "title": "caught napping", "category": "fail"})
    assert made.status_code == 201 and made.json()["category"] == "fail" and made.json()["kind"] == "custom"
    hid = made.json()["id"]
    assert hid in [h["id"] for h in client.get(f"{base}/highlights?category=fail").json()]
    assert hid not in [h["id"] for h in client.get(f"{base}/highlights").json()]

    r = client.post(f"/api/v1/highlights/{hid}/renders", json={"renderer": "radar"}).json()["render"]
    moved = client.patch(f"/api/v1/highlights/{hid}", json={"category": "play"}).json()
    assert moved["category"] == "play" and moved["version"] == made.json()["version"]
    assert client.get(f"/api/v1/renders/{r['id']}").json()["stale"] is False  # listed elsewhere, same video
    assert hid in [h["id"] for h in client.get(f"{base}/highlights").json()]
    assert client.patch(f"/api/v1/highlights/{hid}", json={"category": "fail"}).json()["category"] == "fail"
    client.post(f"{base}/rescore")
    assert client.get(f"/api/v1/highlights/{hid}").json()["category"] == "fail"  # re-scoring leaves it where it was put

    refused = client.patch(f"/api/v1/highlights/{plays[0]['id']}", json={"category": "fail"})
    assert refused.status_code == 400 and "hand-made" in refused.json()["detail"]
    assert client.post(f"{base}/highlights", json={**cut, "category": "meme"}).status_code == 422


def test_rescoring_does_not_take_a_hand_made_clip_for_an_edited_play(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    ace = next(h for h in client.get(f"{base}/highlights").json() if h["kind"] == "ace")
    # The same round and player as the ace, clipped by hand: it shares every kill with the play the model finds.
    made = client.post(f"{base}/highlights", json={"round": 1, "player_steamid": "ct1", "title": "mine",
                                                   "tags": ["by hand"]}).json()
    assert made["kind"] == "player_round" and set(made["kill_ticks"]) & set(ace["kill_ticks"])
    out = client.post(f"{base}/rescore").json()
    assert out["removed"] == 0 and out["added"] == 0
    kept = client.get(f"/api/v1/highlights/{made['id']}").json()
    assert kept["kind"] == "player_round" and kept["tags"] == ["by hand"] and kept["title"] == "mine"
    assert client.get(f"/api/v1/highlights/{ace['id']}").status_code == 200  # the model's own play is still there
    moved = client.patch(f"/api/v1/highlights/{made['id']}", json={"category": "fail"})
    assert moved.status_code == 200 and moved.json()["category"] == "fail"



def test_a_segment_can_be_watched_from_a_fixed_camera(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    seg = client.get(f"{base}/highlights").json()[0]["segments"][0]
    cam = {"x": -324.4, "y": -8.2, "z": 506.0, "pitch": 38.2, "yaw": -121.3}
    span = {"start_tick": seg["start_tick"], "end_tick": seg["end_tick"]}
    made = client.post(f"{base}/highlights", json={"title": "from above", "segments": [
        {**span, "camera": cam}, {**span, "pov_steamid": seg["pov_steamid"], "role": "replay"}]})
    assert made.status_code == 201, made.text
    stored = made.json()["segments"]
    assert stored[0]["camera"] == cam and stored[0]["pov_steamid"] is None
    assert stored[1]["camera"] is None

    from demoreel.render.base import segment_view
    assert segment_view(stored[0]) == "cam:-324.40,-8.20,506.00,38.20,-121.30"
    assert segment_view(stored[1]) == seg["pov_steamid"]

    both = client.post(f"{base}/highlights", json={"segments": [{**span, "camera": cam,
                                                                  "pov_steamid": seg["pov_steamid"]}]})
    assert both.status_code == 400 and "not both" in both.json()["detail"]
    assert client.post(f"{base}/highlights", json={"segments": [{**span, "camera": {"x": 1}}]}).status_code == 422


CAM = {"x": -324.4, "y": -8.2, "z": 506.0, "pitch": 38.2, "yaw": -121.3}


def test_edit_operations_switch_a_segment_between_a_player_and_a_fixed_camera(client, match_id):
    hid = first_highlight(match_id)
    seg = edit(hid, {"op": "remove_segment", "segment": 1}).segments[0]  # keep only the play itself
    player, span = seg.pov_steamid, {"start_tick": seg.start_tick, "end_tick": seg.end_tick}
    out = edit(hid, {"op": "set_camera", "segment": 0, "camera": CAM})
    assert out.segments[0].camera.model_dump() == CAM and out.segments[0].pov_steamid is None
    out = edit(hid, {"op": "set_pov", "segment": 0, "steamid": player})  # back to the player: no "not both"
    assert out.segments[0].pov_steamid == player and out.segments[0].camera is None
    out = edit(hid, {"op": "add_segment", **span, "camera": CAM, "index": 0})
    assert out.segments[0].camera.model_dump() == CAM and out.segments[1].pov_steamid == player
    out = edit(hid, {"op": "set_camera", "segment": 0, "camera": None})
    assert out.segments[0].camera is None and out.segments[0].pov_steamid is None
    out = edit(hid, {"op": "set_camera", "segment": None, "camera": CAM})
    assert all(s.camera is not None and s.pov_steamid is None for s in out.segments)
    out = edit(hid, {"op": "set_pov", "segment": None, "steamid": None})  # the free camera everywhere
    assert all(s.camera is None and s.pov_steamid is None for s in out.segments)
    with pytest.raises(service.ServiceError) as err:
        edit(hid, {"op": "add_segment", **span, "camera": CAM, "pov_steamid": player})
    assert "not both" in err.value.message and "set_camera" in err.value.message
    r = client.post(f"/api/v1/highlights/{hid}/edit", json={"ops": [{"op": "set_camera", "segment": 0, "camera": CAM}]})
    assert r.status_code == 200 and r.json()["segments"][0]["camera"] == CAM


@pytest.mark.parametrize("change, fragment", [
    ({"x": float("nan")}, "finite"), ({"z": float("inf")}, "finite"), ({"y": 20000.0}, "16384"),
    ({"x": -16385.0}, "16384"), ({"pitch": 95.0}, "-90"), ({"pitch": -90.5}, "-90"),
])
def test_a_camera_off_the_map_is_refused_with_the_valid_range(client, match_id, change, fragment):
    import json

    from pydantic import ValidationError

    from demoreel.analysis.schema import SegmentCamera

    with pytest.raises(ValidationError) as err:
        SegmentCamera.model_validate({**CAM, **change})
    assert fragment in str(err.value)
    seg = client.get(f"/api/v1/matches/{match_id}/highlights").json()[0]["segments"][0]
    body = json.dumps({"segments": [{"start_tick": seg["start_tick"], "end_tick": seg["end_tick"],
                                     "camera": {**CAM, **change}}]})  # NaN / Infinity as Python's json writes them
    r = client.post(f"/api/v1/matches/{match_id}/highlights", content=body,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422 and fragment in r.text  # a validation error, not a server error
    assert SegmentCamera.model_validate({**CAM, "x": 16384, "pitch": -90}).x == 16384  # the edges are fine


def test_render_request_carries_its_format(client, match_id):
    hid = first_highlight(match_id)
    r = client.post(f"/api/v1/highlights/{hid}/renders", json={"renderer": "radar", "options": {"format": "9:16"}})
    assert r.status_code == 202 and r.json()["render"]["format"] == "9:16"
    assert client.post(f"/api/v1/highlights/{hid}/renders",
                       json={"renderer": "radar", "options": {"format": "4:3"}}).status_code == 422


# ------------------------------------------------------------------------------------------- curation

def test_highlights_carry_the_curation_and_rescoring_keeps_ids_and_manual_choices(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    before = client.get(f"{base}/highlights").json()
    assert before and all(h["tier"] in ("S", "A", "B", "C") and h["breakdown"] for h in before)
    assert before[0]["featured"] and before[0]["featured_rank"] == 1 and before[0]["team"] in ("team_a", "team_b")
    assert [h["id"] for h in client.get(f"{base}/highlights?featured=true").json()] == [
        h["id"] for h in before if h["featured"]]
    top = before[0]["id"]
    off = client.patch(f"/api/v1/highlights/{top}", json={"featured": False}).json()
    assert off["featured"] is False and off["version"] == before[0]["version"]  # curation is not an edit of the cut
    out = client.post(f"{base}/rescore").json()
    assert out["highlights"] == len(before) and out["added"] == 0 and out["removed"] == 0
    after = client.get(f"{base}/highlights").json()
    assert {h["id"] for h in after} == {h["id"] for h in before}  # ids (and so videos) survive
    assert next(h for h in after if h["id"] == top)["featured"] is False  # the manual choice too


def test_produce_makes_first_person_videos_only_for_featured_plays(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    rows = client.get(f"{base}/highlights").json()
    client.patch(f"/api/v1/highlights/{rows[-1]['id']}", json={"featured": False})
    featured = sum(1 for h in client.get(f"{base}/highlights").json() if h["featured"])
    assert 0 < featured < len(rows)
    body = client.post(f"{base}/produce", json={"renderers": ["ingame"], "formats": ["16:9"]}).json()
    assert body["featured"] == featured and str(featured) in body["detail"]
    everything = client.post(f"{base}/produce", json={"renderers": ["ingame"], "formats": ["16:9"], "pov": "all"}).json()
    assert everything["featured"] == len(rows)
    named = client.post(f"{base}/produce", json={"renderers": ["ingame"], "formats": ["16:9"],
                                                 "highlight_ids": [rows[-1]["id"]]}).json()
    assert named["featured"] == 1  # naming a play asks for it whatever the curation says
    both = client.post(f"{base}/produce", json={"renderers": ["radar"], "formats": ["16:9"]}).json()
    assert both["render_jobs"] == len(rows) and both["featured"] == 0  # 2D replays for every play


def test_a_manual_choice_can_be_handed_back_and_an_edited_play_is_not_duplicated(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    before = client.get(f"{base}/highlights").json()
    top = before[0]["id"]
    assert client.patch(f"/api/v1/highlights/{top}", json={"featured": False}).json()["featured"] is False
    back = client.patch(f"/api/v1/highlights/{top}", json={"featured": None}).json()
    assert back["featured"] is True and back["featured_rank"] == 1  # explicit null: the curation decides again
    assert client.patch(f"/api/v1/highlights/{top}", json={"title": "same"}).json()["featured"] is True  # absent != null
    # An agent edits the cut: the play stops being "auto" but re-scoring must still recognise it.
    edited = client.post(f"/api/v1/highlights/{top}/edit", json={"ops": [
        {"op": "trim", "segment": 0, "start_delta_s": 0.5, "end_delta_s": 0}], "origin": "agent"})
    assert edited.status_code == 200 and edited.json()["origin"] == "agent"
    cut = edited.json()["segments"]
    out = client.post(f"{base}/rescore").json()
    after = client.get(f"{base}/highlights").json()
    assert out["added"] == 0 and len(after) == len(before)  # no second copy of the edited play
    same = next(h for h in after if h["id"] == top)
    assert same["segments"] == cut and same["tier"] == before[0]["tier"] and same["featured"] is True


# ------------------------------------------------------------------------- public viewer / organiser

@pytest.fixture
def admin_key(monkeypatch):
    """Reading stays open; changing anything needs the organiser's key."""
    monkeypatch.setattr(get_settings(), "admin_key", "s3cret-organiser")
    return {"Authorization": "Bearer s3cret-organiser"}


def test_visitors_can_read_but_only_the_organiser_can_change(client, match_id, admin_key):
    base = f"/api/v1/matches/{match_id}"
    assert client.get("/api/v1/session").json() == {"admin": False, "admin_key_required": True}
    assert client.get("/api/v1/session", headers=admin_key).json()["admin"] is True
    rows = client.get(f"{base}/highlights").json()  # reading is open
    assert rows and client.get("/api/v1/feed").status_code == 200
    hid = rows[0]["id"]
    for method, url, body in [("post", f"{base}/produce", {"renderers": ["radar"], "formats": ["16:9"]}),
                              ("patch", f"/api/v1/highlights/{hid}", {"featured": False}),
                              ("post", f"{base}/rescore", None), ("delete", f"/api/v1/highlights/{hid}", None)]:
        r = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert r.status_code == 403 and "organiser" in r.json()["detail"], (method, url)
    assert client.get(f"/api/v1/highlights/{hid}").json()["featured"] is True  # nothing changed
    ok = client.patch(f"/api/v1/highlights/{hid}", json={"title": "by the organiser"}, headers=admin_key)
    assert ok.status_code == 200 and ok.json()["title"] == "by the organiser"
    assert client.patch(f"/api/v1/highlights/{hid}", json={"title": "x"},
                        headers={"X-API-Key": "wrong"}).status_code == 403


def test_without_an_admin_key_everyone_is_the_organiser(client):
    assert client.get("/api/v1/session").json() == {"admin": True, "admin_key_required": False}


def test_feed_can_be_cut_to_a_night_and_paged(client, match_id):
    from datetime import UTC, datetime, timedelta

    hid = first_highlight(match_id)
    r = client.post(f"/api/v1/highlights/{hid}/renders", json={"renderer": "radar"}).json()["render"]
    with session_scope() as s:
        row = s.get(service.Render, r["id"])
        row.status, row.video_path, row.width, row.height = "ready", "renders/x.mp4", 1280, 720
        s.get(Match, match_id).played_at = datetime(2026, 11, 13, 23, 30, tzinfo=UTC)
    ids = lambda q: [i["highlight"]["id"] for i in client.get(f"/api/v1/feed?{q}").json()]  # noqa: E731
    night = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
    fmt = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731
    assert hid in ids(f"since={fmt(night)}&until={fmt(night + timedelta(hours=12))}")
    assert hid not in ids(f"since={fmt(night + timedelta(days=1))}")
    assert hid not in ids(f"until={fmt(night)}")
    everything = ids("limit=200")
    assert ids("limit=200&offset=1") == everything[1:]
    paged = client.get("/api/v1/feed?limit=1")
    assert int(paged.headers["x-total-count"]) == len(everything) >= len(paged.json()) == 1
    # a match the site hid: its plays leave the feed and the count (an unknown id changes nothing)
    assert ids("limit=200&exclude=m_nope") == everything
    hidden = client.get(f"/api/v1/feed?limit=200&exclude={match_id}&exclude=m_nope")
    assert all(i["match"]["id"] != match_id for i in hidden.json())
    assert int(hidden.headers["x-total-count"]) == len(hidden.json()) < len(everything)


def test_share_page_has_a_preview_and_opens_the_app(client, match_id):
    hid = first_highlight(match_id)
    r = client.get(f"/p/{hid}", follow_redirects=False)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert 'property="og:title"' in r.text and f"#/play/{hid}" in r.text and "de_test" in r.text
    assert client.get("/p/h_nope").status_code == 404
    assert f"#/play/{hid}?view=ingame-9x16" in client.get(f"/p/{hid}?view=ingame-9x16").text
    assert client.get(f"/p/{hid}?view=<script>").status_code == 422


def test_api_answers_are_compressed_and_brand_carries_ui_options(client, match_id):
    r = client.get(f"/api/v1/matches/{match_id}/analysis", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert client.get("/api/v1/brand").json()["ui"] == {}


def test_a_new_cut_from_the_model_makes_videos_stale_but_an_edited_cut_is_left_alone(client, match_id):
    base = f"/api/v1/matches/{match_id}"
    rows = client.get(f"{base}/highlights").json()
    auto, edited_id = rows[0], rows[1]["id"]
    edited = client.post(f"/api/v1/highlights/{edited_id}/edit", json={"ops": [
        {"op": "trim", "segment": 0, "start_delta_s": 0.25, "end_delta_s": 0}]}).json()
    with session_scope() as s:  # pretend the stored cut came from an older model
        h = s.get(Highlight, auto["id"])
        h.segments = [{**h.segments[0], "start_tick": h.segments[0]["start_tick"] - 64}, *h.segments[1:]]
    client.post(f"{base}/rescore")
    after = {h["id"]: h for h in client.get(f"{base}/highlights").json()}
    assert after[auto["id"]]["segments"] == auto["segments"] and after[auto["id"]]["version"] == auto["version"] + 1
    assert after[edited_id]["segments"] == edited["segments"] and after[edited_id]["version"] == edited["version"]
    again = client.post(f"{base}/rescore").json()  # nothing changed: nothing goes stale
    assert again["removed"] == 0
    assert client.get(f"/api/v1/highlights/{auto['id']}").json()["version"] == auto["version"] + 1


def test_visitors_never_get_free_chat_text(client, match_id, monkeypatch):
    with session_scope() as s:
        m = s.get(Match, match_id)
        path = service.abs_path(m.analysis_path)
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    data["chat"] = [{"tick": 10, "name": "A", "steamid": "ct1", "text": "kkkkk", "kind": "laugh"},
                    {"tick": 20, "name": "B", "steamid": "t1", "text": "me liga 11 98888-7777 rua das flores 123", "kind": "talk"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    full = client.get(f"/api/v1/matches/{match_id}/analysis").json()["chat"]
    assert full[1]["text"].startswith("me liga")  # no organiser key configured: everybody is the organiser
    monkeypatch.setattr(get_settings(), "admin_key", "k")
    seen = client.get(f"/api/v1/matches/{match_id}/analysis").json()["chat"]
    assert seen[0]["text"] == "kkkkk" and seen[1]["text"] == "" and seen[1]["redacted"] is True
    again = client.get(f"/api/v1/matches/{match_id}/analysis", headers={"Authorization": "Bearer k"}).json()["chat"]
    assert again[1]["text"].startswith("me liga")
