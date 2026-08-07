"""Cliente de Deepgram (endpoint pre-recorded) con diarización.

Mejoras sobre el plan original (Task 3.1):

1. **`utterances=true` + `smart_format=true`.** El plan pedía solo `words` y reconstruía
   los turnos concatenando palabras con un umbral de silencio. Deepgram ya devuelve
   `utterances` (turnos por hablante, con puntuación y mayúsculas correctas); usarlas da
   un transcript legible sin heurísticas propias. El agrupado por palabras se conserva
   como respaldo para cuando el proveedor no las incluya.
2. **Reintentos con backoff exponencial + jitter** dentro del cliente. El plan dejaba el
   reintento al worker, que reencolaba y esperaba 15 s; en un corte de red de 30 s eso
   quemaba los 3 intentos y marcaba el chunk como fallido.
3. **Clasificación de errores** (401 llave, 402/403 cuota, 400 audio inválido, 429/5xx
   reintentable) con mensaje accionable para la UI.
4. **`metadata.duration`** para medir minutos reales facturados (estimador de costos).
5. **Envío por streaming del fichero** en vez de `path.read_bytes()`: un chunk son ~3 MB,
   pero al reprocesar una sesión completa el fichero puede pesar cientos de MB.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.errors import STTError

log = logging.getLogger(__name__)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
PROVIDER = "deepgram"
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class SttResult:
    """Resultado normalizado de una transcripción."""

    utterances: list[dict[str, Any]] = field(default_factory=list)
    words: list[dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    model: str = ""
    diarized: bool = True
    provider: str = PROVIDER

    @property
    def text(self) -> str:
        return " ".join(u["text"].strip() for u in self.utterances if u.get("text"))

    @property
    def speaker_count(self) -> int:
        return len({u.get("speaker", 0) for u in self.utterances})


def _params(model: str, language: str, diarize: bool) -> dict[str, str]:
    params = {
        "model": model,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "diarize": "true" if diarize else "false",
        "filler_words": "false",
    }
    # `multi` activa la detección multilingüe de nova-3 (clase en inglés con apoyos en
    # español); con un idioma concreto se fuerza ese idioma.
    if language:
        params["language"] = language
    return params


def _normalize_words(raw_words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for w in raw_words:
        text = str(w.get("punctuated_word") or w.get("word") or "").strip()
        if not text:
            continue
        out.append(
            {
                "start": float(w.get("start", 0.0)),
                "end": float(w.get("end", 0.0)),
                "speaker": int(w.get("speaker", 0) or 0),
                "text": text + " ",
                "confidence": float(w.get("confidence", 0.0) or 0.0),
                "word": True,
            }
        )
    return out


def parse_response(payload: dict[str, Any]) -> SttResult:
    """Convierte la respuesta cruda de Deepgram en `SttResult`."""
    metadata = payload.get("metadata") or {}
    results = payload.get("results") or {}
    channels = results.get("channels") or []
    alternative: dict[str, Any] = {}
    if channels:
        alternatives = channels[0].get("alternatives") or []
        if alternatives:
            alternative = alternatives[0]

    words = _normalize_words(alternative.get("words") or [])

    utterances: list[dict[str, Any]] = []
    for u in results.get("utterances") or []:
        text = str(u.get("transcript") or "").strip()
        if not text:
            continue
        utterances.append(
            {
                "start": float(u.get("start", 0.0)),
                "end": float(u.get("end", 0.0)),
                "speaker": int(u.get("speaker", 0) or 0),
                "text": text + " ",
                "confidence": float(u.get("confidence", 0.0) or 0.0),
            }
        )

    if not utterances and words:
        from app.transcription import fusion

        utterances = fusion.group_words(words)
    if not utterances:
        transcript = str(alternative.get("transcript") or "").strip()
        if transcript:
            duration = float(metadata.get("duration", 0.0) or 0.0)
            utterances = [
                {
                    "start": 0.0,
                    "end": duration,
                    "speaker": 0,
                    "text": transcript + " ",
                    "confidence": float(alternative.get("confidence", 0.0) or 0.0),
                }
            ]

    models = metadata.get("models") or []
    model_name = ""
    info = metadata.get("model_info") or {}
    if models and isinstance(info, dict):
        first = info.get(models[0]) or {}
        model_name = str(first.get("name") or "")

    return SttResult(
        utterances=utterances,
        words=words,
        duration=float(metadata.get("duration", 0.0) or 0.0),
        model=model_name,
        diarized=any(u.get("speaker", 0) for u in utterances) or len(utterances) > 0,
    )


def _raise_for_status(status: int, body: str) -> None:
    snippet = body[:300]
    if status == 401:
        raise STTError(
            f"Deepgram 401: {snippet}",
            retryable=False,
            status_code=status,
            provider=PROVIDER,
            user_message="La llave de Deepgram no es válida. Revísala en Ajustes.",
        )
    if status in (402, 403):
        raise STTError(
            f"Deepgram {status}: {snippet}",
            retryable=False,
            status_code=status,
            provider=PROVIDER,
            user_message=(
                "Deepgram rechazó la petición (crédito agotado o permiso denegado). "
                "Puedes cambiar a Whisper local en Ajustes → Transcripción."
            ),
        )
    if status == 400:
        raise STTError(
            f"Deepgram 400: {snippet}",
            retryable=False,
            status_code=status,
            provider=PROVIDER,
            user_message="Deepgram no pudo procesar este fragmento de audio.",
        )
    if status in _RETRY_STATUS:
        raise STTError(
            f"Deepgram {status}: {snippet}",
            retryable=True,
            status_code=status,
            provider=PROVIDER,
            user_message="Deepgram no responde ahora mismo; se reintentará.",
        )
    if status >= 400:
        raise STTError(
            f"Deepgram {status}: {snippet}",
            retryable=status >= 500,
            status_code=status,
            provider=PROVIDER,
        )


def transcribe_file(
    path: Path | str,
    *,
    api_key: str,
    language: str = "en",
    diarize: bool = True,
    model: str = "nova-3",
    timeout: float = 180.0,
    max_retries: int = 3,
    client: httpx.Client | None = None,
) -> SttResult:
    """Transcribe un WAV/MP3 local. Lanza `STTError` con `.retryable` informativo."""
    src = Path(path)
    if not api_key:
        raise STTError(
            "deepgram.api_key vacío",
            retryable=False,
            provider=PROVIDER,
            user_message="Falta la llave de Deepgram. Pégala en Ajustes.",
        )
    if not src.exists():
        raise STTError(
            f"Chunk inexistente: {src}",
            retryable=False,
            provider=PROVIDER,
            user_message="El fragmento de audio a transcribir ya no está en el disco.",
        )

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav" if src.suffix.lower() == ".wav" else "audio/mpeg",
    }
    params = _params(model, language, diarize)
    attempt = 0
    last_error: STTError | None = None

    while attempt < max(1, max_retries):
        attempt += 1
        try:
            response = _post(src, headers, params, timeout, client)
            _raise_for_status(response.status_code, response.text)
            result = parse_response(response.json())
            result.model = result.model or model
            return result
        except STTError as exc:
            last_error = exc
            if not exc.retryable or attempt >= max_retries:
                raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = STTError(
                f"Red: {exc}",
                retryable=True,
                provider=PROVIDER,
                user_message="Sin conexión con Deepgram; el audio queda en cola.",
            )
            if attempt >= max_retries:
                raise last_error from exc
        except ValueError as exc:  # JSON inválido
            raise STTError(
                f"Respuesta ilegible de Deepgram: {exc}",
                retryable=True,
                provider=PROVIDER,
            ) from exc
        delay = min(30.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.75)
        log.warning("Deepgram intento %d/%d falló; espero %.1fs", attempt, max_retries, delay)
        time.sleep(delay)

    raise last_error or STTError("Deepgram: fallo desconocido", provider=PROVIDER)


def _post(src: Path, headers: dict[str, str], params: dict[str, str], timeout: float,
          client: httpx.Client | None) -> httpx.Response:
    if client is not None:
        with src.open("rb") as fh:
            return client.post(DEEPGRAM_URL, headers=headers, params=params, content=fh,
                               timeout=timeout)
    with httpx.Client(timeout=timeout) as own, src.open("rb") as fh:
        return own.post(DEEPGRAM_URL, headers=headers, params=params, content=fh)


def check_credentials(api_key: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Valida la llave y devuelve saldo/proyectos si el plan lo permite."""
    if not api_key:
        return {"ok": False, "detail": "llave vacía"}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {api_key}"},
            )
        if r.status_code == 401:
            return {"ok": False, "detail": "llave inválida"}
        if r.status_code >= 400:
            return {"ok": False, "detail": f"HTTP {r.status_code}"}
        projects = (r.json() or {}).get("projects") or []
        extra: dict[str, Any] = {"projects": len(projects)}
        if projects:
            extra["project"] = projects[0].get("name", "")
            balance = _project_balance(api_key, str(projects[0].get("project_id", "")), timeout)
            if balance is not None:
                extra["balance_usd"] = balance
        return {"ok": True, "detail": "conectado", "extra": extra}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"sin conexión: {exc}"}


def _project_balance(api_key: str, project_id: str, timeout: float) -> float | None:
    if not project_id:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(
                f"https://api.deepgram.com/v1/projects/{project_id}/balances",
                headers={"Authorization": f"Token {api_key}"},
            )
        if r.status_code >= 400:
            return None
        balances = (r.json() or {}).get("balances") or []
        if not balances:
            return None
        return round(float(balances[0].get("amount", 0.0)), 2)
    except (httpx.HTTPError, ValueError, TypeError):
        return None
