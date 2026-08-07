"""Chatbot del cuaderno (Fase 8).

Diferencias con el plan (Task 8.2):
- El historial se lee y se escribe en la base (`messages`), no lo manda el navegador: así
  la conversación sobrevive a recargar la página o abrirla en otra pestaña.
- El contexto lleva **nombres reales y horas de pared**, y la respuesta devuelve las
  **citas** con su instante para poder saltar al audio.
- Los errores del proveedor se traducen a mensajes accionables en vez de un 502 opaco.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app import config as app_config
from app import db
from app.ai import chat as chat_ai
from app.ai import opencode_client
from app.errors import AIError, ConfigError
from app.routers._common import final_topics, get_session_or_404, session_clock, speaker_names
from app.schemas import ChatIn, ChatOut, MessageOut

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions/{sid}", tags=["chat"])

HISTORY_TURNS = 6


def _segments(session_id: int) -> list[chat_ai.Segment]:
    row = get_session_or_404(session_id)
    started_at, tz_offset = session_clock(row)
    names = speaker_names(session_id)
    return [
        chat_ai.Segment(
            start_t=float(r["start_t"]),
            end_t=float(r["end_t"]),
            speaker_index=int(r["speaker_index"]),
            text=str(r["text"]),
            speaker_name=names.get(int(r["speaker_index"]), ""),
            wall_clock=db.wall_clock(started_at, float(r["start_t"]), tz_offset),
        )
        for r in db.fetch_segments(session_id)
    ]


@router.get("/messages", response_model=list[MessageOut])
def list_messages(sid: int, limit: int = 200):
    get_session_or_404(sid)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, role, content, meta_json, created_at FROM messages"
            " WHERE session_id=? ORDER BY id LIMIT ?",
            (sid, max(1, min(1000, limit))),
        ).fetchall()
    return [
        MessageOut(
            id=int(r["id"]),
            role=r["role"],
            content=r["content"],
            created_at=r["created_at"],
            meta=db.json_loads(r["meta_json"], {}),
        )
        for r in rows
    ]


@router.delete("/messages", status_code=204)
def clear_messages(sid: int):
    get_session_or_404(sid)
    with db.write() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))


@router.post("/chat", response_model=ChatOut)
def chat(sid: int, body: ChatIn):
    row = get_session_or_404(sid)
    cfg = app_config.load_config()
    if body.reset:
        clear_messages(sid)

    segments = _segments(sid)
    if not segments:
        raise HTTPException(
            409,
            "Este cuaderno todavía no tiene transcripción, así que no hay nada sobre lo "
            "que conversar.",
        )

    with db.read() as conn:
        history_rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=?"
            " ORDER BY id DESC LIMIT ?",
            (sid, HISTORY_TURNS * 2),
        ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(history_rows)]

    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (sid, "user", body.message, now),
        )

    try:
        creds = opencode_client.credentials(cfg, "chat")
        reply, citations = chat_ai.answer(
            segments=segments,
            topics=final_topics(sid),
            question=body.message,
            history=history,
            session_meta={
                "title": row["title"],
                "date": (row["started_at"] or "")[:10],
                "duration_min": round(float(row["duration_sec"] or 0) / 60, 1),
                "account": row["account_tag"],
            },
            session_id=sid,
            pricing=cfg.get("pricing"),
            **creds,
        )
    except ConfigError as exc:
        raise HTTPException(400, exc.user_message) from exc
    except AIError as exc:
        raise HTTPException(502, exc.user_message) from exc

    with db.write() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, meta_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (sid, "assistant", reply, db.json_dumps({"citations": citations}), db.now_iso()),
        )
    return ChatOut(reply=reply, citations=citations, model=creds["model"])
