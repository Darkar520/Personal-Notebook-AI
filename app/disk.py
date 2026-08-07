"""Vigilancia de espacio en disco.

Una clase de 3,5 h escribe ~400 MB de WAV crudo. Si el disco se llena a mitad de
sesión el grabador muere y se pierde la clase, así que el aviso tiene que llegar
**antes** (con margen) y una sola vez por transición de estado.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app import paths

# Bytes por segundo de audio PCM 16 kHz mono 16-bit.
BYTES_PER_SECOND = 16000 * 2


def free_space_mb(path: Path | str | None = None) -> int:
    target = Path(path) if path else paths.ensure_data_dir()
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(str(probe)).free // (1024 * 1024))
    except OSError:  # pragma: no cover - unidad desconectada
        return 0


def should_warn(free_mb: int, min_free_mb: int) -> bool:
    return free_mb <= max(0, int(min_free_mb))


def estimate_session_mb(hours: float = 3.5, *, tracks: int = 1) -> int:
    """MB de WAV crudo para una sesión (por pista de audio)."""
    return int(hours * 3600 * BYTES_PER_SECOND * max(1, tracks) / (1024 * 1024))


def remaining_recording_minutes(free_mb: int, *, tracks: int = 1) -> int:
    per_minute = BYTES_PER_SECOND * 60 * max(1, tracks) / (1024 * 1024)
    return int(free_mb / per_minute) if per_minute else 0
