"""Generación de materiales: podcast, quiz, flashcards y mapa conceptual.

Las funciones `generate_*` son **puras respecto a HTTP**: las usan tanto los endpoints
como la auto-generación al finalizar una sesión (`pipeline._auto_generate`). El plan
duplicaba esa lógica dentro de los handlers, así que la Fase 11 tenía que reimplementarla.

Extra sobre el plan: repetición espaciada real en las flashcards (cajas de Leitner con
`due_at`), que es lo que convierte "tarjetas bonitas" en una herramienta de estudio. El
plan solo guardaba `front`/`back`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException

from app import config as app_config
from app import db, paths, ws
from app.ai import opencode_client, podcast as podcast_ai, study
from app.errors import AIError, ConfigError, NotebookError
from app.routers._common import get_session_or_404, require_topics
from app.schemas import (
    ConceptMapOut,
    FlashcardOut,
    FlashcardReview,
    PodcastOut,
    QuizQuestionOut,
    QuizRequest,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions/{sid}", tags=["generate"])

# Intervalos de repaso por caja de Leitner (días).
LEITNER_DAYS = {1: 0, 2: 1, 3: 3, 4: 7, 5: 21}


def _creds(cfg: dict[str, Any], role: str) -> dict[str, str]:
    try:
        return opencode_client.credentials(cfg, role)
    except ConfigError as exc:
        raise HTTPException(400, exc.user_message) from exc


def _handle(exc: Exception) -> HTTPException:
    message = getattr(exc, "user_message", None) or str(exc)
    if isinstance(exc, ConfigError):
        return HTTPException(400, message)
    return HTTPException(502, message)


# ---------------------------------------------------------------------------
# Quiz
# ---------------------------------------------------------------------------


def generate_quiz(session_id: int, n: int = 10, cfg: dict[str, Any] | None = None
                  ) -> list[dict[str, Any]]:
    cfg = cfg or app_config.load_config()
    topics = require_topics(session_id)
    questions = study.make_quiz(
        topics=topics, n=n, session_id=session_id, pricing=cfg.get("pricing"),
        **opencode_client.credentials(cfg, "study"),
    )
    if not questions:
        raise AIError(
            "El modelo no devolvió preguntas",
            retryable=False,
            user_message="No se pudieron generar preguntas. Prueba otra vez.",
        )
    now = db.now_iso()
    with db.write() as conn:
        conn.execute("DELETE FROM quiz_questions WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO quiz_questions (session_id, question, options_json, correct_index,"
            " explanation, topic_title, created_at) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    session_id,
                    q["question"],
                    db.json_dumps(q["options"]),
                    int(q["correct_index"]),
                    q.get("explanation", ""),
                    q.get("topic_title", ""),
                    now,
                )
                for q in questions
            ],
        )
    ws.hub.publish({"type": "generated", "session_id": session_id, "artifacts": ["quiz"]})
    return questions


@router.post("/quiz", response_model=list[QuizQuestionOut])
def create_quiz(sid: int, body: QuizRequest | None = None):
    get_session_or_404(sid)
    try:
        generate_quiz(sid, (body or QuizRequest()).n)
    except NotebookError as exc:
        raise _handle(exc) from exc
    return list_quiz(sid)


@router.get("/quiz", response_model=list[QuizQuestionOut])
def list_quiz(sid: int):
    get_session_or_404(sid)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, question, options_json, correct_index, explanation, topic_title"
            " FROM quiz_questions WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
    return [
        QuizQuestionOut(
            id=int(r["id"]),
            question=r["question"],
            options=db.json_loads(r["options_json"], []),
            correct_index=int(r["correct_index"]),
            explanation=r["explanation"],
            topic_title=r["topic_title"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------


def generate_flashcards(session_id: int, n: int = 20, cfg: dict[str, Any] | None = None
                        ) -> list[dict[str, str]]:
    cfg = cfg or app_config.load_config()
    topics = require_topics(session_id)
    cards = study.make_cards(
        topics=topics, n=n, session_id=session_id, pricing=cfg.get("pricing"),
        **opencode_client.credentials(cfg, "study"),
    )
    if not cards:
        raise AIError(
            "El modelo no devolvió tarjetas",
            retryable=False,
            user_message="No se pudieron generar flashcards. Prueba otra vez.",
        )
    now = db.now_iso()
    with db.write() as conn:
        conn.execute("DELETE FROM flashcards WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO flashcards (session_id, front, back_md, box, due_at, created_at,"
            " updated_at) VALUES (?,?,?,1,?,?,?)",
            [(session_id, c["front"], c["back"], now, now, now) for c in cards],
        )
    ws.hub.publish(
        {"type": "generated", "session_id": session_id, "artifacts": ["flashcards"]}
    )
    return cards


@router.post("/flashcards", response_model=list[FlashcardOut])
def create_flashcards(sid: int, body: QuizRequest | None = None):
    get_session_or_404(sid)
    try:
        generate_flashcards(sid, (body or QuizRequest(n=20)).n)
    except NotebookError as exc:
        raise _handle(exc) from exc
    return list_flashcards(sid)


@router.get("/flashcards", response_model=list[FlashcardOut])
def list_flashcards(sid: int, due_only: bool = False):
    get_session_or_404(sid)
    sql = "SELECT id, front, back_md, box, due_at FROM flashcards WHERE session_id=?"
    args: list[Any] = [sid]
    if due_only:
        sql += " AND (due_at IS NULL OR due_at<=?)"
        args.append(db.now_iso())
    sql += " ORDER BY box, id"
    with db.read() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        FlashcardOut(
            id=int(r["id"]), front=r["front"], back_md=r["back_md"],
            box=int(r["box"] or 1), due_at=r["due_at"],
        )
        for r in rows
    ]


@router.post("/flashcards/{card_id}/review", response_model=FlashcardOut)
def review_flashcard(sid: int, card_id: int, body: FlashcardReview):
    """Repetición espaciada: acertar sube de caja, fallar vuelve a la primera."""
    get_session_or_404(sid)
    with db.write() as conn:
        row = conn.execute(
            "SELECT * FROM flashcards WHERE id=? AND session_id=?", (card_id, sid)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Tarjeta no encontrada")
        box = int(row["box"] or 1)
        box = min(5, box + 1) if body.correct else 1
        due = db.now_utc() + timedelta(days=LEITNER_DAYS.get(box, 0))
        conn.execute(
            "UPDATE flashcards SET box=?, due_at=?, reviews=reviews+1,"
            " lapses=lapses+?, updated_at=? WHERE id=?",
            (
                box,
                due.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                0 if body.correct else 1,
                db.now_iso(),
                card_id,
            ),
        )
        updated = conn.execute("SELECT * FROM flashcards WHERE id=?", (card_id,)).fetchone()
    return FlashcardOut(
        id=int(updated["id"]), front=updated["front"], back_md=updated["back_md"],
        box=int(updated["box"]), due_at=updated["due_at"],
    )


# ---------------------------------------------------------------------------
# Mapa conceptual
# ---------------------------------------------------------------------------


def generate_concept_map(session_id: int, cfg: dict[str, Any] | None = None
                         ) -> dict[str, Any]:
    cfg = cfg or app_config.load_config()
    topics = require_topics(session_id)
    row = get_session_or_404(session_id)
    layout = study.make_map(
        topics=topics, session_title=row["title"] or "", session_id=session_id,
        pricing=cfg.get("pricing"), **opencode_client.credentials(cfg, "study"),
    )
    if not layout.get("nodes"):
        raise AIError(
            "El modelo no devolvió nodos",
            retryable=False,
            user_message="No se pudo generar el mapa conceptual. Prueba otra vez.",
        )
    with db.write() as conn:
        conn.execute("DELETE FROM concept_maps WHERE session_id=?", (session_id,))
        conn.execute(
            "INSERT INTO concept_maps (session_id, layout_json, created_at) VALUES (?,?,?)",
            (session_id, db.json_dumps(layout), db.now_iso()),
        )
    ws.hub.publish(
        {"type": "generated", "session_id": session_id, "artifacts": ["concept_map"]}
    )
    return layout


@router.post("/concept-map", response_model=ConceptMapOut)
def create_concept_map(sid: int):
    get_session_or_404(sid)
    try:
        layout = generate_concept_map(sid)
    except NotebookError as exc:
        raise _handle(exc) from exc
    return ConceptMapOut(**layout)


@router.get("/concept-map", response_model=ConceptMapOut)
def get_concept_map(sid: int):
    get_session_or_404(sid)
    with db.read() as conn:
        row = conn.execute(
            "SELECT layout_json FROM concept_maps WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    if row is None:
        return ConceptMapOut()
    return ConceptMapOut(**db.json_loads(row["layout_json"], {"nodes": [], "edges": []}))


# ---------------------------------------------------------------------------
# Podcast
# ---------------------------------------------------------------------------


def generate_podcast(session_id: int, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or app_config.load_config()
    topics = require_topics(session_id)
    row = get_session_or_404(session_id)
    with db.read() as conn:
        roleplays = [
            dict(r)
            for r in conn.execute(
                "SELECT title, context_md FROM roleplays WHERE session_id=?", (session_id,)
            )
        ]
    result = podcast_ai.generate_podcast(
        topics=topics,
        roleplays=roleplays,
        session_title=row["title"] or "",
        minutes=int(cfg["settings"].get("podcast_minutes", 4)),
        out_dir=paths.session_dir(session_id) / "podcast",
        session_id=session_id,
        pricing=cfg.get("pricing"),
        **opencode_client.credentials(cfg, "podcast"),
    )
    relative = paths.rel_to_data(result["path"])
    with db.write() as conn:
        conn.execute("DELETE FROM audio_summaries WHERE session_id=?", (session_id,))
        conn.execute(
            "INSERT INTO audio_summaries (session_id, script, voice_a, voice_b, file_path,"
            " duration_sec, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                session_id,
                result["script"],
                result["voice_a"],
                result["voice_b"],
                relative,
                float(result["duration_sec"] or 0.0),
                db.now_iso(),
            ),
        )
    ws.hub.publish({"type": "generated", "session_id": session_id, "artifacts": ["podcast"]})
    return result


@router.post("/podcast", response_model=PodcastOut)
def create_podcast(sid: int):
    get_session_or_404(sid)
    try:
        generate_podcast(sid)
    except NotebookError as exc:
        raise _handle(exc) from exc
    existing = get_podcast(sid)
    if existing is None:
        raise HTTPException(500, "El podcast se generó pero no se pudo guardar")
    return existing


@router.get("/podcast", response_model=PodcastOut | None)
def get_podcast(sid: int):
    get_session_or_404(sid)
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM audio_summaries WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    if row is None:
        return None
    return PodcastOut(
        id=int(row["id"]),
        script=row["script"],
        voice_a=row["voice_a"],
        voice_b=row["voice_b"],
        audio_url=f"/api/sessions/{sid}/media/podcast",
        duration_sec=row["duration_sec"],
        created_at=row["created_at"],
    )
