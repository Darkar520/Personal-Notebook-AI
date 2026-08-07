"""Errores de dominio compartidos.

Se define aquí (y no en `app/transcription/__init__.py` como en el plan original)
para que ni el cliente LLM dependa del paquete de transcripción ni al revés.
"""

from __future__ import annotations


class NotebookError(Exception):
    """Base de todos los errores propios."""

    user_message: str = "Ocurrió un error inesperado."

    def __init__(self, message: str, *, user_message: str | None = None):
        super().__init__(message)
        if user_message:
            self.user_message = user_message
        else:
            self.user_message = message


class ProviderError(NotebookError):
    """Fallo llamando a un proveedor externo (LLM / STT / TTS)."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        provider: str = "",
        user_message: str | None = None,
    ):
        super().__init__(message, user_message=user_message)
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider

    def __str__(self) -> str:  # pragma: no cover - cosmético
        base = super().__str__()
        if self.provider:
            return f"[{self.provider}] {base}"
        return base


class AIError(ProviderError):
    """Fallo del proveedor de texto (OpenCode Go / compatible OpenAI)."""


class STTError(ProviderError):
    """Fallo del proveedor de transcripción."""


class ConfigError(NotebookError):
    """Configuración ausente o inválida (llave vacía, modelo desconocido…)."""

    def __init__(self, message: str, *, user_message: str | None = None):
        super().__init__(message, user_message=user_message)


class AudioDeviceError(NotebookError):
    """No se pudo abrir el dispositivo de audio de captura."""


class SessionStateError(NotebookError):
    """Transición de estado no permitida (p. ej. detener algo que no graba)."""
