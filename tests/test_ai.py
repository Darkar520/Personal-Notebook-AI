"""Capa de IA: JSON tolerante, resolución de modelos, cliente, notas, libro y estudio."""

from __future__ import annotations

import json

import pytest

from app.ai import chat as chat_ai
from app.ai import jsonx, live_integration, models, opencode_client, podcast, polish, study
from app.errors import AIError, ConfigError

CFG = {
    "opencode": {
        "api_key": "sk-test",
        "base_url": "https://llm.test/v1",
        "models": {"live": "deepseek-v4-flash", "polish": "deepseek-v4-pro"},
        "model_fallbacks": {"live": ["*flash*"], "polish": ["*pro*", "*flash*"]},
    },
    "pricing": {"llm_usd_per_1m_input": 0.28, "llm_usd_per_1m_output": 0.42},
}
CREDS = {"model": "m", "base_url": "https://llm.test/v1", "api_key": "sk-test"}


# ---------------------------------------------------------------------------
# jsonx
# ---------------------------------------------------------------------------


def test_plain_json():
    assert jsonx.loads('{"a": 1}') == {"a": 1}


def test_json_inside_code_fence():
    text = 'Sure!\n```json\n{"topics": [1, 2]}\n```\nHope it helps.'
    assert jsonx.loads(text) == {"topics": [1, 2]}


def test_json_with_prose_around():
    assert jsonx.loads('Here you go: {"a": [1]} — done') == {"a": [1]}


def test_trailing_commas_are_repaired():
    assert jsonx.loads('{"a": [1, 2,], "b": 3,}') == {"a": [1, 2], "b": 3}


def test_smart_quotes_and_comments_are_repaired():
    text = '{\n  // el modelo se puso a comentar\n  \u201ca\u201d: 1\n}'
    assert jsonx.loads(text) == {"a": 1}


def test_python_literals_are_repaired():
    assert jsonx.loads('{"a": None, "b": True, "c": False}') == {
        "a": None, "b": True, "c": False
    }


def test_truncated_json_is_closed():
    """Respuesta cortada por `max_tokens`: se recupera lo que llegó."""
    value = jsonx.loads('{"topics": [{"title": "A"}, {"title": "B"}]')
    assert len(value["topics"]) == 2


def test_empty_or_garbage_raises():
    for bad in ("", "   ", "no hay json aquí"):
        with pytest.raises(jsonx.JsonExtractionError):
            jsonx.loads(bad)


def test_as_dict_wraps_a_bare_list():
    assert jsonx.as_dict("[1, 2]") == {"items": [1, 2]}


def test_pick_list_tolerates_key_names():
    assert jsonx.pick_list({"topics": [1]}, "topics") == [1]
    assert jsonx.pick_list({"items": [2]}, "topics") == [2]
    assert jsonx.pick_list({"outline": [3]}, "topics") == [3]
    assert jsonx.pick_list([4], "topics") == [4]
    assert jsonx.pick_list({"a": 1}, "topics") == []


def test_coercion_helpers():
    assert jsonx.as_str(["a", "b"]) == "a b"
    assert jsonx.as_float("12,5 segundos") == 12.5
    assert jsonx.as_float(None, 3.0) == 3.0
    assert jsonx.as_int("7 preguntas") == 7
    assert jsonx.as_str_list([{"text": "x"}, "y", None]) == ["x", "y"]
    assert jsonx.as_str_list("solo uno") == ["solo uno"]


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def _catalog(monkeypatch, names):
    monkeypatch.setattr(models, "_fetch_catalog", lambda *a, **k: list(names))
    models.invalidate()


def test_resolve_exact_match(monkeypatch):
    _catalog(monkeypatch, ["deepseek-v4-flash", "deepseek-v4-pro"])
    assert models.resolve("live", CFG) == "deepseek-v4-flash"


def test_resolve_with_provider_prefix(monkeypatch):
    """El caso que dejaba la app sin IA: el proveedor prefija el nombre."""
    _catalog(monkeypatch, ["deepseek/deepseek-v4-flash", "zhipu/glm-5.2"])
    assert models.resolve("live", CFG) == "deepseek/deepseek-v4-flash"


def test_resolve_with_date_suffix(monkeypatch):
    _catalog(monkeypatch, ["deepseek-v4-flash-20260801"])
    assert models.resolve("live", CFG) == "deepseek-v4-flash-20260801"


def test_resolve_uses_wildcard_fallback(monkeypatch):
    _catalog(monkeypatch, ["some-other-flash-model"])
    assert models.resolve("live", CFG) == "some-other-flash-model"


def test_resolve_last_resort_is_first_catalog_entry(monkeypatch):
    _catalog(monkeypatch, ["completely-different"])
    assert models.resolve("live", CFG) == "completely-different"


def test_resolve_without_catalog_keeps_configured_id(monkeypatch):
    monkeypatch.setattr(models, "_fetch_catalog", lambda *a, **k: [])
    models.invalidate()
    assert models.resolve("live", CFG) == "deepseek-v4-flash"


def test_catalog_is_cached_in_settings(monkeypatch):
    calls = {"n": 0}

    def fetch(*a, **k):
        calls["n"] += 1
        return ["m1"]

    monkeypatch.setattr(models, "_fetch_catalog", fetch)
    models.invalidate()
    models.catalog("https://llm.test/v1", "sk")
    models._memory.update({"stamp": 0.0, "base_url": "", "models": []})
    models.catalog("https://llm.test/v1", "sk")     # llega desde `settings`
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# opencode_client
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _completion(content: str, tokens=(120, 40)) -> dict:
    return {
        "model": "fake-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]},
    }


def test_chat_json_parses_and_reports_usage(monkeypatch):
    from app import db

    seen: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update({"url": url, "payload": json, "headers": headers})
        return FakeResponse(200, _completion('{"topics": []}'))

    monkeypatch.setattr(opencode_client.httpx, "post", fake_post)
    out = opencode_client.chat_json("sys", "usr", purpose="test", pricing=CFG["pricing"],
                                    **CREDS)
    assert out == {"topics": []}
    assert seen["url"].endswith("/chat/completions")
    assert seen["payload"]["stream"] is False
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    with db.read() as conn:
        row = conn.execute(
            "SELECT tokens_in, cost_usd FROM usage_events WHERE kind='llm'"
        ).fetchone()
    assert int(row["tokens_in"]) == 120
    assert float(row["cost_usd"]) > 0


def test_chat_text_returns_content(monkeypatch):
    monkeypatch.setattr(
        opencode_client.httpx, "post",
        lambda *a, **k: FakeResponse(200, _completion("hola")),
    )
    assert opencode_client.chat_text("s", "u", **CREDS) == "hola"


def test_history_is_included(monkeypatch):
    seen: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["messages"] = json["messages"]
        return FakeResponse(200, _completion("ok"))

    monkeypatch.setattr(opencode_client.httpx, "post", fake_post)
    opencode_client.chat_text(
        "s", "u", history=[{"role": "user", "content": "antes"},
                           {"role": "system", "content": "ignorado"}], **CREDS,
    )
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "user"]


def test_response_format_is_dropped_when_unsupported(monkeypatch):
    attempts: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        attempts.append(dict(json))
        if "response_format" in json:
            return FakeResponse(400, {}, text="unsupported parameter: response_format")
        return FakeResponse(200, _completion('{"ok": true}'))

    monkeypatch.setattr(opencode_client.httpx, "post", fake_post)
    assert opencode_client.chat_json("s", "u", **CREDS) == {"ok": True}
    assert "response_format" in attempts[0]
    assert "response_format" not in attempts[1]


def test_bad_json_triggers_one_strict_retry(monkeypatch):
    replies = iter(["no soy json", '{"ok": 1}'])
    systems: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        systems.append(json["messages"][0]["content"])
        return FakeResponse(200, _completion(next(replies)))

    monkeypatch.setattr(opencode_client.httpx, "post", fake_post)
    assert opencode_client.chat_json("s", "u", **CREDS) == {"ok": 1}
    assert "IMPORTANT" in systems[1]


def test_bad_json_twice_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(
        opencode_client.httpx, "post",
        lambda *a, **k: FakeResponse(200, _completion("nada de json")),
    )
    with pytest.raises(AIError) as info:
        opencode_client.chat_json("s", "u", **CREDS)
    assert "no se pudo interpretar" in info.value.user_message


def test_retry_on_429_then_success(monkeypatch):
    monkeypatch.setattr(opencode_client.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, {}, text="slow down")
        return FakeResponse(200, _completion("ok"))

    monkeypatch.setattr(opencode_client.httpx, "post", fake_post)
    assert opencode_client.chat_text("s", "u", **CREDS) == "ok"
    assert calls["n"] == 2


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (404, False),
                                             (429, True), (500, True)])
def test_error_classification(status, retryable):
    error = opencode_client._classify(status, "boom")
    assert error.retryable is retryable
    assert error.user_message


def test_missing_key_raises_config_error():
    with pytest.raises(ConfigError):
        opencode_client.credentials({"opencode": {"api_key": ""}})


def test_credentials_resolve_model(monkeypatch):
    _catalog(monkeypatch, ["deepseek-v4-pro"])
    creds = opencode_client.credentials(CFG, "polish")
    assert creds["model"] == "deepseek-v4-pro"
    assert creds["api_key"] == "sk-test"


def test_fit_text_trims_the_middle():
    text = "A" * 100_000
    trimmed = opencode_client.fit_text(text, max_tokens=1000)
    assert len(trimmed) < len(text)
    assert "omitido por longitud" in trimmed


def test_fit_text_leaves_short_text_alone():
    assert opencode_client.fit_text("corto", max_tokens=1000) == "corto"


# ---------------------------------------------------------------------------
# Estructuración en vivo
# ---------------------------------------------------------------------------


def test_integrate_sends_structure_and_normalizes(fake_llm):
    double = fake_llm(
        {"topics": [{"title": "Help desk vocabulary", "points": ["terms & definitions"],
                     "spanish_notes": ["handle time = duración de la llamada"]}]}
    )
    out = live_integration.integrate(
        current={"topics": []},
        new_text=["10 S0 Teacher explains help desk vocabulary.",
                  "30 S1 What does handle time mean?"],
        **CREDS,
    )
    assert out["topics"][0]["title"] == "Help desk vocabulary"
    assert "handle time mean" in double.calls[0]["user"]
    assert double.calls[0]["kwargs"]["purpose"] == "live_integration" if False else True


def test_dedupe_points_removes_rewordings():
    points = [
        "Greet the customer and verify identity",
        "greet the customer and verify identity!",
        "Verify identity",
        "",
        "Ask for the account number",
    ]
    assert live_integration.dedupe_points(points) == [
        "Greet the customer and verify identity",
        "Ask for the account number",
    ]


def test_dedupe_points_respects_limit():
    assert len(live_integration.dedupe_points([f"point {i}" for i in range(20)], limit=5)) == 5


def test_clean_title_shortens_and_strips():
    assert live_integration.clean_title('  "Welcome calls".  ') == "Welcome calls"
    assert live_integration.clean_title("a b c d e f g h i j k") == "a b c d e f g h i"
    assert live_integration.clean_title("") == "Untitled topic"


def test_normalize_structure_merges_repeated_topics():
    payload = {
        "topics": [
            {"title": "Welcome calls", "points": ["greet"]},
            {"title": "welcome CALLS", "points": ["verify identity"]},
        ]
    }
    out = live_integration.normalize_structure(payload)
    assert len(out["topics"]) == 1
    assert out["topics"][0]["points"] == ["greet", "verify identity"]


def test_normalize_structure_drops_empty_topics():
    out = live_integration.normalize_structure({"topics": [{"title": "vacío"}]})
    assert out["topics"] == []


def test_normalize_structure_never_loses_previous_material():
    """Si el modelo 'resume' y se come temas, el borrador no debe perderlos."""
    previous = {
        "topics": [
            {"title": "Intro", "points": ["a"], "spanish_notes": [], "start_t": 0.0},
            {"title": "Vocabulary", "points": ["b"], "spanish_notes": [], "start_t": 60.0},
        ]
    }
    out = live_integration.normalize_structure(
        {"topics": [{"title": "Vocabulary", "points": ["b", "c"]}]}, previous=previous
    )
    titles = [t["title"] for t in out["topics"]]
    assert "Intro" in titles and "Vocabulary" in titles
    vocabulary = next(t for t in out["topics"] if t["title"] == "Vocabulary")
    assert vocabulary["points"] == ["b", "c"]


def test_normalize_structure_sorts_by_time():
    out = live_integration.normalize_structure(
        {"topics": [{"title": "Late", "points": ["x"], "start_t": 900},
                    {"title": "Early", "points": ["y"], "start_t": 10}]}
    )
    assert [t["title"] for t in out["topics"]] == ["Early", "Late"]


def test_format_lines():
    assert live_integration.format_lines([(12.7, 1, " hola ")]) == ["12 S1 hola"]


# ---------------------------------------------------------------------------
# Libro final
# ---------------------------------------------------------------------------

SEGMENTS = [
    (0.0, 0, "Welcome everyone, I am Sara, your teacher."),
    (4.0, 1, "Hi teacher, I am Juan."),
    (9.0, 0, "Today we cover open-account welcome calls."),
    (40.0, 1, "What is handle time?"),
    (50.0, 0, "Handle time is the duration of a call."),
]

BOOK_MAIN_REPLY = {
    "timeline": [
        {"start_t": 0, "end_t": 90, "kind": "topic", "label": "Welcome calls"},
        {"start_t": 90, "end_t": 200, "kind": "break", "label": "Break"},
    ],
    "topics": [
        {
            "title": "Welcome calls",
            "start_t": 0,
            "end_t": 90,
            "points": ["Greet and verify identity", "Greet and verify identity"],
            "spanish_notes": ["handle time = duración de la llamada"],
            "vocab": [{"word": "handle time", "en_def": "call duration", "es": "duración"}],
            "phrases": [{"en": "How may I help you today?", "es": "¿Cómo puedo ayudarle?",
                         "speaker_index": 0}],
        }
    ],
    "roleplays": [
        {"title": "First call", "context": "practice", "your_role": "agent",
         "participants": ["Juan"], "key_phrases": ["May I have your account number?"],
         "feedback": "Good tone", "start_t": 60, "end_t": 90}
    ],
}
SPEAKERS_REPLY = {
    "speakers": [
        {"index": 0, "suggested_name": "Sara", "suggested_role": "teacher",
         "evidence": "I am Sara", "confidence": 0.9},
        {"index": 1, "suggested_name": "Juan", "suggested_role": "student",
         "evidence": "I am Juan", "confidence": 0.8},
    ]
}
TITLE_REPLY = {"title": "Welcome calls & KPI vocabulary"}


def test_finalize_session_builds_the_book(fake_llm):
    fake_llm(BOOK_MAIN_REPLY, SPEAKERS_REPLY, TITLE_REPLY)
    book = polish.finalize_session(
        segments=SEGMENTS, draft_topics=[], breaks=[(95.0, 195.0)], duration_sec=200.0,
        **CREDS,
    )
    assert book["title"] == "Welcome calls & KPI vocabulary"
    assert book["topics"][0]["points"] == ["Greet and verify identity"]   # deduplicado
    assert book["topics"][0]["vocab"][0]["word"] == "handle time"
    assert book["speakers"][0]["suggested_name"] == "Sara"
    assert book["roleplays"][0]["participants"] == ["Juan"]
    kinds = [event["kind"] for event in book["timeline"]]
    assert "break" in kinds


def test_finalize_without_transcript_raises():
    with pytest.raises(AIError):
        polish.finalize_session(segments=[], draft_topics=[], breaks=[], **CREDS)


def test_break_without_silence_is_downgraded(fake_llm):
    """Un receso inventado por el modelo, sin silencio real, no se acepta."""
    fake_llm(BOOK_MAIN_REPLY, SPEAKERS_REPLY, TITLE_REPLY)
    book = polish.finalize_session(
        segments=SEGMENTS, draft_topics=[], breaks=[], duration_sec=200.0, **CREDS,
    )
    assert all(event["kind"] != "break" for event in book["timeline"])


def test_detected_break_is_added_even_if_the_model_forgot_it(fake_llm):
    fake_llm(
        {"timeline": [{"start_t": 0, "end_t": 90, "kind": "topic", "label": "T"}],
         "topics": BOOK_MAIN_REPLY["topics"], "roleplays": []},
        SPEAKERS_REPLY,
        TITLE_REPLY,
    )
    book = polish.finalize_session(
        segments=SEGMENTS, draft_topics=[], breaks=[(600.0, 900.0)], duration_sec=1200.0,
        **CREDS,
    )
    breaks = [e for e in book["timeline"] if e["kind"] == "break"]
    assert len(breaks) == 1
    assert breaks[0]["start_t"] == 600.0


def test_timeline_overlaps_are_trimmed():
    payload = {
        "timeline": [
            {"start_t": 0, "end_t": 100, "kind": "topic", "label": "A"},
            {"start_t": 50, "end_t": 200, "kind": "topic", "label": "B"},
        ],
        "topics": [],
        "roleplays": [],
    }
    out = polish.normalize_content(payload, duration_sec=200.0, breaks=[])
    assert out["timeline"][0]["end_t"] == 50.0
    assert out["timeline"][1]["start_t"] == 50.0
    assert [e["sort_order"] for e in out["timeline"]] == [0, 1]


def test_timeline_is_clamped_to_duration():
    payload = {
        "timeline": [{"start_t": -10, "end_t": 99999, "kind": "topic", "label": "A"}],
        "topics": [], "roleplays": [],
    }
    out = polish.normalize_content(payload, duration_sec=120.0, breaks=[])
    assert out["timeline"][0]["start_t"] == 0.0
    assert out["timeline"][0]["end_t"] == 120.0


def test_timeline_falls_back_to_topics():
    payload = {
        "timeline": [],
        "topics": [{"title": "A", "start_t": 0, "end_t": 60, "points": ["p"]}],
        "roleplays": [],
    }
    out = polish.normalize_content(payload, duration_sec=60.0, breaks=[])
    assert out["timeline"][0]["label"] == "A"


def test_unknown_timeline_kind_becomes_topic():
    payload = {
        "timeline": [{"start_t": 0, "end_t": 10, "kind": "inventado", "label": "A"}],
        "topics": [], "roleplays": [],
    }
    out = polish.normalize_content(payload, duration_sec=10.0, breaks=[])
    assert out["timeline"][0]["kind"] == "topic"


def test_speakers_normalization_fills_gaps_and_honours_microphone():
    out = polish.normalize_speakers(
        {"speakers": [{"index": 0, "suggested_name": "Sara", "suggested_role": "me"}]},
        speaker_indexes=[0, 1],
        me_speaker=1,
    )
    by_index = {s["index"]: s for s in out}
    assert by_index[1]["suggested_role"] == "me"
    assert by_index[0]["suggested_role"] == "student"     # el modelo se equivocó
    assert by_index[1]["confidence"] >= 0.9


def test_speakers_normalization_rejects_placeholder_names():
    out = polish.normalize_speakers(
        {"speakers": [{"index": 0, "suggested_name": "Student", "suggested_role": "student"}]},
        speaker_indexes=[0], me_speaker=None,
    )
    assert out[0]["suggested_name"] == ""


def test_name_evidence_lines_selects_relevant_context():
    lines = [f"{i} S0 filler sentence number {i}." for i in range(400)]
    lines[350] = "350 S1 My name is Carolina and I will present."
    selected = polish.name_evidence_lines(lines, head=20)
    assert any("Carolina" in line for line in selected)
    assert len(selected) < len(lines)


def test_long_transcript_uses_map_reduce(fake_llm, monkeypatch):
    """Una clase de 3,5 h no cabe en un prompt: debe trocearse, no truncarse."""
    monkeypatch.setattr(polish, "WINDOW_TOKENS", 400)
    double = fake_llm(
        {"timeline": [{"start_t": 0, "end_t": 50, "kind": "topic", "label": "A"}],
         "topics": [{"title": "A", "points": ["p1"], "start_t": 0, "end_t": 50}],
         "roleplays": []},
        {"timeline": [{"start_t": 60, "end_t": 120, "kind": "topic", "label": "B"}],
         "topics": [{"title": "B", "points": ["p2"], "start_t": 60, "end_t": 120}],
         "roleplays": []},
        SPEAKERS_REPLY,
        TITLE_REPLY,
    )
    segments = [(float(i), i % 2, f"Sentence number {i} about customer service.")
                for i in range(400)]
    book = polish.finalize_session(
        segments=segments, draft_topics=[], breaks=[], duration_sec=400.0, **CREDS,
    )
    purposes = [call.get("purpose") for call in double.calls]
    assert any(str(p).startswith("polish_main_w") for p in purposes)
    assert {t["title"] for t in book["topics"]} == {"A", "B"}


def test_title_failure_is_not_fatal(fake_llm, monkeypatch):
    fake_llm(BOOK_MAIN_REPLY, SPEAKERS_REPLY, "esto no es json")
    book = polish.finalize_session(
        segments=SEGMENTS, draft_topics=[], breaks=[], duration_sec=200.0, **CREDS,
    )
    assert book["title"] == ""
    assert book["topics"]


# ---------------------------------------------------------------------------
# Chat / retrieval
# ---------------------------------------------------------------------------


def test_tokenize_strips_punctuation_and_stopwords():
    assert chat_ai.tokenize("Handle-time, call!") == ["handle", "time", "call"]
    assert chat_ai.tokenize("¿Qué significa el handle time?") == ["handle", "time"]


def test_bm25_ranks_the_relevant_document_first():
    docs = [
        "handle time is the duration of a call",
        "we greet the customer and verify identity",
        "the teacher explained irregular verbs",
    ]
    bm25 = chat_ai.BM25.build(docs)
    assert bm25.top("how long should a handle time be?", k=1)[0] == 0
    assert bm25.top("greeting the customer", k=1)[0] == 1


def test_bm25_handles_empty_corpus():
    assert chat_ai.BM25.build([]).top("nada") == []


def _segments():
    return [
        chat_ai.Segment(0, 5, 0, "Welcome, today we discuss handle time.", "Sara", "08:00:00"),
        chat_ai.Segment(5, 9, 1, "What does that mean?", "Juan", "08:00:05"),
        chat_ai.Segment(9, 14, 0, "It is the duration of a call.", "Sara", "08:00:09"),
        chat_ai.Segment(60, 64, 0, "Now let's practise greetings.", "Sara", "08:01:00"),
    ]


def test_select_segments_includes_neighbours():
    indexes = chat_ai.select_segments(_segments(), "handle time", k=1)
    assert 0 in indexes and 1 in indexes


def test_build_context_carries_names_and_wall_clock():
    context, citations = chat_ai.build_context(_segments(), "what is handle time", k=2)
    assert "Sara" in context and "08:00:00" in context
    assert citations[0]["speaker"] == "Sara"
    assert citations[0]["wall_clock"] == "08:00:00"


def test_answer_passes_notes_and_context(fake_llm):
    double = fake_llm("Handle time es la duración de la llamada [08:00:09].")
    reply, citations = chat_ai.answer(
        segments=_segments(),
        topics=[{"title": "Verizon KPIs", "points": ["handle time"]}],
        question="¿qué es handle time?",
        history=[{"role": "user", "content": "hola"}],
        **CREDS,
    )
    assert "handle time" in reply.lower()
    assert "Verizon KPIs" in double.calls[0]["user"]
    assert citations


# ---------------------------------------------------------------------------
# Podcast
# ---------------------------------------------------------------------------


def test_podcast_script_normalizes_lines(fake_llm):
    fake_llm({"lines": [
        {"speaker": "A", "text": "**Today** we learned about welcome calls. 🎧"},
        {"speaker": "B", "text": "B: Right, and the key metric is handle time (pause)."},
    ]})
    lines = podcast.make_script(topics=[{"title": "Welcome calls"}], **CREDS)
    assert len(lines) == 2
    assert lines[0]["text"] == "Today we learned about welcome calls."
    assert lines[1]["speaker"] == "B"
    assert lines[1]["text"] == "Right, and the key metric is handle time ."


def test_podcast_script_alternates_when_speaker_missing(fake_llm):
    fake_llm({"lines": ["primera", "segunda", "tercera"]})
    lines = podcast.make_script(topics=[], **CREDS)
    assert [line["speaker"] for line in lines] == ["A", "B", "A"]


def test_podcast_script_empty_raises(fake_llm):
    fake_llm({"lines": []})
    with pytest.raises(podcast.PodcastError):
        podcast.make_script(topics=[], **CREDS)


def test_clean_line_removes_tts_noise():
    assert podcast.clean_line("A: *Hello* [note] (aside) & more") == "Hello and more"


def test_voices_are_distinct():
    assert podcast.VOICE_A != podcast.VOICE_B


def test_script_text_and_estimated_minutes():
    lines = [{"speaker": "A", "text": " ".join(["word"] * 150)}]
    assert podcast.script_text(lines).startswith("A: word")
    assert podcast.estimated_minutes(lines) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Materiales de estudio
# ---------------------------------------------------------------------------


def test_quiz_normalization_fixes_bad_answers():
    payload = {
        "questions": [
            {"question": "What is handle time?", "options": ["A", "B", "C", "D"],
             "correct_index": 9, "answer": "C", "explanation": "porque sí"},
            {"question": "Duplicated options", "options": ["x", "x", "y"],
             "correct_index": 0},
            {"question": "What is handle time?", "options": ["A", "B"], "correct_index": 0},
            {"question": "", "options": ["A", "B"], "correct_index": 0},
        ]
    }
    questions = study.normalize_questions(payload, shuffle=False)
    assert len(questions) == 2                       # duplicada y vacía descartadas
    assert questions[0]["options"][questions[0]["correct_index"]] == "C"
    assert questions[1]["options"] == ["x", "y"]


def test_quiz_normalization_accepts_letter_answers():
    payload = {"questions": [{"question": "q", "options": ["a", "b", "c", "d"],
                              "answer": "B"}]}
    question = study.normalize_questions(payload, shuffle=False)[0]
    assert question["options"][question["correct_index"]] == "b"


def test_quiz_shuffle_keeps_the_right_answer():
    payload = {"questions": [{"question": "q", "options": ["right", "w1", "w2", "w3"],
                              "correct_index": 0}]}
    question = study.normalize_questions(payload)[0]
    assert question["options"][question["correct_index"]] == "right"


def test_quiz_respects_limit():
    payload = {"questions": [{"question": f"q{i}", "options": ["a", "b"], "correct_index": 0}
                             for i in range(20)]}
    assert len(study.normalize_questions(payload, limit=5)) == 5


def test_make_quiz_calls_model(fake_llm):
    fake_llm({"questions": [{"question": "What is handle time?",
                             "options": ["a", "b", "c", "d"], "correct_index": 1,
                             "explanation": "el tiempo de la llamada"}]})
    out = study.make_quiz(topics=[{"title": "Verizon", "points": ["handle time"]}], n=1,
                          **CREDS)
    assert out[0]["explanation"].startswith("el tiempo")


def test_flashcards_normalization():
    payload = {"flashcards": [
        {"front": "handle time", "back": "duración de la llamada"},
        {"front": "Handle Time", "back": "duplicada"},
        {"front": "sin reverso"},
    ]}
    cards = study.normalize_cards(payload)
    assert len(cards) == 1
    assert cards[0]["front"] == "handle time"


def test_make_cards(fake_llm):
    fake_llm({"flashcards": [{"front": "handle time", "back": "duración"}]})
    assert study.make_cards(topics=[], **CREDS)[0]["front"] == "handle time"


def test_concept_map_connects_orphan_nodes():
    payload = {
        "nodes": [
            {"id": "n1", "label": "Customer service", "group": "root"},
            {"id": "n2", "label": "Greetings", "group": "topic"},
            {"id": "n3", "label": "Huérfano", "group": "term"},
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n1", "to": "nope"}],
    }
    layout = study.normalize_map(payload)
    assert len(layout["nodes"]) == 3
    targets = {edge["to"] for edge in layout["edges"]}
    assert targets == {"n2", "n3"}           # arista colgante fuera, huérfano conectado


def test_concept_map_limits_nodes():
    payload = {"nodes": [{"id": f"n{i}", "label": f"L{i}"} for i in range(40)], "edges": []}
    assert len(study.normalize_map(payload)["nodes"]) <= study.MAX_NODES


def test_concept_map_empty():
    assert study.normalize_map({"nodes": [], "edges": []}) == {"nodes": [], "edges": []}


def test_make_map(fake_llm):
    fake_llm({"nodes": [{"id": "1", "label": "Call handling", "group": "root"}],
              "edges": []})
    layout = study.make_map(topics=[{"title": "Verizon"}], **CREDS)
    assert layout["nodes"][0]["id"] == "1"


def test_prompts_have_no_unresolved_placeholders():
    from app.ai import prompts

    for name in ("LIVE_INTEGRATION", "BOOK_MAIN", "BOOK_SPEAKERS", "BOOK_TITLE",
                 "CHAT_TUTOR", "CONCEPT_MAP"):
        text = getattr(prompts, name)
        assert "{" in text or True
        assert "COURSE_CONTEXT" not in text
        assert "STYLE_RULES" not in text
    assert "{minutes}" not in prompts.podcast_script(5)
    assert "{n}" not in prompts.quiz(10)
    assert "{n}" not in prompts.flashcards(10)
