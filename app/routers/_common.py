"""Utilidades compartidas por los routers."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException

from app import db


def get_session_or_404(session_id: int) -> sqlite3.Row:
    row = db.fetch_session(session_id)
    if row is None:
        raise HTTPException(404, "Cuaderno no encontrado")
    return row


def session_clock(row: sqlite3.Row) -> tuple[str | None, int]:
    """`(started_at, tz_offset_min)` para calcular horas de pared."""
    return row["started_at"], int(row["tz_offset_min"] or 0)


def final_topics(session_id: int) -> list[dict[str, Any]]:
    """Temas listos para alimentar a la IA (finales; si no hay, el borrador)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT title, summary_md, vocab_json, phrases_json, start_t, end_t FROM topics"
            " WHERE session_id=? AND status='final' ORDER BY sort_order",
            (session_id,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT title, summary_md, NULL AS vocab_json, NULL AS phrases_json,"
                " start_t, NULL AS end_t FROM topics"
                " WHERE session_id=? AND status='draft' ORDER BY sort_order",
                (session_id,),
            ).fetchall()
    topics: list[dict[str, Any]] = []
    for row in rows:
        summary = db.json_loads(row["summary_md"], {})
        topics.append(
            {
                "title": row["title"],
                "start_t": row["start_t"],
                "end_t": row["end_t"] if "end_t" in row.keys() else None,
                "points": list(summary.get("points", [])),
                "spanish_notes": list(summary.get("spanish_notes", [])),
                "vocab": db.json_loads(row["vocab_json"], []),
                "phrases": db.json_loads(row["phrases_json"], []),
            }
        )
    return topics


def require_topics(session_id: int) -> list[dict[str, Any]]:
    topics = final_topics(session_id)
    if not topics:
        raise HTTPException(
            409,
            "Este cuaderno todavía no tiene notas. Finaliza la sesión antes de generar "
            "materiales de estudio.",
        )
    return topics


def speaker_names(session_id: int) -> dict[int, str]:
    """`{índice de hablante: nombre a mostrar}` (confirmado > sugerido > genérico)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT s.speaker_index, s.suggested_name, s.is_me, p.name"
            " FROM session_speakers s LEFT JOIN people p ON p.id = s.person_id"
            " WHERE s.session_id=?",
            (session_id,),
        ).fetchall()
    names: dict[int, str] = {}
    for row in rows:
        index = int(row["speaker_index"])
        label = (row["name"] or row["suggested_name"] or "").strip()
        if not label:
            label = "Yo" if row["is_me"] else f"Speaker {index + 1}"
        names[index] = label
    return names
