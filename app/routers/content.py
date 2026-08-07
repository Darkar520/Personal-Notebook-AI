"""Contenido del cuaderno: notas, línea de tiempo, transcripción, búsqueda y consumo."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app import db, pipeline
from app.routers._common import get_session_or_404, session_clock, speaker_names
from app.schemas import (
    DraftTopicsIn,
    RoleplayOut,
    SegmentOut,
    TimelineEventOut,
    TopicOut,
    TopicPatch,
    UsageOut,
    row_to_roleplay,
    row_to_segment,
    row_to_timeline,
    row_to_topic,
    topic_summary_json,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions/{sid}", tags=["content"])


# ---------------------------------------------------------------------------
# Temas
# ---------------------------------------------------------------------------


@router.get("/topics", response_model=list[TopicOut])
def list_topics(sid: int, status: str | None = None):
    """Temas del cuaderno. Sin `status`, devuelve los finales o el borrador si no hay."""
    row = get_session_or_404(sid)
    started_at, tz_offset = session_clock(row)
    with db.read() as conn:
        if status in ("draft", "final"):
            rows = conn.execute(
                "SELECT * FROM topics WHERE session_id=? AND status=? ORDER BY sort_order",
                (sid, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM topics WHERE session_id=? AND status='final'"
                " ORDER BY sort_order",
                (sid,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM topics WHERE session_id=? AND status='draft'"
                    " ORDER BY sort_order",
                    (sid,),
                ).fetchall()
    return [row_to_topic(r, started_at, tz_offset) for r in rows]


@router.put("/topics/draft", response_model=list[TopicOut])
def replace_draft(sid: int, body: DraftTopicsIn):
    """Sustituye el borrador completo (lo usa la integración en vivo y los tests)."""
    get_session_or_404(sid)
    pipeline.save_draft_topics(
        sid,
        [
            {"title": t.title, "points": t.points, "spanish_notes": t.spanish_notes}
            for t in body.topics
        ],
    )
    return list_topics(sid, status="draft")


@router.patch("/topics/{topic_id}", response_model=TopicOut)
def patch_topic(sid: int, topic_id: int, body: TopicPatch):
    """Edición manual. Marca `user_edited` para que el pase final no la sobrescriba."""
    row = get_session_or_404(sid)
    with db.read() as conn:
        topic = conn.execute(
            "SELECT * FROM topics WHERE id=? AND session_id=?", (topic_id, sid)
        ).fetchone()
    if topic is None:
        raise HTTPException(404, "Tema no encontrado")

    updates: list[str] = []
    values: list[Any] = []
    if body.points is not None or body.spanish_notes is not None:
        current = db.json_loads(topic["summary_md"], {})
        points = body.points if body.points is not None else current.get("points", [])
        notes = (
            body.spanish_notes
            if body.spanish_notes is not None
            else current.get("spanish_notes", [])
        )
        updates.append("summary_md=?")
        values.append(topic_summary_json(list(points), list(notes)))
    if body.title is not None:
        updates.append("title=?")
        values.append(body.title.strip())
    if body.vocab is not None:
        updates.append("vocab_json=?")
        values.append(db.json_dumps(body.vocab))
    if body.phrases is not None:
        updates.append("phrases_json=?")
        values.append(db.json_dumps(body.phrases))
    if body.mastered is not None:
        updates.append("mastered=?")
        values.append(1 if body.mastered else 0)
    if not updates:
        return row_to_topic(topic, *session_clock(row))

    # Marcar "mastered" no es editar contenido: no debe bloquear el pase final.
    if any(field for field in updates if not field.startswith("mastered")):
        updates.append("user_edited=1")
    updates.append("updated_at=?")
    values.extend([db.now_iso(), topic_id])
    with db.write() as conn:
        conn.execute(f"UPDATE topics SET {', '.join(updates)} WHERE id=?", values)
        updated = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    return row_to_topic(updated, *session_clock(row))


@router.delete("/topics/{topic_id}", status_code=204)
def delete_topic(sid: int, topic_id: int):
    get_session_or_404(sid)
    with db.write() as conn:
        cursor = conn.execute(
            "DELETE FROM topics WHERE id=? AND session_id=?", (topic_id, sid)
        )
    if not cursor.rowcount:
        raise HTTPException(404, "Tema no encontrado")


# ---------------------------------------------------------------------------
# Línea de tiempo y roleplays
# ---------------------------------------------------------------------------


@router.get("/timeline", response_model=list[TimelineEventOut])
def timeline(sid: int):
    row = get_session_or_404(sid)
    started_at, tz_offset = session_clock(row)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM timeline_events WHERE session_id=? ORDER BY sort_order, start_t",
            (sid,),
        ).fetchall()
    return [row_to_timeline(r, started_at, tz_offset) for r in rows]


@router.get("/roleplays", response_model=list[RoleplayOut])
def roleplays(sid: int):
    get_session_or_404(sid)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM roleplays WHERE session_id=? ORDER BY COALESCE(start_t, id)",
            (sid,),
        ).fetchall()
    return [row_to_roleplay(r) for r in rows]


# ---------------------------------------------------------------------------
# Transcripción
# ---------------------------------------------------------------------------


@router.get("/transcript", response_model=list[SegmentOut])
def transcript(sid: int, after_id: int = 0, limit: int = Query(default=5000, le=20000)):
    """Turnos del transcript. `after_id` permite refrescar solo lo nuevo en vivo."""
    row = get_session_or_404(sid)
    started_at, tz_offset = session_clock(row)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, start_t, end_t, speaker_index, is_me, text, topic_id"
            " FROM transcript_segments WHERE session_id=? AND id>? ORDER BY start_t, id"
            " LIMIT ?",
            (sid, after_id, limit),
        ).fetchall()
    return [row_to_segment(r, started_at, tz_offset) for r in rows]


@router.get("/search")
def search_transcript(sid: int, q: str, limit: int = Query(default=50, le=200)):
    """Búsqueda en el transcript con FTS5 y respaldo `LIKE`."""
    row = get_session_or_404(sid)
    started_at, tz_offset = session_clock(row)
    term = q.strip()
    if not term:
        return []
    names = speaker_names(sid)
    rows: list[Any] = []
    with db.read() as conn:
        try:
            rows = conn.execute(
                "SELECT s.id, s.start_t, s.end_t, s.speaker_index, s.is_me, s.text"
                " FROM transcript_fts f JOIN transcript_segments s ON s.id = f.rowid"
                " WHERE f.text MATCH ? AND s.session_id=?"
                " ORDER BY bm25(transcript_fts) LIMIT ?",
                (_fts_query(term), sid, limit),
            ).fetchall()
        except Exception:  # sintaxis FTS inválida → respaldo literal
            log.debug("FTS falló para %r; uso LIKE", term, exc_info=True)
        if not rows:
            rows = conn.execute(
                "SELECT id, start_t, end_t, speaker_index, is_me, text"
                " FROM transcript_segments WHERE session_id=? AND text LIKE ?"
                " ORDER BY start_t LIMIT ?",
                (sid, f"%{term}%", limit),
            ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "start_t": float(r["start_t"]),
            "end_t": float(r["end_t"]),
            "speaker_index": int(r["speaker_index"]),
            "speaker": names.get(int(r["speaker_index"]), ""),
            "is_me": bool(r["is_me"]),
            "text": str(r["text"]),
            "wall_clock": db.wall_clock(started_at, float(r["start_t"]), tz_offset),
        }
        for r in rows
    ]


def _fts_query(term: str) -> str:
    """Convierte texto libre en una consulta FTS5 segura (prefijos, sin operadores)."""
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in term).split() if t]
    if not tokens:
        return '""'
    return " ".join(f'"{token}"*' for token in tokens)


# ---------------------------------------------------------------------------
# Consumo / costes
# ---------------------------------------------------------------------------


@router.get("/usage", response_model=UsageOut)
def usage(sid: int):
    get_session_or_404(sid)
    with db.read() as conn:
        totals = conn.execute(
            "SELECT COALESCE(SUM(minutes),0) AS minutes,"
            "       COALESCE(SUM(CASE WHEN kind='stt' THEN cost_usd ELSE 0 END),0) AS stt_cost,"
            "       COALESCE(SUM(CASE WHEN kind='llm' THEN cost_usd ELSE 0 END),0) AS llm_cost,"
            "       COALESCE(SUM(CASE WHEN kind='llm' THEN 1 ELSE 0 END),0) AS llm_calls,"
            "       COALESCE(SUM(tokens_in),0) AS tokens_in,"
            "       COALESCE(SUM(tokens_out),0) AS tokens_out"
            " FROM usage_events WHERE session_id=?",
            (sid,),
        ).fetchone()
        by_purpose = conn.execute(
            "SELECT purpose, kind, COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost,"
            "       COALESCE(SUM(minutes),0) AS minutes"
            " FROM usage_events WHERE session_id=? GROUP BY purpose, kind"
            " ORDER BY cost DESC",
            (sid,),
        ).fetchall()
    stt_cost = float(totals["stt_cost"] or 0.0)
    llm_cost = float(totals["llm_cost"] or 0.0)
    return UsageOut(
        stt_minutes=round(float(totals["minutes"] or 0.0), 2),
        stt_cost_usd=round(stt_cost, 4),
        llm_calls=int(totals["llm_calls"] or 0),
        tokens_in=int(totals["tokens_in"] or 0),
        tokens_out=int(totals["tokens_out"] or 0),
        llm_cost_usd=round(llm_cost, 4),
        total_usd=round(stt_cost + llm_cost, 4),
        by_purpose=[dict(r) for r in by_purpose],
    )
