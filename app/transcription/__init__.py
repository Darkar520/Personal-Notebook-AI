"""Transcripción con diarización: cliente Deepgram, fusión de hablantes y cola.

`AIError` / `STTError` se re-exportan desde `app.errors` para mantener el contrato de
importación del plan original (`from app.transcription import AIError`) sin duplicar la
jerarquía de excepciones.
"""

from __future__ import annotations

from app.errors import AIError, ProviderError, STTError

__all__ = ["AIError", "STTError", "ProviderError"]
