"""Hub de WebSocket para eventos en vivo.

Corrección importante respecto al plan original: los eventos se emiten desde **hilos**
(grabador, worker de STT, pase final). El plan usaba `asyncio.ensure_future(...)` —que
falla sin loop corriendo— y luego `asyncio.run(...)`, que crea un loop nuevo y **no
puede** escribir en WebSockets que pertenecen al loop de uvicorn.

Aquí se guarda una referencia al loop principal en el `lifespan` y se publica con
`run_coroutine_threadsafe`, que es la forma correcta y segura de cruzar de hilo a loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, *, history: int = 50) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = 0

    # -- ciclo de vida -----------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def unbind_loop(self) -> None:
        self._loop = None

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.discard(ws)

    # -- emisión -----------------------------------------------------------
    async def broadcast(self, event: dict[str, Any]) -> None:
        """Envía a todos los clientes conectados. Debe llamarse dentro del loop."""
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq}
            self._history.append(event)
            targets = list(self._clients)
        if not targets:
            return
        results = await asyncio.gather(
            *(self._send(ws, event) for ws in targets), return_exceptions=True
        )
        dead = [ws for ws, ok in zip(targets, results) if ok is not True]
        if dead:
            with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    @staticmethod
    async def _send(ws: WebSocket, event: dict[str, Any]) -> bool:
        try:
            await ws.send_json(event)
            return True
        except Exception:
            return False

    def publish(self, event: dict[str, Any]) -> None:
        """Publica desde CUALQUIER hilo. Nunca lanza excepción al llamante."""
        loop = self._loop
        if loop is None or loop.is_closed():
            with self._lock:
                self._seq += 1
                self._history.append({**event, "seq": self._seq})
            return
        try:
            if _running_loop() is loop:
                loop.create_task(self.broadcast(event))
            else:
                asyncio.run_coroutine_threadsafe(self.broadcast(event), loop)
        except Exception:  # pragma: no cover - defensivo
            log.debug("No se pudo publicar el evento %s", event.get("type"), exc_info=True)

    def recent(self, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._history if e.get("seq", 0) > after_seq]


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


hub = Hub()


# --- Atajos semánticos usados por el resto de la app -------------------------


def emit_session_status(
    session_id: int,
    status: str,
    *,
    detail: str | None = None,
    progress: float | None = None,
) -> None:
    hub.publish(
        {
            "type": "session/status",
            "session_id": session_id,
            "status": status,
            "detail": detail,
            "progress": progress,
        }
    )


def emit_segments(session_id: int, segments: list[dict[str, Any]]) -> None:
    hub.publish({"type": "segments", "session_id": session_id, "segments": segments})


def emit_structure(session_id: int) -> None:
    hub.publish({"type": "structure", "session_id": session_id})


def emit_warning(message: str, *, code: str = "warn", session_id: int | None = None) -> None:
    hub.publish({"type": "warn", "code": code, "message": message, "session_id": session_id})
