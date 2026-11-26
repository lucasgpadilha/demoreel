"""`demoreel serve` will not listen beyond this machine without a key unless told to. Nothing is started here:
the refusal comes before the database, the workers and the web server."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from demoreel.cli import ALLOW_OPEN_ENV, app, is_local_host, open_server_refusal
from demoreel.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(("host", "local"), [
    ("127.0.0.1", True), ("127.0.0.5", True), ("::1", True), ("[::1]", True), ("localhost", True),
    ("LOCALHOST", True), ("demoreel.localhost", True),
    ("0.0.0.0", False), ("::", False), ("192.168.0.10", False), ("10.1.2.3", False), ("example.com", False),
])
def test_local_hosts(host, local):
    assert is_local_host(host) is local


def test_an_open_address_without_a_key_is_refused_with_what_to_set():
    message = open_server_refusal("0.0.0.0", keyed=False, allowed=False)
    assert message and "0.0.0.0" in message
    assert "DEMOREEL_ADMIN_KEY" in message and "DEMOREEL_API_KEY" in message
    assert "--i-know-it-is-open" in message and f"{ALLOW_OPEN_ENV}=1" in message
    assert open_server_refusal("0.0.0.0", keyed=True, allowed=False) is None  # a key: fine
    assert open_server_refusal("0.0.0.0", keyed=False, allowed=True) is None  # said so on purpose
    assert open_server_refusal("127.0.0.1", keyed=False, allowed=False) is None  # the default: this machine


def test_serve_stops_before_starting_anything(monkeypatch):
    from typer.testing import CliRunner

    import demoreel.db as db

    settings = get_settings()
    monkeypatch.setattr(settings, "host", "0.0.0.0")
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "admin_key", None)
    monkeypatch.delenv(ALLOW_OPEN_ENV, raising=False)
    monkeypatch.setattr(db, "init_db", lambda: pytest.fail("the database was touched"))
    out = CliRunner().invoke(app, ["serve", "--workers", "0"])
    assert out.exit_code == 2
    assert "Refusing to listen on 0.0.0.0 without a key" in out.output


def test_the_environment_opt_out_is_read(monkeypatch):
    from demoreel import cli

    for value, allowed in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False)):
        monkeypatch.setenv(ALLOW_OPEN_ENV, value)
        assert cli._allow_open_env() is allowed


def test_the_docker_image_opts_out_only_because_the_port_stays_on_this_machine():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"DEMOREEL_HOST=0\.0\.0\.0", dockerfile) and f"{ALLOW_OPEN_ENV}=1" in dockerfile
    assert '"127.0.0.1:8000:8000"' in compose and ALLOW_OPEN_ENV in compose
