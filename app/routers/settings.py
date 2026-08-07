"""Ajustes, prueba de conexión, dispositivos de audio y backup/restore.

Mejoras sobre el plan original (Task 10.1):

- **Las llaves nunca vuelven al navegador.** El plan devolvía el `cfg` completo (con
  `api_key` en claro) y añadía una máscara al lado. Aquí `public_config()` vacía el campo
  y solo expone `..._masked` y `..._set`.
- **Guardar sin borrar.** El plan interpretaba cualquier `api_key` ausente como "dejar
  igual" y `""` como "borrar", pero enviaba el objeto completo desde el formulario, así
  que abrir Ajustes y guardar cualquier preferencia **borraba las llaves**. Aquí el campo
  vacío significa "no tocar" y hay un endpoint explícito para borrar.
- **Prueba real de Deepgram** (consulta de proyectos y saldo), no `bool(api_key)`.
- **Comprobación de audio y de ffmpeg**, que son las dos causas más habituales de que la
  primera clase falle.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.responses import FileResponse

from app import backup, config as app_config, db, disk, paths
from app.ai import models, opencode_client, podcast as podcast_ai
from app.audio import ffmpeg
from app.capture import devices
from app.errors import NotebookError
from app.routers._common import get_session_or_404
from app.schemas import ConnectionReport, ProviderCheck, SettingsPatch
from app.transcription import deepgram_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])
system_router = APIRouter(prefix="/api", tags=["system"])

SECRET_FIELDS = app_config.SECRET_FIELDS


@router.get("")
def get_settings():
    """Configuración efectiva, sin secretos en claro."""
    return app_config.public_config()


@router.put("")
def put_settings(body: SettingsPatch):
    """Aplica cambios. Una llave vacía significa «no cambiar», nunca «borrar»."""
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items() if v}
    for section, field in SECRET_FIELDS:
        block = patch.get(section)
        if isinstance(block, dict) and not str(block.get(field, "")).strip():
            block.pop(field, None)
    if not patch:
        return app_config.public_config()
    app_config.update_config(patch)
    if "opencode" in patch:
        models.invalidate()
    log.info("Ajustes actualizados: %s", ", ".join(sorted(patch)))
    return app_config.public_config()


@router.delete("/keys/{provider}")
def clear_key(provider: str):
    """Borra explícitamente la llave de un proveedor."""
    valid = {section for section, _ in SECRET_FIELDS}
    if provider not in valid:
        raise HTTPException(404, f"Proveedor desconocido: {provider}")
    app_config.update_config({provider: {"api_key": ""}})
    models.invalidate()
    return app_config.public_config()


@router.post("/test", response_model=ConnectionReport)
def test_connection():
    """Prueba las cuatro dependencias externas y devuelve un diagnóstico por cada una."""
    cfg = app_config.load_config()
    report = ConnectionReport()

    llm = opencode_client.ping(cfg)
    report.opencode = ProviderCheck(
        ok=bool(llm.get("ok")), detail=str(llm.get("detail", "")),
        extra=llm.get("extra", {}) or {},
    )

    backend = str(cfg["settings"].get("stt_backend", "deepgram"))
    if backend == "deepgram":
        stt = deepgram_client.check_credentials(cfg["deepgram"].get("api_key", ""))
        report.deepgram = ProviderCheck(
            ok=bool(stt.get("ok")), detail=str(stt.get("detail", "")),
            extra=stt.get("extra", {}) or {},
        )
    else:
        report.deepgram = ProviderCheck(
            ok=True, detail=f"backend alternativo: {backend}", extra={"backend": backend}
        )

    report.tts = _check_tts()
    report.audio = _check_audio(cfg)
    return report


def _check_tts() -> ProviderCheck:
    if not ffmpeg.available():
        return ProviderCheck(ok=False, detail="Falta ffmpeg (pip install imageio-ffmpeg)")
    try:
        voices = asyncio.run(podcast_ai.list_voices())
    except Exception as exc:  # noqa: BLE001 - edge-tts necesita red
        return ProviderCheck(ok=False, detail=f"edge-tts no disponible: {str(exc)[:120]}")
    return ProviderCheck(
        ok=bool(voices), detail=f"{len(voices)} voces en inglés",
        extra={"voices": voices[:20]},
    )


def _check_audio(cfg: dict[str, Any]) -> ProviderCheck:
    inventory = devices.list_devices()
    if inventory.get("error"):
        return ProviderCheck(ok=False, detail=str(inventory["error"][0].get("detail", "")))
    loopbacks = inventory.get("loopback", [])
    inputs = inventory.get("input", [])
    if not loopbacks:
        return ProviderCheck(
            ok=False,
            detail="Windows no expone ningún dispositivo de bucle (¿altavoces activos?)",
            extra=inventory,
        )
    mode = str(cfg["settings"].get("capture_mode", "loopback"))
    if "mic" in mode and not inputs:
        return ProviderCheck(
            ok=False,
            detail="El modo incluye micrófono pero no hay ninguno disponible",
            extra=inventory,
        )
    free_mb = disk.free_space_mb()
    return ProviderCheck(
        ok=True,
        detail=f"{len(loopbacks)} salidas capturables, {len(inputs)} micrófonos",
        extra={
            **inventory,
            "free_mb": free_mb,
            "recording_minutes_left": disk.remaining_recording_minutes(free_mb),
        },
    )


@router.get("/devices")
def audio_devices():
    return devices.list_devices()


@router.get("/models")
def available_models(refresh: bool = False):
    """Catálogo real del proveedor + qué modelo se usará para cada rol."""
    cfg = app_config.load_config()
    catalog = models.catalog(
        cfg["opencode"]["base_url"], cfg["opencode"]["api_key"], force=refresh
    )
    roles = {
        role: models.resolve(role, cfg)
        for role in ("live", "polish", "chat", "podcast", "study")
    }
    return {"catalog": catalog, "resolved": roles}


@router.get("/voices")
def tts_voices(prefix: str = "en-US"):
    try:
        return {"voices": asyncio.run(podcast_ai.list_voices(prefix))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"edge-tts no disponible: {exc}") from exc


# ---------------------------------------------------------------------------
# Estado del sistema y avisos de primera ejecución
# ---------------------------------------------------------------------------


@system_router.get("/system")
def system_status():
    from app import __version__, runtime
    from app.transcription import queue as stt_queue

    cfg = app_config.load_config()
    free_mb = disk.free_space_mb()
    return {
        "version": __version__,
        "schema_version": db.SCHEMA_VERSION,
        "data_dir": str(paths.data_dir()),
        "supervisor": runtime.supervisor.running,
        "queue": stt_queue.counts(),
        "free_mb": free_mb,
        "recording_minutes_left": disk.remaining_recording_minutes(free_mb),
        "keys": {
            "opencode": bool(cfg["opencode"]["api_key"]),
            "deepgram": bool(cfg["deepgram"]["api_key"]),
        },
        "onboarding_done": bool(cfg["settings"].get("onboarding_done")),
        "legal_notice_seen": bool(cfg["settings"].get("legal_notice_seen")),
        "ffmpeg": ffmpeg.available(),
        "audio_capture": devices.available(),
    }


@system_router.post("/system/acknowledge")
def acknowledge(kind: str):
    """Marca como visto el aviso legal o la guía de primera ejecución."""
    field = {"legal": "legal_notice_seen", "onboarding": "onboarding_done"}.get(kind)
    if not field:
        raise HTTPException(422, "kind debe ser 'legal' u 'onboarding'")
    app_config.update_config({"settings": {field: True}})
    return {"ok": True, field: True}


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------


@system_router.get("/sessions/{sid}/export")
def export_session(sid: int):
    row = get_session_or_404(sid)
    folder = paths.data_dir() / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in str(row["title"] or "cuaderno")
    ).strip()[:60]
    target = folder / f"{row['session_number']:03d}-{safe_title or 'cuaderno'}.zip"
    try:
        backup.export_session(sid, target)
    except NotebookError as exc:
        raise HTTPException(400, exc.user_message) from exc
    return FileResponse(
        str(target), media_type="application/zip", filename=target.name,
        headers={"Cache-Control": "no-store"},
    )


@system_router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(422, "Sube un archivo .zip exportado por la app")
    tmp_dir = Path(tempfile.mkdtemp(prefix="notebook-restore-"))
    tmp_zip = tmp_dir / "backup.zip"
    try:
        with tmp_zip.open("wb") as handle:
            while block := await file.read(1024 * 1024):
                handle.write(block)
        session_id = backup.restore_session(tmp_zip)
    except NotebookError as exc:
        raise HTTPException(400, exc.user_message) from exc
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"session_id": session_id}
