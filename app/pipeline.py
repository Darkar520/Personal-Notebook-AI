"""Orquestador de una sesión: grabar → transcribir → estructurar → libro final.

Es el único módulo que conoce el ciclo de vida completo. Los routers solo llaman aquí;
la captura, la transcripción y la IA no se conocen entre sí.

Diferencias relevantes con el plan original:

1. **`start_session` arranca de verdad la captura.** En el plan, `start_session` solo
   insertaba la fila y creaba la carpeta, y el endpoint `/start` "garantizaba el estado
   `recording`": no grababa nada hasta la Task 3.5. Aquí una sesión que dice `recording`
   está grabando.
2. **Duración calculada con `db.duration_between`.** El plan mezclaba `datetime.now()`
   (naive, hora local) con `started_at` en UTC: la duración salía desplazada varias horas.
3. **`stop_session` no pierde el último tramo**: primero cierra el grabador (que vuelca el
   buffer restante), después espera a que la cola se vacíe y solo entonces pule.
4. **El pase final es idempotente y respeta al usuario**: los temas con `user_edited=1` no
   se sobrescriben, ni el título si el usuario lo renombró (`title_locked`).
5. **Recuperación de caídas**: si el proceso muere grabando, la sesión queda `recording`
   con chunks en disco; `recover_orphans` los reencola y la UI ofrece finalizar/descartar.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import config as app_config
from app import db, disk, paths, ws
from app.ai import live_integration, opencode_client, polish
from app.audio import session_audio
from app.capture import loopback, silence
from app.errors import AudioDeviceError, NotebookError, SessionStateError
from app.transcription import fusion, queue as stt_queue, voiceprint, worker

log = logging.getLogger(__name__)

LAST_INTEGRATED_KEY = "last_integrated_seg_"
_lock = threading.Lock()


@dataclass
class ActiveSession:
    """Estado en memoria de la sesión que se está grabando."""

    session_id: int
    recorder: Any
    capture_mode: str
    started_monotonic: float = field(default_factory=time.monotonic)
    chunks: int = 0
    last_integration: float = field(default_factory=time.monotonic)
    words_since_integration: int = 0
    warned_disk: bool = False
    warned_silence: bool = False

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)


ACTIVE: dict[int, ActiveSession] = {}


# ---------------------------------------------------------------------------
# Arranque / parada
# ---------------------------------------------------------------------------


def create_session(
    *,
    title: str | None = None,
    account_tag: str | None = None,
    capture_mode: str = "loopback",
    status: str = "recording",
) -> int:
    now = db.now_iso()
    with db.write() as conn:
        number = db.next_session_number(conn)
        cursor = conn.execute(
            "INSERT INTO sessions (title, title_locked, session_number, started_at,"
            " tz_offset_min, status, capture_mode, account_tag, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                title or f"Sesión {number}",
                1 if title else 0,
                number,
                now,
                db.local_offset_minutes(),
                status,
                capture_mode,
                account_tag,
                now,
                now,
            ),
        )
        session_id = int(cursor.lastrowid or 0)
        conn.execute(
            "UPDATE sessions SET audio_root=? WHERE id=?",
            (f"sessions/{session_id}", session_id),
        )
    paths.session_audio_dir(session_id).mkdir(parents=True, exist_ok=True)
    return session_id


def start_session(
    *,
    capture_mode: str | None = None,
    title: str | None = None,
    account_tag: str | None = None,
    source_wav: str | None = None,
    realtime: bool = True,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Crea la sesión y arranca la captura. Devuelve el id."""
    cfg = cfg or app_config.load_config()
    with _lock:
        if ACTIVE:
            raise SessionStateError(
                f"Ya hay una sesión grabando: {sorted(ACTIVE)}",
                user_message="Ya hay una clase grabando. Deténla antes de empezar otra.",
            )
    mode = capture_mode or str(cfg["settings"].get("capture_mode", "loopback"))
    if source_wav:
        mode = "wav"

    free_mb = disk.free_space_mb()
    minimum = int(cfg["settings"].get("min_free_space_mb", 1024))
    if free_mb < minimum // 2:
        raise NotebookError(
            f"Espacio insuficiente: {free_mb} MB libres",
            user_message=(
                f"Solo quedan {free_mb} MB libres. Libera espacio antes de grabar "
                "(una clase de 3,5 h ocupa unos 400 MB)."
            ),
        )

    session_id = create_session(title=title, account_tag=account_tag, capture_mode=mode)
    audio_dir = paths.session_audio_dir(session_id)
    audio = cfg["audio"]

    def on_chunk(result: loopback.ChunkResult) -> None:
        _handle_chunk(session_id, result)

    def on_error(message: str) -> None:
        db.touch_session(session_id, status_detail=message[:300])
        ws.emit_warning(message, code="audio", session_id=session_id)

    try:
        if source_wav:
            candidate = Path(source_wav).expanduser()
            if not candidate.is_absolute():
                candidate = paths.project_root() / candidate
            recorder: Any = loopback.WavFileRecorder(
                candidate,
                audio_dir,
                chunk_seconds=float(audio.get("chunk_seconds", 90)),
                overlap_seconds=float(audio.get("overlap_seconds", 8)),
                break_min_seconds=float(cfg["settings"].get("break_min_seconds", 45)),
                realtime=realtime,
                on_chunk=on_chunk,
                on_error=on_error,
            )
        else:
            recorder = loopback.LoopbackRecorder(
                audio_dir,
                chunk_seconds=float(audio.get("chunk_seconds", 90)),
                overlap_seconds=float(audio.get("overlap_seconds", 8)),
                capture_mode=mode,
                output_device_index=audio.get("output_device_index"),
                mic_device_index=audio.get("mic_device_index"),
                mic_gain=float(audio.get("mic_gain", 1.0)),
                break_min_seconds=float(cfg["settings"].get("break_min_seconds", 45)),
                on_chunk=on_chunk,
                on_error=on_error,
            )
        recorder.start()
    except (AudioDeviceError, NotebookError) as exc:
        db.touch_session(
            session_id, status="error",
            status_detail=getattr(exc, "user_message", str(exc))[:300],
        )
        raise

    with _lock:
        ACTIVE[session_id] = ActiveSession(
            session_id=session_id, recorder=recorder, capture_mode=mode
        )
    db.touch_session(session_id, status="recording", status_detail=None, progress=0.0)
    ws.emit_session_status(session_id, "recording", detail="Grabando")
    log.info("Sesión %s iniciada (%s)", session_id, mode)
    return session_id


def _handle_chunk(session_id: int, result: loopback.ChunkResult) -> None:
    """Persiste los metadatos del chunk y lo encola para transcribir."""
    stt_queue.write_meta(
        result.path,
        {
            "chunk_index": result.index,
            "start_t": result.start_t,
            "duration": result.duration,
            "overlap_pre": result.overlap_pre,
            "level": round(result.level, 5),
            "final": bool(result.final),
            "mic_ranges": [[a, b] for a, b in result.mic_ranges],
            "silences": [[a, b] for a, b in result.silences],
        },
    )
    stt_queue.enqueue(
        session_id,
        result.path,
        result.start_t,
        chunk_index=result.index,
        duration=result.duration,
        overlap_pre=result.overlap_pre,
    )
    active = ACTIVE.get(session_id)
    if active:
        active.chunks += 1
        _check_signal(active, result)
    ws.hub.publish(
        {
            "type": "chunk",
            "session_id": session_id,
            "index": result.index,
            "start_t": result.start_t,
            "level": round(result.level, 5),
        }
    )


def _check_signal(active: ActiveSession, result: loopback.ChunkResult) -> None:
    """Avisa una sola vez si el audio capturado es prácticamente silencio."""
    if result.level >= 0.006 or active.warned_silence or active.chunks < 2:
        if result.level >= 0.006:
            active.warned_silence = False
        return
    active.warned_silence = True
    ws.emit_warning(
        "El audio capturado está casi en silencio. Revisa que Zoom esté sonando por el "
        "dispositivo de salida seleccionado en Ajustes → Audio.",
        code="audio-level",
        session_id=active.session_id,
    )


def stop_session(session_id: int, *, discard: bool = False, finalize: bool = True,
                 cfg: dict[str, Any] | None = None) -> None:
    """Detiene la captura y (por defecto) lanza el pase final en segundo plano."""
    cfg = cfg or app_config.load_config()
    row = db.fetch_session(session_id)
    if row is None:
        raise KeyError(session_id)

    with _lock:
        active = ACTIVE.pop(session_id, None)
    if active is not None:
        try:
            active.recorder.stop()
        except Exception:  # pragma: no cover - cierre de dispositivo
            log.exception("Error cerrando el grabador de la sesión %s", session_id)

    if discard:
        delete_session(session_id)
        ws.emit_session_status(session_id, "deleted", detail="Sesión descartada")
        return

    ended_at = db.now_iso()
    duration = db.duration_between(row["started_at"], ended_at)
    with db.write() as conn:
        conn.execute(
            "UPDATE sessions SET status='processing', ended_at=?, duration_sec=?,"
            " status_detail=?, progress=?, updated_at=? WHERE id=?",
            (ended_at, duration, "Transcribiendo lo que falta…", 0.05, ended_at, session_id),
        )
    ws.emit_session_status(session_id, "processing", detail="Transcribiendo lo que falta…",
                           progress=0.05)
    if finalize:
        threading.Thread(
            target=finalize_session, args=(session_id,), kwargs={"cfg": cfg},
            name=f"finalize-{session_id}", daemon=True,
        ).start()


def delete_session(session_id: int) -> None:
    """Borrado total: base de datos + audio (requisito de privacidad del spec §12)."""
    import shutil

    with _lock:
        active = ACTIVE.pop(session_id, None)
    if active is not None:
        try:
            active.recorder.stop()
        except Exception:  # pragma: no cover
            log.exception("Error cerrando el grabador al borrar la sesión %s", session_id)
    fusion.SpeakerRegistry.clear(session_id)
    db.setting_delete(f"{worker.BREAKS_SETTING}{session_id}")
    db.setting_delete(f"{LAST_INTEGRATED_KEY}{session_id}")
    with db.write() as conn:
        conn.execute("DELETE FROM usage_events WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    folder = paths.session_dir(session_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    log.info("Sesión %s borrada por completo", session_id)


# ---------------------------------------------------------------------------
# Estructuración en vivo
# ---------------------------------------------------------------------------


def integration_due(session_id: int, interval_sec: int, *, min_words: int = 0) -> bool:
    active = ACTIVE.get(session_id)
    if active is None:
        return False
    elapsed = time.monotonic() - active.last_integration
    if elapsed >= max(60, interval_sec):
        return True
    return bool(min_words) and active.words_since_integration >= min_words


def mark_integrated(session_id: int) -> None:
    active = ACTIVE.get(session_id)
    if active is not None:
        active.last_integration = time.monotonic()
        active.words_since_integration = 0


def _draft_topics(session_id: int) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT title, summary_md, start_t FROM topics"
            " WHERE session_id=? AND status='draft' ORDER BY sort_order",
            (session_id,),
        ).fetchall()
    topics = []
    for row in rows:
        summary = db.json_loads(row["summary_md"], {})
        topics.append(
            {
                "title": row["title"],
                "start_t": row["start_t"] if row["start_t"] is not None else -1.0,
                "points": list(summary.get("points", [])),
                "spanish_notes": list(summary.get("spanish_notes", [])),
            }
        )
    return topics


def save_draft_topics(session_id: int, topics: list[dict[str, Any]]) -> None:
    now = db.now_iso()
    rows = [
        (
            session_id,
            index,
            "draft",
            topic.get("title", ""),
            topic.get("start_t") if float(topic.get("start_t", -1)) >= 0 else None,
            db.json_dumps(
                {
                    "points": list(topic.get("points", [])),
                    "spanish_notes": list(topic.get("spanish_notes", [])),
                }
            ),
            now,
            now,
        )
        for index, topic in enumerate(topics)
    ]
    with db.write() as conn:
        conn.execute(
            "DELETE FROM topics WHERE session_id=? AND status='draft'", (session_id,)
        )
        if rows:
            conn.executemany(
                "INSERT INTO topics (session_id, sort_order, status, title, start_t,"
                " summary_md, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )


def run_integration(session_id: int, cfg: dict[str, Any] | None = None) -> bool:
    """Una pasada de estructuración en vivo. `True` si actualizó el borrador."""
    cfg = cfg or app_config.load_config()
    key = f"{LAST_INTEGRATED_KEY}{session_id}"
    last_id = int(db.setting_get(key, 0) or 0)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, start_t, speaker_index, text FROM transcript_segments"
            " WHERE session_id=? AND id>? ORDER BY start_t, id",
            (session_id, last_id),
        ).fetchall()
    if not rows:
        return False

    lines = live_integration.format_lines(
        [(float(r["start_t"]), int(r["speaker_index"]), str(r["text"])) for r in rows]
    )
    max_id = max(int(r["id"]) for r in rows)
    current = {"topics": _draft_topics(session_id)}
    try:
        creds = opencode_client.credentials(cfg, "live")
        structure = live_integration.integrate(
            current=current,
            new_text=lines,
            session_id=session_id,
            pricing=cfg.get("pricing"),
            **creds,
        )
    except NotebookError as exc:
        log.warning("Integración en vivo pospuesta: %s", exc)
        return False

    save_draft_topics(session_id, structure.get("topics", []))
    db.setting_set(key, max_id)
    mark_integrated(session_id)
    ws.emit_structure(session_id)
    return True


# ---------------------------------------------------------------------------
# Pase final
# ---------------------------------------------------------------------------


def me_speaker_index(session_id: int) -> int | None:
    """Hablante que corresponde al usuario, según la pista de micrófono."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT speaker_index,"
            "       SUM(end_t - start_t) AS total,"
            "       SUM(CASE WHEN is_me=1 THEN end_t - start_t ELSE 0 END) AS mine"
            " FROM transcript_segments WHERE session_id=? GROUP BY speaker_index",
            (session_id,),
        ).fetchall()
    best: tuple[float, int] | None = None
    for row in rows:
        total = float(row["total"] or 0.0)
        mine = float(row["mine"] or 0.0)
        if total <= 0 or mine <= 0:
            continue
        ratio = mine / total
        if ratio >= 0.5 and (best is None or mine > best[0]):
            best = (mine, int(row["speaker_index"]))
    return best[1] if best else None


def session_segments(session_id: int) -> list[tuple[float, int, str]]:
    return [
        (float(r["start_t"]), int(r["speaker_index"]), str(r["text"]))
        for r in db.fetch_segments(session_id)
    ]


def finalize_session(session_id: int, *, cfg: dict[str, Any] | None = None,
                     drain_timeout: float = 900.0) -> None:
    """Vacía la cola, genera el libro, el audio y (si procede) los materiales."""
    cfg = cfg or app_config.load_config()
    try:
        _drain_queue(session_id, cfg, timeout=drain_timeout)
        _progress(session_id, 0.35, "Estructurando la clase…")

        segments = session_segments(session_id)
        if not segments:
            _finish_empty(session_id)
            return

        row = db.fetch_session(session_id)
        duration = float((row["duration_sec"] if row else 0) or 0.0)
        if duration <= 0:
            duration = max(float(s[0]) for s in segments)
        breaks = silence.merge_ranges(worker.load_breaks(session_id), gap_s=30.0)
        me_speaker = me_speaker_index(session_id)

        creds = opencode_client.credentials(cfg, "polish")
        book = polish.finalize_session(
            segments=segments,
            draft_topics=_draft_topics(session_id),
            breaks=breaks,
            duration_sec=duration,
            me_speaker=me_speaker,
            session_id=session_id,
            pricing=cfg.get("pricing"),
            **creds,
        )
        apply_book(session_id, book, me_speaker=me_speaker)
        _progress(session_id, 0.75, "Preparando el audio…")
        _build_audio(session_id, cfg)

        db.touch_session(session_id, status="done", status_detail=None, progress=1.0)
        ws.emit_session_status(session_id, "done", detail="Cuaderno listo", progress=1.0)
        log.info("Sesión %s finalizada: %d temas", session_id, len(book.get("topics", [])))

        if cfg["settings"].get("auto_generate_all"):
            threading.Thread(
                target=_auto_generate, args=(session_id, cfg),
                name=f"autogen-{session_id}", daemon=True,
            ).start()
    except Exception as exc:  # noqa: BLE001 - el hilo no puede propagar
        message = getattr(exc, "user_message", None) or str(exc)
        log.exception("Fallo finalizando la sesión %s", session_id)
        db.touch_session(session_id, status="error", status_detail=message[:400])
        ws.emit_session_status(session_id, "error", detail=message[:200])


def _drain_queue(session_id: int, cfg: dict[str, Any], *, timeout: float) -> None:
    """Espera a que se transcriba todo lo pendiente (o a que se agoten los reintentos)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stt_queue.requeue_stale(timeout_seconds=300)
        counts = stt_queue.counts(session_id)
        if counts["pending"] == 0 and counts["claimed"] == 0:
            return
        processed, errors = worker.process_pending_once(cfg, session_id=session_id)
        if processed == 0 and errors:
            log.warning("Sesión %s: %d chunks no se pudieron transcribir", session_id, errors)
            return
        if processed == 0:
            time.sleep(2.0)
        _progress(
            session_id,
            min(0.3, 0.05 + 0.25 * (1.0 / max(1, counts["pending"] + 1))),
            f"Transcribiendo… quedan {counts['pending']} fragmentos",
        )
    log.warning("Sesión %s: se agotó el tiempo vaciando la cola", session_id)


def _progress(session_id: int, value: float, detail: str) -> None:
    db.touch_session(session_id, progress=value, status_detail=detail)
    ws.emit_session_status(session_id, "processing", detail=detail, progress=value)


def _finish_empty(session_id: int) -> None:
    counts = stt_queue.counts(session_id)
    if counts.get("failed"):
        detail = (
            "No se pudo transcribir el audio. Revisa la llave de Deepgram en Ajustes "
            "y usa «Reintentar transcripción»."
        )
        db.touch_session(session_id, status="error", status_detail=detail)
        ws.emit_session_status(session_id, "error", detail=detail)
        return
    detail = "No se capturó audio con voz en esta sesión."
    db.touch_session(session_id, status="empty", status_detail=detail, progress=1.0)
    ws.emit_session_status(session_id, "empty", detail=detail, progress=1.0)


def _build_audio(session_id: int, cfg: dict[str, Any]) -> None:
    try:
        path, seconds = session_audio.build_session_mp3(session_id)
    except Exception as exc:  # noqa: BLE001 - el audio no debe tumbar el cuaderno
        log.warning("No se pudo generar el MP3 de la sesión %s: %s", session_id, exc)
        return
    if path is None:
        return
    if seconds > 0:
        with db.write() as conn:
            conn.execute(
                "UPDATE sessions SET duration_sec=COALESCE(NULLIF(duration_sec,0),?)"
                " WHERE id=?",
                (int(seconds), session_id),
            )
    if not cfg["settings"].get("keep_raw_audio"):
        session_audio.discard_raw_audio(session_id)


def _auto_generate(session_id: int, cfg: dict[str, Any]) -> None:
    """Genera podcast, quiz, flashcards y mapa al terminar (`auto_generate_all`)."""
    from app.routers import generate as generate_router

    produced: list[str] = []
    for name, action in (
        ("quiz", lambda: generate_router.generate_quiz(session_id, 8, cfg)),
        ("flashcards", lambda: generate_router.generate_flashcards(session_id, 20, cfg)),
        ("concept_map", lambda: generate_router.generate_concept_map(session_id, cfg)),
        ("podcast", lambda: generate_router.generate_podcast(session_id, cfg)),
    ):
        try:
            action()
            produced.append(name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-generación de %s falló: %s", name, exc)
    ws.hub.publish(
        {"type": "generated", "session_id": session_id, "artifacts": produced}
    )


# ---------------------------------------------------------------------------
# Persistencia del libro
# ---------------------------------------------------------------------------


def apply_book(session_id: int, book: dict[str, Any], *, me_speaker: int | None = None
               ) -> None:
    """Guarda el libro respetando las ediciones manuales del usuario."""
    now = db.now_iso()
    topics = book.get("topics") or []
    timeline = book.get("timeline") or []
    roleplays = book.get("roleplays") or []

    with db.write() as conn:
        conn.execute("DELETE FROM timeline_events WHERE session_id=?", (session_id,))
        if timeline:
            conn.executemany(
                "INSERT INTO timeline_events (session_id, sort_order, kind, start_t, end_t,"
                " label, note_md) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        session_id,
                        event.get("sort_order", index),
                        event.get("kind", "topic"),
                        float(event.get("start_t", 0.0)),
                        float(event.get("end_t", 0.0)),
                        event.get("label", ""),
                        event.get("note_md"),
                    )
                    for index, event in enumerate(timeline)
                ],
            )

        # Temas: se conservan los editados a mano y se renumera todo por tiempo.
        kept = conn.execute(
            "SELECT id, title, start_t FROM topics"
            " WHERE session_id=? AND status='final' AND user_edited=1",
            (session_id,),
        ).fetchall()
        conn.execute(
            "DELETE FROM topics WHERE session_id=? AND (status='draft'"
            " OR (status='final' AND user_edited=0))",
            (session_id,),
        )
        for topic in topics:
            conn.execute(
                "INSERT INTO topics (session_id, sort_order, status, title, start_t, end_t,"
                " summary_md, vocab_json, phrases_json, created_at, updated_at)"
                " VALUES (?,?, 'final', ?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    0,
                    topic.get("title", ""),
                    topic.get("start_t"),
                    topic.get("end_t"),
                    db.json_dumps(
                        {
                            "points": topic.get("points", []),
                            "spanish_notes": topic.get("spanish_notes", []),
                        }
                    ),
                    db.json_dumps(topic.get("vocab", [])),
                    db.json_dumps(topic.get("phrases", [])),
                    now,
                    now,
                ),
            )
        _renumber_topics(conn, session_id)
        if kept:
            log.info("Sesión %s: %d temas editados a mano preservados", session_id, len(kept))

        conn.execute("DELETE FROM roleplays WHERE session_id=?", (session_id,))
        if roleplays:
            conn.executemany(
                "INSERT INTO roleplays (session_id, title, context_md, your_role,"
                " participants_json, key_phrases_json, feedback_md, start_t, end_t,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        session_id,
                        item.get("title", ""),
                        item.get("context", ""),
                        item.get("your_role", ""),
                        db.json_dumps(item.get("participants", [])),
                        db.json_dumps(item.get("key_phrases", [])),
                        item.get("feedback", ""),
                        item.get("start_t"),
                        item.get("end_t"),
                        now,
                    )
                    for item in roleplays
                ],
            )

        for speaker in book.get("speakers") or []:
            conn.execute(
                "UPDATE session_speakers SET suggested_name=?, suggested_role=?"
                " WHERE session_id=? AND speaker_index=? AND confirmed=0",
                (
                    speaker.get("suggested_name", ""),
                    speaker.get("suggested_role", "other"),
                    session_id,
                    int(speaker.get("index", 0)),
                ),
            )
        if me_speaker is not None:
            conn.execute(
                "UPDATE session_speakers SET is_me=1 WHERE session_id=? AND speaker_index=?",
                (session_id, me_speaker),
            )

        title = (book.get("title") or "").strip()
        if title:
            conn.execute(
                "UPDATE sessions SET title=?, polish_model=?, updated_at=?"
                " WHERE id=? AND title_locked=0",
                (title, book.get("model"), now, session_id),
            )
        conn.execute(
            "UPDATE sessions SET polish_model=?, updated_at=? WHERE id=?",
            (book.get("model"), now, session_id),
        )

    assign_segment_topics(session_id)
    match_known_people(session_id)


def _renumber_topics(conn, session_id: int) -> None:
    rows = conn.execute(
        "SELECT id FROM topics WHERE session_id=? AND status='final'"
        " ORDER BY COALESCE(start_t, 1e12), id",
        (session_id,),
    ).fetchall()
    for order, row in enumerate(rows):
        conn.execute("UPDATE topics SET sort_order=? WHERE id=?", (order, row["id"]))


def assign_segment_topics(session_id: int) -> None:
    """Asocia cada turno del transcript al tema que lo contiene."""
    with db.write() as conn:
        topics = conn.execute(
            "SELECT id, start_t, end_t FROM topics WHERE session_id=? AND status='final'"
            " ORDER BY COALESCE(start_t, 0)",
            (session_id,),
        ).fetchall()
        conn.execute(
            "UPDATE transcript_segments SET topic_id=NULL WHERE session_id=?", (session_id,)
        )
        for topic in topics:
            start = topic["start_t"]
            end = topic["end_t"]
            if start is None:
                continue
            if end is None or end <= start:
                end = start + 1e9
            conn.execute(
                "UPDATE transcript_segments SET topic_id=?"
                " WHERE session_id=? AND start_t>=? AND start_t<?",
                (topic["id"], session_id, float(start), float(end)),
            )


def match_known_people(session_id: int) -> int:
    """Propone personas ya conocidas comparando la huella vocal (nunca confirma)."""
    with db.read() as conn:
        speakers = conn.execute(
            "SELECT speaker_index, voice_json, suggested_name FROM session_speakers"
            " WHERE session_id=? AND confirmed=0",
            (session_id,),
        ).fetchall()
        people = conn.execute(
            "SELECT id, name, role, voice_json FROM people WHERE voice_json IS NOT NULL"
        ).fetchall()
    if not speakers or not people:
        return 0

    known: list[tuple[str, str, Any]] = []
    for person in people:
        vector, _ = voiceprint.from_json(db.json_loads(person["voice_json"], None))
        if vector is not None:
            known.append((str(person["name"]), str(person["role"]), vector))
    if not known:
        return 0

    matched = 0
    updates: list[tuple[str, str, int, int]] = []
    for speaker in speakers:
        vector, _ = voiceprint.from_json(db.json_loads(speaker["voice_json"], None))
        if vector is None:
            continue
        best = max(known, key=lambda item: voiceprint.similarity(vector, item[2]))
        score = voiceprint.similarity(vector, best[2])
        if score >= voiceprint.SAME_PERSON_MIN:
            updates.append((best[0], best[1], session_id, int(speaker["speaker_index"])))
            matched += 1
            log.info(
                "Hablante %s ≈ %s (%.2f) por huella vocal",
                speaker["speaker_index"], best[0], score,
            )
    if updates:
        with db.write() as conn:
            conn.executemany(
                "UPDATE session_speakers SET suggested_name=?, suggested_role=?,"
                " auto_matched=1 WHERE session_id=? AND speaker_index=? AND confirmed=0",
                updates,
            )
    return matched


# ---------------------------------------------------------------------------
# Estado / recuperación
# ---------------------------------------------------------------------------


def active_status() -> dict[str, Any]:
    """Estado para el gadget: sesión activa, tiempo, salud del audio y cola."""
    with _lock:
        active = next(iter(ACTIVE.values()), None)
    if active is None:
        counts = stt_queue.counts()
        with db.read() as conn:
            row = conn.execute(
                "SELECT id, status, status_detail, progress FROM sessions"
                " WHERE status IN ('processing','recording') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is not None:
            return {
                "state": row["status"],
                "session_id": int(row["id"]),
                "detail": row["status_detail"],
                "progress": float(row["progress"] or 0.0),
                "queue": counts,
            }
        return {"state": "idle", "session_id": None, "queue": counts}
    return {
        "state": "recording",
        "session_id": active.session_id,
        "elapsed": round(active.elapsed, 1),
        "chunks": active.chunks,
        "capture_mode": active.capture_mode,
        "healthy": bool(getattr(active.recorder, "healthy", True)),
        "detail": getattr(active.recorder, "status_detail", "") or None,
        "devices": getattr(active.recorder, "device_names", {}),
        "queue": stt_queue.counts(active.session_id),
    }


def recover_orphans() -> list[int]:
    """Al arrancar: reencola chunks reclamados y lista sesiones que quedaron grabando."""
    stt_queue.requeue_stale(timeout_seconds=0)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE status='recording' ORDER BY id"
        ).fetchall()
    orphans = [int(r["id"]) for r in rows if int(r["id"]) not in ACTIVE]
    for session_id in orphans:
        _reindex_orphan_chunks(session_id)
        db.touch_session(
            session_id,
            status_detail="La app se cerró mientras grababa. Puedes finalizar o descartar.",
        )
    if orphans:
        log.warning("Sesiones huérfanas detectadas: %s", orphans)
    return orphans


def _reindex_orphan_chunks(session_id: int) -> None:
    """Encola chunks que quedaron en disco sin llegar a la cola (caída al escribir)."""
    audio_dir = paths.session_audio_dir(session_id)
    if not audio_dir.exists():
        return
    with db.read() as conn:
        known = {
            int(r["chunk_index"])
            for r in conn.execute(
                "SELECT chunk_index FROM pending_transcriptions WHERE session_id=?",
                (session_id,),
            )
        }
        done = {
            int(r["chunk_index"])
            for r in conn.execute(
                "SELECT DISTINCT chunk_index FROM transcript_segments WHERE session_id=?",
                (session_id,),
            )
        }
    for wav in sorted(audio_dir.glob("chunk_*.wav")):
        meta = stt_queue.read_meta(wav)
        try:
            index = int(meta.get("chunk_index", int(wav.stem.split("_")[1])))
        except (ValueError, IndexError):
            continue
        if index in known or index in done:
            continue
        stt_queue.enqueue(
            session_id,
            wav,
            float(meta.get("start_t", index * 90.0)),
            chunk_index=index,
            duration=float(meta.get("duration", 0.0)),
            overlap_pre=float(meta.get("overlap_pre", 0.0)),
        )
        log.info("Chunk %s de la sesión %s recuperado del disco", index, session_id)


def finalize_recording(session_id: int, cfg: dict[str, Any] | None = None) -> None:
    """Cierra una sesión huérfana (opción "Finalizar" del diálogo de recuperación)."""
    row = db.fetch_session(session_id)
    if row is None:
        raise KeyError(session_id)
    if row["status"] not in ("recording", "processing", "error"):
        raise SessionStateError(
            f"La sesión {session_id} está en estado {row['status']}",
            user_message="Esta sesión ya está finalizada.",
        )
    stop_session(session_id, finalize=True, cfg=cfg)


def repolish(session_id: int, cfg: dict[str, Any] | None = None) -> None:
    """Vuelve a generar el libro de una sesión ya cerrada (tras corregir la llave)."""
    db.touch_session(session_id, status="processing", progress=0.1,
                     status_detail="Regenerando el cuaderno…")
    threading.Thread(
        target=finalize_session, args=(session_id,), kwargs={"cfg": cfg},
        name=f"repolish-{session_id}", daemon=True,
    ).start()
