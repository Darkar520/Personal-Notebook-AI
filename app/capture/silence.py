"""Detección de silencio y de voz (VAD por energía).

Dos usos en el producto:
1. **Recesos**: ≥45 s de silencio continuo → candidato a `timeline_events(kind='break')`.
2. **Máscara de micrófono**: los tramos en los que el usuario habló, que sirven para
   marcar `is_me` en los segmentos del transcript sin pagar diarización extra.

Mejoras sobre el plan original:
- **Perfil RMS vectorizado** (`stride_tricks`) en vez de un bucle Python por ventana:
  para 3,5 h son ~126 000 ventanas; el bucle tardaba segundos y bloqueaba el hilo.
- **Umbral adaptativo por defecto**: el plan fijaba `thr=0.01`, pero el nivel de una
  clase de Zoom depende del volumen del sistema. Aquí el umbral se deriva del propio
  audio (percentil bajo del RMS) con un piso absoluto, así funciona igual con audio
  bajito o alto.
- **Histéresis**: umbral de entrada y de salida distintos para no partir un silencio en
  trocitos cuando el ruido de fondo roza el umbral.
"""

from __future__ import annotations

import numpy as np

WINDOW_S = 0.2
FLOOR_THR = 0.004          # RMS por debajo de esto es silencio digital / ruido de sala
ADAPTIVE_MULTIPLIER = 3.0
LOW_PERCENTILE = 10.0      # estimación del ruido de fondo
HIGH_PERCENTILE = 90.0     # estimación del nivel de voz
SPEECH_FRACTION = 0.35     # el umbral nunca supera esta fracción del nivel de voz


def rms_profile(x: np.ndarray, sr: int, window_s: float = WINDOW_S) -> tuple[np.ndarray, float]:
    """RMS por ventana con 50 % de solape. Devuelve (perfil, segundos por paso)."""
    if x.size == 0 or sr <= 0:
        return np.zeros(0, dtype=np.float32), window_s / 2
    win = max(1, int(sr * window_s))
    step = max(1, win // 2)
    if x.size < win:
        value = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
        return np.array([value], dtype=np.float32), step / sr
    n = (x.size - win) // step + 1
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n, win), strides=(x.strides[0] * step, x.strides[0]), writeable=False
    )
    profile = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    return profile.astype(np.float32), step / sr


def adaptive_threshold(profile: np.ndarray) -> float:
    """Umbral derivado del propio audio, entre el ruido de fondo y el nivel de voz.

    Dos guardas imprescindibles, cada una para un caso real:

    - **Piso absoluto** (`FLOOR_THR`): con audio muy bajito, `p10 × 3` se queda pegado al
      ruido y ni un silencio limpio se detecta.
    - **Techo relativo al habla** (`p90 × SPEECH_FRACTION`): si el audio **no tiene**
      silencios (clase sin pausas, música de fondo), `p10 ≈ p90`, y multiplicar el
      percentil bajo daría un umbral *por encima* de la voz: se marcaría la clase entera
      como silencio y aparecería un "receso" de 3,5 horas. El techo lo impide.
    """
    if profile.size == 0:
        return FLOOR_THR
    low = float(np.percentile(profile, LOW_PERCENTILE))
    high = float(np.percentile(profile, HIGH_PERCENTILE))
    candidate = max(low * ADAPTIVE_MULTIPLIER, FLOOR_THR)
    ceiling = high * SPEECH_FRACTION
    if ceiling <= 0:
        return candidate
    return min(candidate, ceiling)


def _ranges_from_mask(mask: np.ndarray, step_s: float, min_len_s: float,
                      total_s: float) -> list[tuple[float, float]]:
    """Convierte una máscara booleana por ventana en rangos de tiempo."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    ranges: list[tuple[float, float]] = []
    for start_i, end_i in zip(edges[0::2], edges[1::2]):
        start = start_i * step_s
        end = min(total_s, end_i * step_s)
        if end - start >= min_len_s:
            ranges.append((round(float(start), 3), round(float(end), 3)))
    return ranges


def silence_ranges(
    x: np.ndarray,
    sr: int,
    min_silence_s: float = 1.0,
    thr: float | None = None,
    window_s: float = WINDOW_S,
) -> list[tuple[float, float]]:
    """Tramos `(inicio, fin)` en segundos con silencio continuo ≥ `min_silence_s`."""
    profile, step_s = rms_profile(x, sr, window_s)
    if profile.size == 0:
        return []
    enter = adaptive_threshold(profile) if thr is None else float(thr)
    exit_thr = enter * 1.5  # histéresis: hay que superar 1,5× para "romper" el silencio
    silent = np.empty(profile.size, dtype=bool)
    state = profile[0] < enter
    for i, value in enumerate(profile):
        if state:
            state = value < exit_thr
        else:
            state = value < enter
        silent[i] = state
    total_s = x.size / sr if sr else 0.0
    return _ranges_from_mask(silent, step_s, min_silence_s, total_s)


def voiced_ranges(
    x: np.ndarray,
    sr: int,
    min_voice_s: float = 0.35,
    thr: float | None = None,
    merge_gap_s: float = 0.4,
    window_s: float = WINDOW_S,
) -> list[tuple[float, float]]:
    """Tramos con voz. Usado sobre la pista de micrófono para saber cuándo hablé yo."""
    profile, step_s = rms_profile(x, sr, window_s)
    if profile.size == 0:
        return []
    enter = adaptive_threshold(profile) if thr is None else float(thr)
    voiced = profile >= enter
    total_s = x.size / sr if sr else 0.0
    ranges = _ranges_from_mask(voiced, step_s, min_voice_s, total_s)
    return merge_ranges(ranges, gap_s=merge_gap_s)


def segment_silences(x: np.ndarray, sr: int, min_break_s: float = 45.0,
                     thr: float | None = None) -> list[tuple[float, float]]:
    """Solo los silencios largos: candidatos a receso."""
    return silence_ranges(x, sr=sr, min_silence_s=min_break_s, thr=thr)


def merge_ranges(ranges: list[tuple[float, float]], gap_s: float = 1.0
                 ) -> list[tuple[float, float]]:
    """Une rangos separados por menos de `gap_s` (y los que se solapan)."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - out[-1][1] <= gap_s:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def shift_ranges(ranges: list[tuple[float, float]], offset: float
                 ) -> list[tuple[float, float]]:
    return [(round(a + offset, 3), round(b + offset, 3)) for a, b in ranges]


def overlap_fraction(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    """Fracción de `[start, end)` cubierta por `ranges` (0..1)."""
    span = max(0.0, end - start)
    if span <= 0 or not ranges:
        return 0.0
    covered = 0.0
    for a, b in ranges:
        lo, hi = max(start, a), min(end, b)
        if hi > lo:
            covered += hi - lo
    return min(1.0, covered / span)


def signal_level(x: np.ndarray) -> float:
    """RMS global; sirve para avisar 'el audio está casi en silencio'."""
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
