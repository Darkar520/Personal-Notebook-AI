"""Ventana flotante del gadget (pywebview / WebView2).

Detalles que el plan dejaba sin resolver:

1. **Se carga por HTTP, no por `file://`.** Cargarlo como fichero deja al gadget en un
   origen opaco (`Origin: null`) y complica tanto la guardia local como el WebSocket. Con
   `http://127.0.0.1:8787/gadget/gadget.html` es *same-origin* con la API.
2. **Espera a que el backend responda** antes de crear la ventana; si no, la primera carga
   sale en blanco y el usuario ve una burbuja muerta.
3. **`hide()` de verdad**: el botón "Quitar" del plan destruía la ventana y no había forma
   de recuperarla. Aquí se minimiza/oculta y se puede volver con la bandeja o reabriendo.
4. **Posición recordada** en `data/gadget.json`: nadie quiere recolocar la burbuja cada día.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from typing import Any

import httpx

from app import paths

log = logging.getLogger(__name__)

WIDTH = 240
HEIGHT = 210
DEFAULT_MARGIN = 60


def _state_path():
    return paths.data_dir() / "gadget.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(data: dict[str, Any]) -> None:
    try:
        paths.ensure_data_dir()
        _state_path().write_text(json.dumps(data), encoding="utf-8")
    except OSError:  # pragma: no cover
        log.debug("No se pudo guardar la posición del gadget", exc_info=True)


def wait_for_backend(base_url: str, *, timeout: float = 25.0) -> bool:
    """Espera a que `/api/health` responda antes de abrir la ventana."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


class GadgetApi:
    """Puente JS → Python expuesto en `window.pywebview.api`."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.window = None

    def open_web(self, url: str | None = None) -> None:
        webbrowser.open(url or self.base_url)

    def hide(self) -> None:
        if self.window is not None:
            try:
                self.window.hide()
            except Exception:  # pragma: no cover - depende del back-end de webview
                log.debug("No se pudo ocultar la ventana", exc_info=True)
            threading.Timer(0.1, self._notify_hidden).start()

    def _notify_hidden(self) -> None:
        log.info("Gadget oculto. Vuelve a ejecutar run.py o usa la web para controlarlo.")

    def quit(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # pragma: no cover
                log.debug("No se pudo cerrar la ventana", exc_info=True)


def run_gadget(base_url: str = "http://127.0.0.1:8787", *, block: bool = True) -> None:
    """Abre la burbuja flotante. Bloquea el hilo principal (requisito de pywebview)."""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - entorno sin pywebview
        log.error("pywebview no está instalado (%s). Usa la web en %s", exc, base_url)
        return

    if not wait_for_backend(base_url):
        log.error("El backend no respondió en %s; no abro el gadget", base_url)
        return

    state = _load_state()
    api = GadgetApi(base_url)
    window = webview.create_window(
        "Notebook AI",
        f"{base_url}/gadget/gadget.html",
        width=WIDTH,
        height=HEIGHT,
        x=state.get("x"),
        y=state.get("y"),
        frameless=True,
        easy_drag=False,           # el arrastre lo controla la burbuja (app-region)
        on_top=True,
        transparent=True,
        resizable=False,
        js_api=api,
        background_color="#00000000",
    )
    api.window = window

    def _remember_position() -> None:
        try:
            _save_state({"x": window.x, "y": window.y})
        except Exception:  # pragma: no cover
            log.debug("Sin posición que recordar", exc_info=True)

    window.events.closing += _remember_position
    if not block:  # pragma: no cover - solo para pruebas manuales
        threading.Thread(target=webview.start, kwargs={"private_mode": False},
                         daemon=True).start()
        return
    webview.start(private_mode=False)
