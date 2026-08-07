"""Servido de audio con soporte de HTTP Range.

Por qué importa: la SPA reproduce **un solo** `session.mp3` y salta al segundo exacto de
un tema, un receso o una frase del transcript (`audio.currentTime = start`). Para que el
navegador pueda saltar sin descargar 75 MB antes, el servidor tiene que responder
`206 Partial Content` a las peticiones con cabecera `Range`. Sin esto, "ir a la línea de
tiempo" tarda una eternidad la primera vez y no funciona hacia atrás.

Se implementa a mano (y no se delega en `FileResponse`) para garantizar el comportamiento
con independencia de la versión de Starlette instalada, y para validar el rango: un
`Range` malformado debe dar 416, no un fichero entero de 75 MB.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import FileResponse, Response, StreamingResponse

from app import db, paths
from app.audio import session_audio
from app.routers._common import get_session_or_404

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions/{sid}/media", tags=["media"])

CHUNK = 256 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _iter_file(path: Path, start: int, length: int):
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def ranged_response(path: Path, request: Request, media_type: str = "audio/mpeg") -> Response:
    """Devuelve el fichero completo (200) o el tramo pedido (206)."""
    size = path.stat().st_size
    header = request.headers.get("range") or request.headers.get("Range")
    common = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": f'inline; filename="{path.name}"',
    }
    if not header:
        return FileResponse(str(path), media_type=media_type, headers=common)

    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        raise HTTPException(416, "Rango no soportado")
    raw_start, raw_end = match.groups()
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    elif raw_end:                     # sufijo: bytes=-N (últimos N bytes)
        start = max(0, size - int(raw_end))
        end = size - 1
    else:
        raise HTTPException(416, "Rango vacío")
    if start >= size or start > end:
        return Response(
            status_code=416, headers={**common, "Content-Range": f"bytes */{size}"}
        )
    end = min(end, size - 1)
    length = end - start + 1
    return StreamingResponse(
        _iter_file(path, start, length),
        status_code=206,
        media_type=media_type,
        headers={
            **common,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


@router.get("/session")
def session_audio_file(sid: int, request: Request):
    """Audio completo de la clase (MP3 mono 48 kbps)."""
    get_session_or_404(sid)
    path = session_audio.session_mp3_path(sid)
    if path is None:
        raise HTTPException(404, "Esta sesión no tiene audio generado")
    return ranged_response(path, request)


@router.get("/podcast")
def podcast_file(sid: int, request: Request):
    get_session_or_404(sid)
    with db.read() as conn:
        row = conn.execute(
            "SELECT file_path FROM audio_summaries WHERE session_id=?"
            " ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    if row is None or not row["file_path"]:
        raise HTTPException(404, "Todavía no hay podcast para esta sesión")
    try:
        path = paths.from_data(str(row["file_path"]))
    except ValueError as exc:  # pragma: no cover - defensa contra rutas manipuladas
        raise HTTPException(400, "Ruta de audio inválida") from exc
    if not path.exists():
        raise HTTPException(404, "El archivo del podcast ya no está en el disco")
    return ranged_response(path, request)


@router.get("/clip")
def clip_file(
    sid: int,
    request: Request,
    start: float = Query(ge=0),
    end: float = Query(gt=0),
):
    """Descarga un fragmento como MP3 independiente (compartir/estudiar sin la app)."""
    get_session_or_404(sid)
    if end <= start:
        raise HTTPException(422, "El final debe ser posterior al inicio")
    if end - start > 60 * 30:
        raise HTTPException(422, "El fragmento no puede superar los 30 minutos")
    try:
        path = session_audio.export_clip(sid, start, end)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Esta sesión no tiene audio generado") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("No se pudo extraer el fragmento")
        raise HTTPException(500, f"No se pudo extraer el fragmento: {exc}") from exc
    return ranged_response(path, request)
