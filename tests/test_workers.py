"""Remote workers: the protocol a machine with the game speaks to take jobs from this server.

Everything here plays the worker by hand through the HTTP API - no second machine, no game, no real video.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from demoreel import jobs, service, workers
from demoreel.config import get_settings
from demoreel.db import (
    Highlight,
    Job,
    Match,
    Render,
    Worker,
    WorkerMaster,
    init_db,
    new_id,
    now,
    session_scope,
)
from demoreel.render.ingame import xray

FAKE_DEMO = service.CS2_MAGIC + b"\x00" * 64
FAKE_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200
FAKE_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 50
W = "/api/v1/worker"


@pytest.fixture(scope="module")
def client():
    from demoreel.api import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def quiet_queue():
    """Other tests leave jobs behind; a worker takes the oldest one, so each test starts with an empty queue."""
    init_db()
    with session_scope() as s:
        s.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="canceled"))
    yield


def _match(sample_analysis, *, analyzed: bool = True) -> str:
    settings = get_settings()
    with session_scope() as s:
        m = Match(sha256=f"w-{new_id('x')}", filename="synthetic.dem", size_bytes=len(FAKE_DEMO),
                  demo_path="demos/x.dem", status="ready" if analyzed else "queued", map="de_test", tickrate=64,
                  total_ticks=sample_analysis.total_ticks, rounds=2)
        s.add(m)
        s.flush()
        m.demo_path = f"demos/{m.id}.dem"
        settings.path("demos", f"{m.id}.dem").write_bytes(FAKE_DEMO)
        if analyzed:
            m.analysis_path = f"matches/{m.id}/analysis.json"
            settings.path("matches", m.id, "analysis.json").write_text(sample_analysis.model_dump_json(),
                                                                       encoding="utf-8")
            for c in sample_analysis.highlights:
                h = Highlight(match_id=m.id, kind=c.kind, title=c.title, description=c.description, origin="auto")
                service.apply_candidate(h, c)
                s.add(h)
        return m.id


@pytest.fixture
def match_id(sample_analysis) -> str:
    return _match(sample_analysis)


class Remote:
    """A registered worker and the few calls it makes."""

    def __init__(self, client: TestClient, capabilities: list[str]):
        self.client = client
        with session_scope() as s:
            w, self.token = workers.create_worker(s, f"pc-{new_id('t')}", capabilities)
            self.id, self.name = w.id, w.name
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def claim(self, *capabilities: str):
        return self.client.post(f"{W}/claim", json={"capabilities": list(capabilities) or ["cpu"]}, headers=self.auth)

    def send(self, job_id: str, name: str, data: bytes, sha: str | None = None):
        headers = {**self.auth, "X-Content-SHA256": sha or hashlib.sha256(data).hexdigest()}
        return self.client.put(f"{W}/jobs/{job_id}/files/{name}", content=data, headers=headers)

    def finish(self, job_id: str, status: str = "succeeded", **body):
        return self.client.post(f"{W}/jobs/{job_id}/finish", json={"status": status, **body}, headers=self.auth)

    def beat(self, job_id: str, progress: float = 0.5):
        return self.client.post(f"{W}/jobs/{job_id}/heartbeat", json={"progress": progress, "message": "Working"},
                                headers=self.auth)

    def remove(self) -> None:
        with session_scope() as s:
            if s.get(Worker, self.id) is not None:
                workers.remove_worker(s, self.id)


@pytest.fixture
def remote(client):
    made: list[Remote] = []

    def make(*capabilities: str) -> Remote:
        made.append(Remote(client, list(capabilities) or ["cpu"]))
        return made[-1]

    yield make
    for r in made:  # a worker left behind would make every later test believe the server can record
        r.remove()


def _render_job(match_id: str, renderer: str = "radar") -> tuple[str, str]:
    with session_scope() as s:
        hid = service.list_highlights(s, match_id)[0].id
        r, j = service.create_render(s, match_id, [hid], renderer=renderer)
        return r.id, j.id


RESULT = {"duration_s": 12.5, "width": 1280, "height": 720, "fps": 30, "body_offset_s": 2.0,
          "kill_marks": [{"tick": 2000, "t": 3.25}], "warnings": []}


# ------------------------------------------------------------------------------------------- who may speak

def test_the_token_exists_in_clear_only_when_the_worker_is_registered(client, remote):
    pc = remote("cpu", "ingame")
    assert pc.token.startswith("drw_") and len(pc.token) > 40
    with session_scope() as s:
        row = s.get(Worker, pc.id)
        assert row.token_hash == hashlib.sha256(pc.token.encode()).hexdigest() and pc.token not in str(row.__dict__)
    me = client.get(f"{W}/me", headers=pc.auth).json()
    assert me["name"] == pc.name and me["capabilities"] == ["cpu", "ingame"] and me["online"] is True
    with session_scope() as s, pytest.raises(service.Conflict):
        workers.create_worker(s, pc.name.upper(), ["cpu"])  # names are unique, whatever the case
    with session_scope() as s, pytest.raises(service.ServiceError, match="Valid: cpu, ingame"):
        workers.create_worker(s, "other", ["gpu"])


def test_worker_routes_take_a_worker_token_and_nothing_else(client, remote, monkeypatch):
    pc = remote("cpu")
    monkeypatch.setattr(get_settings(), "admin_key", "organiser-key")
    organiser = {"Authorization": "Bearer organiser-key"}
    assert client.post(f"{W}/claim", json={}).status_code == 401
    assert client.post(f"{W}/claim", json={}, headers=organiser).status_code == 401  # not a worker token
    assert client.get(f"{W}/me", headers={"Authorization": "Bearer drw_nope"}).status_code == 401
    assert "demoreel workers add" in client.get(f"{W}/me").json()["detail"]
    # and a worker's token gives none of the organiser's rights
    assert client.post("/api/v1/workers", json={"name": "x"}, headers=pc.auth).status_code == 403
    assert client.get("/api/v1/workers", headers=pc.auth).status_code == 403
    assert client.get("/api/v1/workers").status_code == 403
    listed = client.get("/api/v1/workers", headers=organiser).json()
    assert pc.name in [w["name"] for w in listed] and "token" not in str(listed)
    made = client.post("/api/v1/workers", json={"name": "from-api", "capabilities": ["cpu"]}, headers=organiser)
    assert made.status_code == 201 and made.json()["token"].startswith("drw_")
    assert client.delete("/api/v1/workers/from-api", headers=organiser).status_code == 204
    assert client.get(f"{W}/me", headers={"Authorization": f"Bearer {made.json()['token']}"}).status_code == 401


def test_nothing_to_do_is_a_204_and_a_worker_only_gets_what_it_is_allowed(remote, match_id):
    pc = remote("cpu")
    assert pc.claim("cpu").status_code == 204
    refused = pc.claim("ingame")
    assert refused.status_code == 400 and "may only take: cpu" in refused.json()["detail"]
    assert pc.claim("warp").status_code == 400
    with session_scope() as s:  # a job that writes to the server's own disk is never handed out
        jobs.enqueue(s, "import_remote", {"source_id": "x", "remote_id": "1"})
    assert pc.claim("cpu").status_code == 204


# ------------------------------------------------------------------------------------------- analysis

def test_a_worker_analyses_a_demo_and_the_server_keeps_the_result(client, remote, sample_analysis):
    match_id = _match(sample_analysis, analyzed=False)
    with session_scope() as s:
        job_id = jobs.enqueue(s, "analyze_match", {"match_id": match_id}, match_id=match_id).id
    pc = remote("cpu")
    spec = pc.claim("cpu").json()
    assert spec["job"] == {"id": job_id, "type": "analyze_match", "attempt": 1, "payload": {"match_id": match_id}}
    assert spec["inputs"] == {"demo.dem": True, "analysis.json": False} and list(spec["uploads"]) == ["analysis.json"]
    assert spec["match"]["size_bytes"] == len(FAKE_DEMO)
    with session_scope() as s:
        assert s.get(Match, match_id).status == "analyzing"
    assert client.get(f"{W}/jobs/{job_id}/files/demo.dem", headers=pc.auth).content == FAKE_DEMO
    assert client.get(f"{W}/jobs/{job_id}/files/analysis.json", headers=pc.auth).status_code == 404

    early = pc.finish(job_id)  # nothing was sent yet: refused, and the job stays with the worker
    assert early.status_code == 400 and "Upload analysis.json" in early.json()["detail"]
    data = sample_analysis.model_dump_json().encode()
    damaged = pc.send(job_id, "analysis.json", data, sha="0" * 64)
    assert damaged.status_code == 400 and "damaged" in damaged.json()["detail"]
    junk = b'{"map": 3}'
    assert pc.send(job_id, "analysis.json", junk).status_code == 200
    assert "analysis schema" in pc.finish(job_id).json()["detail"]
    assert pc.send(job_id, "analysis.json", data).json()["size_bytes"] == len(data)

    done = pc.finish(job_id).json()
    assert done["status"] == "succeeded" and done["result"]["highlights"] == len(sample_analysis.highlights)
    with session_scope() as s:
        m = s.get(Match, match_id)
        assert m.status == "ready" and m.map == "de_test" and service.load_analysis(m).map == "de_test"
        assert len(service.list_highlights(s, match_id)) > 0
    assert not workers._upload_dir(job_id).exists()
    assert pc.beat(job_id).status_code == 409  # closed: no longer the worker's


# ------------------------------------------------------------------------------------------- videos

def test_a_worker_makes_a_video_and_the_server_publishes_it(client, remote, match_id):
    render_id, job_id = _render_job(match_id)
    pc = remote("cpu")
    spec = pc.claim("cpu").json()
    assert spec["job"]["id"] == job_id and spec["render"]["id"] == render_id and spec["render"]["renderer"] == "radar"
    assert spec["inputs"] == {"demo.dem": True, "analysis.json": True} and spec["defaults"]["render_fps"] == 30
    clip = spec["clips"][0]
    assert clip["segments"][0].keys() >= {"start_tick", "end_tick", "pov_steamid", "speed", "role"}
    assert client.get(f"/api/v1/renders/{render_id}").json()["status"] == "running"
    assert pc.beat(job_id, 0.4).json() == {"cancel_requested": False}
    assert client.get(f"/api/v1/jobs/{job_id}").json()["message"] == "Working"

    assert pc.send(job_id, "video.mp4", b"this is not a video").status_code == 200
    refused = pc.finish(job_id, result=RESULT)
    assert refused.status_code == 400 and "not an MP4" in refused.json()["detail"]
    assert pc.send(job_id, "video.mp4", FAKE_MP4).status_code == 200
    assert pc.send(job_id, "thumbnail.jpg", FAKE_JPG).status_code == 200
    bad = pc.finish(job_id, result={**RESULT, "fps": 0})
    assert bad.status_code == 400 and "fps" in bad.json()["detail"]

    done = pc.finish(job_id, result=RESULT).json()
    assert done["status"] == "succeeded" and done["result"]["worker"] == pc.name
    out = client.get(f"/api/v1/renders/{render_id}").json()
    assert out["status"] == "ready" and out["duration_s"] == 12.5 and out["size_bytes"] == len(FAKE_MP4)
    assert out["video_url"].endswith(f"/files/renders/{render_id}.mp4") and out["thumbnail_url"].endswith(".jpg")
    assert client.get(f"/files/renders/{render_id}.mp4").content == FAKE_MP4
    with session_scope() as s:
        r = s.get(Render, render_id)
        assert r.kill_marks == [{"tick": 2000, "t": 3.25}] and r.body_offset_s == 2.0


def test_only_the_files_a_job_produces_are_accepted(remote, match_id, monkeypatch):
    _, job_id = _render_job(match_id)
    pc = remote("cpu")
    assert pc.claim("cpu").json()["job"]["id"] == job_id
    wrong = pc.send(job_id, "notes.txt", b"hello")
    assert wrong.status_code == 404 and "video.mp4, thumbnail.jpg" in wrong.json()["detail"]
    assert pc.send(job_id, "../demoreel.db", b"x").status_code in (404, 405)
    no_hash = pc.client.put(f"{W}/jobs/{job_id}/files/video.mp4", content=FAKE_MP4, headers=pc.auth)
    assert no_hash.status_code == 400 and "X-Content-SHA256" in no_hash.json()["detail"]
    assert pc.send(job_id, "thumbnail.jpg", b"x" * (9 * 1024 * 1024)).status_code == 413
    monkeypatch.setattr(get_settings(), "worker_max_file_mb", 0)
    assert pc.send(job_id, "video.mp4", FAKE_MP4).status_code == 413
    assert not list(workers._upload_dir(job_id).glob("*"))  # nothing half-written is left behind


def test_a_job_belongs_to_the_worker_that_took_it_until_it_goes_quiet(remote, match_id):
    _, job_id = _render_job(match_id)
    one, two = remote("cpu"), remote("cpu")
    assert one.claim("cpu").json()["job"]["id"] == job_id
    assert two.claim("cpu").status_code == 204
    assert two.beat(job_id).status_code == 409 and two.finish(job_id, result=RESULT).status_code == 409
    assert two.send(job_id, "video.mp4", FAKE_MP4).status_code == 409
    with session_scope() as s:  # the first worker dies: no heartbeat for longer than the limit
        s.get(Job, job_id).heartbeat_at = now() - jobs.STALE_AFTER - timedelta(seconds=5)
    assert jobs.requeue_stale() >= 1
    gone = one.beat(job_id)
    assert gone.status_code == 409 and "not yours any more" in gone.json()["detail"]
    assert two.claim("cpu").json()["job"]["attempt"] == 2  # ...and somebody else picks it up


def test_a_job_can_be_given_back_or_failed_with_a_retry(remote, match_id):
    render_id, job_id = _render_job(match_id)
    pc = remote("cpu")
    pc.claim("cpu")
    back = pc.finish(job_id, "released", error="players joined the server").json()
    assert back["status"] == "queued" and "players joined" in back["message"]
    with session_scope() as s:
        assert s.get(Job, job_id).attempts == 0 and s.get(Render, render_id).status == "queued"
    assert pc.claim("cpu").json()["job"]["attempt"] == 1
    assert pc.finish(job_id, "failed", error="disk full", retry=True).json()["status"] == "queued"
    assert pc.claim("cpu").json()["job"]["attempt"] == 2
    final = pc.finish(job_id, "failed", error="disk full", retry=True).json()  # second attempt: it gives up
    assert final["status"] == "failed" and final["error"] == "disk full"
    with session_scope() as s:
        r = s.get(Render, render_id)
        assert r.status == "failed" and r.error == "disk full"


def test_a_cancellation_reaches_the_worker_through_the_heartbeat(client, remote, match_id):
    render_id, job_id = _render_job(match_id)
    pc = remote("cpu")
    pc.claim("cpu")
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").json()["status"] == "running"
    assert pc.beat(job_id).json() == {"cancel_requested": True}
    assert pc.finish(job_id, "canceled").json()["status"] == "canceled"
    assert client.get(f"/api/v1/renders/{render_id}").json()["status"] == "canceled"


def test_a_video_deleted_meanwhile_ends_the_job_as_canceled(client, remote, match_id):
    render_id, job_id = _render_job(match_id)
    pc = remote("cpu")
    pc.claim("cpu")
    pc.send(job_id, "video.mp4", FAKE_MP4)
    with session_scope() as s:
        s.delete(s.get(Render, render_id))
    assert pc.finish(job_id, result=RESULT).json()["status"] == "canceled"
    assert not get_settings().path("renders", f"{render_id}.mp4").exists()


# ------------------------------------------------------------------------------------------- footage

def _masters_for(spec: dict) -> list[dict]:
    """What a worker reports after recording the clips of a capture job: one take per segment, with handles."""
    covered = [{"start_tick": seg["start_tick"] - 64, "end_tick": seg["end_tick"] + 64, "pov": seg["pov_steamid"]}
               for clip in spec["clips"] for seg in clip["segments"]]
    return [{"id": f"ms_{new_id('x')[-8:]}", "match_id": spec["match"]["id"], "width": 1920, "height": 1080,
             "fps": 120, "has_audio": True, "options": {}, "xray": xray.mode({}),
             "spans": [{"start_tick": c["start_tick"], "end_tick": c["end_tick"], "frames": 600} for c in covered],
             "covered": covered}]


def test_recording_goes_to_a_worker_with_the_game_and_the_edits_follow_the_footage(client, remote, match_id):
    with session_scope() as s:
        hid = service.list_highlights(s, match_id)[0].id
        with pytest.raises(service.Conflict):  # nobody can record yet
            service.create_render(s, match_id, [hid], renderer="ingame")
    recorder, editor = remote("cpu", "ingame"), remote("cpu")
    ingame = next(r for r in client.get("/api/v1/capabilities").json()["renderers"] if r["name"] == "ingame")
    assert ingame["available"] is True and ingame["details"]["remote_workers"] == [recorder.name]

    with session_scope() as s:
        out = service.produce(s, match_id, renderers=["ingame"], formats=["16:9", "9:16"], highlight_ids=[hid])
        s.flush()
        capture_id = out["capture_job"].id
        assert out["render_jobs"] == 0 and out["pending_after_capture"] == 2 and out["detail"] is None
    assert editor.claim("cpu").status_code == 204  # recording needs the game
    assert recorder.claim("cpu").status_code == 204  # ...and the recorder is busy hosting a match right now
    assert jobs.claim("local:1", ["cpu"]) is None
    spec = recorder.claim("cpu", "ingame").json()
    assert spec["job"]["id"] == capture_id and spec["job"]["type"] == "capture_match" and spec["uploads"] == {}
    assert spec["clips"][0]["highlight_id"] == hid

    missing = recorder.finish(capture_id, result={"takes": 2})
    assert missing.status_code == 400 and "masters" in missing.json()["detail"]
    done = recorder.finish(capture_id, result={"masters": _masters_for(spec), "takes": 2, "capture_fps": 120}).json()
    assert done["status"] == "succeeded" and done["result"]["renders_queued"] == 2 and done["result"]["masters"] == 1

    # The two videos need that footage: they wait for the recorder, whoever else asks for work.
    with session_scope() as s:
        queued = s.scalars(service.select(Job).where(Job.status == "queued", Job.type == "render")).all()
        assert len(queued) == 2 and {j.target_worker for j in queued} == {recorder.id}
        assert {j.capability for j in queued} == {"cpu"}  # editing: no game needed
    assert editor.claim("cpu").status_code == 204
    assert jobs.claim("local:1", ["cpu", "ingame"]) is None
    edit = recorder.claim("cpu").json()
    assert edit["job"]["type"] == "render" and edit["render"]["renderer"] == "ingame"
    assert client.get("/api/v1/workers").json()[-2]["masters"] == 1

    # A new cut of the same play is addressed to the recorder too, without recording again...
    with session_scope() as s:
        _, again = service.create_render(s, match_id, [hid], renderer="ingame",
                                         options=service.RenderOptionsIn(format="1:1"))
        assert (again.capability, again.target_worker) == ("cpu", recorder.id)
    # ...until the recorder says the footage is gone: then the play has to be recorded again.
    wiped = client.put(f"{W}/masters", json={"masters": []}, headers=recorder.auth)
    assert wiped.json() == {"masters": 0}
    with session_scope() as s:
        _, fresh = service.create_render(s, match_id, [hid], renderer="ingame",
                                         options=service.RenderOptionsIn(format="1:1"))
        assert (fresh.capability, fresh.target_worker) == ("ingame", None)


def test_removing_a_worker_fails_what_only_it_could_do_and_requeues_the_rest(remote, match_id):
    recorder = remote("cpu", "ingame")
    with session_scope() as s:
        hid = service.list_highlights(s, match_id)[0].id
        segs = s.get(Highlight, hid).segments
        w = s.get(Worker, recorder.id)
        workers.sync_masters(s, w, _masters_for({"match": {"id": match_id}, "clips": [{"segments": segs}]}))
        r, targeted = service.create_render(s, match_id, [hid], renderer="ingame")
        assert targeted.target_worker == recorder.id
        render_id, targeted_id = r.id, targeted.id
    _, radar_job = _render_job(match_id)
    with session_scope() as s:  # the radar video is the older request of the two the recorder may take
        s.get(Job, radar_job).priority = 1
    assert recorder.claim("cpu").json()["job"]["id"] == radar_job
    recorder.remove()
    assert recorder.beat(radar_job).status_code == 401
    with session_scope() as s:
        assert s.get(Job, radar_job).status == "queued" and s.get(Job, radar_job).worker_id is None
        gone = s.get(Job, targeted_id)
        assert gone.status == "failed" and recorder.name in gone.error and "recorded once more" in gone.error
        assert s.get(Render, render_id).status == "failed"
        assert s.scalar(service.select(WorkerMaster.id).where(WorkerMaster.worker_id == recorder.id)) is None


def test_a_report_of_footage_must_describe_masters(client, remote):
    pc = remote("cpu", "ingame")
    bad = client.put(f"{W}/masters", json={"masters": [{"id": "ms_1"}]}, headers=pc.auth)
    assert bad.status_code == 400 and "not a master description" in bad.json()["detail"]


# ------------------------------------------------------------------------------------------- brand kit

def test_the_brand_kit_of_a_job_is_served_to_its_worker_only(client, remote, match_id):
    kit = get_settings().data_dir / "brands" / "workerkit"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / "brand.json").write_text('{"name": "Worker Kit"}', encoding="utf-8")
    (kit / "logo.png").write_bytes(b"\x89PNG fake")
    with session_scope() as s:
        s.get(Match, match_id).brand = "workerkit"
    try:
        _, job_id = _render_job(match_id)
        pc, other = remote("cpu"), remote("cpu")
        assert pc.claim("cpu").json()["brand"] == "workerkit"
        manifest = client.get(f"{W}/jobs/{job_id}/brand", headers=pc.auth).json()
        assert manifest["slug"] == "workerkit" and [f["path"] for f in manifest["files"]] == ["brand.json", "logo.png"]
        assert manifest["files"][1]["sha256"] == hashlib.sha256(b"\x89PNG fake").hexdigest()
        assert client.get(f"{W}/jobs/{job_id}/brand/logo.png", headers=pc.auth).content == b"\x89PNG fake"
        assert client.get(f"{W}/jobs/{job_id}/brand/..%2F..%2Fdemoreel.db", headers=pc.auth).status_code == 404
        assert client.get(f"{W}/jobs/{job_id}/brand", headers=other.auth).status_code == 409
    finally:
        with session_scope() as s:
            s.get(Match, match_id).brand = None
        for f in kit.iterdir():
            f.unlink()
        kit.rmdir()


# ------------------------------------------------------------------------------------------- command line

def test_workers_are_managed_from_the_command_line():
    from typer.testing import CliRunner

    from demoreel.cli import app

    run = CliRunner()
    name = f"cli-{new_id('t')}"
    added = run.invoke(app, ["workers", "add", name, "-c", "cpu", "-c", "ingame"])
    assert added.exit_code == 0 and "cpu, ingame" in added.output
    token = added.output.strip().splitlines()[-1]
    with session_scope() as s:
        assert workers.authenticate(s, token).name == name
    assert run.invoke(app, ["workers", "add", name]).exit_code == 1  # the name is taken
    listed = run.invoke(app, ["workers", "list"])
    assert name in listed.output and "offline" not in [ln for ln in listed.output.splitlines() if name in ln][0]
    assert run.invoke(app, ["workers", "remove", name]).exit_code == 0
    with session_scope() as s, pytest.raises(workers.Unauthorized):
        workers.authenticate(s, token)
    assert run.invoke(app, ["workers", "remove", name]).exit_code == 1
