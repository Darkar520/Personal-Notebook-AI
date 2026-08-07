"""Materiales de estudio: quiz, flashcards y mapa conceptual (Fase 9).

El plan los repartía en tres módulos (`quiz.py`, `flashcards.py`, `concept_map.py`) con
~15 líneas cada uno: tres ficheros que hacen exactamente lo mismo (serializar temas →
llamar al LLM → leer una clave del JSON). Se unifican aquí porque comparten el saneado y
la validación, que es la parte con sustancia:

- **Quiz**: el plan devolvía `q["correct_index"]` sin comprobar nada. Un modelo que
  devuelve 3 opciones, o `correct_index: 4`, o la respuesta correcta duplicada, genera un
  quiz imposible de responder. Aquí se validan cardinalidad, rango, duplicados y se
  **rota la posición de la respuesta correcta** (los LLM tienden a poner la buena en A/B).
- **Flashcards**: se descartan tarjetas sin reverso o duplicadas por anverso.
- **Mapa conceptual**: se garantiza que el grafo sea conexo y sin aristas colgantes; si no,
  el SVG sale con nodos flotando sueltos.
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any

from app.ai import jsonx, live_integration, opencode_client, prompts

log = logging.getLogger(__name__)

MAX_QUESTIONS = 30
MAX_CARDS = 60
MAX_NODES = 18


def _topics_payload(topics: list[dict[str, Any]], *, with_phrases: bool = True
                    ) -> list[dict[str, Any]]:
    payload = []
    for topic in topics:
        item: dict[str, Any] = {
            "title": topic.get("title"),
            "points": list(topic.get("points") or [])[:8],
            "spanish_notes": list(topic.get("spanish_notes") or [])[:3],
            "vocab": [
                {"word": v.get("word"), "en_def": v.get("en_def"), "es": v.get("es")}
                for v in (topic.get("vocab") or [])[:12]
            ],
        }
        if with_phrases:
            item["phrases"] = [p.get("en") for p in (topic.get("phrases") or [])[:6]]
        payload.append(item)
    return payload


def _call(system: str, payload: dict[str, Any], *, creds: dict[str, str],
          temperature: float, purpose: str, session_id: int | None,
          pricing: dict[str, Any] | None) -> dict[str, Any]:
    return opencode_client.chat_json(
        system,
        opencode_client.fit_text(json.dumps(payload, ensure_ascii=False), max_tokens=20_000),
        temperature=temperature,
        session_id=session_id,
        purpose=purpose,
        pricing=pricing,
        **creds,
    )


# ---------------------------------------------------------------------------
# Quiz
# ---------------------------------------------------------------------------


def normalize_questions(payload: Any, *, limit: int = MAX_QUESTIONS,
                        shuffle: bool = True) -> list[dict[str, Any]]:
    items = jsonx.pick_list(payload, "questions", "quiz")
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    rng = random.Random(20260804)
    for item in items:
        if not isinstance(item, dict):
            continue
        text = jsonx.as_str(item.get("question") or item.get("prompt"))
        options = [jsonx.as_str(o) for o in jsonx.pick_list(item.get("options") or [], "options")]
        options = [o for o in options if o]
        if not text or len(options) < 2:
            continue
        raw_index = item.get("correct_index", item.get("answer_index"))
        correct = jsonx.as_int(raw_index, -1) if raw_index is not None else -1
        if not 0 <= correct < len(options):
            # Algunos modelos devuelven la letra ("B") o el texto de la respuesta en
            # vez del índice, o un índice fuera de rango.
            answer = jsonx.as_str(item.get("answer") or item.get("correct"))
            if answer and answer in options:
                correct = options.index(answer)
            elif answer[:1].upper() in "ABCD":
                correct = "ABCD".index(answer[:1].upper())
            else:
                correct = 0
            correct = min(max(0, correct), len(options) - 1)

        # Opciones duplicadas: se eliminan manteniendo la correcta.
        unique: list[str] = []
        correct_text = options[correct]
        for option in options:
            if option not in unique:
                unique.append(option)
        if len(unique) < 2:
            continue
        options = unique[:4]
        if correct_text not in options:
            options[-1] = correct_text
        if shuffle:
            rng.shuffle(options)
        correct = options.index(correct_text)

        key = live_integration._normalize(text)
        if key in seen:
            continue
        seen.add(key)
        questions.append(
            {
                "question": text,
                "options": options,
                "correct_index": correct,
                "explanation": jsonx.as_str(item.get("explanation")),
                "topic_title": jsonx.as_str(item.get("topic_title") or item.get("topic")),
            }
        )
        if len(questions) >= limit:
            break
    return questions


def make_quiz(
    *,
    topics: list[dict[str, Any]],
    n: int = 10,
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    payload = {"n_questions": int(n), "topics": _topics_payload(topics)}
    response = _call(
        prompts.quiz(n), payload,
        creds={"model": model, "base_url": base_url, "api_key": api_key},
        temperature=0.5, purpose="quiz", session_id=session_id, pricing=pricing,
    )
    return normalize_questions(response, limit=int(n))


# ---------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------


def normalize_cards(payload: Any, *, limit: int = MAX_CARDS) -> list[dict[str, str]]:
    items = jsonx.pick_list(payload, "flashcards", "cards")
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        front = jsonx.as_str(item.get("front") or item.get("term") or item.get("question"))
        back = jsonx.as_str(
            item.get("back") or item.get("back_md") or item.get("definition")
            or item.get("answer")
        )
        if not front or not back:
            continue
        key = live_integration._normalize(front)
        if key in seen:
            continue
        seen.add(key)
        cards.append({"front": front[:160], "back": back[:600]})
        if len(cards) >= limit:
            break
    return cards


def make_cards(
    *,
    topics: list[dict[str, Any]],
    n: int = 20,
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    payload = {"n_cards": int(n), "topics": _topics_payload(topics)}
    response = _call(
        prompts.flashcards(n), payload,
        creds={"model": model, "base_url": base_url, "api_key": api_key},
        temperature=0.4, purpose="flashcards", session_id=session_id, pricing=pricing,
    )
    return normalize_cards(response, limit=int(n) + 10)


# ---------------------------------------------------------------------------
# Mapa conceptual
# ---------------------------------------------------------------------------


def normalize_map(payload: Any) -> dict[str, list[dict[str, Any]]]:
    raw_nodes = jsonx.pick_list(payload, "nodes")
    raw_edges = jsonx.pick_list(payload, "edges", "links")
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_nodes):
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label = jsonx.as_str(item.get("label") or item.get("name") or item.get("title"))
        if not label:
            continue
        node_id = jsonx.as_str(item.get("id")) or f"n{index + 1}"
        if node_id in by_id:
            continue
        group = jsonx.as_str(item.get("group") or item.get("kind"), "term").lower()
        if group not in ("root", "topic", "term"):
            group = "root" if not nodes else "term"
        node = {"id": node_id, "label": label[:60], "group": group}
        by_id[node_id] = node
        nodes.append(node)
        if len(nodes) >= MAX_NODES:
            break

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source = jsonx.as_str(item.get("from") or item.get("source"))
        target = jsonx.as_str(item.get("to") or item.get("target"))
        if source not in by_id or target not in by_id or source == target:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"from": source, "to": target, "label": jsonx.as_str(item.get("label"))})

    if not nodes:
        return {"nodes": [], "edges": []}

    # Garantizamos un grafo conexo: todo nodo huérfano cuelga de la raíz.
    root = next((n for n in nodes if n["group"] == "root"), nodes[0])
    root["group"] = "root"
    connected = {root["id"]}
    for edge in edges:
        connected.add(edge["from"])
        connected.add(edge["to"])
    for node in nodes:
        if node["id"] not in connected:
            edges.append({"from": root["id"], "to": node["id"], "label": ""})
    return {"nodes": nodes, "edges": edges}


def make_map(
    *,
    topics: list[dict[str, Any]],
    session_title: str = "",
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    payload = {
        "session_title": session_title,
        "topics": _topics_payload(topics, with_phrases=False),
    }
    response = _call(
        prompts.CONCEPT_MAP, payload,
        creds={"model": model, "base_url": base_url, "api_key": api_key},
        temperature=0.3, purpose="concept_map", session_id=session_id, pricing=pricing,
    )
    return normalize_map(response)
