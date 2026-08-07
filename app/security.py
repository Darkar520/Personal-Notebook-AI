"""Endurecimiento del servidor local.

**Hueco del plan original:** el backend expone en `localhost:8787` una API sin
autenticación que contiene transcripciones de clases corporativas. Aunque solo escuche
en loopback, dos vectores reales de navegador la alcanzan:

1. **CSRF simple**: cualquier página web abierta en el navegador puede hacer
   `POST http://localhost:8787/api/...` (las peticiones "simples" no llevan preflight),
   por ejemplo para borrar cuadernos.
2. **DNS rebinding**: un dominio atacante que resuelva a `127.0.0.1` se convierte en
   *same-origin* y puede **leer** las respuestas (todo el transcript).

Defensa aplicada (barata y efectiva, sin fricción para el usuario):
- El header `Host` debe ser loopback.
- Si viene `Origin`, debe estar en la lista blanca local.
- Se rechaza `Sec-Fetch-Site: cross-site` en endpoints de escritura.
- Respuestas de API con `no-store` y `nosniff`.

Además se genera un token local (`data/api.token`) para clientes no-navegador; con
`server.require_token=true` se exige en cada llamada a `/api`.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import paths

log = logging.getLogger(__name__)

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "testserver"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HEADER_TOKEN = "x-notebook-token"


def _host_is_local(host_header: str | None) -> bool:
    if not host_header:
        # uvicorn siempre manda Host; TestClient también. Sin Host, no confiamos.
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # IPv6 con puerto: [::1]:8787
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_origins(port: int) -> set[str]:
    origins = set()
    for host in ("localhost", "127.0.0.1", "[::1]"):
        origins.add(f"http://{host}:{port}")
        origins.add(f"http://{host}")
    # pywebview carga el gadget desde file:// → Origin "null" o ausente.
    origins.add("null")
    return origins


def read_or_create_token() -> str:
    path = paths.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


class LocalGuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, port: int = 8787, require_token: bool = False) -> None:
        super().__init__(app)
        self.port = port
        self.require_token = require_token
        self.origins = allowed_origins(port)
        self._token = read_or_create_token() if require_token else ""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if not _host_is_local(request.headers.get("host")):
            log.warning("Host rechazado: %r", request.headers.get("host"))
            return JSONResponse(
                {"detail": "Host no permitido (solo acceso local)."}, status_code=421
            )

        origin = request.headers.get("origin")
        if origin and origin not in self.origins:
            return JSONResponse({"detail": "Origen no permitido."}, status_code=403)

        if request.method not in SAFE_METHODS:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Petición cruzada bloqueada."}, status_code=403)

        if self.require_token and path.startswith("/api") and not path.startswith("/api/health"):
            provided = request.headers.get(HEADER_TOKEN) or request.query_params.get("token")
            if not provided or not secrets.compare_digest(provided, self._token):
                return JSONResponse({"detail": "Token local inválido."}, status_code=401)

        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
