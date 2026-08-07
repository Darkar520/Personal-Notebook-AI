"""Transcripción: cliente Deepgram, fusión de hablantes, huella vocal, cola y worker."""

from __future__ import annotations

import numpy as np
import pytest

from app import db
from app.errors import STTError
from app.transcription import deepgram_client, fallback, fusion, voiceprint
from app.transcription import queue as stt_queue
from app.transcription import worker
from tests.conftest import two_speaker_signal, write_wav


def _session(number: int = 1) -> int:
    with db.write() as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (title, session_number, started_at, created_at, updated_at)"
            " VALUES ('t', ?, ?, ?, ?)",
            (number, db.now_iso(), db.now_iso(), db.now_iso()),
        )
        return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Cliente Deepgram
# ---------------------------------------------------------------------------

PAYLOAD = {
    "metadata": {"duration": 90.5, "models": ["m1"], "model_info": {"m1": {"name": "nova-3"}}},
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "transcript": "Hello there team",
                        "confidence": 0.98,
                        "words": [
                            {"word": "hello", "punctuated_word": "Hello", "start": 0.0,
                             "end": 0.4, "speaker": 0, "confidence": 0.99},
                            {"word": "there", "punctuated_word": "there,", "start": 0.5,
                             "end": 0.9, "speaker": 0, "confidence": 0.97},
                            {"word": "team", "punctuated_word": "team.", "start": 1.2,
                             "end": 1.6, "speaker": 1, "confidence": 0.95},
                        ],
                    }
                ]
            }
        ],
        "utterances": [
            {"start": 0.0, "end": 0.9, "speaker": 0, "transcript": "Hello there,",
             "confidence": 0.98},
            {"start": 1.2, "end": 1.6, "speaker": 1, "transcript": "team.",
             "confidence": 0.95},
        ],
    },
}


def test_parse_response_prefers_utterances():
    result = deepgram_client.parse_response(PAYLOAD)
    assert len(result.utterances) == 2
    assert result.utterances[0]["text"] == "Hello there, "
    assert result.utterances[1]["speaker"] == 1
    assert result.duration == 90.5
    assert result.model == "nova-3"
    assert result.speaker_count == 2
    assert len(result.words) == 3
    assert result.words[0]["text"] == "Hello "


def test_parse_response_falls_back_to_word_grouping():
    payload = {
        "metadata": {"duration": 10.0},
        "results": {"channels": PAYLOAD["results"]["channels"]},
    }
    result = deepgram_client.parse_response(payload)
    assert len(result.utterances) == 2       # agrupadas desde las palabras
    assert result.utterances[0]["text"].startswith("Hello there,")


def test_parse_response_handles_transcript_only():
    payload = {
        "metadata": {"duration": 4.0},
        "results": {"channels": [{"alternatives": [{"transcript": "just text",
                                                    "confidence": 0.5}]}]},
    }
    result = deepgram_client.parse_response(payload)
    assert result.utterances[0]["text"] == "just text "
    assert result.utterances[0]["end"] == 4.0


def test_transcribe_file_sends_expected_params(monkeypatch, short_wav):
    seen: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return PAYLOAD

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, params=None, content=None, **kw):
            seen.update({"url": url, "headers": headers, "params": params})
            return FakeResponse()

    monkeypatch.setattr(deepgram_client.httpx, "Client", FakeClient)
    result = deepgram_client.transcribe_file(short_wav, api_key="k", language="en")
    assert seen["params"]["diarize"] == "true"
    assert seen["params"]["utterances"] == "true"
    assert seen["params"]["smart_format"] == "true"
    assert seen["params"]["model"] == "nova-3"
    assert seen["headers"]["Authorization"] == "Token k"
    assert result.duration == 90.5


def test_transcribe_file_without_key_is_not_retryable(short_wav):
    with pytest.raises(STTError) as info:
        deepgram_client.transcribe_file(short_wav, api_key="")
    assert info.value.retryable is False
    assert "Deepgram" in info.value.user_message


def test_transcribe_missing_file_is_not_retryable(tmp_path):
    with pytest.raises(STTError) as info:
        deepgram_client.transcribe_file(tmp_path / "nope.wav", api_key="k")
    assert info.value.retryable is False


@pytest.mark.parametrize(
    "status,retryable",
    [(401, False), (402, False), (400, False), (429, True), (503, True), (500, True)],
)
def test_error_classification(status, retryable):
    with pytest.raises(STTError) as info:
        deepgram_client._raise_for_status(status, "boom")
    assert info.value.retryable is retryable


def test_retries_then_succeeds(monkeypatch, short_wav):
    monkeypatch.setattr(deepgram_client.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.text = "rate limit"

        @staticmethod
        def json():
            return PAYLOAD

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            attempts["n"] += 1
            return FakeResponse(429 if attempts["n"] == 1 else 200)

    monkeypatch.setattr(deepgram_client.httpx, "Client", FakeClient)
    result = deepgram_client.transcribe_file(short_wav, api_key="k", max_retries=3)
    assert attempts["n"] == 2
    assert result.duration == 90.5


# ---------------------------------------------------------------------------
# Fusión
# ---------------------------------------------------------------------------


def test_group_words_into_turns():
    words = [
        {"start": 0.0, "end": 0.4, "speaker": 0, "text": "Hello ", "confidence": 1.0},
        {"start": 0.5, "end": 1.0, "speaker": 0, "text": "world ", "confidence": 1.0},
        {"start": 1.2, "end": 1.7, "speaker": 1, "text": "Hi ", "confidence": 1.0},
    ]
    turns = fusion.group_words(words, gap_s=0.8)
    assert len(turns) == 2
    assert turns[0]["text"] == "Hello world "
    assert turns[1]["speaker"] == 1


def test_group_words_splits_on_long_gap():
    words = [
        {"start": 0.0, "end": 0.4, "speaker": 0, "text": "one ", "confidence": 1.0},
        {"start": 5.0, "end": 5.4, "speaker": 0, "text": "two ", "confidence": 1.0},
    ]
    assert len(fusion.group_words(words, gap_s=0.8)) == 2


def test_group_words_empty():
    assert fusion.group_words([]) == []


def test_to_absolute_shifts_times():
    out = fusion.to_absolute([{"start": 1.0, "end": 2.0, "speaker": 0, "text": "x "}], 82.0)
    assert out[0]["start"] == 83.0 and out[0]["end"] == 84.0


def test_dedupe_overlap_drops_repeated_turns():
    """Un turno del pre-roll pertenece al chunk anterior y no debe duplicarse."""
    utterances = [
        {"start": 83.0, "end": 86.0, "text": "del chunk anterior ", "speaker": 0},
        {"start": 91.0, "end": 95.0, "text": "nuevo ", "speaker": 0},
    ]
    kept = fusion.dedupe_overlap(utterances, chunk_start_t=90.0)
    assert [u["text"] for u in kept] == ["nuevo "]


def test_dedupe_overlap_keeps_everything_in_first_chunk():
    utterances = [{"start": 0.0, "end": 3.0, "text": "a ", "speaker": 0}]
    assert fusion.dedupe_overlap(utterances, chunk_start_t=0.0, is_first=True) == utterances


def test_turn_cut_by_the_boundary_is_published_exactly_once():
    """La frase del borde sale entera en el chunk siguiente y no se repite en el anterior."""
    truncated = [{"start": 88.0, "end": 90.0, "text": "frase par ", "speaker": 0}]
    complete = [{"start": 88.0, "end": 93.0, "text": "frase partida entera ", "speaker": 0}]

    # Chunk 0: útil [0, 90). Su versión cortada se descarta…
    assert fusion.dedupe_overlap(truncated, chunk_start_t=0.0, chunk_end_t=90.0,
                                 is_first=True) == []
    # …y el chunk 1 (útil [90, 180), con pre-roll desde 82) publica la completa.
    assert len(fusion.dedupe_overlap(complete, chunk_start_t=90.0, chunk_end_t=180.0)) == 1


def test_turn_at_the_boundary_is_kept_in_the_last_chunk():
    """En el último chunk no hay sucesor: lo del borde no se puede descartar."""
    tail = [{"start": 268.0, "end": 270.0, "text": "cierre ", "speaker": 0}]
    assert fusion.dedupe_overlap(tail, chunk_start_t=180.0, chunk_end_t=270.0,
                                 is_last=True) == tail


# ---------------------------------------------------------------------------
# Huella vocal
# ---------------------------------------------------------------------------


def _tone(freq: float, seconds: float = 3.0, sr: int = 16000, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    signal = 0.3 * np.sin(2 * np.pi * freq * t) + 0.12 * np.sin(2 * np.pi * freq * 2.3 * t)
    return (signal + 0.02 * rng.normal(0, 1, t.size)).astype(np.float32)


def test_embedding_shape_and_normalization():
    vector = voiceprint.embed(_tone(180))
    assert vector is not None
    assert vector.size == voiceprint.DIM
    assert pytest.approx(float(np.linalg.norm(vector)), abs=1e-4) == 1.0


def test_embedding_needs_enough_audio():
    assert voiceprint.embed(_tone(180, seconds=0.4)) is None
    assert voiceprint.embed(np.zeros(16000 * 3, dtype=np.float32)) is None


def test_same_voice_scores_higher_than_different_voice():
    a1 = voiceprint.embed(_tone(180, seed=1))
    a2 = voiceprint.embed(_tone(180, seed=2))
    b = voiceprint.embed(_tone(340, seed=3))
    assert voiceprint.similarity(a1, a2) > voiceprint.similarity(a1, b)
    assert voiceprint.similarity(a1, a2) > voiceprint.SAME_SPEAKER_MIN


def test_similarity_handles_missing_vectors():
    assert voiceprint.similarity(None, None) == 0.0
    assert voiceprint.similarity(voiceprint.embed(_tone(180)), None) == 0.0


def test_json_roundtrip():
    vector = voiceprint.embed(_tone(200))
    payload = voiceprint.to_json(vector, 12.5)
    restored, seconds = voiceprint.from_json(payload)
    assert seconds == 12.5
    assert voiceprint.similarity(vector, restored) > 0.99


def test_combine_is_weighted():
    a = voiceprint.embed(_tone(180))
    b = voiceprint.embed(_tone(340))
    merged = voiceprint.combine(a, 100.0, b, 1.0)
    assert voiceprint.similarity(merged, a) > voiceprint.similarity(merged, b)


def test_speaker_embeddings_splits_by_speaker():
    sr = 16000
    samples = np.concatenate([_tone(180, 4.0), _tone(340, 4.0, seed=5)])
    utterances = [
        {"start": 0.0, "end": 4.0, "speaker": 0, "text": "a "},
        {"start": 4.0, "end": 8.0, "speaker": 1, "text": "b "},
    ]
    embeddings = voiceprint.speaker_embeddings(samples, utterances, sr=sr)
    assert set(embeddings) == {0, 1}
    assert embeddings[0][1] == pytest.approx(4.0)
    assert voiceprint.similarity(embeddings[0][0], embeddings[1][0]) < voiceprint.SAME_SPEAKER_MIN


# ---------------------------------------------------------------------------
# Registro de hablantes
# ---------------------------------------------------------------------------


def test_registry_keeps_identity_when_deepgram_swaps_indexes():
    """El caso que rompía el `sticky-by-index` del plan original."""
    session_id = _session()
    teacher = voiceprint.embed(_tone(180, seed=1))
    student = voiceprint.embed(_tone(340, seed=2))

    registry = fusion.SpeakerRegistry(session_id)
    first = registry.assign({0: (teacher, 40.0), 1: (student, 20.0)})
    registry.save()
    assert first == {0: 0, 1: 1}

    # En el chunk siguiente habla primero el estudiante: Deepgram le da el índice 0.
    registry = fusion.SpeakerRegistry(session_id)
    second = registry.assign({0: (student, 15.0), 1: (teacher, 30.0)})
    registry.save()
    assert second[0] == 1, "el estudiante debe seguir siendo el hablante global 1"
    assert second[1] == 0, "la teacher debe seguir siendo la hablante global 0"


def test_registry_creates_new_speaker_for_new_voice():
    session_id = _session()
    registry = fusion.SpeakerRegistry(session_id)
    registry.assign({0: (voiceprint.embed(_tone(180)), 30.0)})
    registry.save()

    registry = fusion.SpeakerRegistry(session_id)
    mapping = registry.assign({0: (voiceprint.embed(_tone(520, seed=9)), 10.0)})
    registry.save()
    assert mapping[0] == 1
    assert len(fusion.SpeakerRegistry(session_id).speakers) == 2


def test_registry_falls_back_to_index_without_embeddings():
    session_id = _session()
    registry = fusion.SpeakerRegistry(session_id)
    assert registry.assign({0: (None, 5.0), 1: (None, 4.0)}) == {0: 0, 1: 1}
    registry.save()
    registry = fusion.SpeakerRegistry(session_id)
    assert registry.assign({0: (None, 5.0), 1: (None, 4.0)}) == {0: 0, 1: 1}


def test_registry_accumulates_talk_seconds_and_persists():
    session_id = _session()
    registry = fusion.SpeakerRegistry(session_id)
    registry.assign({0: (voiceprint.embed(_tone(180)), 30.0)})
    registry.save()
    registry = fusion.SpeakerRegistry(session_id)
    registry.assign({0: (voiceprint.embed(_tone(180, seed=4)), 20.0)})
    registry.save()
    assert fusion.SpeakerRegistry(session_id).talk_seconds()[0] == pytest.approx(50.0)
    assert fusion.SpeakerRegistry(session_id).voice_json(0) is not None


def test_registry_clear_removes_state():
    session_id = _session()
    registry = fusion.SpeakerRegistry(session_id)
    registry.assign({0: (None, 1.0)})
    registry.save()
    fusion.SpeakerRegistry.clear(session_id)
    assert fusion.SpeakerRegistry(session_id).speakers == {}


def test_merge_speakers_legacy_contract():
    chunk_a = [{"start": 0.0, "end": 5.0, "speaker": 0, "text": "A "},
               {"start": 5.0, "end": 9.0, "speaker": 1, "text": "B "}]
    chunk_b = [{"start": 0.0, "end": 4.0, "speaker": 0, "text": "C "},
               {"start": 4.0, "end": 8.0, "speaker": 1, "text": "D "}]
    segments, mapping = fusion.merge_speakers([chunk_a, chunk_b])
    assert mapping[1][0] == mapping[0][0]
    assert mapping[1][1] == mapping[0][1]
    assert all(s["speaker_global"] in (0, 1) for s in segments)


# ---------------------------------------------------------------------------
# is_me
# ---------------------------------------------------------------------------


def test_mark_is_me_uses_microphone_mask():
    utterances = [
        {"start": 0.0, "end": 4.0, "text": "teacher ", "speaker": 0},
        {"start": 10.0, "end": 14.0, "text": "yo ", "speaker": 1},
    ]
    fusion.mark_is_me(utterances, [(9.5, 14.5)])
    assert utterances[0]["is_me"] is False
    assert utterances[1]["is_me"] is True


def test_mark_is_me_ignores_brief_overlap():
    utterances = [{"start": 0.0, "end": 10.0, "text": "teacher ", "speaker": 0}]
    fusion.mark_is_me(utterances, [(9.0, 10.0)])     # solo 10 % del turno
    assert utterances[0]["is_me"] is False


def test_dominant_me_speaker():
    utterances = [
        {"start": 0, "end": 10, "speaker_global": 0, "is_me": False},
        {"start": 10, "end": 20, "speaker_global": 1, "is_me": True},
        {"start": 20, "end": 26, "speaker_global": 1, "is_me": True},
    ]
    assert fusion.dominant_me_speaker(utterances) == 1


def test_dominant_me_speaker_none_when_ambiguous():
    utterances = [
        {"start": 0, "end": 100, "speaker_global": 0, "is_me": False},
        {"start": 100, "end": 101, "speaker_global": 0, "is_me": True},
    ]
    assert fusion.dominant_me_speaker(utterances) is None


# ---------------------------------------------------------------------------
# Cola
# ---------------------------------------------------------------------------


def test_enqueue_and_claim():
    session_id = _session()
    stt_queue.enqueue(session_id, "sessions/1/audio/chunk_000000.wav", 0.0, chunk_index=0)
    stt_queue.enqueue(session_id, "sessions/1/audio/chunk_000001.wav", 90.0, chunk_index=1)
    assert stt_queue.counts(session_id)["pending"] == 2

    row = stt_queue.claim_one()
    assert row is not None
    assert row["start_t"] == 0.0
    counts = stt_queue.counts(session_id)
    assert counts["claimed"] == 1 and counts["pending"] == 1


def test_enqueue_is_idempotent_per_chunk_index():
    session_id = _session()
    stt_queue.enqueue(session_id, "a.wav", 0.0, chunk_index=0)
    stt_queue.enqueue(session_id, "a.wav", 0.0, chunk_index=0, duration=90.0)
    assert stt_queue.counts(session_id)["total"] == 1


def test_claim_batch_does_not_hand_out_the_same_chunk_twice():
    session_id = _session()
    for index in range(4):
        stt_queue.enqueue(session_id, f"c{index}.wav", index * 90.0, chunk_index=index)
    first = stt_queue.claim_batch(2)
    second = stt_queue.claim_batch(2)
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})


def test_mark_failed_retries_then_gives_up():
    session_id = _session()
    stt_queue.enqueue(session_id, "c.wav", 0.0, chunk_index=0)
    row = stt_queue.claim_one()
    assert stt_queue.mark_failed(row["id"], "boom", max_retries=3) == "pending"
    assert stt_queue.mark_failed(row["id"], "boom", max_retries=3) == "pending"
    assert stt_queue.mark_failed(row["id"], "boom", max_retries=3) == "failed"
    assert stt_queue.counts(session_id)["failed"] == 1


def test_mark_failed_non_retryable_fails_immediately():
    session_id = _session()
    stt_queue.enqueue(session_id, "c.wav", 0.0, chunk_index=0)
    row = stt_queue.claim_one()
    assert stt_queue.mark_failed(row["id"], "bad key", retryable=False) == "failed"


def test_retry_failed_reactivates():
    session_id = _session()
    stt_queue.enqueue(session_id, "c.wav", 0.0, chunk_index=0)
    row = stt_queue.claim_one()
    stt_queue.mark_failed(row["id"], "boom", retryable=False)
    assert stt_queue.retry_failed(session_id=session_id) == 1
    assert stt_queue.counts(session_id)["pending"] == 1


def test_requeue_stale_recovers_after_a_crash():
    session_id = _session()
    stt_queue.enqueue(session_id, "c.wav", 0.0, chunk_index=0)
    stt_queue.claim_one()
    assert stt_queue.requeue_stale(timeout_seconds=0) == 1
    assert stt_queue.counts(session_id)["pending"] == 1


def test_mark_ok_removes_the_row():
    session_id = _session()
    stt_queue.enqueue(session_id, "c.wav", 0.0, chunk_index=0)
    row = stt_queue.claim_one()
    stt_queue.mark_ok(row["id"])
    assert stt_queue.counts(session_id)["total"] == 0


def test_meta_sidecar_roundtrip(tmp_path):
    chunk = tmp_path / "chunk_000000.wav"
    chunk.write_bytes(b"fake")
    stt_queue.write_meta(chunk, {"mic_ranges": [[1.0, 2.0]], "silences": []})
    assert stt_queue.read_meta(chunk)["mic_ranges"] == [[1.0, 2.0]]
    assert stt_queue.read_meta(tmp_path / "missing.wav") == {}


def test_absolute_path_resolves_relative_rows(isolated_env):
    row = {"chunk_path": "sessions/5/audio/chunk_000000.wav"}
    assert stt_queue.absolute_path(row) == (
        isolated_env["data"] / "sessions" / "5" / "audio" / "chunk_000000.wav"
    ).resolve()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _prepare_chunk(session_id: int, index: int, start_t: float, *, overlap: float = 0.0,
                   mic_ranges=None):
    from app import paths

    audio_dir = paths.session_audio_dir(session_id)
    path = write_wav(audio_dir / f"chunk_{index:06d}.wav", two_speaker_signal(30.0))
    stt_queue.write_meta(
        path,
        {
            "chunk_index": index,
            "start_t": start_t,
            "duration": 90.0,
            "overlap_pre": overlap,
            "mic_ranges": mic_ranges or [],
            "silences": [],
        },
    )
    stt_queue.enqueue(session_id, path, start_t, chunk_index=index, duration=90.0,
                      overlap_pre=overlap)
    return path


def test_worker_inserts_segments_with_absolute_times(fake_stt, config_file):
    """El segundo chunk lleva 8 s de pre-roll: sus tiempos deben salir desplazados a 90+."""
    config_file()

    def utterances(chunk_index: int):
        base = 8.0 if chunk_index else 0.0     # justo después del pre-roll
        return [
            {"start": base + 1, "end": base + 5, "speaker": 0, "text": "Teacher talks. "},
            {"start": base + 6, "end": base + 9, "speaker": 1, "text": "Student answers. "},
        ]

    fake_stt(utterances)
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0)
    _prepare_chunk(session_id, 1, 90.0, overlap=8.0)

    processed, errors = worker.process_pending_once(session_id=session_id)
    assert (processed, errors) == (2, 0)

    segments = db.fetch_segments(session_id)
    starts = sorted(round(float(s["start_t"]), 1) for s in segments)
    assert starts == [1.0, 6.0, 91.0, 96.0]
    assert stt_queue.counts(session_id)["total"] == 0


def test_worker_is_idempotent(fake_stt, config_file):
    config_file()
    fake_stt()
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0)
    worker.process_pending_once(session_id=session_id)
    _prepare_chunk(session_id, 0, 0.0)         # reencolado del mismo chunk
    worker.process_pending_once(session_id=session_id)
    assert len(db.fetch_segments(session_id)) == 2


def test_worker_marks_is_me_from_microphone(fake_stt, config_file):
    config_file()
    fake_stt()
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0, mic_ranges=[[5.5, 9.5]])
    worker.process_pending_once(session_id=session_id)
    rows = {int(s["speaker_index"]): bool(s["is_me"]) for s in db.fetch_segments(session_id)}
    assert rows[0] is False
    assert rows[1] is True


def test_worker_creates_session_speakers_with_colors(fake_stt, config_file):
    config_file()
    fake_stt()
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0)
    worker.process_pending_once(session_id=session_id)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT speaker_index, color, talk_seconds FROM session_speakers"
            " WHERE session_id=?", (session_id,),
        ).fetchall()
    assert len(rows) == 2
    assert all(r["color"] for r in rows)
    assert all(float(r["talk_seconds"]) > 0 for r in rows)


def test_worker_records_usage(fake_stt, config_file):
    config_file()
    fake_stt()
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0)
    worker.process_pending_once(session_id=session_id)
    with db.read() as conn:
        row = conn.execute(
            "SELECT minutes, cost_usd FROM usage_events WHERE session_id=? AND kind='stt'",
            (session_id,),
        ).fetchone()
    assert float(row["minutes"]) == pytest.approx(1.5)
    assert float(row["cost_usd"]) > 0


def test_worker_accumulates_breaks(config_file, monkeypatch):
    config_file()
    session_id = _session()
    path = _prepare_chunk(session_id, 0, 0.0)
    stt_queue.write_meta(
        path,
        {"chunk_index": 0, "start_t": 0.0, "duration": 90.0, "overlap_pre": 0.0,
         "mic_ranges": [], "silences": [[30.0, 80.0]]},
    )
    from app.transcription import deepgram_client as dg

    monkeypatch.setattr(
        worker, "transcribe",
        lambda row, cfg: dg.SttResult(utterances=[], duration=90.0, provider="deepgram"),
    )
    worker.process_pending_once(session_id=session_id)
    assert worker.load_breaks(session_id) == [(30.0, 80.0)]


def test_worker_keeps_going_after_a_retryable_failure(config_file, monkeypatch):
    config_file()
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0)
    monkeypatch.setattr(
        worker, "transcribe",
        lambda row, cfg: (_ for _ in ()).throw(STTError("503", retryable=True)),
    )
    processed, errors = worker.process_pending_once(session_id=session_id)
    assert (processed, errors) == (0, 1)
    assert stt_queue.counts(session_id)["pending"] == 1     # sigue en cola


def test_worker_stops_on_invalid_key(config_file, monkeypatch):
    config_file()
    session_id = _session()
    _prepare_chunk(session_id, 0, 0.0)
    _prepare_chunk(session_id, 1, 90.0)
    monkeypatch.setattr(
        worker, "transcribe",
        lambda row, cfg: (_ for _ in ()).throw(STTError("401", retryable=False)),
    )
    processed, errors = worker.process_pending_once(session_id=session_id)
    assert processed == 0 and errors >= 1
    assert stt_queue.counts(session_id)["failed"] >= 1


def test_worker_dispatches_to_whisper_backend(config_file, monkeypatch, short_wav):
    cfg = config_file(settings={"stt_backend": "whisper"})
    called: dict = {}

    def fake_whisper(path, **kwargs):
        called["path"] = path
        from app.transcription.deepgram_client import SttResult

        return SttResult(utterances=[], duration=1.0, provider="whisper", diarized=False)

    monkeypatch.setattr(fallback, "transcribe_whisper", fake_whisper)
    row = {"session_id": 1, "chunk_index": 0, "chunk_path": str(short_wav), "start_t": 0.0}
    result = worker.transcribe(row, cfg)
    assert result.provider == "whisper"
    assert called["path"] == short_wav


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------


def test_gemini_parser_recovers_structured_segments():
    text = '```json\n{"segments":[{"start":0,"end":3,"speaker":0,"text":"hello"}]}\n```'
    parsed = fallback._parse_gemini(text, 10.0)
    assert parsed[0]["text"] == "hello "
    assert parsed[0]["end"] == 3.0


def test_gemini_parser_spreads_plain_prose():
    """Sin JSON, el plan devolvía start=end=0 y destrozaba la línea de tiempo."""
    parsed = fallback._parse_gemini("Hello there. How are you? Fine.", 30.0)
    assert len(parsed) == 3
    assert parsed[0]["start"] == 0.0
    assert parsed[-1]["end"] == pytest.approx(30.0, abs=0.5)
    assert all(p["end"] > p["start"] for p in parsed)


def test_gemini_parser_fixes_broken_timeline():
    text = '{"segments":[{"start":10,"end":5,"speaker":0,"text":"a"},' \
           '{"start":2,"end":3,"speaker":1,"text":"b"}]}'
    parsed = fallback._parse_gemini(text, 60.0)
    assert parsed[0]["end"] > parsed[0]["start"]
    assert parsed[1]["start"] >= parsed[0]["end"]
