"""Descubrimiento de dispositivos de audio (WASAPI loopback + micrófono).

El plan original elegía el loopback comparando `dev["name"] == default_speakers["name"]`.
Eso falla en la práctica: PyAudioWPatch nombra los loopback como
`"Altavoces (Realtek…) [Loopback]"`, así que la igualdad exacta no coincide nunca y la
grabación no arranca. Aquí se usa una cascada de estrategias:

1. `get_default_wasapi_loopback()` cuando la versión de PyAudioWPatch lo expone.
2. Coincidencia por prefijo/subcadena con el nombre del dispositivo de salida por defecto.
3. Cualquier dispositivo loopback disponible (con aviso).

Además se expone la lista completa para que Ajustes permita fijar el dispositivo a mano
(imprescindible si el usuario tiene varias salidas: monitor HDMI, auriculares USB…).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.errors import AudioDeviceError

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceInfo:
    index: int
    name: str
    channels: int
    sample_rate: int
    kind: str  # "loopback" | "input" | "output"
    is_default: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "kind": self.kind,
            "is_default": self.is_default,
        }


def _import_pyaudio():
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
    except ImportError as exc:  # pragma: no cover - depende de la plataforma
        raise AudioDeviceError(
            f"PyAudioWPatch no está instalado ({exc})",
            user_message=(
                "Falta PyAudioWPatch (captura de audio de Windows). "
                "Instálalo con: pip install PyAudioWPatch"
            ),
        ) from exc
    return pyaudio


def available() -> bool:
    """¿Se puede capturar audio del sistema en esta máquina?"""
    try:
        _import_pyaudio()
        return True
    except AudioDeviceError:
        return False


def _normalize(info: dict[str, Any], kind: str, *, is_default: bool = False) -> DeviceInfo:
    return DeviceInfo(
        index=int(info["index"]),
        name=str(info["name"]),
        channels=int(info.get("maxInputChannels") or info.get("maxOutputChannels") or 1),
        sample_rate=int(info.get("defaultSampleRate") or 48000),
        kind=kind,
        is_default=is_default,
        raw=info,
    )


def list_devices() -> dict[str, list[dict[str, Any]]]:
    """Inventario para la pantalla de Ajustes. Nunca lanza: devuelve listas vacías."""
    result: dict[str, list[dict[str, Any]]] = {"loopback": [], "input": [], "output": []}
    try:
        pyaudio = _import_pyaudio()
    except AudioDeviceError as exc:
        result["error"] = [{"detail": exc.user_message}]  # type: ignore[assignment]
        return result
    pa = pyaudio.PyAudio()
    try:
        default_out_name = ""
        default_in_index = -1
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out_name = str(
                pa.get_device_info_by_index(int(wasapi["defaultOutputDevice"]))["name"]
            )
        except Exception:  # pragma: no cover - host API ausente
            log.debug("No se pudo leer el host API WASAPI", exc_info=True)
        try:
            default_in_index = int(pa.get_default_input_device_info()["index"])
        except Exception:
            default_in_index = -1

        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("isLoopbackDevice"):
                dev = _normalize(
                    info, "loopback", is_default=_names_match(info["name"], default_out_name)
                )
                result["loopback"].append(dev.as_dict())
            elif int(info.get("maxInputChannels") or 0) > 0:
                dev = _normalize(info, "input", is_default=(i == default_in_index))
                result["input"].append(dev.as_dict())
            elif int(info.get("maxOutputChannels") or 0) > 0:
                dev = _normalize(
                    info, "output", is_default=_names_match(info["name"], default_out_name)
                )
                result["output"].append(dev.as_dict())
    finally:
        pa.terminate()
    return result


def _names_match(a: str, b: str) -> bool:
    """Compara nombres de dispositivo tolerando el sufijo `[Loopback]` y truncados."""
    if not a or not b:
        return False
    na, nb = _clean_name(a), _clean_name(b)
    if not na or not nb:
        return False
    return na == nb or na.startswith(nb) or nb.startswith(na) or na in nb or nb in na


def _clean_name(name: str) -> str:
    cleaned = name.replace("[Loopback]", "").strip().lower()
    # Windows trunca a 31 caracteres en algunos back-ends: comparamos por prefijo corto.
    return cleaned[:28]


def resolve_loopback(pa, pyaudio, preferred_index: int | None = None) -> DeviceInfo:
    """Elige el dispositivo loopback a grabar. Lanza `AudioDeviceError` si no hay."""
    if preferred_index is not None:
        try:
            info = pa.get_device_info_by_index(int(preferred_index))
            return _normalize(info, "loopback")
        except Exception as exc:
            raise AudioDeviceError(
                f"El dispositivo de audio {preferred_index} no existe: {exc}",
                user_message=(
                    "El dispositivo de captura configurado ya no está disponible. "
                    "Elige otro en Ajustes → Audio."
                ),
            ) from exc

    getter = getattr(pa, "get_default_wasapi_loopback", None)
    if callable(getter):
        try:
            info = getter()
            if info:
                return _normalize(info, "loopback", is_default=True)
        except Exception:  # pragma: no cover - versión sin soporte
            log.debug("get_default_wasapi_loopback falló", exc_info=True)

    default_out_name = ""
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out_name = str(
            pa.get_device_info_by_index(int(wasapi["defaultOutputDevice"]))["name"]
        )
    except Exception:  # pragma: no cover
        log.debug("Sin host API WASAPI", exc_info=True)

    candidates: list[dict[str, Any]] = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("isLoopbackDevice"):
            candidates.append(info)
    if not candidates:
        raise AudioDeviceError(
            "No hay dispositivos loopback WASAPI",
            user_message=(
                "Windows no expone ningún dispositivo de bucle. Comprueba que tengas "
                "unos altavoces o auriculares activos como salida predeterminada."
            ),
        )
    for info in candidates:
        if _names_match(str(info["name"]), default_out_name):
            return _normalize(info, "loopback", is_default=True)
    log.warning(
        "Ningún loopback coincide con la salida por defecto (%r); uso %r",
        default_out_name,
        candidates[0]["name"],
    )
    return _normalize(candidates[0], "loopback")


def resolve_microphone(pa, preferred_index: int | None = None) -> DeviceInfo | None:
    """Micrófono para la pista propia. Devuelve None si no hay ninguno usable."""
    try:
        if preferred_index is not None:
            info = pa.get_device_info_by_index(int(preferred_index))
        else:
            info = pa.get_default_input_device_info()
    except Exception:
        log.warning("Sin micrófono disponible; se graba solo el loopback")
        return None
    if int(info.get("maxInputChannels") or 0) <= 0 or info.get("isLoopbackDevice"):
        return None
    return _normalize(info, "input", is_default=preferred_index is None)
