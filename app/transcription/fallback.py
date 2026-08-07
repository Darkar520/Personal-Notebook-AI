"""Motores de transcripción alternativos: Whisper local y Gemini.

Se usan cuando Deepgram no está disponible (crédito agotado, llave revocada) o cuando el
usuario prefiere no enviar el audio a un servicio de pago. Ambos son dependencias
**opcionales**: no están en `requirements.txt` y se importan solo al usarse, de modo que
la app arranca sin ellos.

Mejora sobre el plan original (Task 10.2): el fallback de Gemini devolvía un único
segmento con `start=0, end=0`, lo que destruye la línea de tiempo y el playback. Aquí se
pide una respuesta estructurada con marcas de tiempo y etiqueta de hablante, y si el
modelo no la respeta se reparte el texto de forma proporcional sobre la duración real del
audio: peor que Deepgram, pero utilizable.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.capture import wavio
from app.errors import STTError
from app.transcription.deepgram_client import SttResult

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_PROMPT = (
    "Transcribe this class audio in English. Identify the speakers and return ONLY JSON: "
    '{"segments":[{"start":<seconds>,"end":<seconds>,"speaker":<int from 0>,'
    '"text":"..."}]}. Use one entry per speaker turn, keep the original wording, and give '
    "realistic timestamps in seconds from the beginning of the audio."
)


def transcribe_whisper(
    path: Path | str,
    *,
    model_size: str = "small",
    language: str = "en",
    compute_type: str = "int8",
) -> SttResult:
    """Whisper local (faster-whisper). Sin diarización: todo va al hablante 0."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise STTError(
            f"faster-whisper no está instalado: {exc}",
            retryable=False,
            provider="whisper",
            user_message=(
                "Para transcribir en local instala faster-whisper: "
                "pip install faster-whisper"
            ),
        ) from exc

    src = Path(path)
    model = WhisperModel(model_size, device="auto", compute_type=compute_type)
    segments, info = model.transcribe(str(src), language=language, vad_filter=True)
    utterances = []
    for s in segments:
        text = str(getattr(s, "text", "")).strip()
        if not text:
            continue
        utterances.append(
            {
                "start": float(getattr(s, "start", 0.0) or 0.0),
                "end": float(getattr(s, "end", 0.0) or 0.0),
                "speaker": 0,
                "text": text + " ",
                "confidence": 0.0,
            }
        )
    duration = float(getattr(info, "duration", 0.0) or 0.0) or wavio.wav_duration(src)
    return SttResult(
        utterances=utterances,
        words=[],
        duration=duration,
        model=f"whisper-{model_size}",
        diarized=False,
        provider="whisper",
    )


def transcribe_gemini(path: Path | str, *, api_key: str,
                      model: str = GEMINI_MODEL) -> SttResult:
    """Gemini como STT de emergencia. Diarización aproximada."""
    if not api_key:
        raise STTError(
            "gemini.api_key vacío",
            retryable=False,
            provider="gemini",
            user_message="Falta la llave de Gemini para usar el fallback de transcripción.",
        )
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        raise STTError(
            f"google-genai no está instalado: {exc}",
            retryable=False,
            provider="gemini",
            user_message="Para el fallback de Gemini instala: pip install google-genai",
        ) from exc

    src = Path(path)
    duration = wavio.wav_duration(src)
    mime = "audio/wav" if src.suffix.lower() == ".wav" else "audio/mpeg"
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=src.read_bytes(), mime_type=mime),
                GEMINI_PROMPT,
            ],
        )
        text = (getattr(response, "text", "") or "").strip()
    except Exception as exc:  # pragma: no cover - depende de la red
        raise STTError(
            f"Gemini falló: {exc}", retryable=True, provider="gemini"
        ) from exc

    utterances = _parse_gemini(text, duration)
    return SttResult(
        utterances=utterances,
        words=[],
        duration=duration,
        model=model,
        diarized=any(u["speaker"] for u in utterances),
        provider="gemini",
    )


def _parse_gemini(text: str, duration: float) -> list[dict]:
    payload = _extract_json(text)
    segments = (payload or {}).get("segments") or []
    utterances: list[dict] = []
    for s in segments:
        body = str(s.get("text", "")).strip()
        if not body:
            continue
        utterances.append(
            {
                "start": max(0.0, float(s.get("start", 0.0) or 0.0)),
                "end": max(0.0, float(s.get("end", 0.0) or 0.0)),
                "speaker": int(s.get("speaker", 0) or 0),
                "text": body + " ",
                "confidence": 0.0,
            }
        )
    if utterances:
        return _fix_timeline(utterances, duration)
    # El modelo devolvió prosa: repartimos las frases sobre la duración real.
    return _spread_plain_text(text, duration)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _fix_timeline(utterances: list[dict], duration: float) -> list[dict]:
    """Corrige tiempos incoherentes (fin ≤ inicio, saltos hacia atrás, desbordes)."""
    limit = duration if duration > 0 else max((u["end"] for u in utterances), default=0.0)
    cursor = 0.0
    for u in utterances:
        start = min(max(u["start"], cursor), limit) if limit else max(u["start"], cursor)
        end = u["end"] if u["end"] > start else start + max(1.0, len(u["text"]) / 15.0)
        if limit:
            end = min(end, limit)
        u["start"], u["end"] = round(start, 3), round(max(end, start + 0.2), 3)
        cursor = u["end"]
    return utterances


def _spread_plain_text(text: str, duration: float) -> list[dict]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if not sentences:
        return []
    total_chars = sum(len(s) for s in sentences) or 1
    span = duration if duration > 0 else total_chars / 15.0
    cursor = 0.0
    out = []
    for sentence in sentences:
        length = span * len(sentence) / total_chars
        out.append(
            {
                "start": round(cursor, 3),
                "end": round(cursor + length, 3),
                "speaker": 0,
                "text": sentence + " ",
                "confidence": 0.0,
            }
        )
        cursor += length
    return out
