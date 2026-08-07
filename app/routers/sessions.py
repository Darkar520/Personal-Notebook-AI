"""CRUD de cuadernos y control de la captura.

Nota de orden de rutas: `/pending-recording` se declara **antes** de `/{sid}`. Con el
orden inverso (el del plan) FastAPI intenta convertir "pending-recording" en `int` y
responde 422 en vez de atender el endpoint.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app import config as app_config
from app import db, pipeline
from app.errors import NotebookError, SessionStateError
from app.routers._common import get_session_or_404
from app.schemas import (
    SessionCreate,
    SessionOut,
    SessionPatch,
    StartRequest,
    StopRequest,
    row_to_session,
)
from app.transcription import queue as stt_queue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _decorate(row) -> SessionOut:
    """Añade los contadores que la lista de cuadernos necesita mostrar."""
    session_id = int(row["id"])
    with db.read() as conn:
        counts = conn.execute(
            "SELECT SUM(status='final') AS final_n, SUM(status='draft') AS draft_n"
            " FROM topics WHERE session_id=?",
            (session_id,),
        ).fetchone()
        segments = conn.execute(
            "SELECT COUNT(*) AS n FROM transcript_segments WHERE session_id=?",
            (session_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM session_speakers"
            " WHERE session_id=? AND confirmed=0 AND (suggested_name<>'' OR talk_seconds>5)",
            (session_id,),
        ).fetchone()
    from app.audio import session_audio

    final_n = int((counts["final_n"] if counts else 0) or 0)
    draft_n = int((counts["draft_n"] if counts else 0) or 0)
    return row_to_session(
        row,
        topics_count=final_n or draft_n,
        segments_count=int(segments["n"] if segments else 0),
        speakers_pending=bool(pending["n"] if pending else 0),
        has_audio=session_audio.session_mp3_path(session_id) is not None,
    )


# ---------------------------------------------------------------------------
# Estado global y recuperación (antes de las rutas con parámetro)
# ---------------------------------------------------------------------------


@router.get("/status")
def capture_status():
    """Estado de la captura para el gadget."""
    return pipeline.active_status()


@router.get("/pending-recording", response_model=list[SessionOut])
def pending_recording():
    """Sesiones que quedaron 'recording' tras un cierre inesperado."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status='recording' ORDER BY id DESC"
        ).fetchall()
    return [_decorate(row) for row in rows if int(row["id"]) not in pipeline.ACTIVE]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[SessionOut])
def list_sessions(limit: int = 200, offset: int = 0):
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY COALESCE(started_at, created_at) DESC, id DESC"
            " LIMIT ? OFFSET ?",
            (max(1, min(500, limit)), max(0, offset)),
        ).fetchall()
    return [_decorate(row) for row in rows]


@router.post("", status_code=201, response_model=SessionOut)
def create_session(body: SessionCreate):
    """Crea un cuaderno vacío (sin grabar). Para grabar se usa `/start`."""
    session_id = pipeline.create_session(
        title=body.title, account_tag=body.account_tag, status="empty"
    )
    return _decorate(get_session_or_404(session_id))


@router.post("/start", response_model=SessionOut)
def start(body: StartRequest):
    """Crea la sesión **y arranca la captura de audio**."""
    try:
        session_id = pipeline.start_session(
            capture_mode=body.capture_mode,
            title=body.title,
            account_tag=body.account_tag,
            source_wav=body.source_wav,
            realtime=body.realtime,
        )
    except SessionStateError as exc:
        raise HTTPException(409, exc.user_message) from exc
    except NotebookError as exc:
        raise HTTPException(503, exc.user_message) from exc
    return _decorate(get_session_or_404(session_id))


@router.get("/{sid}", response_model=SessionOut)
def get_session(sid: int):
    return _decorate(get_session_or_404(sid))


@router.patch("/{sid}", response_model=SessionOut)
def patch_session(sid: int, body: SessionPatch):
    get_session_or_404(sid)
    updates: list[str] = []
    values: list[object] = []
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "El título no puede estar vacío")
        # Renombrar a mano bloquea el título: el pase final ya no lo sobrescribe.
        updates += ["title=?", "title_locked=1"]
        values.append(title)
    if body.account_tag is not None:
        updates.append("account_tag=?")
        values.append(body.account_tag.strip() or None)
    if body.status is not None:
        updates.append("status=?")
        values.append(body.status)
    if updates:
        updates.append("updated_at=?")
        values.extend([db.now_iso(), sid])
        with db.write() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE id=?", values)
    return _decorate(get_session_or_404(sid))


@router.delete("/{sid}", status_code=204)
def delete_session(sid: int):
    """Borrado total: filas, audio, huellas y ajustes de la sesión."""
    get_session_or_404(sid)
    pipeline.delete_session(sid)


# ---------------------------------------------------------------------------
# Captura
# ---------------------------------------------------------------------------


@router.post("/{sid}/stop", response_model=SessionOut)
def stop(sid: int, body: StopRequest | None = None):
    options = body or StopRequest()
    get_session_or_404(sid)
    try:
        pipeline.stop_session(sid, discard=options.discard, finalize=options.finalize)
    except KeyError as exc:
        raise HTTPException(404, "Cuaderno no encontrado") from exc
    if options.discard:
        return SessionOut(id=sid, status="deleted")
    return _decorate(get_session_or_404(sid))


@router.post("/{sid}/finalize-recording", response_model=SessionOut)
def finalize_recording(sid: int):
    """Cierra una sesión huérfana tras un cierre inesperado."""
    get_session_or_404(sid)
    try:
        pipeline.finalize_recording(sid)
    except SessionStateError as exc:
        raise HTTPException(409, exc.user_message) from exc
    return _decorate(get_session_or_404(sid))


@router.post("/{sid}/discard-recording", status_code=204)
def discard_recording(sid: int):
    get_session_or_404(sid)
    pipeline.delete_session(sid)


@router.post("/{sid}/repolish", response_model=SessionOut)
def repolish(sid: int):
    """Regenera el libro (útil tras corregir la llave o cambiar de modelo)."""
    row = get_session_or_404(sid)
    if int(row["id"]) in pipeline.ACTIVE:
        raise HTTPException(409, "La sesión está grabando todavía")
    pipeline.repolish(sid, app_config.load_config())
    return _decorate(get_session_or_404(sid))


@router.post("/{sid}/retry-transcription")
def retry_transcription(sid: int):
    """Reactiva los chunks fallidos (por ejemplo, tras recuperar la conexión)."""
    get_session_or_404(sid)
    reactivated = stt_queue.retry_failed(session_id=sid)
    stt_queue.reset_retries(sid)
    return {"reactivated": reactivated, "queue": stt_queue.counts(sid)}


@router.get("/{sid}/queue")
def queue_state(sid: int):
    get_session_or_404(sid)
    return stt_queue.counts(sid)
