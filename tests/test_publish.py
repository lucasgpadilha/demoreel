"""Publishing to a read-only mirror: what is copied and what never is (no SSH involved)."""

from __future__ import annotations

import sqlite3

import pytest

from demoreel import publish
from demoreel.config import get_settings


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMOREEL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEMOREEL_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    db = sqlite3.connect(tmp_path / "demoreel.db")
    db.executescript("""
        CREATE TABLE matches (id TEXT, status TEXT, analysis_path TEXT);
        CREATE TABLE highlights (id TEXT);
        CREATE TABLE renders (id TEXT, status TEXT, job_id TEXT, video_path TEXT, thumbnail_path TEXT, renderer TEXT);
        CREATE TABLE jobs (id TEXT);
        CREATE TABLE webhooks (id TEXT, secret TEXT);
        INSERT INTO matches VALUES ('m1', 'ready', 'matches/m1/analysis.json'), ('m2', 'failed', NULL);
        INSERT INTO renders VALUES ('r1', 'ready', 'j1', 'renders/r1.mp4', 'renders/r1.jpg', 'ingame'),
                                   ('r2', 'queued', 'j2', NULL, NULL, 'ingame'),
                                   ('r3', 'ready', 'j3', 'renders/gone.mp4', NULL, 'radar'),
                                   ('r4', 'ready', 'j4', '../outside.mp4', NULL, 'ingame');
        INSERT INTO jobs VALUES ('j1'), ('j2');
        INSERT INTO webhooks VALUES ('w1', 'do-not-publish');
    """)
    db.commit()
    db.close()
    for rel in ("matches/m1/analysis.json", "renders/r1.mp4", "renders/r1.jpg", "renders/orphan.mp4",
                "brands/kit/brand.json", "brands/kit/logo.png", "brands/kit/track.mp3", "demos/m1.dem",
                "masters/m1/ms_1.mp4"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 10)
    (tmp_path.parent / "outside.mp4").write_bytes(b"x")
    yield tmp_path
    get_settings.cache_clear()


def test_target_must_name_a_host_and_an_absolute_folder():
    t = publish.Target.parse("demoreel@example.org:/srv/demoreel/data/")
    assert (t.host, t.path) == ("demoreel@example.org", "/srv/demoreel/data")
    for bad in (None, "", "example.org", "example.org:relative/path", "host:/with space"):
        with pytest.raises(publish.PublishError, match="host:/absolute/path"):
            publish.Target.parse(bad)


def test_the_snapshot_holds_no_jobs_no_webhooks_and_only_finished_videos(instance, tmp_path):
    snap = publish.snapshot_database(instance / "snapshot.db")
    db = sqlite3.connect(snap)
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM webhooks").fetchone()[0] == 0  # they carry secrets
    assert [r[0] for r in db.execute("SELECT id FROM renders ORDER BY id")] == ["r1", "r3", "r4"]
    assert db.execute("SELECT count(*) FROM renders WHERE job_id IS NOT NULL").fetchone()[0] == 0
    db.close()
    # the instance's own database is untouched
    live = sqlite3.connect(instance / "demoreel.db")
    assert live.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    live.close()


def test_the_mirror_can_be_limited_to_some_renderers(instance, monkeypatch):
    # a community that dropped the 2D replays: the mirror gets only first-person videos, nothing is deleted here
    monkeypatch.setenv("DEMOREEL_PUBLISH_RENDERERS", '["ingame"]')
    get_settings.cache_clear()
    snap = publish.snapshot_database(instance / "snapshot.db")
    db = sqlite3.connect(snap)
    assert [r[0] for r in db.execute("SELECT id FROM renders ORDER BY id")] == ["r1", "r4"]
    db.close()
    live = sqlite3.connect(instance / "demoreel.db")
    assert live.execute("SELECT count(*) FROM renders WHERE renderer = 'radar'").fetchone()[0] == 1
    live.close()
    monkeypatch.setenv("DEMOREEL_PUBLISH_RENDERERS", '["ingame", "3d"]')
    get_settings.cache_clear()
    with pytest.raises(publish.PublishError, match="'3d'.*ingame, radar"):
        publish.snapshot_database(instance / "snapshot.db")


def test_only_what_a_mirror_shows_is_sent(instance):
    snap = publish.snapshot_database(instance / "snapshot.db")
    files, missing, matches, renders = publish.wanted_files(snap, instance)
    assert sorted(files) == ["brands/kit/brand.json", "brands/kit/logo.png", "matches/m1/analysis.json",
                             "renders/r1.jpg", "renders/r1.mp4"]  # no demo, master, music, orphan or outside file
    assert missing == ["renders/gone.mp4"] and (matches, renders) == (1, 3)


def test_a_video_deleted_here_but_still_listed_stays_on_the_mirror(instance):
    snap = publish.snapshot_database(instance / "snapshot.db")
    files, missing, _, _ = publish.wanted_files(snap, instance)
    there = ["renders/r1.mp4", "renders/gone.mp4", "renders/unlisted.mp4", "matches/old/analysis.json",
             "brands/kit/logo.png", "demoreel.db"]
    # gone.mp4 is listed by the database: the mirror's copy may be the only one, so it is not pruned
    assert publish.stale_files(there, files, missing) == ["matches/old/analysis.json", "renders/unlisted.mp4"]
    assert publish.stale_files(["renders/a.mp4"], [], ["renders\\a.mp4"]) == []  # paths stored on Windows


def test_another_database_engine_is_refused_with_a_reason(instance, monkeypatch):
    monkeypatch.setenv("DEMOREEL_DATABASE_URL", "postgresql+psycopg://u@h/db")
    get_settings.cache_clear()
    with pytest.raises(publish.PublishError, match="SQLite"):
        publish.snapshot_database(instance / "snapshot.db")
