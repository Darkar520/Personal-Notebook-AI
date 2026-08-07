"""Resolución de identificadores de modelo contra el catálogo del proveedor.

**Hueco del plan original.** El plan fija en la configuración los ids
`deepseek-v4-flash`, `deepseek-v4-pro` y `glm-5.2`. Si el proveedor los nombra de otra
forma (`deepseek/deepseek-v4-flash`, `deepseek-v4-flash-0820`, …) *toda* llamada al LLM
devuelve 400/404: la app se queda sin notas, sin libro, sin chat y sin podcast, y el
usuario solo ve "error" sin pista de por qué.

Aquí se consulta `GET {base_url}/models` (estándar OpenAI-compatible) una vez, se cachea
en la tabla `settings` con TTL y se resuelve el id pedido:

1. Coincidencia exacta.
2. Coincidencia por sufijo/prefijo (`proveedor/modelo`, sufijos de fecha).
3. Lista de alternativas de `opencode.model_fallbacks` (admite comodines `*flash*`).
4. Si no hay catálogo (sin red), se usa el id tal cual: mejor intentarlo que no llamar.

El resultado se registra en el log para que quede claro qué modelo se usó de verdad.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from typing import Any

import httpx

from app import db

log = logging.getLogger(__name__)

CACHE_KEY = "model_catalog"
CACHE_TTL_SECONDS = 6 * 3600
_memory: dict[str, Any] = {"stamp": 0.0, "base_url": "", "models": []}


def _fetch_catalog(base_url: str, api_key: str, *, timeout: float = 20.0) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"HTTP {response.status_code}", request=response.request, response=response
        )
    payload = response.json()
    items = payload.get("data") if isinstance(payload, dict) else payload
    models: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                models.append(str(name))
    return sorted(set(models))


def catalog(base_url: str, api_key: str, *, force: bool = False) -> list[str]:
    """Lista de modelos disponibles (cacheada). Lista vacía si no se pudo consultar."""
    now = time.time()
    if (
        not force
        and _memory["base_url"] == base_url
        and _memory["models"]
        and now - float(_memory["stamp"]) < CACHE_TTL_SECONDS
    ):
        return list(_memory["models"])

    if not force:
        cached = db.setting_get(CACHE_KEY, None)
        if (
            isinstance(cached, dict)
            and cached.get("base_url") == base_url
            and now - float(cached.get("stamp", 0)) < CACHE_TTL_SECONDS
            and cached.get("models")
        ):
            _memory.update(
                {"stamp": cached["stamp"], "base_url": base_url, "models": cached["models"]}
            )
            return list(cached["models"])

    if not api_key:
        return []
    try:
        models = _fetch_catalog(base_url, api_key)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("No se pudo leer el catálogo de modelos: %s", exc)
        return list(_memory["models"]) if _memory["base_url"] == base_url else []

    _memory.update({"stamp": now, "base_url": base_url, "models": models})
    db.setting_set(CACHE_KEY, {"stamp": now, "base_url": base_url, "models": models})
    log.info("Catálogo de modelos (%d): %s", len(models), ", ".join(models[:12]))
    return models


def _match(wanted: str, models: list[str]) -> str | None:
    if not wanted:
        return None
    if wanted in models:
        return wanted
    lowered = {m.lower(): m for m in models}
    target = wanted.lower()
    if target in lowered:
        return lowered[target]
    # `proveedor/modelo` ↔ `modelo`
    for key, original in lowered.items():
        tail = key.split("/")[-1]
        if tail == target or key == target.split("/")[-1]:
            return original
    # Sufijos de versión/fecha: deepseek-v4-flash-20260801
    for key, original in lowered.items():
        if key.startswith(target) or target.startswith(key):
            return original
    if any(ch in wanted for ch in "*?["):
        hits = fnmatch.filter(models, wanted)
        if hits:
            return sorted(hits, key=len)[0]
        hits = [m for m in models if fnmatch.fnmatch(m.lower(), target)]
        if hits:
            return sorted(hits, key=len)[0]
    return None


def resolve(
    role: str,
    cfg: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> str:
    """Modelo real a usar para un rol (`live`, `polish`, `chat`, `podcast`, `study`)."""
    opencode = cfg.get("opencode", {})
    configured = str((opencode.get("models") or {}).get(role) or "").strip()
    fallbacks = [
        str(x) for x in (opencode.get("model_fallbacks") or {}).get(role, []) if x
    ]
    models = catalog(
        str(opencode.get("base_url", "")), str(opencode.get("api_key", "")),
        force=force_refresh,
    )
    if not models:
        return configured or (fallbacks[0] if fallbacks else "")

    for candidate in [configured, *fallbacks]:
        hit = _match(candidate, models)
        if hit:
            if hit != configured:
                log.info("Modelo para %s: %r → %r", role, candidate, hit)
            return hit

    log.warning(
        "Ningún modelo del rol %s existe en el catálogo (%r); uso %r",
        role, configured, models[0],
    )
    return models[0]


def invalidate() -> None:
    _memory.update({"stamp": 0.0, "base_url": "", "models": []})
    db.setting_set(CACHE_KEY, None)
