"""Estructuración incremental de las notas mientras la clase ocurre (Fase 4).

Coste controlado: al modelo se le manda **solo** la estructura actual + el transcript
nuevo desde la última pasada, nunca el histórico completo. Con un intervalo de 5 minutos,
una clase de 3,5 h son ~42 llamadas pequeñas en vez de una que crece sin límite.

Mejoras sobre el plan original (Task 4.2):
- **Saneado del resultado.** El plan persistía tal cual lo que devolvía el modelo. Aquí se
  normaliza: se recortan títulos, se quitan puntos duplicados (comparando texto
  normalizado, que es donde el LLM repite), se limita el número de puntos por tema y se
  descartan temas vacíos. Sin esto, la regla de producto "sin duplicados" depende de la
  suerte de cada llamada.
- **Fusión defensiva.** Si el modelo devuelve menos temas de los que ya había (a veces
  "resume" y se come los primeros), se conservan los anteriores: la estructura en vivo
  solo puede crecer o refinarse, nunca perder material ya visto.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai import jsonx, opencode_client, prompts

log = logging.getLogger(__name__)

SYSTEM = prompts.LIVE_INTEGRATION
MAX_POINTS_PER_TOPIC = 8
MAX_NOTES_PER_TOPIC = 3
MAX_TITLE_WORDS = 9
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(text: str) -> str:
    """Clave de comparación para detectar el mismo punto escrito de otra forma."""
    lowered = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", lowered).strip()


def dedupe_points(points: list[str], *, limit: int = MAX_POINTS_PER_TOPIC) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for point in points:
        text = _WS_RE.sub(" ", str(point)).strip(" -•\t")
        if not text:
            continue
        key = _normalize(text)
        if not key or key in seen:
            continue
        # Un punto contenido en otro ya presente es redundante.
        if any(key in other or other in key for other in seen):
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def clean_title(title: str, fallback: str = "Untitled topic") -> str:
    text = _WS_RE.sub(" ", str(title or "")).strip(" .:-–—\"'")
    if not text:
        return fallback
    words = text.split()
    if len(words) > MAX_TITLE_WORDS:
        text = " ".join(words[:MAX_TITLE_WORDS])
    return text


def normalize_structure(payload: Any, previous: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
    """Convierte la respuesta del modelo en `{"topics": [...]}` limpio y ordenado."""
    raw_topics = jsonx.pick_list(payload, "topics", "outline", "sections")
    topics: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for item in raw_topics:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = clean_title(jsonx.as_str(item.get("title") or item.get("name")))
        points = dedupe_points(
            jsonx.as_str_list(item.get("points") or item.get("bullets") or item.get("key_points"))
        )
        notes = dedupe_points(
            jsonx.as_str_list(item.get("spanish_notes") or item.get("notes_es")),
            limit=MAX_NOTES_PER_TOPIC,
        )
        if not points and not notes:
            continue
        key = _normalize(title)
        if key in seen_titles:
            # Mismo tema repetido: fusionamos sus puntos en el que ya existe.
            for existing in topics:
                if _normalize(existing["title"]) == key:
                    existing["points"] = dedupe_points(existing["points"] + points)
                    existing["spanish_notes"] = dedupe_points(
                        existing["spanish_notes"] + notes, limit=MAX_NOTES_PER_TOPIC
                    )
                    break
            continue
        seen_titles.add(key)
        start = item.get("start_t", item.get("start"))
        topics.append(
            {
                "title": title,
                "start_t": jsonx.as_float(start, -1.0) if start is not None else -1.0,
                "points": points,
                "spanish_notes": notes,
            }
        )

    previous_topics = list((previous or {}).get("topics") or [])
    if previous_topics and len(topics) < len(previous_topics):
        merged = _keep_previous(previous_topics, topics)
        log.info(
            "El modelo devolvió %d temas y había %d: conservo los previos",
            len(topics), len(previous_topics),
        )
        topics = merged

    topics.sort(key=lambda t: (t["start_t"] if t["start_t"] >= 0 else 1e12))
    return {"topics": topics}


def _keep_previous(previous: list[dict[str, Any]], current: list[dict[str, Any]]
                   ) -> list[dict[str, Any]]:
    by_key = {_normalize(t["title"]): dict(t) for t in previous}
    for topic in current:
        key = _normalize(topic["title"])
        if key in by_key:
            base = by_key[key]
            base["points"] = dedupe_points(base.get("points", []) + topic["points"])
            base["spanish_notes"] = dedupe_points(
                base.get("spanish_notes", []) + topic["spanish_notes"],
                limit=MAX_NOTES_PER_TOPIC,
            )
            if topic["start_t"] >= 0 and base.get("start_t", -1) < 0:
                base["start_t"] = topic["start_t"]
        else:
            by_key[key] = topic
    return list(by_key.values())


def format_lines(segments: list[tuple[float, int, str]]) -> list[str]:
    """`(segundo, hablante, texto)` → `"<s> S<n> texto"` para el prompt."""
    return [
        f"{int(start)} S{speaker} {str(text).strip()}"
        for start, speaker, text in segments
        if str(text).strip()
    ]


def integrate(
    *,
    current: dict[str, Any],
    new_text: list[str],
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
    max_input_tokens: int = 24_000,
) -> dict[str, Any]:
    """Devuelve la estructura completa actualizada."""
    user = opencode_client.fit_text(
        json.dumps(
            {
                "current_structure": {"topics": (current or {}).get("topics", [])},
                "new_transcript": "\n".join(new_text),
            },
            ensure_ascii=False,
        ),
        max_tokens=max_input_tokens,
    )
    payload = opencode_client.chat_json(
        SYSTEM,
        user,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.2,
        session_id=session_id,
        purpose="live_integration",
        pricing=pricing,
    )
    return normalize_structure(payload, previous=current)
