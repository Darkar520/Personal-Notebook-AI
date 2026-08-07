"""Fusión de resultados de transcripción entre chunks.

Tres problemas que resuelve este módulo:

1. **Solape**: cada chunk incluye unos segundos del anterior para no cortar palabras.
   Hay que quedarse con cada turno una sola vez (`dedupe_overlap`), decidiendo por el
   punto medio del turno a qué chunk pertenece.
2. **Identidad de hablantes**: Deepgram numera los hablantes por orden de aparición en
   *cada* petición. `SpeakerRegistry` mantiene la correspondencia local→global de toda la
   sesión usando la huella vocal (ver `voiceprint.py`) con el índice como desempate.
3. **`is_me`**: la pista de micrófono dice exactamente cuándo habló el usuario; con esa
   máscara marcamos sus turnos sin depender de que la IA lo adivine.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from app import db
from app.capture import silence
from app.transcription import voiceprint

log = logging.getLogger(__name__)

SETTING_PREFIX = "speaker_map_"
MAX_SPEAKERS = 8
_VOICE_WEIGHT = 0.85
_INDEX_WEIGHT = 0.15
# Fracción del turno que debe coincidir con la máscara del micrófono para ser "yo".
IS_ME_MIN_OVERLAP = 0.45


# ---------------------------------------------------------------------------
# Agrupado de palabras (respaldo cuando el proveedor no da `utterances`)
# ---------------------------------------------------------------------------


def group_words(words: list[dict[str, Any]], gap_s: float = 0.8) -> list[dict[str, Any]]:
    """Agrupa palabras consecutivas del mismo hablante en turnos."""
    if not words:
        return []
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for w in words:
        speaker = int(w.get("speaker", 0) or 0)
        if (
            current is not None
            and speaker == current["speaker"]
            and float(w["start"]) - current["end"] <= gap_s
        ):
            current["end"] = float(w["end"])
            current["text"] += w["text"]
            current["_n"] += 1
            current["confidence"] += float(w.get("confidence", 0.0) or 0.0)
            continue
        if current is not None:
            segments.append(_close(current))
        current = {
            "start": float(w["start"]),
            "end": float(w["end"]),
            "speaker": speaker,
            "text": w["text"],
            "confidence": float(w.get("confidence", 0.0) or 0.0),
            "_n": 1,
        }
    if current is not None:
        segments.append(_close(current))
    return segments


def _close(segment: dict[str, Any]) -> dict[str, Any]:
    n = max(1, segment.pop("_n", 1))
    segment["confidence"] = round(segment["confidence"] / n, 4)
    return segment


# ---------------------------------------------------------------------------
# Solape entre chunks
# ---------------------------------------------------------------------------


def to_absolute(utterances: list[dict[str, Any]], file_start_t: float) -> list[dict[str, Any]]:
    """Traslada los tiempos del fichero a segundos absolutos de sesión."""
    out = []
    for u in utterances:
        item = dict(u)
        item["start"] = round(float(u["start"]) + file_start_t, 3)
        item["end"] = round(float(u["end"]) + file_start_t, 3)
        out.append(item)
    return out


EDGE_TOLERANCE_S = 0.35


def dedupe_overlap(
    utterances: list[dict[str, Any]],
    *,
    chunk_start_t: float,
    chunk_end_t: float | None = None,
    is_first: bool = False,
    is_last: bool = False,
) -> list[dict[str, Any]]:
    """Deja cada turno **una sola vez**, y completo, pese al solape entre chunks.

    Cada chunk *N* contiene `[start_t - solape, start_t + duración)`. Dos reglas:

    1. **Punto medio ≥ inicio útil**: lo que se oye en el pre-roll ya lo publicó el chunk
       anterior, así que aquí se descarta.
    2. **Fin pegado al corte → se descarta** (salvo en el último chunk): una frase cortada
       por el final del chunk aparecerá *completa* en el pre-roll del siguiente, y esa es
       la versión que queremos. Sin esta regla la frase del borde sale dos veces: mocha en
       el chunk *N* y entera en el *N+1*.
    """
    lower = float("-inf") if is_first else chunk_start_t
    edge = (
        None
        if is_last or chunk_end_t is None
        else float(chunk_end_t) - EDGE_TOLERANCE_S
    )
    kept = []
    for u in utterances:
        start, end = float(u["start"]), float(u["end"])
        if (start + end) / 2.0 < lower - 1e-6:
            continue
        if edge is not None and end >= edge:
            continue
        kept.append(u)
    return kept


# ---------------------------------------------------------------------------
# Registro de hablantes por sesión
# ---------------------------------------------------------------------------


class SpeakerRegistry:
    """Correspondencia estable `hablante local del chunk` → `hablante global`."""

    def __init__(self, session_id: int, state: dict[str, Any] | None = None) -> None:
        self.session_id = int(session_id)
        raw = state if state is not None else db.setting_get(self.key, {}) or {}
        self.next_index = int(raw.get("next", 0))
        self.speakers: dict[int, dict[str, Any]] = {}
        for key, value in (raw.get("speakers") or {}).items():
            vector, _ = voiceprint.from_json(value.get("voice"))
            self.speakers[int(key)] = {
                "seconds": float(value.get("seconds", 0.0) or 0.0),
                "voice": vector,
                "samples": int(value.get("samples", 0) or 0),
            }

    @property
    def key(self) -> str:
        return f"{SETTING_PREFIX}{self.session_id}"

    # -- persistencia ------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "next": self.next_index,
            "speakers": {
                str(index): {
                    "seconds": round(data["seconds"], 2),
                    "samples": data["samples"],
                    "voice": voiceprint.to_json(data["voice"], data["seconds"]),
                }
                for index, data in self.speakers.items()
            },
        }

    def save(self) -> None:
        db.setting_set(self.key, self.as_dict())

    @classmethod
    def clear(cls, session_id: int) -> None:
        db.setting_delete(f"{SETTING_PREFIX}{session_id}")

    # -- asignación --------------------------------------------------------
    def assign(
        self, local: dict[int, tuple[np.ndarray | None, float]]
    ) -> dict[int, int]:
        """Devuelve `{hablante_local: hablante_global}` y actualiza los centroides."""
        if not local:
            return {}
        mapping: dict[int, int] = {}
        taken: set[int] = set()

        # (puntuación para ordenar, parecido de voz, local, global)
        pairs: list[tuple[float, float, int, int]] = []
        for local_index, (vector, _seconds) in local.items():
            for global_index, data in self.speakers.items():
                sim = voiceprint.similarity(vector, data["voice"])
                index_prior = 1.0 if local_index == global_index else 0.0
                if vector is None or data["voice"] is None:
                    score = index_prior          # sin huella: continuidad por índice
                else:
                    score = _VOICE_WEIGHT * sim + _INDEX_WEIGHT * index_prior
                pairs.append((score, sim, local_index, global_index))
        pairs.sort(key=lambda item: (-item[0], item[2], item[3]))

        saturated = len(self.speakers) >= MAX_SPEAKERS
        for score, sim, local_index, global_index in pairs:
            if local_index in mapping or global_index in taken:
                continue
            vector = local[local_index][0]
            known = self.speakers[global_index]["voice"]
            if vector is not None and known is not None:
                # El parecido de voz decide; el índice solo ordena los empates. Si se
                # aplicara el umbral a la puntuación combinada, el bonus por índice
                # colaría voces distintas como si fueran la misma persona.
                if sim < voiceprint.SAME_SPEAKER_MIN and not saturated:
                    continue
            elif score <= 0.0 and not saturated:
                continue
            mapping[local_index] = global_index
            taken.add(global_index)

        for local_index in sorted(local, key=lambda i: -local[i][1]):
            if local_index in mapping:
                continue
            mapping[local_index] = self.next_index
            self.speakers[self.next_index] = {"seconds": 0.0, "voice": None, "samples": 0}
            taken.add(self.next_index)
            self.next_index += 1

        for local_index, global_index in mapping.items():
            vector, seconds = local[local_index]
            data = self.speakers.setdefault(
                global_index, {"seconds": 0.0, "voice": None, "samples": 0}
            )
            data["voice"] = voiceprint.combine(data["voice"], data["seconds"], vector, seconds)
            data["seconds"] += seconds
            data["samples"] += 1
        return mapping

    def talk_seconds(self) -> dict[int, float]:
        return {index: data["seconds"] for index, data in self.speakers.items()}

    def voice_json(self, global_index: int) -> dict | None:
        data = self.speakers.get(global_index)
        if not data:
            return None
        return voiceprint.to_json(data["voice"], data["seconds"])


# ---------------------------------------------------------------------------
# Marcado de la voz propia
# ---------------------------------------------------------------------------


def mark_is_me(
    utterances: list[dict[str, Any]], mic_ranges: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Marca `is_me` en los turnos que coinciden con la actividad del micrófono."""
    if not mic_ranges:
        for u in utterances:
            u.setdefault("is_me", False)
        return utterances
    merged = silence.merge_ranges([tuple(r) for r in mic_ranges], gap_s=0.6)
    for u in utterances:
        fraction = silence.overlap_fraction(float(u["start"]), float(u["end"]), merged)
        u["is_me"] = fraction >= IS_ME_MIN_OVERLAP
        u["mic_overlap"] = round(fraction, 3)
    return utterances


def dominant_me_speaker(utterances: list[dict[str, Any]]) -> int | None:
    """Hablante global con más segundos marcados como `is_me` (probablemente el usuario)."""
    scores: dict[int, float] = {}
    totals: dict[int, float] = {}
    for u in utterances:
        speaker = int(u.get("speaker_global", u.get("speaker", 0)) or 0)
        duration = max(0.0, float(u["end"]) - float(u["start"]))
        totals[speaker] = totals.get(speaker, 0.0) + duration
        if u.get("is_me"):
            scores[speaker] = scores.get(speaker, 0.0) + duration
    if not scores:
        return None
    best = max(scores, key=lambda s: scores[s])
    # Exigimos que sea mayoritariamente "yo" para no marcar a la teacher por eco.
    if totals.get(best, 0.0) > 0 and scores[best] / totals[best] >= 0.5:
        return best
    return None


# ---------------------------------------------------------------------------
# API histórica del plan
# ---------------------------------------------------------------------------


def merge_speakers(
    chunks: list[list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], dict[int, dict[int, int]]]:
    """Fusión sin audio disponible: continuidad por índice (contrato del plan, T3.2).

    Se mantiene por compatibilidad y para reprocesar transcripts antiguos. El camino
    real de la app usa `SpeakerRegistry`, que además compara la huella vocal.
    """
    segments: list[dict[str, Any]] = []
    mapping: dict[int, dict[int, int]] = {}
    known: dict[int, int] = {}
    next_global = 0
    for chunk_index, utterances in enumerate(chunks):
        local_map: dict[int, int] = {}
        for u in utterances:
            local = int(u.get("speaker", 0) or 0)
            if local not in local_map:
                if local in known:
                    local_map[local] = known[local]
                else:
                    local_map[local] = next_global
                    known[local] = next_global
                    next_global += 1
            segments.append(
                {
                    "start": float(u["start"]),
                    "end": float(u["end"]),
                    "speaker_global": local_map[local],
                    "text": u["text"],
                }
            )
        mapping[chunk_index] = local_map
    return segments, mapping
