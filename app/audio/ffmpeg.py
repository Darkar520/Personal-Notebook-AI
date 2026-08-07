"""Envoltorio de ffmpeg (binario embebido de `imageio-ffmpeg`).

Mejoras sobre el plan original (Task 6.1 / 7.2):

1. **Concatenación con el demuxer `concat` y `-c copy`** en vez de
   `-filter_complex concat=n=N`. El plan pasaba un `-i` por fichero: con los ~60 clips de
   un podcast o los ~140 chunks de una clase de 3,5 h la línea de comandos se acerca al
   límite de Windows (32 768 caracteres) y, además, `filter_complex` **recodifica** todo.
   Con el demuxer se pasa una lista en un fichero y se copian los flujos: segundos en vez
   de minutos, y sin pérdida de calidad.
2. **Errores visibles.** El plan enviaba `stderr` a `DEVNULL`, así que un fallo de ffmpeg
   solo se veía como `CalledProcessError` sin motivo. Aquí se captura y se registra.
3. **Sin ventanas negras en Windows** (`CREATE_NO_WINDOW`): cada llamada abría una consola
   sobre la clase de Zoom.
4. **Duración real** leída de ffmpeg, necesaria para la barra del reproductor.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_cached_exe: str | None = None


class FfmpegError(RuntimeError):
    """ffmpeg terminó con error; el mensaje incluye su salida."""


def ffmpeg_exe() -> str:
    """Ruta al binario de ffmpeg. Prefiere el embebido, cae al del sistema."""
    global _cached_exe
    if _cached_exe:
        return _cached_exe
    try:
        import imageio_ffmpeg

        _cached_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - instalación sin imageio-ffmpeg
        found = shutil.which("ffmpeg")
        if not found:
            raise FfmpegError(
                "No encuentro ffmpeg. Instala imageio-ffmpeg (pip install imageio-ffmpeg)."
            )
        _cached_exe = found
    return _cached_exe


def available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except FfmpegError:
        return False


def run(args: list[str], *, timeout: float = 1800.0) -> str:
    """Ejecuta ffmpeg con `-y -hide_banner`. Devuelve su salida de diagnóstico."""
    command = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "warning", *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffmpeg excedió {timeout:.0f}s") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()[-800:]
        log.error("ffmpeg falló (%s): %s", completed.returncode, message)
        raise FfmpegError(f"ffmpeg error {completed.returncode}: {message}")
    return completed.stderr or ""


def duration(path: Path | str) -> float:
    """Duración en segundos leyendo la cabecera con ffmpeg."""
    command = [ffmpeg_exe(), "-hide_banner", "-i", str(path)]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=120,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
        return 0.0
    match = _DURATION_RE.search(completed.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def concat_copy(parts: list[Path | str], dst: Path | str) -> Path:
    """Une ficheros del mismo formato sin recodificar (demuxer `concat`)."""
    files = [Path(p) for p in parts if Path(p).exists() and Path(p).stat().st_size > 0]
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not files:
        raise FfmpegError("No hay fragmentos de audio que unir")
    if len(files) == 1:
        shutil.copyfile(files[0], target)
        return target

    listing = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            listing = Path(handle.name)
            for file in files:
                # El demuxer concat exige comillas simples escapadas.
                safe = str(file.resolve()).replace("'", "'\\''")
                handle.write(f"file '{safe}'\n")
        run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(target)])
    except FfmpegError:
        # Formatos ligeramente distintos (bitrate/canales): recodificamos como respaldo.
        log.info("concat -c copy falló; reintento recodificando")
        run(["-f", "concat", "-safe", "0", "-i", str(listing), "-ac", "1", "-ar", "24000",
             "-b:a", "48k", str(target)])
    finally:
        if listing and listing.exists():
            listing.unlink(missing_ok=True)
    return target


def to_mp3(src: Path | str, dst: Path | str, *, bitrate_kbps: int = 48,
           sample_rate: int = 16000) -> Path:
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    run(["-i", str(src), "-ac", "1", "-ar", str(sample_rate), "-b:a",
         f"{int(bitrate_kbps)}k", str(target)])
    return target


def clip(src: Path | str, start_s: float, end_s: float, dst: Path | str, *,
         bitrate_kbps: int = 48) -> Path:
    """Recorta `[start, end)` reencodificando (corte exacto, no por keyframe)."""
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    length = max(0.2, float(end_s) - float(start_s))
    run(["-ss", f"{max(0.0, float(start_s)):.3f}", "-i", str(src), "-t", f"{length:.3f}",
         "-ac", "1", "-ar", "16000", "-b:a", f"{int(bitrate_kbps)}k", str(target)])
    return target


def encode_pcm_stream(
    blocks,
    dst: Path | str,
    *,
    sample_rate: int = 16000,
    bitrate_kbps: int = 48,
    timeout: float = 3600.0,
) -> Path:
    """Codifica a MP3 un flujo de PCM 16-bit mono recibido por `stdin`.

    Se usa para construir `session.mp3` a partir de los chunks WAV sin materializar un
    WAV intermedio de ~400 MB (una clase de 3,5 h). `blocks` es cualquier iterable de
    `bytes`.
    """
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "s16le", "-ar", str(int(sample_rate)), "-ac", "1", "-i", "pipe:0",
        "-b:a", f"{int(bitrate_kbps)}k", str(target),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        assert process.stdin is not None
        for block in blocks:
            if block:
                process.stdin.write(block)
        process.stdin.close()
    except BrokenPipeError:  # pragma: no cover - ffmpeg murió antes de tiempo
        pass
    try:
        _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise FfmpegError(f"ffmpeg excedió {timeout:.0f}s codificando el MP3") from exc
    if process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip()[-800:]
        raise FfmpegError(f"ffmpeg error {process.returncode}: {message}")
    return target


def silence_mp3(dst: Path | str, seconds: float = 0.35, *, sample_rate: int = 24000,
                bitrate_kbps: int = 48) -> Path:
    """Genera un silencio corto para separar réplicas del podcast."""
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
         "-t", f"{max(0.05, seconds):.2f}", "-b:a", f"{int(bitrate_kbps)}k", str(target)])
    return target
