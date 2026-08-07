"""Resumen en audio tipo podcast: guion con LLM + dos voces de edge-tts (Fase 7).

Mejoras sobre el plan original (Task 7.1/7.2):

1. **Síntesis concurrente con límite.** El plan sintetizaba línea a línea en serie: con
   60 réplicas son ~2 minutos de espera. Aquí van en paralelo con un semáforo de 4
   (edge-tts corta la conexión si se abusa), manteniendo el orden en el resultado.
2. **Reintentos por réplica.** edge-tts lanza `NoAudioReceived` de forma intermitente. El
   plan perdía el podcast completo; aquí se reintenta la réplica y, si no hay manera, se
   omite y se avisa, en lugar de tirar todo el trabajo.
3. **Concatenación con `-c copy` + silencios entre turnos** (ver `audio/ffmpeg.py`):
   sin recodificar, sin límite de longitud de línea de comandos, y con pausa natural.
4. **Guion saneado para TTS**: se quitan markdown, emojis, acotaciones y etiquetas de
   hablante que el motor leería en voz alta ("asterisco", "A dos puntos").
5. **Contenido real de la clase**: al modelo se le pasan puntos, vocabulario y roleplays,
   no solo los títulos de los temas (el plan mandaba `[{"title": ...}]`, con lo que el
   podcast salía genérico).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.ai import jsonx, opencode_client, prompts
from app.audio import ffmpeg
from app.errors import NotebookError

log = logging.getLogger(__name__)

VOICE_A = "en-US-AvaMultilingualNeural"
VOICE_B = "en-US-AndrewMultilingualNeural"
# Alternativas si el catálogo de edge-tts cambia (se prueban en orden).
VOICE_FALLBACKS_A = ["en-US-AvaNeural", "en-US-JennyNeural", "en-US-AriaNeural"]
VOICE_FALLBACKS_B = ["en-US-AndrewNeural", "en-US-ChristopherNeural", "en-US-GuyNeural"]
GAP_SECONDS = 0.32
MAX_PARALLEL_TTS = 4
TTS_RETRIES = 3
WORDS_PER_MINUTE = 150

_MD_RE = re.compile(r"[*_`#>\[\]]")
# Acotaciones que el TTS leería en voz alta: "(pausa)", "[music]", "[laughs]".
_STAGE_RE = re.compile(r"\((?:[^()]{0,80})\)|\[(?:[^\[\]]{0,80})\]")
_SPEAKER_TAG_RE = re.compile(r"^\s*(?:host\s*)?[AB]\s*[:\-–]\s*", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2190-\u21FF\u2B00-\u2BFF]"
)


class PodcastError(NotebookError):
    """No se pudo generar el resumen en audio."""


# ---------------------------------------------------------------------------
# Guion
# ---------------------------------------------------------------------------


def clean_line(text: str) -> str:
    """Deja el texto en algo que un TTS lea con naturalidad."""
    out = _SPEAKER_TAG_RE.sub("", str(text or ""))
    out = _STAGE_RE.sub(" ", out)
    out = _MD_RE.sub("", out)
    out = _EMOJI_RE.sub("", out)
    out = out.replace("&", " and ")
    return re.sub(r"\s+", " ", out).strip()


def normalize_lines(payload: Any) -> list[dict[str, str]]:
    items = jsonx.pick_list(payload, "lines", "script", "dialogue")
    lines: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            item = {"speaker": "A" if index % 2 == 0 else "B", "text": item}
        if not isinstance(item, dict):
            continue
        text = clean_line(jsonx.as_str(item.get("text") or item.get("line")))
        if not text:
            continue
        speaker = jsonx.as_str(item.get("speaker"), "A").strip().upper()[:1]
        if speaker not in ("A", "B"):
            speaker = "A" if index % 2 == 0 else "B"
        lines.append({"speaker": speaker, "text": text})
    return lines


def make_script(
    *,
    topics: list[dict[str, Any]],
    roleplays: list[dict[str, Any]] | None = None,
    session_title: str = "",
    minutes: int = 4,
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Guion de dos voces a partir del contenido real de la sesión."""
    payload = {
        "session_title": session_title,
        "topics": [
            {
                "title": t.get("title"),
                "points": t.get("points", [])[:6],
                "vocab": [v.get("word") for v in (t.get("vocab") or [])][:8],
                "phrases": [p.get("en") for p in (t.get("phrases") or [])][:4],
            }
            for t in topics[:12]
        ],
        "roleplays": [
            {"title": r.get("title"), "context": r.get("context_md") or r.get("context")}
            for r in (roleplays or [])[:4]
        ],
    }
    response = opencode_client.chat_json(
        prompts.podcast_script(minutes),
        opencode_client.fit_text(json.dumps(payload, ensure_ascii=False), max_tokens=16_000),
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.6,
        session_id=session_id,
        purpose="podcast_script",
        pricing=pricing,
    )
    lines = normalize_lines(response)
    if not lines:
        raise PodcastError(
            "El modelo no devolvió guion",
            user_message="No se pudo escribir el guion del podcast. Inténtalo otra vez.",
        )
    return lines


def script_text(lines: list[dict[str, str]]) -> str:
    return "\n".join(f"{line['speaker']}: {line['text']}" for line in lines)


def estimated_minutes(lines: list[dict[str, str]]) -> float:
    words = sum(len(line["text"].split()) for line in lines)
    return round(words / WORDS_PER_MINUTE, 1)


# ---------------------------------------------------------------------------
# Síntesis
# ---------------------------------------------------------------------------


async def _synthesize_line(
    text: str, voices: list[str], dst: Path, semaphore: asyncio.Semaphore
) -> Path | None:
    import edge_tts

    async with semaphore:
        for attempt in range(TTS_RETRIES):
            voice = voices[min(attempt, len(voices) - 1)]
            try:
                communicate = edge_tts.Communicate(text, voice, rate="+3%")
                await communicate.save(str(dst))
                if dst.exists() and dst.stat().st_size > 512:
                    return dst
            except Exception as exc:  # edge_tts.NoAudioReceived y errores de red
                log.warning("TTS falló (intento %d, voz %s): %s", attempt + 1, voice, exc)
            await asyncio.sleep(0.6 * (attempt + 1))
    log.error("Réplica omitida tras %d intentos: %r", TTS_RETRIES, text[:60])
    return None


async def render_podcast(
    lines: list[dict[str, str]],
    out_dir: Path,
    *,
    voice_a: str = VOICE_A,
    voice_b: str = VOICE_B,
) -> Path:
    """Sintetiza y une el podcast. Devuelve la ruta del MP3."""
    target_dir = Path(out_dir)
    work = target_dir / "parts"
    work.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(MAX_PARALLEL_TTS)
    voices_a = [voice_a, *VOICE_FALLBACKS_A]
    voices_b = [voice_b, *VOICE_FALLBACKS_B]

    tasks = [
        _synthesize_line(
            line["text"],
            voices_a if line["speaker"] == "A" else voices_b,
            work / f"line_{index:03d}.mp3",
            semaphore,
        )
        for index, line in enumerate(lines)
    ]
    rendered = await asyncio.gather(*tasks)
    parts = [path for path in rendered if path is not None]
    if not parts:
        raise PodcastError(
            "edge-tts no devolvió audio",
            user_message=(
                "No se pudo sintetizar la voz del podcast. Comprueba tu conexión "
                "(edge-tts necesita internet) y vuelve a intentarlo."
            ),
        )

    gap = ffmpeg.silence_mp3(work / "gap.mp3", GAP_SECONDS)
    sequence: list[Path] = []
    for index, part in enumerate(parts):
        if index:
            sequence.append(gap)
        sequence.append(part)

    output = target_dir / "podcast.mp3"
    ffmpeg.concat_copy(sequence, output)
    for file in work.glob("*.mp3"):
        file.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:  # pragma: no cover
        pass
    return output


def generate_podcast(
    *,
    topics: list[dict[str, Any]],
    out_dir: Path,
    roleplays: list[dict[str, Any]] | None = None,
    session_title: str = "",
    minutes: int = 4,
    model: str,
    base_url: str,
    api_key: str,
    session_id: int | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Guion + audio. Devuelve `{path, script, duration_sec, voice_a, voice_b}`."""
    lines = make_script(
        topics=topics, roleplays=roleplays, session_title=session_title, minutes=minutes,
        model=model, base_url=base_url, api_key=api_key, session_id=session_id,
        pricing=pricing,
    )
    path = asyncio.run(render_podcast(lines, Path(out_dir)))
    duration = ffmpeg.duration(path)
    return {
        "path": path,
        "script": script_text(lines),
        "duration_sec": duration or estimated_minutes(lines) * 60,
        "voice_a": VOICE_A,
        "voice_b": VOICE_B,
        "lines": lines,
    }


async def list_voices(prefix: str = "en-US") -> list[str]:  # pragma: no cover - red
    """Voces disponibles (para el selector de Ajustes)."""
    import edge_tts

    voices = await edge_tts.list_voices()
    return sorted(
        v["ShortName"] for v in voices if str(v.get("ShortName", "")).startswith(prefix)
    )
