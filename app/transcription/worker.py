"""Worker de transcripción: consume la cola y escribe `transcript_segments`.

Responsabilidades por chunk:
1. Llamar al motor STT configurado (Deepgram / Whisper local / Gemini).
2. Pasar los tiempos del fichero a segundos absolutos de sesión y quitar el solape.
3. Mantener estable la identidad de los hablantes (`SpeakerRegistry` + huella vocal).
4. Marcar `is_me` con la máscara del micrófono.
5. Insertar de forma **idempotente** (borra antes lo que hubiera de ese chunk), acumular
   recesos detectados, registrar el consumo y emitir el evento en vivo.

Notas sobre el plan original (Task 3.3):
- El plan derivaba el offset del chunk parseando el nombre del fichero con
  `Path(...).stem.split("_")[1]` y `__import__("pathlib")` en línea. Aquí el offset viene
  en la fila de la cola, que es la única fuente de verdad.
- El plan emitía el evento con `asyncio.ensure_future(...)` desde un hilo sin loop (no
  hace nada) y luego con `asyncio.run(...)` (crea otro loop y no alcanza a los WebSocket
  de uvicorn). Aquí se usa `ws.hub.publish`, seguro entre hilos.
- El plan rompía el bucle al primer error no reintentable; eso dejaba parada la cola de
  toda la sesión. Aquí un chunk envenenado se marca `failed` y el resto sigue.
"""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app import config as app_config
from app import db, ws
from app.capture import silence, wavio
from app.errors import ConfigError, ProviderError, STTError
from app.transcription import deepgram_client, fallback, fusion, queue, voiceprint
from app.transcription.deepgram_client import SttResult

log = logging.getLogger(__name__)

BREAKS_SETTING = "breaks_"
SPEAKER_COLORS = [
    "#4f46e5", "#0891b2", "#c2410c", "#15803d",
    "#a21caf", "#b45309", "#0f766e", "#be123c",
]


# ---------------------------------------------------------------------------
# Motores
# ---------------------------------------------------------------------------


def transcribe(row: sqlite3.Row | dict[str, Any], cfg: dict[str, Any]) -> SttResult:
    """Despacha al backend STT configurado."""
    path = queue.absolute_path(row)
    backend = str(cfg["settings"].get("stt_backend", "deepgram")).lower()
    if backend == "whisper":
        return fallback.transcribe_whisper(path, language=cfg["deepgram"].get("language", "en"))
    if backend == "gemini":
        return fallback.transcribe_gemini(path, api_key=cfg["gemini"].get("api_key", ""))
    return deepgram_client.transcribe_file(
        path,
        api_key=cfg["deepgram"].get("api_key", ""),
        language=cfg["deepgram"].get("language", "en"),
        model=cfg["deepgram"].get("model", "nova-3"),
        timeout=float(cfg["deepgram"].get("timeout_sec", 180)),
        max_retries=int(cfg["deepgram"].get("max_retries", 3)),
    )


# ---------------------------------------------------------------------------
# Procesado de un chunk
# ---------------------------------------------------------------------------


def process_row(row: sqlite3.Row | dict[str, Any], cfg: dict[str, Any]) -> int:
    """Transcribe e integra un chunk. Devuelve el nº de segmentos insertados."""
    data = dict(row)
    session_id = int(data["session_id"])
    chunk_index = int(data.get("chunk_index", 0) or 0)
    start_t = float(data.get("start_t", 0.0) or 0.0)
    overlap_pre = float(data.get("overlap_pre", 0.0) or 0.0)
    duration = float(data.get("duration", 0.0) or 0.0)
    file_start_t = start_t - overlap_pre
    path = queue.absolute_path(row)

    result = transcribe(row, cfg)
    meta = queue.read_meta(path)

    # 1. Tiempos absolutos y eliminación del solape.
    absolute = fusion.to_absolute(result.utterances, file_start_t)
    kept = fusion.dedupe_overlap(
        absolute,
        chunk_start_t=start_t,
        chunk_end_t=(start_t + duration) if duration > 0 else None,
        is_first=(chunk_index == 0 or overlap_pre <= 0),
        is_last=bool(meta.get("final")),
    )

    # 2. Identidad de hablantes por huella vocal (solo con los turnos que conservamos).
    mapping = _resolve_speakers(session_id, path, result, kept, file_start_t)
    for utterance in kept:
        local = int(utterance.get("speaker", 0) or 0)
        utterance["speaker_global"] = mapping.get(local, local)

    # 3. Máscara del micrófono → `is_me`.
    fusion.mark_is_me(kept, [tuple(r) for r in meta.get("mic_ranges", [])])

    inserted = _store_segments(session_id, chunk_index, kept)
    _accumulate_breaks(session_id, meta.get("silences", []))
    _record_usage(session_id, result, cfg)

    if kept:
        ws.emit_segments(
            session_id,
            [
                {
                    "start_t": u["start"],
                    "end_t": u["end"],
                    "speaker_index": u["speaker_global"],
                    "is_me": bool(u.get("is_me")),
                    "text": u["text"],
                }
                for u in kept
            ],
        )
    log.info(
        "Chunk %s de la sesión %s: %d segmentos (%.1f s de audio, %s)",
        chunk_index, session_id, inserted, result.duration, result.provider,
    )
    return inserted


def _resolve_speakers(
    session_id: int,
    path,
    result: SttResult,
    kept: list[dict[str, Any]],
    file_start_t: float,
) -> dict[int, int]:
    """Correspondencia local→global usando la huella vocal del propio chunk."""
    registry = fusion.SpeakerRegistry(session_id)
    local_embeddings: dict[int, tuple[Any, float]] = {}
    try:
        samples, sr = wavio.read_wav(path)
        # Las utterances originales son relativas al fichero.
        relative = [
            {**u, "start": u["start"] - file_start_t, "end": u["end"] - file_start_t}
            for u in kept
        ]
        local_embeddings = voiceprint.speaker_embeddings(samples, relative, sr=sr)
    except Exception:  # pragma: no cover - WAV borrado o ilegible
        log.debug("Sin huella vocal para %s", path, exc_info=True)

    if not local_embeddings:
        for u in kept:
            speaker = int(u.get("speaker", 0) or 0)
            duration = max(0.0, float(u["end"]) - float(u["start"]))
            previous = local_embeddings.get(speaker, (None, 0.0))
            local_embeddings[speaker] = (previous[0], previous[1] + duration)

    mapping = registry.assign(local_embeddings)
    registry.save()
    _upsert_session_speakers(session_id, registry)
    return mapping


def _upsert_session_speakers(session_id: int, registry: fusion.SpeakerRegistry) -> None:
    """Crea/actualiza las filas de `session_speakers` sin tocar lo confirmado."""
    rows = []
    for index, seconds in registry.talk_seconds().items():
        voice = registry.voice_json(index)
        rows.append(
            (
                session_id,
                index,
                round(seconds, 2),
                SPEAKER_COLORS[index % len(SPEAKER_COLORS)],
                db.json_dumps(voice) if voice else None,
            )
        )
    if not rows:
        return
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO session_speakers (session_id, speaker_index, talk_seconds, color,"
            " voice_json) VALUES (?,?,?,?,?)"
            " ON CONFLICT(session_id, speaker_index) DO UPDATE SET"
            "  talk_seconds=excluded.talk_seconds, color=COALESCE(session_speakers.color,"
            "  excluded.color), voice_json=excluded.voice_json",
            rows,
        )


def _store_segments(session_id: int, chunk_index: int,
                    utterances: list[dict[str, Any]]) -> int:
    """Inserción idempotente: reprocesar un chunk no duplica segmentos."""
    with db.write() as conn:
        conn.execute(
            "DELETE FROM transcript_segments WHERE session_id=? AND chunk_index=?",
            (session_id, chunk_index),
        )
        if utterances:
            conn.executemany(
                "INSERT INTO transcript_segments (session_id, chunk_index, start_t, end_t,"
                " speaker_index, is_me, confidence, text) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        session_id,
                        chunk_index,
                        float(u["start"]),
                        float(u["end"]),
                        int(u.get("speaker_global", 0)),
                        1 if u.get("is_me") else 0,
                        float(u.get("confidence", 0.0) or 0.0),
                        str(u["text"]).strip() + " ",
                    )
                    for u in utterances
                ],
            )
    return len(utterances)


def _accumulate_breaks(session_id: int, silences: list[Any]) -> None:
    """Une los silencios largos detectados durante la grabación."""
    if not silences:
        return
    key = f"{BREAKS_SETTING}{session_id}"
    existing = [tuple(r) for r in (db.setting_get(key, []) or [])]
    merged = silence.merge_ranges(existing + [tuple(r) for r in silences], gap_s=5.0)
    db.setting_set(key, [[a, b] for a, b in merged])


def load_breaks(session_id: int) -> list[tuple[float, float]]:
    raw = db.setting_get(f"{BREAKS_SETTING}{session_id}", []) or []
    return [(float(a), float(b)) for a, b in raw]


def _record_usage(session_id: int, result: SttResult, cfg: dict[str, Any]) -> None:
    minutes = (result.duration or 0.0) / 60.0
    rate = float(cfg.get("pricing", {}).get("deepgram_usd_per_minute", 0.0))
    cost = minutes * rate if result.provider == "deepgram" else 0.0
    db.record_usage(
        session_id=session_id,
        kind="stt",
        provider=result.provider,
        model=result.model,
        purpose="transcription",
        minutes=minutes,
        cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# Bucle de la cola
# ---------------------------------------------------------------------------


def process_pending_once(
    cfg: dict[str, Any] | None = None,
    *,
    limit: int | None = None,
    session_id: int | None = None,
) -> tuple[int, int]:
    """Vacía la cola pendiente. Devuelve `(chunks_ok, chunks_con_error)`."""
    cfg = cfg or app_config.load_config()
    concurrency = max(1, int(cfg["settings"].get("stt_concurrency", 2)))
    max_retries = int(cfg["deepgram"].get("max_retries", 3))
    processed = errors = 0
    budget = limit if limit is not None else 10_000
    # Un chunto que falla con error reintentable vuelve a 'pending'. Sin esta memoria, el
    # mismo chunk se reclamaría una y otra vez dentro de la misma pasada, gastando la
    # cuota del proveedor en segundos. Se deja para el siguiente ciclo del supervisor.
    attempted: set[int] = set()

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="stt") as pool:
        while budget > 0:
            batch: list[Any] = []
            skipped: list[int] = []
            for row in queue.claim_batch(min(concurrency, budget), session_id=session_id):
                row_id = int(dict(row)["id"])
                if row_id in attempted:
                    skipped.append(row_id)
                else:
                    batch.append(row)
            queue.release(skipped)
            if not batch:
                break
            budget -= len(batch)
            attempted.update(int(dict(row)["id"]) for row in batch)
            futures = {pool.submit(process_row, row, cfg): row for row in batch}
            for future, row in futures.items():
                try:
                    future.result()
                    queue.mark_ok(int(dict(row)["id"]))
                    processed += 1
                except (STTError, ProviderError) as exc:
                    errors += 1
                    _handle_failure(row, exc, max_retries=max_retries,
                                    retryable=getattr(exc, "retryable", True))
                    if not getattr(exc, "retryable", True):
                        budget = 0
                except ConfigError as exc:
                    errors += 1
                    _handle_failure(row, exc, max_retries=max_retries, retryable=False)
                    budget = 0
                except Exception as exc:  # pragma: no cover - defensivo
                    errors += 1
                    log.exception("Fallo inesperado transcribiendo el chunk")
                    _handle_failure(row, exc, max_retries=max_retries, retryable=True)
    return processed, errors


def _handle_failure(row: sqlite3.Row | dict[str, Any], exc: Exception, *,
                    max_retries: int, retryable: bool) -> None:
    data = dict(row)
    status = queue.mark_failed(
        int(data["id"]), str(exc), max_retries=max_retries, retryable=retryable
    )
    message = getattr(exc, "user_message", None) or str(exc)
    log.warning("Chunk %s → %s (%s)", data.get("chunk_index"), status, message)
    if status == "failed":
        ws.emit_warning(message, code="stt", session_id=int(data["session_id"]))


def retry_failed(*, max_retries: int = 3) -> int:
    return queue.retry_failed(max_retries=max_retries)
