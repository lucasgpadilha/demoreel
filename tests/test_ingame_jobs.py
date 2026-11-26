"""Recording jobs and the queue: what a job that needs the game does when the game cannot record right now.

The renderer is a fake here; nothing looks at the real CS2 install of the machine running the tests."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from test_service_api import first_highlight
from test_service_api import match_id as seeded_match  # noqa: F401 - the fixture is used by name

from demoreel import jobs, service
from demoreel.config import get_settings
from demoreel.db import Job, Render, session_scope
from demoreel.render.base import Availability


@pytest.fixture
def match_id(seeded_match):  # noqa: F811
    """The service tests' seeded match, deleted afterwards: these tests run before the insights tests, which
    count every match in the database."""
    yield seeded_match
    with session_scope() as s:
        service.delete_match(s, seeded_match)


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


def test_a_recording_waits_in_the_queue_while_the_owner_plays(match_id, monkeypatch):
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


def test_a_recording_that_cannot_run_here_fails_with_the_real_reason(match_id, monkeypatch):
    from demoreel import tasks

    reason = "CS Demo Manager's CS2 plugin is missing. Copy `static/cs2/server.dll` ..."
    monkeypatch.setattr(tasks, "get_renderer", lambda name: Game(ok=False, reason=reason))
    monkeypatch.setattr(get_settings(), "ingame_enabled", True)
    rid, jid = recording_job(match_id)

    assert jobs.run_one("w", ["testrecord17"])
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == "failed" and reason in j.error and "disabled" not in j.error


def test_an_interrupted_recording_is_put_back_before_the_job_gives_up(match_id, monkeypatch):
    from demoreel import tasks

    game = Game(ok=False, reason="The CS2 folder is still changed", transient=True, left=["gameinfo.gi"])
    monkeypatch.setattr(tasks, "get_renderer", lambda name: game)
    state = tasks._recorder_state()
    assert state.ok and game.recovered == 1


def test_a_whole_match_recording_waits_the_same_way(match_id, monkeypatch):
    from demoreel import tasks

    monkeypatch.setattr(tasks, "get_renderer",
                        lambda name: Game(ok=False, reason="Steam is not running. Start Steam.", transient=True))
    hid = first_highlight(match_id)
    with pytest.raises(jobs.JobDeferred, match="Waiting to record: Steam is not running") as excinfo:
        tasks.capture_match(jobs.JobContext(job_id="j_none", payload={"match_id": match_id, "highlight_ids": [hid]}))
    assert excinfo.value.wait_s == tasks.DEFER_WAIT_S


# --- what a render request may ask of the game ----------------------------------------------------------------


def test_console_commands_and_launch_options_are_refused_per_render_with_where_they_live_now():
    from pydantic import ValidationError

    from demoreel.schemas import RenderOptionsIn

    for key, env in (("ingame_commands", "DEMOREEL_INGAME_COMMANDS"),
                     ("ingame_launch_args", "DEMOREEL_INGAME_LAUNCH_ARGS")):
        with pytest.raises(ValidationError) as excinfo:
            RenderOptionsIn(extra={key: "anything"})
        message = str(excinfo.value)
        assert f"extra.{key} is not accepted per render" in message and env in message
        assert "ingame_hud" in message  # and what is valid


def test_only_documented_extra_keys_are_kept():
    from demoreel.schemas import RENDER_EXTRA_KEYS, RenderOptionsIn

    given = {"ingame_hud": "full", "zoom": 2, "camera": "full", "ingame_backend": "startmovie",
             "made_up": 1, "mirv_streams": "x", "brand_name": "Someone else's"}
    kept = RenderOptionsIn(extra=given).extra
    assert kept == {"ingame_hud": "full", "zoom": 2, "camera": "full", "ingame_backend": "startmovie"}
    assert {"ingame_xray", "ingame_recapture", "ingame_lock_wait_s", "brand"} <= RENDER_EXTRA_KEYS

    # options stored before this change are read back without the keys that are gone, not refused
    stored = {"format": "9:16", "extra": {"ingame_commands": ["bind x quit"], "ingame_hud": "full"}}
    old = RenderOptionsIn.model_validate(stored, context={"stored": True})
    assert old.format == "9:16" and old.extra == {"ingame_hud": "full"}


def test_a_worker_is_handed_the_documented_options_only(match_id):
    from demoreel import workers

    with session_scope() as s:
        r, job = service.create_render(s, match_id, [first_highlight(match_id)], renderer="radar")
        r.options = {"format": "1:1", "extra": {"ingame_commands": "bind x quit", "ingame_hud": "full",
                                                "made_up": True}}  # as a render stored before this change
        s.flush()
        spec = workers.job_spec(s, job)
    assert spec["render"]["options"] == {"format": "1:1", "extra": {"ingame_hud": "full"}}


def test_the_machine_sets_its_own_commands_and_launch_options(tmp_path):
    from demoreel.render.ingame import settings as ingame_settings

    s = ingame_settings.load({"DEMOREEL_INGAME_COMMANDS": "csdm_voice_indicator 1; cl_drawhud_force_radar 0\nfov 90",
                              "DEMOREEL_INGAME_LAUNCH_ARGS": '-noborder -language "brazilian portuguese"'},
                             data_dir=tmp_path / "data", platform="linux")
    assert s.console_commands == ("csdm_voice_indicator 1", "cl_drawhud_force_radar 0", "fov 90")
    assert s.launch_args == ("-noborder", "-language", "brazilian portuguese")
    win = ingame_settings.load({"DEMOREEL_INGAME_LAUNCH_ARGS": '-noborder -x "C:\\a b"'},
                               data_dir=tmp_path / "data", platform="win32")
    assert win.launch_args == ("-noborder", "-x", "C:\\a b")
    broken = ingame_settings.load({"DEMOREEL_INGAME_LAUNCH_ARGS": '-x "unbalanced'}, data_dir=tmp_path / "d")
    assert broken.launch_args == ()


def test_the_mcp_render_tools_say_what_is_wrong_with_extra(match_id):
    import asyncio

    from demoreel.mcp_server import build_server

    server = build_server()
    with pytest.raises(Exception) as excinfo:
        asyncio.run(server.call_tool("render_highlight", {"highlight_id": first_highlight(match_id),
                                                          "extra": {"ingame_commands": ["bind x quit"]}}))
    message = str(excinfo.value)
    assert "extra.ingame_commands is not accepted per render" in message and "DEMOREEL_INGAME_COMMANDS" in message
    assert "Value error" not in message
