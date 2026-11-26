"""The MCP tools agents use to make and change clips, called through the server object, and the key on /mcp.

The REST side of the same behaviour is tested in test_service_api.py; these tests catch a renamed parameter or
a tool that stops passing something on, which the REST tests cannot see."""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from demoreel import service
from demoreel.config import get_settings
from demoreel.db import Highlight, Match, init_db, now, session_scope

CAM = {"x": -324.4, "y": -8.2, "z": 506.0, "pitch": 38.2, "yaw": -121.3}


@pytest.fixture(scope="module")
def server():
    from demoreel.mcp_server import build_server

    return build_server()


@pytest.fixture
def call(server):
    def call(name: str, **args):
        data = anyio.run(server.call_tool, name, args).structured_content
        return data["result"] if set(data) == {"result"} else data
    return call


@pytest.fixture
def match_id(sample_analysis) -> str:
    """A 'ready' match seeded straight into the database, with the plays the analysis found."""
    init_db()
    settings = get_settings()
    with session_scope() as s:
        m = Match(sha256=f"mcp-{now().timestamp()}", filename="synthetic.dem", size_bytes=72, demo_path="x",
                  status="ready", map="de_test", tickrate=64, total_ticks=sample_analysis.total_ticks, rounds=2,
                  team_a_name="Mix da Vila", team_b_name="Os Outros", team_a_score=1, team_b_score=1)
        s.add(m)
        s.flush()
        m.demo_path, m.analysis_path = f"demos/{m.id}.dem", f"matches/{m.id}/analysis.json"
        settings.path("demos", f"{m.id}.dem").write_bytes(service.CS2_MAGIC + b"\x00" * 64)
        settings.path("matches", m.id, "analysis.json").write_text(sample_analysis.model_dump_json(), encoding="utf-8")
        for c in sample_analysis.highlights:
            h = Highlight(match_id=m.id, kind=c.kind, title=c.title, description=c.description, origin="auto")
            service.apply_candidate(h, c)
            s.add(h)
        return m.id


def test_the_clip_tools_are_registered(server):
    names = {t.name for t in anyio.run(server.list_tools)}
    assert {"create_highlight", "set_category", "edit_highlight", "render_highlight", "render_reel",
            "get_highlight", "list_highlights"} <= names


def test_a_hand_made_clip_is_created_as_banter_and_moved_back(call, match_id):
    play = call("list_highlights", match_id=match_id)[0]
    seg = call("get_highlight", highlight_id=play["id"])["segments"][0]
    made = call("create_highlight", match_id=match_id, title="caught napping", start_tick=seg["start_tick"],
                end_tick=seg["end_tick"], pov="CT1", category="fail")  # a player by name
    assert made["category"] == "fail" and made["kind"] == "custom" and made["origin"] == "agent"
    assert made["segments"][0]["pov_steamid"] == "ct1"
    assert made["id"] in [h["id"] for h in call("list_highlights", match_id=match_id, category="fail")]
    assert made["id"] not in [h["id"] for h in call("list_highlights", match_id=match_id)]
    moved = call("set_category", highlight_id=made["id"], category="play")
    assert moved["category"] == "play" and moved["id"] in [h["id"] for h in call("list_highlights", match_id=match_id)]
    by_round = call("create_highlight", match_id=match_id, round_number=1, player="ct1", category="fail")
    assert by_round["kind"] == "player_round" and by_round["category"] == "fail"


def test_what_the_analysis_found_cannot_be_moved(call, match_id):
    play = call("list_highlights", match_id=match_id)[0]
    with pytest.raises(ToolError, match="hand-made"):
        call("set_category", highlight_id=play["id"], category="fail")
    with pytest.raises(ToolError, match="'play' or 'fail'"):
        call("set_category", highlight_id=play["id"], category="meme")
    assert call("get_highlight", highlight_id=play["id"])["category"] == "play"


def test_edit_highlight_takes_every_operation_including_the_camera(call, match_id):
    hid = call("list_highlights", match_id=match_id)[0]["id"]
    seg = call("get_highlight", highlight_id=hid)["segments"][0]
    mid = (seg["start_tick"] + seg["end_tick"]) // 2
    out = call("edit_highlight", highlight_id=hid, ops=[
        {"op": "remove_segment", "segment": 1},
        {"op": "split_segment", "segment": 0, "at_tick": mid},
        {"op": "set_speed", "segment": 1, "speed": 0.5},
        {"op": "set_caption", "segment": 1, "caption": "devagar"},
        {"op": "set_camera", "segment": 0, "camera": CAM},
        {"op": "set_meta", "title": "from above, then his eyes"}])
    assert out["title"] == "from above, then his eyes" and out["origin"] == "agent"
    assert out["segments"][0]["camera"] == CAM and out["segments"][0]["pov_steamid"] is None
    assert out["segments"][1]["speed"] == 0.5 and out["segments"][1]["pov_steamid"] == seg["pov_steamid"]
    back = call("edit_highlight", highlight_id=hid, ops=[{"op": "set_pov", "segment": 0, "steamid": "ct1"}])
    assert back["segments"][0]["camera"] is None and back["segments"][0]["pov_steamid"] == "ct1"
    with pytest.raises(ToolError, match="16384"):
        call("edit_highlight", highlight_id=hid, ops=[{"op": "set_camera", "segment": 0,
                                                       "camera": {**CAM, "x": 99999}}])
    with pytest.raises(ToolError, match="out of range"):
        call("edit_highlight", highlight_id=hid, ops=[{"op": "set_caption", "segment": 9, "caption": "x"}])
    assert call("get_highlight", highlight_id=hid)["version"] == back["version"]  # failed edits change nothing


def test_render_tools_pass_the_shape_on(call, match_id):
    hid = call("list_highlights", match_id=match_id)[0]["id"]
    one = call("render_highlight", highlight_id=hid, renderer="radar", format="9:16")
    assert call("get_render", render_id=one["render_id"])["format"] == "9:16"
    reel = call("render_reel", match_id=match_id, top=2, renderer="radar", format="1:1")
    assert call("get_render", render_id=reel["render_id"])["format"] == "1:1"
    plain = call("render_highlight", highlight_id=hid, renderer="radar")
    assert call("get_render", render_id=plain["render_id"])["format"] == "16:9"
    with pytest.raises(ToolError, match="16:9"):
        call("render_highlight", highlight_id=hid, renderer="radar", format="4:3")


# ------------------------------------------------------------------------------------------- /mcp and its key

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_the_mcp_endpoint_needs_the_organiser_key_when_one_is_set(monkeypatch):
    from demoreel.api import create_app

    monkeypatch.setattr(get_settings(), "admin_key", "s3cret-organiser")
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as c:
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        wrong = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": "Bearer s3cret-organiser"})
        assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "demoreel"
        assert c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "x-api-key": "s3cret-organiser"}).status_code == 200


def test_the_mcp_endpoint_is_open_without_a_key():
    from demoreel.api import create_app

    assert not get_settings().admin_key and not get_settings().api_key
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as c:
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200
