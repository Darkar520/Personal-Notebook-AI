"""Resolución central de rutas del proyecto.

Regla de diseño: **ninguna** ruta se congela en una constante de módulo. Todas se
resuelven en el momento de la llamada leyendo `NOTEBOOK_HOME` / `NOTEBOOK_DATA_DIR`.
Así los tests aíslan datos con un `tmp_path` (sin monkeypatch frágil de constantes)
y el usuario puede mover `data/` a otro disco sin tocar código.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "NOTEBOOK_HOME"
ENV_DATA = "NOTEBOOK_DATA_DIR"


def project_root() -> Path:
    """Raíz del repo (donde viven app/, static/, config.local.json)."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """Carpeta de datos mutables: base de datos, audio, backups."""
    override = os.environ.get(ENV_DATA)
    base = Path(override).expanduser().resolve() if override else project_root() / "data"
    return base


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "app.db"


def config_path() -> Path:
    return project_root() / "config.local.json"


def config_example_path() -> Path:
    return project_root() / "config.local.example.json"


def static_dir() -> Path:
    return project_root() / "static"


def gadget_dir() -> Path:
    return project_root() / "gadget"


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def session_dir(session_id: int) -> Path:
    return sessions_dir() / str(session_id)


def session_audio_dir(session_id: int) -> Path:
    return session_dir(session_id) / "audio"


def token_path() -> Path:
    return data_dir() / "api.token"


def rel_to_data(path: Path | str) -> str:
    """Devuelve una ruta relativa a `data/` con separadores POSIX.

    Todas las rutas persistidas en la base son relativas: así mover o restaurar
    `data/` no invalida la base de datos.
    """
    p = Path(path)
    base = data_dir()
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def from_data(rel: str) -> Path:
    """Inverso de `rel_to_data`, con defensa contra path traversal."""
    base = data_dir().resolve()
    candidate = (base / rel).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Ruta fuera de data/: {rel!r}")
    return candidate
