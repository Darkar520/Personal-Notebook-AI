"""Cliente LLM compatible con la API de OpenAI (OpenCode Go / Zen).

Único punto por el que pasan **todas** las llamadas de texto de la app. Además de la
firma del plan (`chat_json` / `chat_text`), añade lo que hace falta para que una clase
real no se caiga a medio procesar:

1. **Reintentos con backoff** en 429/5xx y errores de red (el plan solo clasificaba el
   error y lo propagaba, así que un 429 puntual perdía el pase final completo).
2. **Modo JSON degradable**: pide `response_format={"type":"json_object"}` y, si el
   proveedor no lo soporta (400), repite sin él en vez de fallar.
3. **Reintento de formato**: si la respuesta no trae JSON válido ni tras la reparación de
   `jsonx`, se reintenta una vez con una instrucción más severa y temperatura 0.
4. **Medición de consumo** (`usage_events`) para el estimador de costos de la Fase 11.
5. **Resolución de modelo** vía `app.ai.models`, así un id inexistente no tumba la app.
6. **Recorte defensivo del prompt**: un transcript de 3,5 h puede pasar de 200 000
   tokens; `fit_text` avisa y recorta por el centro conservando principio y final.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app import db
from app.ai import jsonx, models
from app.errors import AIError, ConfigError

log = logging.getLogger(__name__)

PROVIDER = "opencode"
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_JSON_NUDGE = (
    "\n\nIMPORTANT: respond with a single valid JSON document and nothing else. "
    "No prose, no markdown fences, no comments."
)
# ~3,6 caracteres por token en inglés técnico; margen de seguridad incluido.
CHARS_PER_TOKEN = 3.6
DEFAULT_INPUT_BUDGET_TOKENS = 90_000


@dataclass(slots=True)
class LlmResponse:
    content: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


# ---------------------------------------------------------------------------
# Utilidades de tamaño
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def fit_text(text: str, max_tokens: int = DEFAULT_INPUT_BUDGET_TOKENS) -> str:
    """Recorta por el centro si el texto no cabe, avisando en el propio contenido."""
    limit = int(max_tokens * CHARS_PER_TOKEN)
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    log.warning("Prompt recortado: %d → %d caracteres", len(text), limit)
    return (
        text[:head]
        + "\n\n[...contenido intermedio omitido por longitud...]\n\n"
        + text[-tail:]
    )


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


def credentials(cfg: dict[str, Any], role: str = "live") -> dict[str, str]:
    """`{model, base_url, api_key}` listos para pasar al cliente."""
    opencode = cfg.get("opencode", {})
    api_key = str(opencode.get("api_key") or "")
    if not api_key:
        raise ConfigError(
            "opencode.api_key vacío",
            user_message="Falta la llave de OpenCode Go. Pégala en Ajustes.",
        )
    return {
        "model": models.resolve(role, cfg),
        "base_url": str(opencode.get("base_url") or ""),
        "api_key": api_key,
    }


# ---------------------------------------------------------------------------
# Llamada
# ---------------------------------------------------------------------------


def _build_payload(
    system: str,
    user: str,
    *,
    model: str,
    temperature: float,
    json_mode: bool,
    max_tokens: int | None,
    history: list[dict[str, str]] | None,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    for item in history or []:
        role = str(item.get("role", "")).lower()
        content = str(item.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _classify(status: int, body: str) -> AIError:
    snippet = body[:300]
    if status == 401:
        return AIError(
            f"OpenCode 401: {snippet}",
            retryable=False,
            status_code=status,
            provider=PROVIDER,
            user_message="La llave de OpenCode Go no es válida. Revísala en Ajustes.",
        )
    if status in (402, 403):
        return AIError(
            f"OpenCode {status}: {snippet}",
            retryable=False,
            status_code=status,
            provider=PROVIDER,
            user_message=(
                "OpenCode Go rechazó la petición (límite de uso o permisos). "
                "Revisa tu cuota o cambia de modelo en Ajustes."
            ),
        )
    if status == 404:
        return AIError(
            f"OpenCode 404: {snippet}",
            retryable=False,
            status_code=status,
            provider=PROVIDER,
            user_message=(
                "El modelo configurado no existe en el proveedor. "
                "Elige otro en Ajustes → Modelos."
            ),
        )
    if status == 429:
        return AIError(
            f"OpenCode 429: {snippet}",
            retryable=True,
            status_code=status,
            provider=PROVIDER,
            user_message="OpenCode Go está limitando las peticiones; se reintentará.",
        )
    return AIError(
        f"OpenCode {status}: {snippet}",
        retryable=status in _RETRY_STATUS,
        status_code=status,
        provider=PROVIDER,
    )


def complete(
    system: str,
    user: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.3,
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: float = 240.0,
    max_retries: int = 3,
    history: list[dict[str, str]] | None = None,
    session_id: int | None = None,
    purpose: str = "",
    pricing: dict[str, Any] | None = None,
) -> LlmResponse:
    """Una llamada de chat completion, con reintentos y medición de consumo."""
    if not api_key:
        raise ConfigError(
            "opencode.api_key vacío",
            user_message="Falta la llave de OpenCode Go. Pégala en Ajustes.",
        )
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = _build_payload(
        system, user, model=model, temperature=temperature, json_mode=json_mode,
        max_tokens=max_tokens, history=history,
    )
    attempt = 0
    last: AIError | None = None

    while attempt < max(1, max_retries):
        attempt += 1
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = AIError(
                f"Red: {exc}", retryable=True, provider=PROVIDER,
                user_message="Sin conexión con OpenCode Go.",
            )
            if attempt >= max_retries:
                raise last from exc
            _sleep(attempt)
            continue

        if response.status_code >= 400:
            error = _classify(response.status_code, response.text)
            # El proveedor puede no soportar response_format: repetimos sin él.
            if (
                response.status_code == 400
                and "response_format" in payload
                and "response_format" in response.text.lower()
            ):
                log.info("El proveedor no acepta response_format; repito sin modo JSON")
                payload.pop("response_format", None)
                continue
            last = error
            if not error.retryable or attempt >= max_retries:
                raise error
            _sleep(attempt)
            continue

        try:
            body = response.json()
        except ValueError as exc:
            raise AIError(
                f"Respuesta ilegible de OpenCode: {exc}", retryable=True, provider=PROVIDER
            ) from exc

        result = _parse_completion(body, fallback_model=model)
        _record(result, session_id=session_id, purpose=purpose, pricing=pricing or {})
        return result

    raise last or AIError("OpenCode: fallo desconocido", provider=PROVIDER)


def _parse_completion(body: dict[str, Any], *, fallback_model: str) -> LlmResponse:
    choices = body.get("choices") or []
    if not choices:
        raise AIError(
            f"OpenCode devolvió una respuesta sin `choices`: {str(body)[:200]}",
            retryable=True,
            provider=PROVIDER,
        )
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # algunos proveedores devuelven bloques
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    usage = body.get("usage") or {}
    return LlmResponse(
        content=str(content or ""),
        model=str(body.get("model") or fallback_model),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        finish_reason=str(choices[0].get("finish_reason") or ""),
    )


def _record(result: LlmResponse, *, session_id: int | None, purpose: str,
            pricing: dict[str, Any]) -> None:
    rate_in = float(pricing.get("llm_usd_per_1m_input", 0.0))
    rate_out = float(pricing.get("llm_usd_per_1m_output", 0.0))
    cost = (result.tokens_in * rate_in + result.tokens_out * rate_out) / 1_000_000
    db.record_usage(
        session_id=session_id,
        kind="llm",
        provider=PROVIDER,
        model=result.model,
        purpose=purpose,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=cost,
    )


def _sleep(attempt: int) -> None:
    delay = min(30.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.75)
    log.warning("Reintento %d de OpenCode en %.1fs", attempt, delay)
    time.sleep(delay)


# ---------------------------------------------------------------------------
# API pública (contrato del plan)
# ---------------------------------------------------------------------------


def chat_text(
    system: str,
    user: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    timeout: float = 240.0,
    max_retries: int = 3,
    history: list[dict[str, str]] | None = None,
    session_id: int | None = None,
    purpose: str = "",
    pricing: dict[str, Any] | None = None,
) -> str:
    return complete(
        system, user, model=model, base_url=base_url, api_key=api_key,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        max_retries=max_retries, history=history, session_id=session_id,
        purpose=purpose, pricing=pricing,
    ).content


def chat_json(
    system: str,
    user: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    timeout: float = 240.0,
    max_retries: int = 3,
    session_id: int | None = None,
    purpose: str = "",
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Como `chat_text` pero garantizando un dict; reintenta si el JSON viene roto."""
    response = complete(
        system, user, model=model, base_url=base_url, api_key=api_key,
        temperature=temperature, json_mode=True, max_tokens=max_tokens, timeout=timeout,
        max_retries=max_retries, session_id=session_id, purpose=purpose, pricing=pricing,
    )
    try:
        return jsonx.as_dict(response.content)
    except jsonx.JsonExtractionError as first_error:
        log.warning("JSON inválido del modelo (%s); reintento estricto", first_error)
        retry = complete(
            system + _JSON_NUDGE, user, model=model, base_url=base_url, api_key=api_key,
            temperature=0.0, json_mode=True,
            max_tokens=max_tokens, timeout=timeout, max_retries=max_retries,
            session_id=session_id, purpose=f"{purpose}:retry", pricing=pricing,
        )
        try:
            return jsonx.as_dict(retry.content)
        except jsonx.JsonExtractionError as exc:
            raise AIError(
                f"El modelo no devolvió JSON utilizable: {exc}",
                retryable=False,
                provider=PROVIDER,
                user_message=(
                    "El modelo devolvió una respuesta que no se pudo interpretar. "
                    "Prueba con otro modelo en Ajustes."
                ),
            ) from exc


def ping(cfg: dict[str, Any], *, role: str = "live") -> dict[str, Any]:
    """Prueba de conexión para la pantalla de Ajustes."""
    try:
        creds = credentials(cfg, role)
    except ConfigError as exc:
        return {"ok": False, "detail": exc.user_message}
    try:
        reply = chat_text(
            "You are a health check. Reply with exactly: ok",
            "ping",
            temperature=0.0,
            max_tokens=8,
            timeout=45.0,
            max_retries=1,
            purpose="healthcheck",
            pricing=cfg.get("pricing"),
            **creds,
        )
    except (AIError, ConfigError) as exc:
        return {"ok": False, "detail": getattr(exc, "user_message", str(exc))}
    available = models.catalog(creds["base_url"], creds["api_key"])
    return {
        "ok": bool(reply.strip()),
        "detail": f"modelo {creds['model']}",
        "extra": {"model": creds["model"], "catalog": available[:40]},
    }
