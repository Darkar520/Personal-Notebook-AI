"""Pase final: convierte el transcript en el "libro" de la sesión (Fase 5).

Produce cuatro cosas en llamadas separadas (cada una con su propio prompt, así un fallo
parcial no arruina el resto): línea de tiempo + temas + roleplays, propuesta de nombres
de hablantes, y título de la sesión.

Mejoras sobre el plan original (Task 5.1):

1. **Map-reduce para clases largas.** El plan metía el transcript completo en un único
   prompt. Una clase de 3,5 h son ~27 000 palabras; con la estructura borrador y los
   recesos, el prompt se acerca o pasa el límite del modelo y el resultado se trunca
   *silenciosamente*: el libro se queda sin la última hora. Aquí, si el transcript no
   cabe en el presupuesto, se procesa por ventanas solapadas y se fusionan los resultados
   en Python (determinista, sin coste extra de tokens).
2. **Prompt de nombres selectivo.** Para adivinar quién es quién no hace falta el
   transcript completo: se envían la apertura de la clase y **todas** las líneas con
   indicios de nombre (presentaciones, vocativos, "teacher"). Sale más barato y acierta
   más que enviar 27 000 palabras donde el nombre aparece tres veces.
3. **Validación de la línea de tiempo.** El plan insertaba tal cual lo que devolvía el
   modelo. Aquí se ordena, se recorta a la duración real, se eliminan solapes y huecos
   absurdos y se garantiza que los recesos coincidan con silencio detectado localmente:
   la timeline es un dato verificable, no una alucinación.
4. **`me_speaker`**: se le dice al modelo qué hablante es el usuario (lo sabemos por la
   pista de micrófono) en vez de pedirle que lo intuya.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai import jsonx, live_integration, opencode_client, prompts
from app.capture import silence
from app.errors import AIError

log = logging.getLogger(__name__)

# A partir de aquí se trocea el transcript (deja margen para la respuesta del modelo).
WINDOW_TOKENS = 30_000
SINGLE_CALL_BUDGET = 45_000
WINDOW_OVERLAP_LINES = 12
MAX_TOPICS = 24
NAME_HINT_RE = re.compile(
    r"\b(i am|i'm|my name is|this is|call me|teacher|trainer|instructor|coach|"
    r"good morning|welcome|thank you|thanks|your turn|go ahead)\b",
    re.IGNORECASE,
)
VOCATIVE_RE = re.compile(r"\b[A-Z][a-z]{2,},|,\s*[A-Z][a-z]{2,}\b")


# ---------------------------------------------------------------------------
# Preparación de la entrada
# ---------------------------------------------------------------------------


def format_lines(segments: list[tuple[float, int, str]]) -> list[str]:
    return live_integration.format_lines(segments)


def _windows(lines: list[str], budget_tokens: int | None = None) -> list[list[str]]:
    """Trocea las líneas en ventanas con solape (para no cortar un tema en dos)."""
    if not lines:
        return []
    limit_chars = int((budget_tokens or WINDOW_TOKENS) * opencode_client.CHARS_PER_TOKEN)
    windows: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > limit_chars:
            windows.append(current)
            current = current[-WINDOW_OVERLAP_LINES:]
            size = sum(len(x) for x in current)
        current.append(line)
        size += len(line)
    if current:
        windows.append(current)
    return windows


def _line_seconds(line: str) -> float:
    head = line.split(" ", 1)[0]
    return jsonx.as_float(head, 0.0)


def name_evidence_lines(lines: list[str], *, head: int = 160, max_lines: int = 700
                        ) -> list[str]:
    """Apertura de la clase + líneas con pistas de nombres propios."""
    selected = list(lines[:head])
    seen = set(selected)
    for line in lines[head:]:
        body = line.split(" ", 2)[-1]
        if NAME_HINT_RE.search(body) or VOCATIVE_RE.search(body):
            if line not in seen:
                selected.append(line)
                seen.add(line)
        if len(selected) >= max_lines:
            break
    return selected


# ---------------------------------------------------------------------------
# Llamadas al modelo
# ---------------------------------------------------------------------------


def _call_main(
    lines: list[str],
    *,
    draft_topics: list[dict[str, Any]],
    breaks: list[tuple[float, float]],
    duration_sec: float,
    me_speaker: int | None,
    creds: dict[str, str],
    session_id: int | None,
    pricing: dict[str, Any] | None,
    purpose: str,
) -> dict[str, Any]:
    user = json.dumps(
        {
            "duration_sec": round(float(duration_sec), 1),
            "me_speaker": me_speaker,
            "draft_topics": draft_topics,
            "candidate_breaks": [{"start": a, "end": b} for a, b in breaks],
            "transcript": "\n".join(lines),
        },
        ensure_ascii=False,
    )
    return opencode_client.chat_json(
        prompts.BOOK_MAIN,
        opencode_client.fit_text(user, max_tokens=SINGLE_CALL_BUDGET),
        temperature=0.25,
        session_id=session_id,
        purpose=purpose,
        pricing=pricing,
        **creds,
    )


def build_content(
    lines: list[str],
    *,
    draft_topics: list[dict[str, Any]],
    breaks: list[tuple[float, float]],
    duration_sec: float,
    me_speaker: int | None,
    creds: dict[str, str],
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Temas + timeline + roleplays, en una o varias pasadas según el tamaño."""
    joined = "\n".join(lines)
    if opencode_client.estimate_tokens(joined) <= WINDOW_TOKENS:
        payload = _call_main(
            lines, draft_topics=draft_topics, breaks=breaks, duration_sec=duration_sec,
            me_speaker=me_speaker, creds=creds, session_id=session_id, pricing=pricing,
            purpose="polish_main",
        )
        return normalize_content(payload, duration_sec=duration_sec, breaks=breaks)

    chunks = _windows(lines)
    log.info("Transcript largo: pase final en %d ventanas", len(chunks))
    merged: dict[str, list[Any]] = {"timeline": [], "topics": [], "roleplays": []}
    for index, window in enumerate(chunks):
        start_t, end_t = _line_seconds(window[0]), _line_seconds(window[-1])
        window_breaks = [b for b in breaks if start_t <= b[0] <= end_t + 1]
        window_drafts = [
            t for t in draft_topics
            if jsonx.as_float(t.get("start_t"), -1) < 0
            or start_t - 60 <= jsonx.as_float(t.get("start_t"), 0) <= end_t + 60
        ]
        try:
            payload = _call_main(
                window, draft_topics=window_drafts, breaks=window_breaks,
                duration_sec=duration_sec, me_speaker=me_speaker, creds=creds,
                session_id=session_id, pricing=pricing,
                purpose=f"polish_main_w{index + 1}",
            )
        except AIError as exc:
            log.warning("Ventana %d del pase final falló: %s", index + 1, exc)
            continue
        merged["timeline"].extend(jsonx.pick_list(payload, "timeline"))
        merged["topics"].extend(jsonx.pick_list(payload, "topics"))
        merged["roleplays"].extend(jsonx.pick_list(payload, "roleplays"))
    if not merged["topics"]:
        raise AIError(
            "El pase final no produjo ningún tema",
            retryable=False,
            user_message=(
                "No se pudo estructurar la clase. Revisa la llave del modelo en Ajustes "
                "y vuelve a finalizar el cuaderno."
            ),
        )
    return normalize_content(merged, duration_sec=duration_sec, breaks=breaks)


def infer_speakers(
    lines: list[str],
    *,
    me_speaker: int | None,
    speaker_indexes: list[int],
    creds: dict[str, str],
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not lines or not speaker_indexes:
        return []
    user = json.dumps(
        {
            "me_speaker": me_speaker,
            "speaker_indexes": speaker_indexes,
            "transcript": "\n".join(name_evidence_lines(lines)),
        },
        ensure_ascii=False,
    )
    try:
        payload = opencode_client.chat_json(
            prompts.BOOK_SPEAKERS,
            opencode_client.fit_text(user, max_tokens=WINDOW_TOKENS),
            temperature=0.05,
            session_id=session_id,
            purpose="polish_speakers",
            pricing=pricing,
            **creds,
        )
    except AIError as exc:
        log.warning("No se pudo proponer nombres de hablantes: %s", exc)
        return []
    return normalize_speakers(payload, speaker_indexes=speaker_indexes, me_speaker=me_speaker)


def make_title(
    topics: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    *,
    creds: dict[str, str],
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> str:
    if not topics:
        return ""
    user = json.dumps(
        {
            "topics": [{"title": t.get("title"), "points": t.get("points", [])[:3]}
                       for t in topics[:12]],
            "timeline": [{"kind": e.get("kind"), "label": e.get("label")}
                         for e in timeline[:20]],
        },
        ensure_ascii=False,
    )
    try:
        payload = opencode_client.chat_json(
            prompts.BOOK_TITLE, user, temperature=0.3, session_id=session_id,
            purpose="polish_title", pricing=pricing, **creds,
        )
    except AIError as exc:
        log.warning("No se pudo generar el título: %s", exc)
        return ""
    return live_integration.clean_title(jsonx.as_str(payload.get("title")), fallback="")


# ---------------------------------------------------------------------------
# Normalización / validación
# ---------------------------------------------------------------------------

VALID_KINDS = {"topic", "break", "activity", "roleplay", "closing"}


def normalize_content(payload: Any, *, duration_sec: float,
                      breaks: list[tuple[float, float]]) -> dict[str, Any]:
    topics = _normalize_topics(jsonx.pick_list(payload, "topics"), duration_sec)
    timeline = _normalize_timeline(
        jsonx.pick_list(payload, "timeline"), duration_sec, breaks, topics
    )
    roleplays = _normalize_roleplays(jsonx.pick_list(payload, "roleplays"), duration_sec)
    return {"topics": topics, "timeline": timeline, "roleplays": roleplays}


def _clamp(value: float, duration_sec: float) -> float:
    top = duration_sec if duration_sec > 0 else max(value, 0.0)
    return round(min(max(0.0, value), top), 2)


def _normalize_topics(items: list[Any], duration_sec: float) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = live_integration.clean_title(jsonx.as_str(item.get("title")))
        points = live_integration.dedupe_points(jsonx.as_str_list(item.get("points")))
        notes = live_integration.dedupe_points(
            jsonx.as_str_list(item.get("spanish_notes")),
            limit=live_integration.MAX_NOTES_PER_TOPIC,
        )
        vocab = _normalize_vocab(jsonx.pick_list(item.get("vocab") or [], "vocab"))
        phrases = _normalize_phrases(jsonx.pick_list(item.get("phrases") or [], "phrases"))
        if not (points or notes or vocab or phrases):
            continue
        key = live_integration._normalize(title)
        if key in seen:
            for existing in topics:
                if live_integration._normalize(existing["title"]) == key:
                    existing["points"] = live_integration.dedupe_points(
                        existing["points"] + points
                    )
                    existing["vocab"] = _dedupe_by(existing["vocab"] + vocab, "word")
                    existing["phrases"] = _dedupe_by(existing["phrases"] + phrases, "en")
                    existing["end_t"] = max(
                        existing.get("end_t") or 0.0,
                        _clamp(jsonx.as_float(item.get("end_t")), duration_sec),
                    )
                    break
            continue
        seen.add(key)
        start = _clamp(jsonx.as_float(item.get("start_t")), duration_sec)
        end = _clamp(jsonx.as_float(item.get("end_t")), duration_sec)
        topics.append(
            {
                "title": title,
                "start_t": start,
                "end_t": max(end, start),
                "points": points,
                "spanish_notes": notes,
                "vocab": vocab,
                "phrases": phrases,
            }
        )
    topics.sort(key=lambda t: t["start_t"])
    return topics[:MAX_TOPICS]


def _dedupe_by(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        value = live_integration._normalize(jsonx.as_str(item.get(key)))
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(item)
    return out


def _normalize_vocab(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"word": item}
        if not isinstance(item, dict):
            continue
        word = jsonx.as_str(item.get("word") or item.get("term"))
        if not word:
            continue
        out.append(
            {
                "word": word,
                "en_def": jsonx.as_str(item.get("en_def") or item.get("definition")),
                "es": jsonx.as_str(item.get("es") or item.get("spanish")),
                "example_en": jsonx.as_str(item.get("example_en") or item.get("example")),
            }
        )
    return _dedupe_by(out, "word")[:30]


def _normalize_phrases(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"en": item}
        if not isinstance(item, dict):
            continue
        english = jsonx.as_str(item.get("en") or item.get("english") or item.get("text"))
        if not english:
            continue
        speaker = item.get("speaker_index", item.get("speaker"))
        out.append(
            {
                "en": english,
                "es": jsonx.as_str(item.get("es") or item.get("spanish")),
                "speaker_index": jsonx.as_int(speaker, -1) if speaker is not None else -1,
            }
        )
    return _dedupe_by(out, "en")[:24]


def _normalize_timeline(
    items: list[Any],
    duration_sec: float,
    breaks: list[tuple[float, float]],
    topics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = jsonx.as_str(item.get("kind"), "topic").lower()
        if kind not in VALID_KINDS:
            kind = "topic"
        start = _clamp(jsonx.as_float(item.get("start_t", item.get("start"))), duration_sec)
        end = _clamp(jsonx.as_float(item.get("end_t", item.get("end"))), duration_sec)
        label = jsonx.as_str(item.get("label") or item.get("title"))
        if end <= start:
            end = start
        events.append({"kind": kind, "start_t": start, "end_t": end, "label": label,
                       "note_md": jsonx.as_str(item.get("note_md") or item.get("note"))})

    # Un receso solo se acepta si hay silencio real que lo respalde.
    verified = silence.merge_ranges([tuple(b) for b in breaks], gap_s=30.0)
    filtered: list[dict[str, Any]] = []
    for event in events:
        if event["kind"] == "break":
            overlap = silence.overlap_fraction(event["start_t"], event["end_t"], verified)
            if overlap < 0.35:
                event["kind"] = "activity"
                event["note_md"] = (event["note_md"] or "") or None
        filtered.append(event)

    # Recesos detectados localmente que el modelo no incluyó: los añadimos.
    for start, end in verified:
        if end - start < 60:
            continue
        if any(
            e["kind"] == "break"
            and silence.overlap_fraction(start, end, [(e["start_t"], e["end_t"])]) > 0.3
            for e in filtered
        ):
            continue
        filtered.append(
            {
                "kind": "break",
                "start_t": _clamp(start, duration_sec),
                "end_t": _clamp(end, duration_sec),
                "label": f"Break ({int((end - start) / 60)} min)",
                "note_md": None,
            }
        )

    filtered.sort(key=lambda e: (e["start_t"], e["end_t"]))
    # Recorta solapes: el evento anterior cede el trozo compartido.
    cleaned: list[dict[str, Any]] = []
    for event in filtered:
        if cleaned and event["start_t"] < cleaned[-1]["end_t"]:
            cleaned[-1]["end_t"] = max(cleaned[-1]["start_t"], event["start_t"])
            if cleaned[-1]["end_t"] - cleaned[-1]["start_t"] < 1.0:
                cleaned.pop()
        if event["end_t"] - event["start_t"] < 1.0 and event["kind"] != "closing":
            continue
        cleaned.append(event)

    if not cleaned and topics:
        cleaned = [
            {
                "kind": "topic",
                "start_t": t["start_t"],
                "end_t": t["end_t"] or t["start_t"],
                "label": t["title"],
                "note_md": None,
            }
            for t in topics
        ]
    for index, event in enumerate(cleaned):
        event["sort_order"] = index
    return cleaned


def _normalize_roleplays(items: list[Any], duration_sec: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = jsonx.as_str(item.get("title"))
        context = jsonx.as_str(item.get("context") or item.get("context_md"))
        if not (title or context):
            continue
        out.append(
            {
                "title": title or "Roleplay",
                "context": context,
                "your_role": jsonx.as_str(item.get("your_role") or item.get("role")),
                "participants": jsonx.as_str_list(item.get("participants")),
                "key_phrases": jsonx.as_str_list(item.get("key_phrases")),
                "feedback": jsonx.as_str(item.get("feedback") or item.get("feedback_md")),
                "start_t": _clamp(jsonx.as_float(item.get("start_t")), duration_sec),
                "end_t": _clamp(jsonx.as_float(item.get("end_t")), duration_sec),
            }
        )
    return _dedupe_by(out, "title")[:12]


def normalize_speakers(
    payload: Any, *, speaker_indexes: list[int], me_speaker: int | None
) -> list[dict[str, Any]]:
    items = jsonx.pick_list(payload, "speakers")
    by_index: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        index = jsonx.as_int(item.get("index", item.get("speaker_index")), -1)
        if index not in speaker_indexes:
            continue
        role = jsonx.as_str(item.get("suggested_role") or item.get("role"), "other").lower()
        if role not in {"me", "teacher", "student", "other"}:
            role = "other"
        name = jsonx.as_str(item.get("suggested_name") or item.get("name"))
        if name.lower() in {"unknown", "speaker", "n/a", "none", "student", "teacher"}:
            name = ""
        by_index[index] = {
            "index": index,
            "suggested_name": name[:60],
            "suggested_role": role,
            "evidence": jsonx.as_str(item.get("evidence"))[:200],
            "confidence": max(0.0, min(1.0, jsonx.as_float(item.get("confidence"), 0.0))),
        }
    for index in speaker_indexes:
        by_index.setdefault(
            index,
            {"index": index, "suggested_name": "", "suggested_role": "other",
             "evidence": "", "confidence": 0.0},
        )
    if me_speaker is not None and me_speaker in by_index:
        by_index[me_speaker]["suggested_role"] = "me"
        by_index[me_speaker]["confidence"] = max(
            by_index[me_speaker]["confidence"], 0.9
        )
        by_index[me_speaker]["evidence"] = (
            by_index[me_speaker]["evidence"] or "Detectado por la pista de micrófono"
        )
    # Un solo "me": si el modelo marcó varios, gana el que sabemos por micrófono.
    me_candidates = [s for s in by_index.values() if s["suggested_role"] == "me"]
    if len(me_candidates) > 1:
        keeper = me_speaker if me_speaker is not None else max(
            me_candidates, key=lambda s: s["confidence"]
        )["index"]
        for speaker in me_candidates:
            if speaker["index"] != keeper:
                speaker["suggested_role"] = "student"
    return [by_index[i] for i in sorted(by_index)]


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------


def finalize_session(
    *,
    segments: list[tuple[float, int, str]],
    draft_topics: list[dict[str, Any]],
    breaks: list[tuple[float, float]],
    duration_sec: float = 0.0,
    me_speaker: int | None = None,
    speaker_indexes: list[int] | None = None,
    base_url: str,
    api_key: str,
    model: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Genera el libro completo de la sesión."""
    lines = format_lines(segments)
    if not lines:
        raise AIError(
            "Sesión sin transcript",
            retryable=False,
            user_message="Esta sesión no tiene transcripción, así que no hay nada que resumir.",
        )
    creds = {"model": model, "base_url": base_url, "api_key": api_key}
    if duration_sec <= 0 and segments:
        duration_sec = max(float(s[0]) for s in segments)
    indexes = speaker_indexes or sorted({int(s[1]) for s in segments})

    content = build_content(
        lines, draft_topics=draft_topics, breaks=breaks, duration_sec=duration_sec,
        me_speaker=me_speaker, creds=creds, session_id=session_id, pricing=pricing,
    )
    speakers = infer_speakers(
        lines, me_speaker=me_speaker, speaker_indexes=indexes, creds=creds,
        session_id=session_id, pricing=pricing,
    )
    title = make_title(
        content["topics"], content["timeline"], creds=creds, session_id=session_id,
        pricing=pricing,
    )
    return {
        "timeline": content["timeline"],
        "topics": content["topics"],
        "roleplays": content["roleplays"],
        "speakers": speakers,
        "title": title,
        "model": model,
    }
