"""Cimientos: rutas, configuración, base de datos y guardia local."""

from __future__ import annotations

import json

import pytest

from app import config as app_config
from app import db, disk, paths
from app.errors import ConfigError


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_paths_follow_environment(isolated_env):
    assert paths.data_dir() == isolated_env["data"]
    assert paths.db_path() == isolated_env["data"] / "app.db"
    assert paths.session_audio_dir(7) == isolated_env["data"] / "sessions" / "7" / "audio"


def test_rel_to_data_roundtrip(isolated_env):
    target = paths.session_dir(3) / "session.mp3"
    relative = paths.rel_to_data(target)
    assert relative == "sessions/3/session.mp3"
    assert paths.from_data(relative) == target.resolve()


def test_from_data_blocks_traversal():
    with pytest.raises(ValueError):
        paths.from_data("../../../windows/system32/config")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_defaults_when_file_missing():
    cfg = app_config.load_config(force=True)
    assert cfg["settings"]["capture_mode"] in ("loopback", "loopback+mic")
    assert cfg["deepgram"]["api_key"] == ""
    assert cfg["audio"]["chunk_seconds"] == 90


def test_save_and_reload_roundtrip():
    cfg = app_config.load_config(force=True)
    cfg["opencode"]["api_key"] = "sk-roundtrip"
    app_config.save_config(cfg)
    saved = json.loads(paths.config_path().read_text(encoding="utf-8"))
    assert saved["opencode"]["api_key"] == "sk-roundtrip"
    assert app_config.load_config(force=True)["opencode"]["api_key"] == "sk-roundtrip"


def test_update_config_merges_only_editable_sections():
    app_config.update_config({"settings": {"keep_raw_audio": True}, "server": {"port": 1}})
    cfg = app_config.load_config(force=True)
    assert cfg["settings"]["keep_raw_audio"] is True
    assert cfg["server"]["port"] == 8787       # `server` no es editable por API


def test_public_config_hides_secrets(config_file):
    config_file()
    public = app_config.public_config()
    assert public["opencode"]["api_key"] == ""
    assert public["opencode"]["api_key_set"] is True
    assert "…" in public["opencode"]["api_key_masked"]


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-from-env")
    assert app_config.load_config(force=True)["deepgram"]["api_key"] == "dg-from-env"


def test_require_keys_raises_with_message():
    with pytest.raises(ConfigError) as info:
        app_config.require_deepgram(app_config.load_config(force=True))
    assert "Deepgram" in info.value.user_message


def test_invalid_json_config_raises_clear_error():
    paths.config_path().write_text("{ not json", encoding="utf-8")
    app_config.reset_cache()
    with pytest.raises(ConfigError) as info:
        app_config.load_config(force=True)
    assert "config.local.json" in info.value.user_message


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "sessions", "people", "session_speakers", "topics", "timeline_events",
    "transcript_segments", "pending_transcriptions", "messages", "quiz_questions",
    "flashcards", "concept_maps", "audio_summaries", "roleplays", "usage_events",
    "settings",
}


def test_schema_has_all_tables_and_version():
    with db.read() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    assert EXPECTED_TABLES <= tables
    assert "transcript_fts" in tables
    assert version == db.SCHEMA_VERSION


def test_init_db_is_idempotent():
    db.init_db(force=True)
    db.init_db(force=True)
    with db.read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION


def test_wal_and_foreign_keys_enabled():
    with db.read() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_next_session_number_per_year():
    with db.write() as conn:
        assert db.next_session_number(conn) == 1
        conn.execute(
            "INSERT INTO sessions (title, session_number, started_at, created_at, updated_at)"
            " VALUES ('a', 4, ?, ?, ?)",
            (f"{db.now_iso()[:4]}-01-01T08:00:00Z", db.now_iso(), db.now_iso()),
        )
    with db.read() as conn:
        assert db.next_session_number(conn) == 5


def test_cascade_delete_removes_children():
    with db.write() as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (title, session_number, created_at, updated_at)"
            " VALUES ('x', 1, ?, ?)", (db.now_iso(), db.now_iso()),
        )
        sid = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO transcript_segments (session_id, start_t, end_t, text)"
            " VALUES (?,0,1,'hola ')", (sid,),
        )
    with db.write() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0


def test_fts_index_is_maintained_by_triggers():
    with db.write() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, session_number, created_at, updated_at)"
            " VALUES (1,'s',1,?,?)", (db.now_iso(), db.now_iso()),
        )
        conn.execute(
            "INSERT INTO transcript_segments (session_id, start_t, end_t, text)"
            " VALUES (1, 0, 4, 'the average handle time matters ')"
        )
    with db.read() as conn:
        hits = conn.execute(
            "SELECT rowid FROM transcript_fts WHERE transcript_fts MATCH 'handle'"
        ).fetchall()
    assert len(hits) == 1


def test_settings_helpers_roundtrip_json():
    db.setting_set("demo", {"a": [1, 2, 3]})
    assert db.setting_get("demo")["a"] == [1, 2, 3]
    db.setting_delete("dem")
    assert db.setting_get("demo", "gone") == "gone"


def test_wall_clock_uses_timezone_offset():
    started = "2026-08-04T13:00:00Z"
    assert db.wall_clock(started, 0, -300) == "08:00:00"
    assert db.wall_clock(started, 3661, -300) == "09:01:01"


def test_duration_between_handles_utc():
    assert db.duration_between("2026-08-04T08:00:00Z", "2026-08-04T11:30:00Z") == 12600
    assert db.duration_between(None, "2026-08-04T11:30:00Z") == 0


def test_record_usage_never_raises_and_accumulates():
    db.record_usage(session_id=None, kind="llm", tokens_in=10, tokens_out=5, cost_usd=0.01)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# disk
# ---------------------------------------------------------------------------


def test_disk_helpers():
    assert disk.free_space_mb() > 0
    assert disk.should_warn(100, 1024) is True
    assert disk.should_warn(5000, 1024) is False
    assert 350 < disk.estimate_session_mb(3.5) < 420
    assert disk.remaining_recording_minutes(1024) > 0


# ---------------------------------------------------------------------------
# security / API base
# ---------------------------------------------------------------------------


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_api_responses_are_not_cached(client):
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_foreign_host_is_rejected(client):
    response = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 421


def test_cross_site_write_is_blocked(client):
    response = client.post(
        "/api/sessions",
        json={"title": "x"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_foreign_origin_is_blocked(client):
    response = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_validation_error_has_friendly_detail(client):
    response = client.post("/api/sessions/1/chat", json={"message": "   "})
    assert response.status_code == 422
    assert "detail" in response.json()
