"""Huella vocal ligera (solo numpy) para mantener la identidad de los hablantes.

**Hueco del plan original.** El plan fusionaba los hablantes entre chunks con una
estrategia *sticky-by-index*: "si el chunk N+1 tiene los mismos M hablantes, reutiliza
los índices". Eso falla de forma sistemática, porque Deepgram numera los hablantes
**por orden de aparición dentro de cada petición**: si en el chunk 3 habla primero la
teacher es `speaker 0`, y en el chunk 4 habla primero un compañero, ese compañero pasa a
ser `speaker 0`. Con sticky-by-index las etiquetas (y los colores, y los nombres ya
confirmados) se intercambian a mitad de clase.

Solución: comparar **cómo suena** cada hablante local con los hablantes globales ya
conocidos. El descriptor es un banco de filtros mel logarítmico resumido en media y
desviación típica por banda (80 dimensiones), normalizado. No es un embedding de
verificación de locutor de última generación, pero para 2–5 personas en una clase
distingue de sobra, cuesta milisegundos, no añade dependencias (nada de torch ni
speechbrain) y funciona sin GPU.

Bonus: el mismo descriptor se guarda en `people.voice_json`, así la segunda clase con la
misma teacher propone su nombre automáticamente (`auto_matched`).
"""

from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 16000
FRAME_LEN = 400          # 25 ms
FRAME_HOP = 160          # 10 ms
N_FFT = 512
N_MELS = 40
DIM = N_MELS * 2

# Umbrales calibrados sobre voces distintas del mismo canal (loopback de Zoom).
SAME_SPEAKER_MIN = 0.62      # por debajo, se asume que es otra persona
SAME_PERSON_MIN = 0.74       # más exigente para reutilizar una persona de otra sesión
MIN_SECONDS = 1.2            # menos audio que esto no da una huella fiable


def _hz_to_mel(hz: float) -> float:
    return 2595.0 * math.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: float) -> float:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(sr: int = SAMPLE_RATE, n_fft: int = N_FFT, n_mels: int = N_MELS,
                    fmin: float = 60.0, fmax: float = 7600.0) -> np.ndarray:
    fmax = min(fmax, sr / 2)
    n_bins = n_fft // 2 + 1
    mel_points = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    hz_points = np.array([_mel_to_hz(m) for m in mel_points])
    bins = np.floor((n_fft + 1) * hz_points / sr).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    fb = np.zeros((n_mels, n_bins), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        if center == left:
            center = min(left + 1, n_bins - 1)
        if right <= center:
            right = min(center + 1, n_bins - 1)
        for k in range(left, center):
            fb[m - 1, k] = (k - left) / max(1, center - left)
        for k in range(center, right):
            fb[m - 1, k] = (right - k) / max(1, right - center)
    return fb


_FILTERBANK = _mel_filterbank()
_WINDOW = np.hamming(FRAME_LEN).astype(np.float32)


def log_mel(samples: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Espectrograma log-mel `(n_frames, N_MELS)`. Devuelve vacío si hay poco audio."""
    x = np.asarray(samples, dtype=np.float32)
    if x.size < FRAME_LEN:
        return np.zeros((0, N_MELS), dtype=np.float32)
    n_frames = 1 + (x.size - FRAME_LEN) // FRAME_HOP
    frames = np.lib.stride_tricks.as_strided(
        x,
        shape=(n_frames, FRAME_LEN),
        strides=(x.strides[0] * FRAME_HOP, x.strides[0]),
        writeable=False,
    ) * _WINDOW
    spectrum = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)).astype(np.float32) ** 2
    mel = spectrum @ _FILTERBANK.T
    return np.log(mel + 1e-8).astype(np.float32)


SILENCE_RMS = 0.002      # por debajo no hay voz de la que sacar una huella


def embed(samples: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray | None:
    """Huella de `DIM` dimensiones; `None` si el audio es insuficiente o es silencio."""
    x = np.asarray(samples, dtype=np.float32)
    if x.size < MIN_SECONDS * sr:
        return None
    if float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) < SILENCE_RMS:
        # Silencio o ruido de sala: cualquier "huella" aquí sería basura y haría que dos
        # tramos callados se pareciesen entre sí más que dos frases de la misma persona.
        return None
    mel = log_mel(x, sr)
    if mel.shape[0] < 10:
        return None
    # Nos quedamos con los frames con energía (evita que el silencio domine la huella).
    energy = mel.mean(axis=1)
    voiced = mel[energy > np.percentile(energy, 40)]
    if voiced.shape[0] < 8:
        voiced = mel
    mean = voiced.mean(axis=0)
    std = voiced.std(axis=0)
    # Normalización por media cepstral: robustez frente al volumen del sistema.
    mean = mean - mean.mean()
    vector = np.concatenate([mean, std]).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-6:
        return None
    return (vector / norm).astype(np.float32)


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Coseno reescalado a 0..1 (las huellas ya vienen L2-normalizadas)."""
    if a is None or b is None:
        return 0.0
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.size != vb.size or va.size == 0:
        return 0.0
    cos = float(np.dot(va, vb))
    return max(0.0, min(1.0, (cos + 1.0) / 2.0))


def combine(base: np.ndarray | None, base_count: float,
            new: np.ndarray | None, new_count: float) -> np.ndarray | None:
    """Centroide incremental ponderado por segundos de habla."""
    if new is None:
        return base
    if base is None or base_count <= 0:
        return np.asarray(new, dtype=np.float32)
    total = base_count + new_count
    if total <= 0:
        return base
    merged = (np.asarray(base, dtype=np.float32) * base_count
              + np.asarray(new, dtype=np.float32) * new_count) / total
    norm = float(np.linalg.norm(merged))
    return (merged / norm).astype(np.float32) if norm > 1e-6 else base


def to_json(vector: np.ndarray | None, seconds: float = 0.0) -> dict | None:
    if vector is None:
        return None
    return {"v": [round(float(x), 5) for x in vector], "seconds": round(float(seconds), 2)}


def from_json(payload: dict | None) -> tuple[np.ndarray | None, float]:
    if not payload or not isinstance(payload, dict):
        return None, 0.0
    raw = payload.get("v")
    if not raw:
        return None, 0.0
    vector = np.asarray(raw, dtype=np.float32)
    if vector.size != DIM:
        return None, 0.0
    return vector, float(payload.get("seconds", 0.0) or 0.0)


def speaker_embeddings(
    samples: np.ndarray,
    utterances: list[dict],
    *,
    sr: int = SAMPLE_RATE,
    offset: float = 0.0,
) -> dict[int, tuple[np.ndarray | None, float]]:
    """Huella por hablante local a partir del audio del chunk.

    `offset` es el instante (en el fichero) que corresponde a `samples[0]`; las
    utterances traen tiempos relativos al fichero.
    """
    buckets: dict[int, list[np.ndarray]] = {}
    seconds: dict[int, float] = {}
    total = samples.size / sr if sr else 0.0
    for u in utterances:
        speaker = int(u.get("speaker", 0) or 0)
        start = max(0.0, float(u.get("start", 0.0)) - offset)
        end = min(total, float(u.get("end", 0.0)) - offset)
        if end - start < 0.25:
            continue
        chunk = samples[int(start * sr) : int(end * sr)]
        if chunk.size:
            buckets.setdefault(speaker, []).append(chunk)
            seconds[speaker] = seconds.get(speaker, 0.0) + (end - start)
    out: dict[int, tuple[np.ndarray | None, float]] = {}
    for speaker, parts in buckets.items():
        joined = np.concatenate(parts) if len(parts) > 1 else parts[0]
        out[speaker] = (embed(joined, sr), seconds.get(speaker, 0.0))
    return out
