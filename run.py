"""Punto de entrada: arranca el backend y el gadget flotante.

    python run.py                 # backend + burbuja flotante
    python run.py --no-gadget     # solo el backend (útil si el gadget da problemas)
    python run.py --open          # además abre el navegador en la plataforma

Por qué así: uvicorn corre en un hilo *daemon* y pywebview se queda con el hilo principal,
porque en Windows el bucle de WebView2 **tiene** que ser el principal. Al cerrar la
burbuja, el proceso termina y con él el backend; si prefieres que el servidor siga vivo,
usa `--no-gadget` en una consola aparte.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

from app import __version__, config as app_config, logging_setup, paths

log = logging.getLogger("run")


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def serve(host: str, port: int, log_level: str) -> threading.Thread:
    import uvicorn

    from app.main import app

    config = uvicorn.Config(
        app, host=host, port=port, log_level=log_level.lower(), access_log=False,
        ws_ping_interval=20, ws_ping_timeout=20,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Notebook AI")
    parser.add_argument("--no-gadget", action="store_true",
                       help="no abrir la burbuja flotante")
    parser.add_argument("--open", action="store_true",
                       help="abrir la plataforma en el navegador al arrancar")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = app_config.load_config()
    host = args.host or str(cfg["server"].get("host", "127.0.0.1"))
    port = args.port or int(cfg["server"].get("port", 8787))
    log_level = str(cfg["settings"].get("log_level", "INFO"))
    logging_setup.setup_logging(log_level)

    base_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"

    if port_in_use(host, port):
        log.warning("El puerto %s ya está en uso: asumo que la app ya está corriendo.", port)
        if args.open:
            webbrowser.open(base_url)
        if args.no_gadget:
            return 1
    else:
        log.info("Personal Notebook AI %s", __version__)
        log.info("Datos en %s", paths.data_dir())
        serve(host, port, log_level)
        deadline = time.monotonic() + 20
        while not port_in_use(host, port) and time.monotonic() < deadline:
            time.sleep(0.2)
        log.info("Plataforma disponible en %s", base_url)

    if args.open:
        webbrowser.open(base_url)

    if args.no_gadget:
        log.info("Modo sin gadget: Ctrl+C para salir.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Hasta luego.")
        return 0

    from gadget.gadget_app import run_gadget

    run_gadget(base_url)
    log.info("Gadget cerrado; se detiene también el backend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
