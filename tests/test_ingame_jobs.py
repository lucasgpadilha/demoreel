"""Recording jobs and the queue: what a job that needs the game does when the game cannot record right now.

The renderer is a fake here; nothing looks at the real CS2 install of the machine running the tests."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from test_service_api import first_highlight, match_id  # noqa: F401 - the fixture is used by name

from demoreel import jobs, service
from demoreel.config import get_settings
from demoreel.db import Job, Render, session_scope
from demoreel.render.base import Availability


class Game:
    """The in-game renderer as tasks.py sees it, with an availability chosen by the test."""

    name, description = "ingame", "fake"

    def __init__(self, ok: bool, reason: str = "", transient: bool = False, left: list[str] | None = None):
        self.state = Availability(ok=ok, reason=reason, details={"transient": transient,
                                                                  "interrupted_recording": left or []})
        self.recovered = 0

    def availability(self):
        return self.state

    def recover_install(self):
        self.recovered += 1
        self.state = Availability(ok=True, details={"transient": False, "interrupted_recording": []})
        return ["gameinfo.gi restored"]

    @contextmanager
    def recorder(self, wait_s=0.0, cancelled=None, on_wait=None):
        yield

    def render(self, job):
        raise AssertionError("nothing may be recorded in these tests")


def recording_job(mid: str) -> tuple[str, str]:
    """A first-person render whose footage was never recorded, queued for a worker with the test capability."""
    hid = first_highlight(mid)
    with session_scope() as s:
        r, job = service.create_render(s, mid, [hid], renderer="radar")
        r.renderer, job.capability = "ingame", "testrecord17"
        return r.id, job.id


def test_a_recording_waits_in_the_queue_while_the_owner_plays(match_id, monkeypatch):  # noqa: F811
    """It used to fail with "in-game recording is disabled or unavailable" - the wrong reason, and a failure
    where waiting was right."""
    from demoreel import tasks

    reason = "CS2 is already running; close the game to record."
    monkeypatch.setattr(tasks, "get_renderer", lambda name: Game(ok=False, reason=reason, transient=True))
    monkeypatch.setattr(get_settings(), "ingame_enabled", True)
    monkeypatch.setitem(jobs._BACK_OFF, "s", 0.0)
    rid, jid = recording_job(match_id)

    assert not jobs.run_one("w", ["testrecord17"])
    with session_scope() as s:
        j = s.get(Job, jid)
        assert (j.status, j.attempts, j.error) == ("queued", 0, None)
        assert j.message == f"Waiting to record: {reason}"
        assert s.get(Render, rid).status == "queued"
    assert jobs._BACK_OFF["s"] == tasks.DEFER_WAIT_S  # and the worker does not take it back every second
    with session_scope() as s:
        s.get(Job, jid).status = "canceled"


def test_a_recording_that_cannot_run_here_fails_with_the_real_reason(match_id, monkeypatch):  # noqa: F811
    from demoreel import tasks

    reason = "CS Demo Manager's CS2 plugin is missing. Copy `static/cs2/server.dll` ..."
    monkeypatch.setattr(tasks, "get_renderer", lambda name: Game(ok=False, reason=reason))
    monkeypatch.setattr(get_settings(), "ingame_enabled", True)
    rid, jid = recording_job(match_id)

    assert jobs.run_one("w", ["testrecord17"])
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == "failed" and reason in j.error and "disabled" not in j.error


def test_an_interrupted_recording_is_put_back_before_the_job_gives_up(match_id, monkeypatch):  # noqa: F811
    from demoreel import tasks

    game = Game(ok=False, reason="The CS2 folder is still changed", transient=True, left=["gameinfo.gi"])
    monkeypatch.setattr(tasks, "get_renderer", lambda name: game)
    state = tasks._recorder_state()
    assert state.ok and game.recovered == 1


def test_a_whole_match_recording_waits_the_same_way(match_id, monkeypatch):  # noqa: F811
    from demoreel import tasks

    monkeypatch.setattr(tasks, "get_renderer",
                        lambda name: Game(ok=False, reason="Steam is not running. Start Steam.", transient=True))
    hid = first_highlight(match_id)
    with pytest.raises(jobs.JobDeferred, match="Waiting to record: Steam is not running") as excinfo:
        tasks.capture_match(jobs.JobContext(job_id="j_none", payload={"match_id": match_id, "highlight_ids": [hid]}))
    assert excinfo.value.wait_s == tasks.DEFER_WAIT_S
