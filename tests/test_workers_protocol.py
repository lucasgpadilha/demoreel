"""Remote workers: what a worker program says it understands, and which footage it may claim to keep.

Played by hand through the HTTP API, like tests/test_workers.py (whose fixtures are reused)."""

from __future__ import annotations

from test_workers import FAKE_MP4, RESULT, W, _masters_for, client, match_id, quiet_queue, remote  # noqa: F401

from demoreel import service, workers
from demoreel.db import Highlight, Job, Match, session_scope

CAMERA = {"x": -1520.5, "y": 410.0, "z": 96.0, "pitch": 12.5, "yaw": -90.0}


def _filmed_from_a_fixed_camera(mid: str) -> str:
    """Turn the first play of the match into one watched from a fixed spot; returns its id."""
    with session_scope() as s:
        h = service.list_highlights(s, mid)[0]
        h.segments = [{**seg, "pov_steamid": None, "camera": dict(CAMERA)} for seg in (h.segments or [])][:1]
        return h.id


def _render(mid: str, hid: str) -> str:
    with session_scope() as s:
        return service.create_render(s, mid, [hid], renderer="radar")[1].id


def _claim(c, pc, protocol: int | None = None):
    body = {"capabilities": ["cpu"], **({"info": {"protocol": protocol}} if protocol is not None else {})}
    return c.post(f"{W}/claim", json=body, headers=pc.auth)


def test_a_worker_that_does_not_know_fixed_cameras_is_not_handed_them(client, remote, match_id):  # noqa: F811
    """An older worker program would take "cam:x,y,z,pitch,yaw" for a SteamID and fail ("no spectator slot")."""
    old, new = remote("cpu"), remote("cpu")
    camera_job = _render(match_id, _filmed_from_a_fixed_camera(match_id))
    with session_scope() as s:
        plain = service.list_highlights(s, match_id)[1].id
    plain_job = _render(match_id, plain)

    got = _claim(client, old)  # says nothing about its protocol: the first one
    assert got.status_code == 200 and got.json()["job"]["id"] == plain_job  # the camera job is passed over...
    with session_scope() as s:
        row = s.get(Job, camera_job)
        assert (row.status, row.attempts, row.worker_id) == ("queued", 0, None)  # ...left in its place
        assert "fixed cameras" in row.message and old.name in row.message
    assert _claim(client, old, protocol=1).status_code == 204

    spec = _claim(client, new, protocol=workers.WORKER_PROTOCOL).json()
    assert spec["job"]["id"] == camera_job
    (seg,) = spec["clips"][0]["segments"]
    assert seg["pov_steamid"].startswith("cam:") and seg["camera"] == CAMERA
    assert workers.worker_protocol({"protocol": "x"}) == 1 and workers.worker_protocol(None) == 1


def test_a_job_result_can_only_describe_footage_of_its_own_match(client, remote, match_id, sample_analysis):  # noqa: F811
    from test_workers import _match

    other_match = _match(sample_analysis)
    pc = remote("cpu")
    job_id = _render(match_id, _first(match_id))
    spec = _claim(client, pc).json()
    assert spec["job"]["id"] == job_id
    assert pc.send(job_id, "video.mp4", FAKE_MP4).status_code == 200
    foreign = _masters_for({"match": {"id": other_match}, "clips": spec["clips"]})
    refused = pc.finish(job_id, result={**RESULT, "masters": foreign})
    assert refused.status_code == 400 and other_match in refused.json()["detail"]
    with session_scope() as s:
        assert s.get(Job, job_id).status == "running"  # still the worker's: it may send the right result
    ok = pc.finish(job_id, result={**RESULT, "masters": _masters_for(spec)})
    assert ok.status_code == 200 and ok.json()["status"] == "succeeded"
    _delete(other_match)


def test_a_full_report_keeps_only_matches_the_worker_worked_on(client, remote, match_id, sample_analysis):  # noqa: F811
    """Footage draws the edits of its match to whoever keeps it: a worker could otherwise attract the jobs of
    any match by reporting footage of it."""
    from test_workers import _match

    stranger = _match(sample_analysis)
    pc = remote("cpu", "ingame")
    _render(match_id, _first(match_id))
    spec = _claim(client, pc).json()  # it took a job of match_id: footage of that match is plausible
    mine = _masters_for(spec)
    theirs = _masters_for({"match": {"id": stranger}, "clips": spec["clips"]})
    reported = client.put(f"{W}/masters", json={"masters": [*mine, *theirs]}, headers=pc.auth)
    assert reported.json() == {"masters": 1}
    with session_scope() as s:
        assert workers.footage_holder(s, stranger, [], width=1920, height=1080, min_fps=30, extra={}) is None
    again = client.put(f"{W}/masters", json={"masters": mine}, headers=pc.auth)  # what it already reported stays
    assert again.json() == {"masters": 1}
    _delete(stranger)


def _first(mid: str) -> str:
    with session_scope() as s:
        return service.list_highlights(s, mid)[0].id


def _delete(mid: str) -> None:
    with session_scope() as s:
        if s.get(Match, mid) is not None:
            service.delete_match(s, mid)


def test_camera_segments_are_recognised_in_stored_highlights(match_id):  # noqa: F811
    hid = _filmed_from_a_fixed_camera(match_id)
    with session_scope() as s:
        job = s.get(Job, _render(match_id, hid))
        assert workers._uses_camera(s, job)
        assert s.get(Highlight, hid).segments[0]["camera"] == CAMERA
