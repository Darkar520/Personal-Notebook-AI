"""Lectura/escritura de WAV y remuestreo, sin dependencias externas (solo numpy).

Formato canónico interno: **float32 mono en [-1, 1]**. En disco se guarda PCM 16-bit
mono a 16 kHz, que es el formato nativo de Deepgram: 32 KB/s (≈2,9 MB por chunk de 90 s)
frente a 176 KB/s de 44,1 kHz estéreo.

Por qué remuestreo propio: `scipy` pesa ~40 MB y `librosa` arrastra numba. Para voz,
una decimación con promedio (paso bajo de media móvil) + interpolación lineal es
suficiente y evita 100 MB de dependencias en una app de escritorio.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 16000
INT16_SCALE = 32767.0


# ---------------------------------------------------------------------------
# Conversión de formatos
# ---------------------------------------------------------------------------


def pcm16_to_float(raw: bytes, channels: int = 1) -> np.ndarray:
    """bytes PCM 16-bit intercalado → float32 mono en [-1, 1]."""
    if not raw:
        return np.zeros(0, dtype=np.float32)
    data = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        usable = (len(data) // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return (data.astype(np.float32) / INT16_SCALE).astype(np.float32)


def float_to_pcm16(x: np.ndarray) -> bytes:
    clipped = np.clip(x, -1.0, 1.0)
    return (clipped * INT16_SCALE).astype("<i2").tobytes()


def resample(x: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    """Remuestrea mono float32. Decimación exacta cuando el ratio es entero."""
    if src_rate == dst_rate or x.size == 0:
        return x.astype(np.float32, copy=False)
    if src_rate > dst_rate and src_rate % dst_rate == 0:
        factor = src_rate // dst_rate
        usable = (x.size // factor) * factor
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        return x[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    if src_rate > dst_rate:
        # Paso bajo (media móvil) para no aliasear, luego interpolación lineal.
        width = max(2, int(math.ceil(src_rate / dst_rate)))
        kernel = np.ones(width, dtype=np.float32) / width
        x = np.convolve(x, kernel, mode="same").astype(np.float32)
    n_out = max(1, int(round(x.size * dst_rate / src_rate)))
    src_idx = np.linspace(0.0, x.size - 1, num=n_out, dtype=np.float64)
    return np.interp(src_idx, np.arange(x.size), x).astype(np.float32)


def mix(tracks: list[tuple[np.ndarray, float]], *, headroom: float = 0.9) -> np.ndarray:
    """Suma pistas (señal, ganancia) alineadas al largo de la primera."""
    if not tracks:
        return np.zeros(0, dtype=np.float32)
    length = tracks[0][0].size
    out = np.zeros(length, dtype=np.float32)
    for signal, gain in tracks:
        if signal.size == 0 or gain == 0:
            continue
        if signal.size < length:
            signal = np.pad(signal, (0, length - signal.size))
        out += signal[:length] * float(gain)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > headroom:
        out *= headroom / peak
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# Disco
# ---------------------------------------------------------------------------


def write_wav(path: Path | str, samples: np.ndarray, rate: int = TARGET_RATE) -> Path:
    """Escribe WAV mono 16-bit de forma atómica (tmp + replace)."""
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(float_to_pcm16(samples))
    tmp.replace(dst)
    return dst


def read_wav(path: Path | str, *, target_rate: int | None = TARGET_RATE) -> tuple[np.ndarray, int]:
    """Lee WAV (cualquier nº de canales, 8/16/32-bit PCM) → (float32 mono, rate)."""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = pcm16_to_float(raw, channels)
    elif width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
        if channels > 1:
            usable = (data.size // channels) * channels
            data = data[:usable].reshape(-1, channels).mean(axis=1)
        x = data.astype(np.float32)
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483647.0
        if channels > 1:
            usable = (data.size // channels) * channels
            data = data[:usable].reshape(-1, channels).mean(axis=1)
        x = data.astype(np.float32)
    else:  # pragma: no cover - formatos exóticos
        raise ValueError(f"Ancho de muestra no soportado: {width * 8} bits")
    if target_rate and rate != target_rate:
        x = resample(x, rate, target_rate)
        rate = target_rate
    return x, rate


def wav_duration(path: Path | str) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or TARGET_RATE
            return w.getnframes() / float(rate)
    except (wave.Error, OSError):
        return 0.0


def iter_wav_windows(
    path: Path | str, window_s: float = 90.0, *, target_rate: int = TARGET_RATE
):
    """Itera un WAV largo en ventanas sin cargarlo entero en RAM."""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames_per_window = max(1, int(window_s * rate))
        position = 0.0
        while True:
            raw = w.readframes(frames_per_window)
            if not raw:
                return
            if width != 2:  # pragma: no cover - el pipeline genera siempre 16-bit
                raise ValueError("iter_wav_windows solo soporta PCM 16-bit")
            x = pcm16_to_float(raw, channels)
            if rate != target_rate:
                x = resample(x, rate, target_rate)
            yield position, x
            position += x.size / target_rate
