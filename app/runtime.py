"""Supervisor de tareas de fondo (un solo hilo, ciclo corto).

El plan metía este bucle dentro de `app/main.py` con un `except Exception: pass` que
tragaba cualquier fallo sin dejar rastro. Aquí vive aparte, registra los errores, aplica
espera creciente cuando algo falla de forma repetida y publica latidos por WebSocket para
que el gadget y la SPA no tengan que hacer *polling* agresivo.

Qué hace en cada ciclo (por defecto cada 5 s):
1. Devuelve a la cola los chunks que quedaron reclamados por un cierre inesperado.
2. Transcribe lo pendiente (si hay llave y trabajo).
3. Lanza la estructuración en vivo de la sesión activa cuando toca.
4. Vigila el espacio en disco y avisa una sola vez por transición.
5. Emite un latido con el estado (tiempo grabado, cola, salud del audio).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app import config as app_config
from app import db, disk, pipeline, ws
from app.transcription import queue as stt_queue, worker

log = logging.getLogger(__name__)

TICK_SECONDS = 5.0
HEARTBEAT_SECONDS = 5.0
STALE_SWEEP_SECONDS = 120.0
DISK_CHECK_SECONDS = 60.0
MAX_BACKOFF = 60.0


class Supervisor:
    def __init__(self, *, tick: float = TICK_SECONDS) -> None:
        self.tick = tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_stale_sweep = 0.0
        self._last_disk_check = 0.0
        self._last_heartbeat = 0.0
        self._failures = 0
        self._disk_warned = False

    # -- ciclo de vida -----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="supervisor", daemon=True)
        self._thread.start()
        log.info("Supervisor de tareas iniciado")

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        log.info("Supervisor detenido")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- bucle -------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
                self._failures = 0
                wait = self.tick
            except Exception:  # noqa: BLE001 - el supervisor nunca debe morir
                self._failures += 1
                wait = min(MAX_BACKOFF, self.tick * (2**self._failures))
                log.exception("Ciclo del supervisor falló (%d seguidos)", self._failures)
            self._stop.wait(wait)

    def _cycle(self) -> None:
        cfg = app_config.load_config()
        now = time.monotonic()

        if now - self._last_stale_sweep >= STALE_SWEEP_SECONDS:
            self._last_stale_sweep = now
            stt_queue.requeue_stale()

        self._drain(cfg)
        self._integrate(cfg)

        if now - self._last_disk_check >= DISK_CHECK_SECONDS:
            self._last_disk_check = now
            self._check_disk(cfg)

        if now - self._last_heartbeat >= HEARTBEAT_SECONDS:
            self._last_heartbeat = now
            self._heartbeat()

    def _drain(self, cfg: dict[str, Any]) -> None:
        """Transcribe lo pendiente. No hace nada si falta la llave o no hay trabajo."""
        backend = str(cfg["settings"].get("stt_backend", "deepgram"))
        if backend == "deepgram" and not cfg["deepgram"].get("api_key"):
            return
        if not stt_queue.counts()["pending"]:
            return
        processed, errors = worker.process_pending_once(cfg, limit=6)
        if processed:
            log.debug("Supervisor: %d chunks transcritos (%d errores)", processed, errors)

    def _integrate(self, cfg: dict[str, Any]) -> None:
        """Estructuración en vivo de la sesión activa, si toca y hay llave."""
        if not cfg["opencode"].get("api_key"):
            return
        interval = int(cfg["settings"].get("integration_interval_sec", 300))
        min_words = int(cfg["settings"].get("integration_min_words", 0) or 0)
        for session_id in list(pipeline.ACTIVE):
            if pipeline.integration_due(session_id, interval, min_words=min_words):
                pipeline.mark_integrated(session_id)   # evita reentradas si tarda
                pipeline.run_integration(session_id, cfg)

    def _check_disk(self, cfg: dict[str, Any]) -> None:
        minimum = int(cfg["settings"].get("min_free_space_mb", 1024))
        free_mb = disk.free_space_mb()
        should = disk.should_warn(free_mb, minimum)
        if should and not self._disk_warned:
            self._disk_warned = True
            minutes = disk.remaining_recording_minutes(free_mb)
            ws.emit_warning(
                f"Quedan {free_mb} MB libres (~{minutes} min de grabación). "
                "Borra cuadernos antiguos o desactiva «conservar audio crudo».",
                code="disk",
            )
            log.warning("Espacio bajo: %d MB libres", free_mb)
        elif not should:
            self._disk_warned = False

    def _heartbeat(self) -> None:
        status = pipeline.active_status()
        if status.get("state") == "idle" and not status.get("queue", {}).get("total"):
            return
        ws.hub.publish({"type": "heartbeat", **status})
        session_id = status.get("session_id")
        if status.get("state") == "recording" and session_id:
            db.touch_session(int(session_id), status_detail=status.get("detail"))


supervisor = Supervisor()
