"""Pipeline completo: grabar (desde WAV) → transcribir → libro final → audio."""

from __future__ import annotations

import time

import pytest

from app import db, paths, pipeline
from app.errors import SessionStateError
from app.transcription import queue as stt_queue
from tests.conftest import two_speaker_signal, write_wav

BOOK_MAIN = {
    "timeline": [
        {"start_t": 0, "end_t": 100, "kind": "topic", "label": "Welcome calls"},
        {"start_t": 120, "end_t": 180, "kind": "break", "label": "Break"},
        {"start_t": 180, "end_t": 300, "kind": "roleplay", "label": "Roleplay"},
    ],
    "topics": [
        {"title": "Welcome calls", "start_t": 0, "end_t": 100,
         "points": ["Greet and verify identity"], "spanish_notes": ["saludo formal"],
         "vocab": [{"word": "handle time", "en_def": "call duration", "es": "duración"}],
         "phrases": [{"en": "How may I help you?", "es": "¿Cómo puedo ayudarle?",
                      "speaker_index": 0}]},
        {"title": "Roleplay debrief", "start_t": 180, "end_t": 300,
         "points": ["Confirm the account before sharing data"], "spanish_notes": [],
         "vocab": [], "phrases": []},
    ],
    "roleplays": [
        {"title": "First call", "context": "practice", "your_role": "agent",
         "participants": ["Juan"], "key_phrases": ["May I have your account number?"],
         "feedback": "Good tone", "start_t": 180, "end_t": 300},
    ],
}
SPEAKERS = {"speakers": [
    {"index": 0, "suggested_name": "Sara", "suggested_role": "teacher", "confidence": 0.9},
    {"index": 1, "suggested_name": "Juan", "suggested_role": "student", "confidence": 0.7},
]}
TITLE = {"title": "Welcome calls and KPIs"}


@pytest.fixture()
def source(tmp_path):
    return write_wav(tmp_path / "class.wav", two_speaker_signal(300.0))


def _wait_for_chunks(session_id: int, expected: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stt_queue.counts(session_id)["total"] >= expected:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"solo se encolaron {stt_queue.counts(session_id)} chunks de {expected}"
    )


# ---------------------------------------------------------------------------
# Creación y grabación
# ---------------------------------------------------------------------------


def test_create_session_sets_number_and_folders():
    session_id = pipeline.create_session(status="empty")
    row = db.fetch_session(session_id)
    assert row["session_number"] == 1
    assert row["audio_root"] == f"sessions/{session_id}"
    assert paths.session_audio_dir(session_id).exists()


def test_manual_title_locks_it():
    session_id = pipeline.create_session(title="Mi clase")
    assert db.fetch_session(session_id)["title_locked"] == 1
    auto = pipeline.create_session()
    assert db.fetch_session(auto)["title_locked"] == 0


def test_start_from_wav_records_and_enqueues(source, config_file):
    config_file()
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    try:
        _wait_for_chunks(session_id, 4)
        assert db.fetch_session(session_id)["status"] == "recording"
        assert session_id in pipeline.ACTIVE
        status = pipeline.active_status()
        assert status["state"] == "recording"
        assert status["session_id"] == session_id
        chunks = sorted(paths.session_audio_dir(session_id).glob("chunk_*.wav"))
        assert len(chunks) == 4
        # Cada chunk lleva su sidecar con los metadatos de tiempo.
        meta = stt_queue.read_meta(chunks[1])
        assert meta["start_t"] == pytest.approx(90.0, abs=0.1)
        assert meta["overlap_pre"] > 0
    finally:
        pipeline.stop_session(session_id, finalize=False)


def test_only_one_session_can_record(source, config_file):
    config_file()
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    try:
        with pytest.raises(SessionStateError):
            pipeline.start_session(source_wav=str(source), realtime=False)
    finally:
        pipeline.stop_session(session_id, finalize=False)


def test_stop_sets_duration_and_processing(source, config_file):
    config_file()
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 1)
    pipeline.stop_session(session_id, finalize=False)
    row = db.fetch_session(session_id)
    assert row["status"] == "processing"
    assert row["ended_at"]
    assert row["duration_sec"] is not None
    assert session_id not in pipeline.ACTIVE


def test_stop_with_discard_deletes_everything(source, config_file):
    config_file()
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 1)
    pipeline.stop_session(session_id, discard=True)
    assert db.fetch_session(session_id) is None
    assert not paths.session_dir(session_id).exists()


def test_delete_session_removes_audio_and_settings(source, config_file):
    config_file()
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 1)
    pipeline.stop_session(session_id, finalize=False)
    db.setting_set(f"breaks_{session_id}", [[1, 2]])
    pipeline.delete_session(session_id)
    assert db.setting_get(f"breaks_{session_id}") is None
    assert not paths.session_dir(session_id).exists()
    assert stt_queue.counts(session_id)["total"] == 0


# ---------------------------------------------------------------------------
# Estructuración en vivo
# ---------------------------------------------------------------------------


def _segments(session_id: int, count: int = 4, first: int = 0) -> None:
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO transcript_segments (session_id, chunk_index, start_t, end_t,"
            " speaker_index, text) VALUES (?,?,?,?,?,?)",
            [
                (session_id, 0, index * 10.0, index * 10.0 + 8.0, index % 2,
                 f"Line number {index} about welcome calls. ")
                for index in range(first, first + count)
            ],
        )


def test_run_integration_saves_draft(config_file, fake_llm):
    cfg = config_file()
    fake_llm({"topics": [{"title": "Welcome calls", "points": ["greet"],
                          "spanish_notes": []}]})
    session_id = pipeline.create_session()
    _segments(session_id)
    assert pipeline.run_integration(session_id, cfg) is True
    topics = pipeline._draft_topics(session_id)
    assert topics[0]["title"] == "Welcome calls"


def test_integration_only_sends_new_segments(config_file, fake_llm):
    cfg = config_file()
    double = fake_llm(
        {"topics": [{"title": "A", "points": ["p"]}]},
        {"topics": [{"title": "A", "points": ["p", "q"]}]},
    )
    session_id = pipeline.create_session()
    _segments(session_id, count=2, first=0)
    pipeline.run_integration(session_id, cfg)
    _segments(session_id, count=1, first=2)
    pipeline.run_integration(session_id, cfg)
    assert "Line number 0" in double.calls[0]["user"]
    # La segunda pasada solo manda el delta: así el coste no crece con la clase.
    assert "Line number 0" not in double.calls[1]["user"]
    assert "Line number 2" in double.calls[1]["user"]


def test_integration_without_new_segments_is_a_noop(config_file, fake_llm):
    cfg = config_file()
    fake_llm({"topics": [{"title": "A", "points": ["p"]}]})
    session_id = pipeline.create_session()
    assert pipeline.run_integration(session_id, cfg) is False


def test_integration_failure_does_not_lose_the_draft(config_file, monkeypatch):
    cfg = config_file()
    session_id = pipeline.create_session()
    pipeline.save_draft_topics(session_id, [{"title": "Previo", "points": ["p"]}])
    _segments(session_id)

    from app.ai import live_integration
    from app.errors import AIError

    monkeypatch.setattr(
        live_integration, "integrate",
        lambda **kw: (_ for _ in ()).throw(AIError("503")),
    )
    assert pipeline.run_integration(session_id, cfg) is False
    assert pipeline._draft_topics(session_id)[0]["title"] == "Previo"


def test_integration_due_respects_interval():
    session_id = pipeline.create_session()
    assert pipeline.integration_due(session_id, 300) is False   # sin sesión activa
    pipeline.ACTIVE[session_id] = pipeline.ActiveSession(
        session_id=session_id, recorder=None, capture_mode="wav"
    )
    try:
        assert pipeline.integration_due(session_id, 300) is False
        pipeline.ACTIVE[session_id].last_integration -= 400
        assert pipeline.integration_due(session_id, 300) is True
    finally:
        pipeline.ACTIVE.pop(session_id, None)


# ---------------------------------------------------------------------------
# Libro final
# ---------------------------------------------------------------------------


def test_apply_book_persists_everything(config_file):
    config_file()
    session_id = pipeline.create_session()
    _segments(session_id, 6)
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO session_speakers (session_id, speaker_index) VALUES (?,?)",
            [(session_id, 0), (session_id, 1)],
        )
    pipeline.apply_book(session_id, {**BOOK_MAIN, **SPEAKERS, **TITLE,
                                     "model": "fake-polish"}, me_speaker=1)
    with db.read() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE session_id=?", (session_id,)
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM topics WHERE session_id=? AND status='final'",
            (session_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM roleplays WHERE session_id=?", (session_id,)
        ).fetchone()[0] == 1
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        assert row["title"] == "Welcome calls and KPIs"
        assert row["polish_model"] == "fake-polish"
        speakers = conn.execute(
            "SELECT speaker_index, suggested_name, is_me FROM session_speakers"
            " WHERE session_id=? ORDER BY speaker_index", (session_id,),
        ).fetchall()
    assert speakers[0]["suggested_name"] == "Sara"
    assert speakers[1]["is_me"] == 1


def test_apply_book_assigns_topics_to_segments(config_file):
    config_file()
    session_id = pipeline.create_session()
    _segments(session_id, 6)          # 0,10,20,30,40,50 s
    pipeline.apply_book(session_id, {**BOOK_MAIN, "speakers": [], "title": ""})
    with db.read() as conn:
        rows = conn.execute(
            "SELECT start_t, topic_id FROM transcript_segments WHERE session_id=?"
            " ORDER BY start_t", (session_id,),
        ).fetchall()
    assert all(row["topic_id"] is not None for row in rows)


def test_apply_book_keeps_user_edited_topics(config_file):
    config_file()
    session_id = pipeline.create_session()
    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO topics (session_id, sort_order, status, title, start_t,"
            " summary_md, user_edited, created_at, updated_at)"
            " VALUES (?,0,'final','Mi versión',5.0,?,1,?,?)",
            (session_id, db.json_dumps({"points": ["mío"], "spanish_notes": []}),
             now, now),
        )
    pipeline.apply_book(session_id, {**BOOK_MAIN, "speakers": [], "title": ""})
    with db.read() as conn:
        titles = [
            r["title"] for r in conn.execute(
                "SELECT title FROM topics WHERE session_id=? AND status='final'"
                " ORDER BY sort_order", (session_id,),
            )
        ]
    assert "Mi versión" in titles
    assert "Welcome calls" in titles


def test_apply_book_does_not_rename_a_locked_title(config_file):
    config_file()
    session_id = pipeline.create_session(title="Nombre elegido por mí")
    pipeline.apply_book(session_id, {**BOOK_MAIN, **SPEAKERS, **TITLE})
    assert db.fetch_session(session_id)["title"] == "Nombre elegido por mí"


def test_apply_book_respects_confirmed_speakers(config_file):
    config_file()
    session_id = pipeline.create_session()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO people (name, role, created_at, updated_at)"
            " VALUES ('Carolina','teacher',?,?)", (db.now_iso(), db.now_iso()),
        )
        conn.execute(
            "INSERT INTO session_speakers (session_id, speaker_index, person_id,"
            " suggested_name, confirmed) VALUES (?,0,1,'Carolina',1)", (session_id,),
        )
    pipeline.apply_book(session_id, {**BOOK_MAIN, **SPEAKERS, **TITLE})
    with db.read() as conn:
        row = conn.execute(
            "SELECT suggested_name FROM session_speakers WHERE session_id=? AND"
            " speaker_index=0", (session_id,),
        ).fetchone()
    assert row["suggested_name"] == "Carolina"


def test_me_speaker_index_from_microphone_flags():
    session_id = pipeline.create_session()
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO transcript_segments (session_id, start_t, end_t, speaker_index,"
            " is_me, text) VALUES (?,?,?,?,?,?)",
            [
                (session_id, 0, 20, 0, 0, "teacher "),
                (session_id, 20, 40, 1, 1, "yo "),
                (session_id, 40, 55, 1, 1, "yo otra vez "),
            ],
        )
    assert pipeline.me_speaker_index(session_id) == 1


def test_match_known_people_suggests_without_confirming():
    """Segunda clase con la misma voz: se propone el nombre, no se aplica solo."""
    import numpy as np

    from app.transcription import voiceprint

    t = np.arange(16000 * 4, dtype=np.float32) / 16000
    voice = voiceprint.embed((0.3 * np.sin(2 * np.pi * 180 * t)).astype(np.float32))
    payload = db.json_dumps(voiceprint.to_json(voice, 40.0))
    session_id = pipeline.create_session()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO people (name, role, voice_json, created_at, updated_at)"
            " VALUES ('Sara','teacher',?,?,?)", (payload, db.now_iso(), db.now_iso()),
        )
        conn.execute(
            "INSERT INTO session_speakers (session_id, speaker_index, voice_json)"
            " VALUES (?,0,?)", (session_id, payload),
        )
    assert pipeline.match_known_people(session_id) == 1
    with db.read() as conn:
        row = conn.execute(
            "SELECT suggested_name, auto_matched, confirmed, person_id FROM"
            " session_speakers WHERE session_id=?", (session_id,),
        ).fetchone()
    assert row["suggested_name"] == "Sara"
    assert row["auto_matched"] == 1
    assert row["confirmed"] == 0
    assert row["person_id"] is None


# ---------------------------------------------------------------------------
# Finalización de punta a punta
# ---------------------------------------------------------------------------


def test_finalize_end_to_end(source, config_file, fake_llm, fake_stt, monkeypatch):
    """Grabar desde WAV, transcribir con doble, pulir y generar el MP3."""
    config_file(settings={"keep_raw_audio": True})
    fake_stt()
    fake_llm(BOOK_MAIN, SPEAKERS, TITLE)

    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 4)
    pipeline.stop_session(session_id, finalize=False)
    pipeline.finalize_session(session_id)

    row = db.fetch_session(session_id)
    assert row["status"] == "done", row["status_detail"]
    assert row["title"] == "Welcome calls and KPIs"
    assert row["progress"] == 1.0
    assert stt_queue.counts(session_id)["total"] == 0

    with db.read() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE session_id=?", (session_id,)
        ).fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM topics WHERE session_id=? AND status='final'",
            (session_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM topics WHERE session_id=? AND status='draft'",
            (session_id,),
        ).fetchone()[0] == 0

    from app.audio import session_audio

    mp3 = session_audio.session_mp3_path(session_id)
    assert mp3 is not None and mp3.stat().st_size > 2000
    assert list(paths.session_audio_dir(session_id).glob("chunk_*.wav"))   # keep_raw_audio


def test_finalize_discards_raw_audio_by_default(source, config_file, fake_llm, fake_stt):
    config_file(settings={"keep_raw_audio": False})
    fake_stt()
    fake_llm(BOOK_MAIN, SPEAKERS, TITLE)
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 4)
    pipeline.stop_session(session_id, finalize=False)
    pipeline.finalize_session(session_id)

    audio_dir = paths.session_audio_dir(session_id)
    assert not list(audio_dir.glob("chunk_*.wav"))
    assert list(audio_dir.glob("chunk_*.json"))       # los sidecars se conservan
    from app.audio import session_audio

    assert session_audio.session_mp3_path(session_id) is not None


def test_finalize_without_audio_marks_empty(config_file, fake_llm):
    config_file()
    fake_llm(BOOK_MAIN, SPEAKERS, TITLE)
    session_id = pipeline.create_session()
    pipeline.finalize_session(session_id)
    row = db.fetch_session(session_id)
    assert row["status"] == "empty"
    assert "audio" in (row["status_detail"] or "")


def test_finalize_reports_error_when_the_model_fails(config_file, monkeypatch):
    config_file()
    session_id = pipeline.create_session()
    _segments(session_id, 4)

    from app.ai import polish
    from app.errors import AIError

    monkeypatch.setattr(
        polish, "finalize_session",
        lambda **kw: (_ for _ in ()).throw(
            AIError("boom", user_message="El modelo se cayó")
        ),
    )
    pipeline.finalize_session(session_id)
    row = db.fetch_session(session_id)
    assert row["status"] == "error"
    assert row["status_detail"] == "El modelo se cayó"


def test_failed_transcription_marks_error_not_empty(config_file, monkeypatch):
    config_file()
    session_id = pipeline.create_session()
    stt_queue.enqueue(session_id, "missing.wav", 0.0, chunk_index=0)
    row = stt_queue.claim_one()
    stt_queue.mark_failed(row["id"], "401", retryable=False)
    pipeline.finalize_session(session_id)
    assert db.fetch_session(session_id)["status"] == "error"


def test_auto_generate_runs_when_enabled(source, config_file, fake_llm, fake_stt,
                                         monkeypatch):
    config_file(settings={"auto_generate_all": True})
    fake_stt()
    fake_llm(BOOK_MAIN, SPEAKERS, TITLE)
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "_auto_generate",
                        lambda sid, cfg: calls.append("auto"))
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 4)
    pipeline.stop_session(session_id, finalize=False)
    pipeline.finalize_session(session_id)
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls == ["auto"]


# ---------------------------------------------------------------------------
# Recuperación tras caída
# ---------------------------------------------------------------------------


def test_recover_orphans_requeues_and_flags(source, config_file):
    config_file()
    session_id = pipeline.start_session(source_wav=str(source), realtime=False)
    _wait_for_chunks(session_id, 2)
    # Simulamos el cierre brusco: el proceso muere y ACTIVE se pierde.
    pipeline.ACTIVE[session_id].recorder.stop()
    pipeline.ACTIVE.clear()
    stt_queue.claim_one()

    orphans = pipeline.recover_orphans()
    assert orphans == [session_id]
    assert stt_queue.counts(session_id)["claimed"] == 0
    assert "cerró" in db.fetch_session(session_id)["status_detail"]


def test_recover_reindexes_chunks_missing_from_the_queue(config_file):
    config_file()
    session_id = pipeline.create_session()
    audio_dir = paths.session_audio_dir(session_id)
    path = write_wav(audio_dir / "chunk_000000.wav", two_speaker_signal(5.0))
    stt_queue.write_meta(path, {"chunk_index": 0, "start_t": 0.0, "duration": 5.0,
                                "overlap_pre": 0.0})
    db.touch_session(session_id, status="recording")
    pipeline.recover_orphans()
    assert stt_queue.counts(session_id)["pending"] == 1


def test_finalize_recording_rejects_a_finished_session(config_file):
    config_file()
    session_id = pipeline.create_session()
    db.touch_session(session_id, status="done")
    with pytest.raises(SessionStateError):
        pipeline.finalize_recording(session_id)


def test_active_status_reports_processing_sessions():
    session_id = pipeline.create_session()
    db.touch_session(session_id, status="processing", status_detail="pulido", progress=0.4)
    status = pipeline.active_status()
    assert status["state"] == "processing"
    assert status["session_id"] == session_id
    assert status["progress"] == 0.4


def test_active_status_idle():
    assert pipeline.active_status()["state"] == "idle"
