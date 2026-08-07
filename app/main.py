"""Fábrica de la aplicación FastAPI.

Además de montar routers y estáticos (lo que hacía el plan), aquí se resuelven cuatro
cosas que el plan dejaba abiertas:

1. **El hub de WebSocket queda ligado al loop de uvicorn** en el `lifespan`, que es lo que
   permite emitir eventos desde los hilos de captura/transcripción (ver `app/ws.py`).
2. **Manejadores de excepción de dominio**: un `NotebookError` se convierte en una
   respuesta con `user_message` accionable en vez de un 500 con traza.
3. **Recuperación al arrancar**: chunks reclamados vuelven a la cola y las sesiones que
   quedaron grabando se marcan para que la UI ofrezca finalizar o descartar.
4. **Middleware de guardia local** (`app/security.py`) y la SPA montada *después* de la
   API, para que `/api/...` nunca lo intercepte el servidor de ficheros estáticos.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import __version__, backup, config as app_config, db, logging_setup, paths, pipeline, runtime
from app.errors import ConfigError, NotebookError, ProviderError, SessionStateError
from app.routers import chat, content, generate, media, sessions, settings, speakers
from app.security import LocalGuardMiddleware, check_websocket_origin
from app.ws import hub

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app_config.load_config()
    logging_setup.setup_logging(str(cfg["settings"].get("log_level", "INFO")))
    log.info("Personal Notebook AI %s — datos en %s", __version__, paths.data_dir())
    db.init_db()
    try:
        backup.cleanup_exports()
    except Exception:  # noqa: BLE001 - la limpieza nunca debe impedir el arranque
        log.exception("Fallo en la limpieza de exports")
    hub.bind_loop(asyncio.get_running_loop())
    try:
        orphans = pipeline.recover_orphans()
        if orphans:
            log.warning("Cuadernos pendientes de cerrar: %s", orphans)
    except Exception:  # noqa: BLE001 - nunca impedir el arranque
        log.exception("Fallo en la recuperación de arranque")
    runtime.supervisor.start()
    try:
        yield
    finally:
        runtime.supervisor.stop()
        for session_id in list(pipeline.ACTIVE):
            log.warning("Cerrando la grabación de la sesión %s por apagado", session_id)
            try:
                pipeline.stop_session(session_id, finalize=False)
            except Exception:  # noqa: BLE001
                log.exception("Error deteniendo la sesión %s", session_id)
        hub.unbind_loop()


def create_app(*, serve_static: bool = True) -> FastAPI:
    cfg = app_config.load_config()
    server = cfg.get("server", {})

    app = FastAPI(
        title="Personal Notebook AI",
        version=__version__,
        summary="Cuadernos de clase con transcripción, notas bilingües y materiales de estudio.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        LocalGuardMiddleware,
        port=int(server.get("port", 8787)),
        require_token=bool(server.get("require_token", False)),
    )

    _register_error_handlers(app)

    @app.get("/health", tags=["system"])
    @app.get("/api/health", tags=["system"])
    def health():
        return {"status": "ok", "version": __version__}

    for router in (
        sessions.router,
        content.router,
        speakers.router,
        speakers.people_router,
        chat.router,
        generate.router,
        media.router,
        settings.router,
        settings.system_router,
    ):
        app.include_router(router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # `LocalGuardMiddleware` (BaseHTTPMiddleware) no envuelve WebSockets en Starlette:
        # validamos aquí Host y Origin para que un cliente malicioso no pueda leer la
        # transcripción en vivo a través del socket.
        if not check_websocket_origin(
            websocket.headers.get("host"),
            websocket.headers.get("origin"),
            port=int(server.get("port", 8787)),
        ):
            await websocket.close(code=1008, reason="Origen no permitido")
            return
        await hub.connect(websocket)
        try:
            await websocket.send_json(
                {"type": "hello", "version": __version__, **pipeline.active_status()}
            )
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - un cliente roto no debe ensuciar el log
            log.debug("WebSocket cerrado con error", exc_info=True)
        finally:
            hub.disconnect(websocket)

    if serve_static:
        static_dir = paths.static_dir()
        static_dir.mkdir(parents=True, exist_ok=True)
        gadget_dir = paths.gadget_dir()
        if gadget_dir.exists():
            app.mount("/gadget", StaticFiles(directory=str(gadget_dir), html=True),
                      name="gadget")

        # Middleware que fuerza revalidación de estáticos para que los cambios
        # en JS/CSS se vean sin necesidad de Ctrl+F5.
        @app.middleware("http")
        async def no_cache_static(request: Request, call_next):
            response = await call_next(request)
            path = request.url.path
            if (
                not path.startswith("/api")
                and not path.startswith("/ws")
                and not path.startswith("/gadget")
                and not path.startswith("/health")
                and (path.endswith(".js")
                     or path.endswith(".css")
                     or path.endswith(".mjs")
                     or path.endswith(".html")
                     or path == "/")
            ):
                response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
            return response

        # La SPA va al final: así no captura las rutas de /api.
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")

    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, exc: ConfigError):
        return JSONResponse(
            {"detail": exc.user_message, "code": "config"}, status_code=400
        )

    @app.exception_handler(SessionStateError)
    async def _state_error(request: Request, exc: SessionStateError):
        return JSONResponse({"detail": exc.user_message, "code": "state"}, status_code=409)

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError):
        log.warning("Proveedor %s falló: %s", exc.provider or "?", exc)
        return JSONResponse(
            {"detail": exc.user_message, "code": "provider", "provider": exc.provider},
            status_code=502,
        )

    @app.exception_handler(NotebookError)
    async def _domain_error(request: Request, exc: NotebookError):
        log.warning("Error de dominio en %s: %s", request.url.path, exc)
        return JSONResponse({"detail": exc.user_message, "code": "domain"}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # `exc.errors()` puede traer objetos no serializables en `ctx` (Pydantic v2):
        # nos quedamos solo con lo que la UI necesita.
        simplified = [
            {
                "field": ".".join(str(p) for p in error.get("loc", [])[1:]),
                "msg": str(error.get("msg", "valor inválido")),
            }
            for error in (exc.errors() or [])
        ]
        first = simplified[0] if simplified else {"field": "petición", "msg": "valor inválido"}
        return JSONResponse(
            {
                "detail": f"{first['field'] or 'petición'}: {first['msg']}",
                "code": "validation",
                "errors": simplified,
            },
            status_code=422,
        )


app = create_app()
