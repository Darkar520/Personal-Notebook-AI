"""API HTTP: cuadernos, contenido, hablantes, chat, materiales, medios y ajustes."""

from __future__ import annotations

import io

import pytest

from app import db, paths, pipeline
from tests.conftest import two_speaker_signal, write_wav

BOOK = {
    "timeline": [{"start_t": 0, "end_t": 60, "kind": "topic", "label": "Welcome calls"},
                 {"start_t": 60, "end_t": 120, "kind": "break", "label": "Break"}],
    "topics": [{"title": "Welcome calls", "start_t": 0, "end_t": 60,
                "points": ["Greet and verify identity"],
                "spanish_notes": ["saludo formal"],
                "vocab": [{"word": "handle time", "en_def": "call duration",
                           "es": "duración", "example_en": "Keep handle time low."}],
                "phrases": [{"en": "How may I help you?", "es": "¿Cómo puedo ayudarle?",
                             "speaker_index": 0}]}],
    "roleplays": [{"title": "First call", "context": "practice", "your_role": "agent",
                   "participants": ["Juan"], "key_phrases": ["Account number?"],
                   "feedback": "Good tone", "start_t": 30, "end_t": 60}],
    "speakers": [{"index": 0, "suggested_name": "Sara", "suggested_role": "teacher"},
                 {"index": 1, "suggested_name": "Juan", "suggested_role": "student"}],
    "title": "Welcome calls",
}


@pytest.fixture()
def notebook(client):
    """Cuaderno con transcripción, libro aplicado y hablantes."""
    session_id = pipeline.create_session(title="Clase de prueba")
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO transcript_segments (session_id, chunk_index, start_t, end_t,"
            " speaker_index, is_me, text) VALUES (?,?,?,?,?,?,?)",
            [
                (session_id, 0, 0.0, 8.0, 0, 0, "Welcome, today we cover handle time. "),
                (session_id, 0, 9.0, 14.0, 1, 1, "What does handle time mean? "),
                (session_id, 0, 15.0, 25.0, 0, 0, "It is the duration of a call. "),
                (session_id, 1, 95.0, 99.0, 0, 0, "Let's practise greetings now. "),
            ],
        )
        conn.executemany(
            "INSERT INTO session_speakers (session_id, speaker_index, talk_seconds, color)"
            " VALUES (?,?,?,?)",
            [(session_id, 0, 18.0, "#4f46e5"), (session_id, 1, 5.0, "#0891b2")],
        )
    pipeline.apply_book(session_id, BOOK, me_speaker=1)
    db.touch_session(session_id, status="done", progress=1.0)
    return session_id


# ---------------------------------------------------------------------------
# Cuadernos
# ---------------------------------------------------------------------------


def test_crud_lifecycle(client):
    created = client.post("/api/sessions", json={"title": "Mi clase"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert created.json()["session_number"] == 1

    listed = client.get("/api/sessions").json()
    assert len(listed) == 1 and listed[0]["title"] == "Mi clase"

    renamed = client.patch(f"/api/sessions/{session_id}", json={"title": "Renombrada"})
    assert renamed.json()["title"] == "Renombrada"
    assert renamed.json()["title_locked"] is True

    tagged = client.patch(f"/api/sessions/{session_id}", json={"account_tag": "Yardi"})
    assert tagged.json()["account_tag"] == "Yardi"

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get("/api/sessions").json() == []


def test_get_missing_session_is_404(client):
    assert client.get("/api/sessions/999").status_code == 404


def test_empty_title_is_rejected(client):
    session_id = client.post("/api/sessions", json={}).json()["id"]
    assert client.patch(f"/api/sessions/{session_id}", json={"title": "  "}).status_code == 422


def test_list_includes_counters(client, notebook):
    entry = client.get("/api/sessions").json()[0]
    assert entry["topics_count"] == 1
    assert entry["segments_count"] == 4
    assert entry["speakers_pending"] is True
    assert entry["has_audio"] is False


def test_delete_removes_audio_folder(client, notebook):
    folder = paths.session_dir(notebook)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.mp3").write_bytes(b"fake")
    assert client.delete(f"/api/sessions/{notebook}").status_code == 204
    assert not folder.exists()


def test_status_endpoint_is_reachable_before_any_session(client):
    assert client.get("/api/sessions/status").json()["state"] == "idle"


def test_pending_recording_route_is_not_shadowed(client):
    """Con el orden del plan, esta ruta devolvía 422 al intentar castear a int."""
    db.touch_session(pipeline.create_session(), status="recording")
    response = client.get("/api/sessions/pending-recording")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_start_and_stop_from_wav(client, config_file, tmp_path, fake_stt, fake_llm):
    config_file()
    fake_stt()
    fake_llm(BOOK, {"speakers": BOOK["speakers"]}, {"title": "Welcome calls"})
    source = write_wav(tmp_path / "class.wav", two_speaker_signal(120.0))

    started = client.post("/api/sessions/start",
                          json={"source_wav": str(source), "realtime": False})
    assert started.status_code == 200
    session_id = started.json()["id"]
    assert started.json()["status"] == "recording"

    conflict = client.post("/api/sessions/start",
                           json={"source_wav": str(source), "realtime": False})
    assert conflict.status_code == 409

    stopped = client.post(f"/api/sessions/{session_id}/stop", json={"finalize": False})
    assert stopped.json()["status"] == "processing"


def test_stop_with_discard(client, config_file, tmp_path):
    config_file()
    source = write_wav(tmp_path / "c.wav", two_speaker_signal(30.0))
    session_id = client.post("/api/sessions/start",
                             json={"source_wav": str(source), "realtime": False}).json()["id"]
    response = client.post(f"/api/sessions/{session_id}/stop", json={"discard": True})
    assert response.json()["status"] == "deleted"
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_finalize_and_discard_recording(client, config_file):
    config_file()
    session_id = pipeline.create_session()
    db.touch_session(session_id, status="recording")
    assert client.post(f"/api/sessions/{session_id}/finalize-recording").status_code == 200
    other = pipeline.create_session()
    db.touch_session(other, status="recording")
    assert client.post(f"/api/sessions/{other}/discard-recording").status_code == 204
    assert client.get(f"/api/sessions/{other}").status_code == 404


def test_retry_transcription_reactivates_failures(client, notebook):
    from app.transcription import queue as stt_queue

    stt_queue.enqueue(notebook, "c.wav", 0.0, chunk_index=0)
    row = stt_queue.claim_one()
    stt_queue.mark_failed(row["id"], "401", retryable=False)
    response = client.post(f"/api/sessions/{notebook}/retry-transcription")
    assert response.json()["reactivated"] == 1
    assert client.get(f"/api/sessions/{notebook}/queue").json()["pending"] == 1


# ---------------------------------------------------------------------------
# Contenido
# ---------------------------------------------------------------------------


def test_topics_include_notes_vocab_and_phrases(client, notebook):
    topics = client.get(f"/api/sessions/{notebook}/topics").json()
    assert len(topics) == 1
    topic = topics[0]
    assert topic["status"] == "final"
    assert topic["points"] == ["Greet and verify identity"]
    assert topic["spanish_notes"] == ["saludo formal"]
    assert topic["vocab"][0]["word"] == "handle time"
    assert topic["phrases"][0]["es"].startswith("¿Cómo")


def test_topics_fall_back_to_draft(client):
    session_id = pipeline.create_session()
    pipeline.save_draft_topics(session_id, [{"title": "Borrador", "points": ["p"]}])
    topics = client.get(f"/api/sessions/{session_id}/topics").json()
    assert topics[0]["status"] == "draft"


def test_put_draft_topics(client):
    session_id = pipeline.create_session()
    response = client.put(
        f"/api/sessions/{session_id}/topics/draft",
        json={"topics": [{"title": "A", "points": ["p1"], "spanish_notes": ["s1"]}]},
    )
    assert response.status_code == 200
    assert response.json()[0]["status"] == "draft"
    assert response.json()[0]["spanish_notes"] == ["s1"]


def test_patch_topic_marks_user_edited(client, notebook):
    topic_id = client.get(f"/api/sessions/{notebook}/topics").json()[0]["id"]
    response = client.patch(
        f"/api/sessions/{notebook}/topics/{topic_id}",
        json={"points": ["Mi propio punto"], "title": "Mi título"},
    )
    assert response.status_code == 200
    assert response.json()["user_edited"] is True
    assert response.json()["points"] == ["Mi propio punto"]


def test_marking_mastered_does_not_lock_the_topic(client, notebook):
    """Marcar "dominado" no debe impedir que el pase final mejore el tema."""
    topic_id = client.get(f"/api/sessions/{notebook}/topics").json()[0]["id"]
    response = client.patch(f"/api/sessions/{notebook}/topics/{topic_id}",
                            json={"mastered": True})
    assert response.json()["mastered"] is True
    assert response.json()["user_edited"] is False


def test_patch_missing_topic_is_404(client, notebook):
    assert client.patch(f"/api/sessions/{notebook}/topics/9999", json={"title": "x"}) \
        .status_code == 404


def test_delete_topic(client, notebook):
    topic_id = client.get(f"/api/sessions/{notebook}/topics").json()[0]["id"]
    assert client.delete(f"/api/sessions/{notebook}/topics/{topic_id}").status_code == 204
    assert client.get(f"/api/sessions/{notebook}/topics").json() == []


def test_timeline_has_wall_clock_and_breaks(client, notebook):
    events = client.get(f"/api/sessions/{notebook}/timeline").json()
    assert [e["kind"] for e in events] == ["topic", "break"]
    assert events[0]["wall_clock"]
    assert events[0]["sort_order"] == 0


def test_roleplays(client, notebook):
    roleplays = client.get(f"/api/sessions/{notebook}/roleplays").json()
    assert roleplays[0]["participants"] == ["Juan"]
    assert roleplays[0]["your_role"] == "agent"


def test_transcript_includes_speaker_and_wall_clock(client, notebook):
    segments = client.get(f"/api/sessions/{notebook}/transcript").json()
    assert len(segments) == 4
    assert segments[0]["wall_clock"]
    assert segments[1]["is_me"] is True
    assert segments[0]["topic_id"] is not None


def test_transcript_supports_incremental_fetch(client, notebook):
    segments = client.get(f"/api/sessions/{notebook}/transcript").json()
    newer = client.get(
        f"/api/sessions/{notebook}/transcript?after_id={segments[1]['id']}"
    ).json()
    assert len(newer) == 2


def test_search_finds_terms(client, notebook):
    hits = client.get(f"/api/sessions/{notebook}/search?q=handle").json()
    assert len(hits) == 2
    assert all("handle" in hit["text"].lower() for hit in hits)
    # Los nombres propuestos por la IA ya se usan al mostrar resultados.
    assert {hit["speaker"] for hit in hits} == {"Sara", "Juan"}
    assert all(hit["wall_clock"] for hit in hits)


def test_search_is_accent_insensitive_and_safe(client, notebook):
    assert client.get(f"/api/sessions/{notebook}/search?q=").json() == []
    # Caracteres que romperían la sintaxis de FTS5 no deben provocar un 500.
    assert client.get(f"/api/sessions/{notebook}/search?q=%22AND+*").status_code == 200


def test_usage_reports_costs(client, notebook):
    db.record_usage(session_id=notebook, kind="stt", provider="deepgram", minutes=210,
                    cost_usd=1.2)
    db.record_usage(session_id=notebook, kind="llm", provider="opencode",
                    purpose="polish_main", tokens_in=50000, tokens_out=8000, cost_usd=0.02)
    usage = client.get(f"/api/sessions/{notebook}/usage").json()
    assert usage["stt_minutes"] == 210
    assert usage["llm_calls"] == 1
    assert usage["total_usd"] == pytest.approx(1.22)
    assert usage["by_purpose"]


# ---------------------------------------------------------------------------
# Hablantes y personas
# ---------------------------------------------------------------------------


def test_speakers_listing_includes_suggestions_and_sample(client, notebook):
    speakers = client.get(f"/api/sessions/{notebook}/speakers").json()
    assert speakers[0]["suggested_name"] == "Sara"
    assert speakers[0]["confirmed"] is False
    assert speakers[0]["sample_text"]
    assert speakers[0]["color"]


def test_confirming_speakers_creates_people_and_remembers(client, notebook):
    response = client.put(
        f"/api/sessions/{notebook}/speakers",
        json={"speakers": [
            {"speaker_index": 0, "name": "Sara Rivera", "role": "teacher"},
            {"speaker_index": 1, "name": "Juan", "role": "me"},
        ]},
    )
    assert response.status_code == 200
    confirmed = {s["speaker_index"]: s for s in response.json()}
    assert confirmed[0]["name"] == "Sara Rivera"
    assert confirmed[0]["confirmed"] is True
    assert confirmed[1]["is_me"] is True

    people = client.get("/api/people").json()
    assert {p["name"] for p in people} == {"Sara Rivera", "Juan"}
    assert all(p["sessions"] == 1 for p in people)


def test_confirming_reuses_an_existing_person(client, notebook):
    client.post("/api/people", json={"name": "Sara Rivera", "role": "teacher"})
    person_id = client.get("/api/people").json()[0]["id"]
    client.put(
        f"/api/sessions/{notebook}/speakers",
        json={"speakers": [{"speaker_index": 0, "person_id": person_id, "role": "teacher"}]},
    )
    assert len(client.get("/api/people").json()) == 1


def test_confirming_without_remember_does_not_store_the_person(client, notebook):
    client.put(
        f"/api/sessions/{notebook}/speakers",
        json={"speakers": [{"speaker_index": 0, "name": "Invitado", "role": "other",
                            "remember": False}]},
    )
    assert client.get("/api/people").json() == []


def test_confirming_me_propagates_to_the_transcript(client, notebook):
    client.put(
        f"/api/sessions/{notebook}/speakers",
        json={"speakers": [{"speaker_index": 0, "name": "Yo", "role": "me"}]},
    )
    segments = client.get(f"/api/sessions/{notebook}/transcript").json()
    mine = {s["speaker_index"] for s in segments if s["is_me"]}
    assert mine == {0}


def test_duplicate_person_is_rejected(client):
    client.post("/api/people", json={"name": "Sara", "role": "teacher"})
    assert client.post("/api/people", json={"name": "sara", "role": "teacher"}) \
        .status_code == 409


def test_person_can_be_deleted(client):
    person_id = client.post("/api/people", json={"name": "Error", "role": "other"}) \
        .json()["id"]
    assert client.delete(f"/api/people/{person_id}").status_code == 204
    assert client.delete(f"/api/people/{person_id}").status_code == 404


def test_person_name_cannot_be_blank(client):
    assert client.post("/api/people", json={"name": "   "}).status_code == 422


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


def test_chat_answers_and_persists_history(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm("Handle time es la duración de la llamada [08:00:00].")
    response = client.post(f"/api/sessions/{notebook}/chat",
                           json={"message": "¿qué es handle time?"})
    assert response.status_code == 200
    body = response.json()
    assert "handle time" in body["reply"].lower()
    assert body["citations"]

    messages = client.get(f"/api/sessions/{notebook}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["meta"]["citations"]


def test_chat_sends_previous_turns_as_history(client, notebook, config_file, fake_llm):
    config_file()
    double = fake_llm("primera", "segunda")
    client.post(f"/api/sessions/{notebook}/chat", json={"message": "hola"})
    client.post(f"/api/sessions/{notebook}/chat", json={"message": "y ahora?"})
    history = double.calls[1]["history"]
    assert any(item["content"] == "hola" for item in history)


def test_chat_reset_clears_history(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm("uno", "dos")
    client.post(f"/api/sessions/{notebook}/chat", json={"message": "hola"})
    client.post(f"/api/sessions/{notebook}/chat", json={"message": "otra", "reset": True})
    messages = client.get(f"/api/sessions/{notebook}/messages").json()
    assert len(messages) == 2


def test_chat_without_transcript_is_409(client, config_file, fake_llm):
    config_file()
    fake_llm("x")
    session_id = pipeline.create_session()
    assert client.post(f"/api/sessions/{session_id}/chat", json={"message": "hola"}) \
        .status_code == 409


def test_chat_without_key_is_400(client, notebook):
    response = client.post(f"/api/sessions/{notebook}/chat", json={"message": "hola"})
    assert response.status_code == 400
    assert "OpenCode" in response.json()["detail"]


def test_clear_messages(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm("hola")
    client.post(f"/api/sessions/{notebook}/chat", json={"message": "hola"})
    assert client.delete(f"/api/sessions/{notebook}/messages").status_code == 204
    assert client.get(f"/api/sessions/{notebook}/messages").json() == []


# ---------------------------------------------------------------------------
# Materiales de estudio
# ---------------------------------------------------------------------------


def test_quiz_generation_and_listing(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm({"questions": [
        {"question": "What is handle time?", "options": ["Duration", "Greeting", "Tone",
                                                         "Script"],
         "correct_index": 0, "explanation": "es la duración", "topic_title": "Welcome calls"},
    ]})
    created = client.post(f"/api/sessions/{notebook}/quiz", json={"n": 1})
    assert created.status_code == 200
    question = created.json()[0]
    assert question["options"][question["correct_index"]] == "Duration"
    assert client.get(f"/api/sessions/{notebook}/quiz").json()[0]["id"] == question["id"]


def test_quiz_regeneration_replaces_previous(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm(
        {"questions": [{"question": "vieja", "options": ["a", "b"], "correct_index": 0}]},
        {"questions": [{"question": "nueva", "options": ["a", "b"], "correct_index": 0}]},
    )
    client.post(f"/api/sessions/{notebook}/quiz", json={"n": 1})
    client.post(f"/api/sessions/{notebook}/quiz", json={"n": 1})
    questions = client.get(f"/api/sessions/{notebook}/quiz").json()
    assert len(questions) == 1 and questions[0]["question"] == "nueva"


def test_quiz_without_notes_is_409(client, config_file, fake_llm):
    config_file()
    fake_llm({"questions": []})
    session_id = pipeline.create_session()
    assert client.post(f"/api/sessions/{session_id}/quiz").status_code == 409


def test_quiz_size_is_validated(client, notebook, config_file):
    config_file()
    assert client.post(f"/api/sessions/{notebook}/quiz", json={"n": 99}).status_code == 422


def test_flashcards_with_spaced_repetition(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm({"flashcards": [{"front": "handle time", "back": "duración de la llamada"}]})
    cards = client.post(f"/api/sessions/{notebook}/flashcards", json={"n": 1}).json()
    assert cards[0]["box"] == 1
    card_id = cards[0]["id"]

    right = client.post(f"/api/sessions/{notebook}/flashcards/{card_id}/review",
                        json={"correct": True}).json()
    assert right["box"] == 2 and right["due_at"]

    wrong = client.post(f"/api/sessions/{notebook}/flashcards/{card_id}/review",
                        json={"correct": False}).json()
    assert wrong["box"] == 1


def test_flashcard_review_missing_card(client, notebook):
    assert client.post(f"/api/sessions/{notebook}/flashcards/999/review",
                       json={"correct": True}).status_code == 404


def test_concept_map_generation(client, notebook, config_file, fake_llm):
    config_file()
    fake_llm({"nodes": [{"id": "n1", "label": "Customer service", "group": "root"},
                        {"id": "n2", "label": "Greetings", "group": "topic"}],
              "edges": [{"from": "n1", "to": "n2", "label": "incluye"}]})
    created = client.post(f"/api/sessions/{notebook}/concept-map").json()
    assert len(created["nodes"]) == 2
    assert created["edges"][0]["from"] == "n1"
    assert client.get(f"/api/sessions/{notebook}/concept-map").json() == created


def test_concept_map_empty_before_generating(client, notebook):
    assert client.get(f"/api/sessions/{notebook}/concept-map").json() == {
        "nodes": [], "edges": []
    }


def test_podcast_endpoint_persists_and_serves(client, notebook, config_file, fake_llm,
                                             monkeypatch):
    config_file()
    fake_llm({"lines": [{"speaker": "A", "text": "Today we reviewed welcome calls."},
                        {"speaker": "B", "text": "And handle time."}]})

    from app.ai import podcast as podcast_ai

    async def fake_render(lines, out_dir, **kwargs):
        target = paths.session_dir(notebook) / "podcast" / "podcast.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ID3" + b"\0" * 4096)
        return target

    monkeypatch.setattr(podcast_ai, "render_podcast", fake_render)
    monkeypatch.setattr(podcast_ai.ffmpeg, "duration", lambda path: 210.0)

    created = client.post(f"/api/sessions/{notebook}/podcast")
    assert created.status_code == 200
    body = created.json()
    assert body["duration_sec"] == 210.0
    assert "A: Today we reviewed" in body["script"]
    assert body["audio_url"].endswith("/media/podcast")

    audio = client.get(body["audio_url"])
    assert audio.status_code == 200
    assert audio.headers["accept-ranges"] == "bytes"


def test_podcast_is_none_before_generating(client, notebook):
    assert client.get(f"/api/sessions/{notebook}/podcast").json() is None


# ---------------------------------------------------------------------------
# Medios (Range)
# ---------------------------------------------------------------------------


@pytest.fixture()
def with_audio(notebook):
    target = paths.session_dir(notebook) / "session.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(range(256)) * 40)      # 10 240 bytes
    return notebook, target


def test_session_audio_full_download(client, with_audio):
    session_id, target = with_audio
    response = client.get(f"/api/sessions/{session_id}/media/session")
    assert response.status_code == 200
    assert len(response.content) == target.stat().st_size
    assert response.headers["accept-ranges"] == "bytes"


def test_session_audio_range_request(client, with_audio):
    """Sin esto, saltar a un tramo de la timeline descargaría el MP3 completo."""
    session_id, _ = with_audio
    response = client.get(f"/api/sessions/{session_id}/media/session",
                          headers={"Range": "bytes=100-199"})
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 100-199/10240"
    assert len(response.content) == 100


def test_session_audio_open_ended_range(client, with_audio):
    session_id, _ = with_audio
    response = client.get(f"/api/sessions/{session_id}/media/session",
                          headers={"Range": "bytes=10000-"})
    assert response.status_code == 206
    assert len(response.content) == 240


def test_session_audio_suffix_range(client, with_audio):
    session_id, _ = with_audio
    response = client.get(f"/api/sessions/{session_id}/media/session",
                          headers={"Range": "bytes=-100"})
    assert response.status_code == 206
    assert len(response.content) == 100


def test_session_audio_unsatisfiable_range(client, with_audio):
    session_id, _ = with_audio
    response = client.get(f"/api/sessions/{session_id}/media/session",
                          headers={"Range": "bytes=99999-"})
    assert response.status_code == 416


def test_session_audio_malformed_range(client, with_audio):
    session_id, _ = with_audio
    response = client.get(f"/api/sessions/{session_id}/media/session",
                          headers={"Range": "elephants=1-2"})
    assert response.status_code == 416


def test_session_audio_missing_is_404(client, notebook):
    assert client.get(f"/api/sessions/{notebook}/media/session").status_code == 404


def test_clip_validation(client, with_audio):
    session_id, _ = with_audio
    assert client.get(f"/api/sessions/{session_id}/media/clip?start=10&end=5") \
        .status_code == 422
    assert client.get(f"/api/sessions/{session_id}/media/clip?start=0&end=99999") \
        .status_code == 422


# ---------------------------------------------------------------------------
# Ajustes y sistema
# ---------------------------------------------------------------------------


def test_settings_never_return_secrets(client, config_file):
    config_file()
    settings = client.get("/api/settings").json()
    assert settings["opencode"]["api_key"] == ""
    assert settings["opencode"]["api_key_set"] is True


def test_saving_settings_does_not_erase_keys(client, config_file):
    """El bug del plan: guardar una preferencia borraba las llaves."""
    config_file()
    response = client.put("/api/settings", json={
        "opencode": {"api_key": ""},
        "settings": {"integration_interval_sec": 120},
    })
    assert response.status_code == 200
    assert response.json()["opencode"]["api_key_set"] is True
    from app import config as app_config

    assert app_config.load_config(force=True)["settings"]["integration_interval_sec"] == 120


def test_settings_can_update_a_key(client, config_file):
    config_file()
    client.put("/api/settings", json={"deepgram": {"api_key": "dg-nueva-llave-larga"}})
    from app import config as app_config

    assert app_config.load_config(force=True)["deepgram"]["api_key"] == "dg-nueva-llave-larga"


def test_keys_can_be_cleared_explicitly(client, config_file):
    config_file()
    response = client.delete("/api/settings/keys/deepgram")
    assert response.json()["deepgram"]["api_key_set"] is False
    assert client.delete("/api/settings/keys/inventado").status_code == 404


def test_test_connection_reports_every_provider(client, config_file, monkeypatch):
    config_file()
    from app.ai import opencode_client
    from app.routers import settings as settings_router
    from app.transcription import deepgram_client

    monkeypatch.setattr(opencode_client, "ping",
                        lambda cfg, role="live": {"ok": True, "detail": "modelo fake"})
    monkeypatch.setattr(deepgram_client, "check_credentials",
                        lambda key, **kw: {"ok": True, "detail": "conectado",
                                           "extra": {"balance_usd": 199.5}})
    monkeypatch.setattr(settings_router, "_check_tts",
                        lambda: settings_router.ProviderCheck(ok=True, detail="12 voces"))

    report = client.post("/api/settings/test").json()
    assert report["opencode"]["ok"] is True
    assert report["deepgram"]["extra"]["balance_usd"] == 199.5
    assert report["tts"]["ok"] is True
    assert "audio" in report


def test_devices_endpoint_never_crashes(client):
    body = client.get("/api/settings/devices").json()
    assert set(body) >= {"loopback", "input", "output"}


def test_models_endpoint_lists_resolution(client, config_file, monkeypatch):
    config_file()
    from app.ai import models

    monkeypatch.setattr(models, "_fetch_catalog", lambda *a, **k: ["deepseek-v4-flash"])
    models.invalidate()
    body = client.get("/api/settings/models").json()
    assert body["catalog"] == ["deepseek-v4-flash"]
    assert body["resolved"]["live"] == "deepseek-v4-flash"


def test_system_status(client, config_file):
    config_file()
    body = client.get("/api/system").json()
    assert body["keys"]["opencode"] is True
    assert body["free_mb"] > 0
    assert "queue" in body
    assert body["data_dir"]


def test_acknowledge_flags(client):
    assert client.post("/api/system/acknowledge?kind=legal").json()["legal_notice_seen"]
    assert client.post("/api/system/acknowledge?kind=onboarding").json()["onboarding_done"]
    assert client.post("/api/system/acknowledge?kind=otro").status_code == 422


# ---------------------------------------------------------------------------
# Backup / restore por HTTP
# ---------------------------------------------------------------------------


def test_export_and_restore_roundtrip(client, notebook):
    exported = client.get(f"/api/sessions/{notebook}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"

    restored = client.post(
        "/api/backup/restore",
        files={"file": ("cuaderno.zip", io.BytesIO(exported.content), "application/zip")},
    )
    assert restored.status_code == 200
    new_id = restored.json()["session_id"]
    assert new_id != notebook
    topics = client.get(f"/api/sessions/{new_id}/topics").json()
    assert topics[0]["title"] == "Welcome calls"


def test_restore_rejects_non_zip(client):
    response = client.post(
        "/api/backup/restore",
        files={"file": ("notas.txt", io.BytesIO(b"hola"), "text/plain")},
    )
    assert response.status_code == 422


def test_restore_rejects_a_corrupt_zip(client):
    response = client.post(
        "/api/backup/restore",
        files={"file": ("x.zip", io.BytesIO(b"no soy un zip"), "application/zip")},
    )
    assert response.status_code == 400
