"""Construcción del audio final de la sesión y playback por tramo.

**Decisión de diseño distinta a la del plan (Task 6.1), y el motivo.**

El plan generaba, además de `session.mp3`, un clip MP3 por cada tramo de la línea de
tiempo (`seg_<i>.mp3`) para poder "reproducir ese intervalo". Eso implica:

- Ejecutar ffmpeg una vez por tramo (20-40 veces por clase, minutos de CPU).
- Duplicar en disco todo el audio de la sesión, otra vez, en trocitos.
- Que solo se pueda escuchar lo que la IA decidió cortar: pinchar una frase concreta del
  transcript (requisito del spec §8.2, pestaña "Transcripción": *click para saltar al
  audio*) sería imposible sin generar todavía más clips.

Aquí se sirve **un único `session.mp3` con soporte de HTTP Range** y el reproductor de la
SPA hace `currentTime = start` y para en `end`. Resultado: mismo comportamiento visible,
un solo ffmpeg, ~75 MB en vez de ~150 MB, y se puede saltar a *cualquier* instante
(timeline, tema, frase textual o turno del transcript). El corte a fichero se conserva
como función (`export_clip`) para descargar un fragmento, que es el único caso donde
tener un MP3 aparte aporta algo.

El otro detalle importante: los chunks se graban con solape (ver `capture/loopback.py`),
así que concatenarlos tal cual duplicaría audio y **desplazaría todos los timestamps**.
Aquí se recorta el pre-roll de cada chunk antes de unir, con lo que el segundo *N* del
MP3 es exactamente el segundo *N* de la sesión.
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app import db, paths
from app.audio import ffmpeg
from app.capture import wavio

log = logging.getLogger(__name__)

SESSION_MP3 = "session.mp3"
BITRATE_KBPS = 48
_READ_FRAMES = 16000 * 30   # 30 s por lectura


@dataclass(slots=True)
class ChunkRef:
    path: Path
    start_t: float
    overlap_pre: float
    duration: float


def list_chunks(session_id: int) -> list[ChunkRef]:
    """Chunks WAV de la sesión, ordenados, con su solape leído del sidecar/cola."""
    audio_dir = paths.session_audio_dir(session_id)
    if not audio_dir.exists():
        return []
    overlaps: dict[str, tuple[float, float, float]] = {}
    with db.read() as conn:
        rows = conn.execute(
            "SELECT chunk_path, start_t, overlap_pre, duration FROM pending_transcriptions"
            " WHERE session_id=?",
            (session_id,),
        ).fetchall()
    for row in rows:
        overlaps[Path(str(row["chunk_path"])).name] = (
            float(row["start_t"] or 0.0),
            float(row["overlap_pre"] or 0.0),
            float(row["duration"] or 0.0),
        )

    refs: list[ChunkRef] = []
    for wav in sorted(audio_dir.glob("chunk_*.wav")):
        from app.transcription import queue as stt_queue

        meta = stt_queue.read_meta(wav)
        start_t, overlap_pre, duration = overlaps.get(wav.name, (None, None, None))  # type: ignore[assignment]
        if start_t is None:
            start_t = float(meta.get("start_t", len(refs) * 90.0))
            overlap_pre = float(meta.get("overlap_pre", 0.0))
            duration = float(meta.get("duration", 0.0))
        refs.append(
            ChunkRef(
                path=wav,
                start_t=float(start_t),
                overlap_pre=float(overlap_pre or 0.0),
                duration=float(duration or wavio.wav_duration(wav)),
            )
        )
    refs.sort(key=lambda r: r.start_t)
    return refs


def _pcm_blocks(refs: list[ChunkRef]) -> Iterator[bytes]:
    """PCM continuo de la sesión, quitando el solape de cada chunk."""
    for ref in refs:
        try:
            with wave.open(str(ref.path), "rb") as handle:
                rate = handle.getframerate() or wavio.TARGET_RATE
                channels = handle.getnchannels()
                width = handle.getsampwidth()
                skip = int(ref.overlap_pre * rate)
                if skip:
                    handle.setpos(min(skip, handle.getnframes()))
                if rate == wavio.TARGET_RATE and channels == 1 and width == 2:
                    while True:
                        raw = handle.readframes(_READ_FRAMES)
                        if not raw:
                            break
                        yield raw
                else:  # pragma: no cover - el grabador siempre produce 16k mono 16-bit
                    raw = handle.readframes(handle.getnframes())
                    samples = wavio.pcm16_to_float(raw, channels)
                    yield wavio.float_to_pcm16(
                        wavio.resample(samples, rate, wavio.TARGET_RATE)
                    )
        except (wave.Error, OSError) as exc:
            log.warning("Chunk ilegible %s: %s", ref.path.name, exc)


def build_session_mp3(session_id: int, *, bitrate_kbps: int = BITRATE_KBPS
                      ) -> tuple[Path | None, float]:
    """Genera `data/sessions/<id>/session.mp3`. Devuelve `(ruta, duración)`."""
    refs = list_chunks(session_id)
    if not refs:
        log.info("Sesión %s sin chunks WAV: no se genera MP3", session_id)
        return None, 0.0
    target = paths.session_dir(session_id) / SESSION_MP3
    ffmpeg.encode_pcm_stream(
        _pcm_blocks(refs), target, sample_rate=wavio.TARGET_RATE,
        bitrate_kbps=bitrate_kbps,
    )
    seconds = ffmpeg.duration(target)
    log.info("session.mp3 de la sesión %s: %.1f s, %.1f MB",
             session_id, seconds, target.stat().st_size / 1e6)
    return target, seconds


def discard_raw_audio(session_id: int) -> int:
    """Borra los WAV crudos conservando los sidecars (`keep_raw_audio=false`)."""
    audio_dir = paths.session_audio_dir(session_id)
    if not audio_dir.exists():
        return 0
    removed = 0
    for wav in audio_dir.glob("chunk_*.wav"):
        try:
            wav.unlink()
            removed += 1
        except OSError:  # pragma: no cover
            log.warning("No se pudo borrar %s", wav)
    if removed:
        log.info("Sesión %s: %d WAV crudos borrados", session_id, removed)
    return removed


def session_mp3_path(session_id: int) -> Path | None:
    candidate = paths.session_dir(session_id) / SESSION_MP3
    return candidate if candidate.exists() else None


def export_clip(session_id: int, start_t: float, end_t: float, *,
                name: str | None = None) -> Path:
    """Extrae un fragmento a MP3 (para descargar/compartir un tramo concreto)."""
    source = session_mp3_path(session_id)
    if source is None:
        raise FileNotFoundError("La sesión no tiene audio generado")
    label = name or f"clip_{int(start_t)}_{int(end_t)}.mp3"
    target = paths.session_dir(session_id) / "clips" / label
    if target.exists():
        return target
    return ffmpeg.clip(source, start_t, end_t, target)
