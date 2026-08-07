"""Personal Notebook AI — backend local.

Un solo proceso: FastAPI (API + SPA + WebSocket) + hilos de captura, transcripción
y post-proceso. Todo el estado vive en `data/` (SQLite + audio).
"""

from __future__ import annotations

__version__ = "0.11.0"
__all__ = ["__version__"]
