"""Carga y guardado de la configuración local.

Diseño:
- `DEFAULTS` es la fuente de verdad del esquema; `config.local.json` solo lleva overrides.
- La lectura se cachea por `mtime`: el loop de sesión consulta la config cada pocos
  segundos y no tiene sentido releer/parsear el JSON cada vez, pero sí queremos que un
  cambio guardado desde Ajustes se aplique sin reiniciar.
- Los secretos nunca salen de aquí sin pasar por `public_config()`.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from app import paths
from app.errors import ConfigError

DEFAULTS: dict[str, Any] = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key": "",
        "models": {
            "live": "deepseek-v4-flash",
            "polish": "deepseek-v4-pro",
            "chat": "deepseek-v4-flash",
            "podcast": "deepseek-v4-flash",
            "study": "deepseek-v4-flash",
        },
        # Si el id exacto no existe en el catálogo del proveedor, se prueba esta lista
        # en orden (admite comodines tipo "*flash*"). Ver app/ai/models.py.
        "model_fallbacks": {
            "live": ["deepseek-v4-flash", "deepseek-v3.2-flash", "glm-5.2-air", "*flash*", "*mini*"],
            "polish": ["deepseek-v4-pro", "deepseek-v4-flash", "glm-5.2", "*pro*", "*flash*"],
            "chat": ["deepseek-v4-flash", "glm-5.2-air", "*flash*"],
            "podcast": ["deepseek-v4-flash", "*flash*"],
            "study": ["deepseek-v4-flash", "*flash*"],
        },
        "timeout_sec": 240,
        "max_retries": 3,
    },
    "deepgram": {
        "api_key": "",
        "model": "nova-3",
        "language": "en",
        "timeout_sec": 180,
        "max_retries": 3,
    },
    "gemini": {"api_key": ""},
    "audio": {
        "chunk_seconds": 90,
        "overlap_seconds": 8,
        "sample_rate": 16000,
        "output_device_index": None,
        "mic_device_index": None,
        "mic_gain": 1.0,
    },
    "settings": {
        "capture_mode": "loopback+mic",   # loopback | loopback+mic
        "keep_raw_audio": False,
        "notes_language": "bilingue_inteligente",
        "auto_generate_all": False,
        "integration_interval_sec": 300,
        "integration_min_words": 1200,
        "break_min_seconds": 45,
        "min_free_space_mb": 2048,
        "stt_backend": "deepgram",        # deepgram | whisper | gemini
        "stt_concurrency": 2,
        "podcast_minutes": 4,
        "log_level": "INFO",
        "onboarding_done": False,
        "legal_notice_seen": False,
    },
    "pricing": {
        "deepgram_usd_per_minute": 0.0058,
        "llm_usd_per_1m_input": 0.28,
        "llm_usd_per_1m_output": 0.42,
    },
    "server": {"host": "127.0.0.1", "port": 8787},
}

# Secciones editables desde la API de Ajustes.
EDITABLE_SECTIONS = ("opencode", "deepgram", "gemini", "audio", "settings", "pricing")
SECRET_FIELDS = (("opencode", "api_key"), ("deepgram", "api_key"), ("gemini", "api_key"))

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_stamp: tuple[str, float, int] | None = None


def deep_merge(base: dict, override: dict) -> dict:
    """Mezcla recursiva; las listas se reemplazan completas (no se concatenan)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _stamp(path: Path) -> tuple[str, float, int]:
    try:
        st = path.stat()
        return (str(path), st.st_mtime, st.st_size)
    except FileNotFoundError:
        return (str(path), 0.0, -1)


def load_config(*, force: bool = False) -> dict[str, Any]:
    """Devuelve la config efectiva (defaults + overrides del fichero)."""
    global _cache, _cache_stamp
    path = paths.config_path()
    stamp = _stamp(path)
    with _lock:
        if not force and _cache is not None and _cache_stamp == stamp:
            return copy.deepcopy(_cache)
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"config.local.json no es JSON válido: {exc}",
                    user_message="El archivo config.local.json tiene un error de formato JSON.",
                ) from exc
        raw.pop("_readme", None)
        cfg = deep_merge(DEFAULTS, raw)
        cfg = _apply_env_overrides(cfg)
        _cache, _cache_stamp = cfg, stamp
        return copy.deepcopy(cfg)


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """Permite inyectar llaves por variable de entorno (útil en CI y para no tocar disco)."""
    env_map = {
        "OPENCODE_API_KEY": ("opencode", "api_key"),
        "OPENCODE_BASE_URL": ("opencode", "base_url"),
        "DEEPGRAM_API_KEY": ("deepgram", "api_key"),
        "GEMINI_API_KEY": ("gemini", "api_key"),
    }
    for env_key, (section, field) in env_map.items():
        value = os.environ.get(env_key)
        if value:
            cfg[section][field] = value
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    """Escribe la config de forma atómica y con permisos restringidos."""
    global _cache, _cache_stamp
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {k: v for k, v in cfg.items() if k != "_readme"}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    _harden_permissions(path)
    with _lock:
        _cache, _cache_stamp = None, None
    return path


def _harden_permissions(path: Path) -> None:
    """Quita permisos de otros usuarios en lo posible (best effort, multiplataforma)."""
    try:
        if os.name == "nt":
            import subprocess

            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    check=False,
                    capture_output=True,
                )
        else:  # pragma: no cover - no es la plataforma objetivo
            path.chmod(0o600)
    except Exception:  # pragma: no cover - endurecer nunca debe romper el arranque
        pass


def update_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Aplica un patch parcial (solo secciones editables) y persiste."""
    cfg = load_config(force=True)
    for section in EDITABLE_SECTIONS:
        if section in patch and isinstance(patch[section], dict):
            cfg[section] = deep_merge(cfg.get(section, {}), patch[section])
    save_config(cfg)
    return load_config(force=True)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def public_config() -> dict[str, Any]:
    """Config apta para enviar al navegador: sin secretos en claro."""
    cfg = load_config()
    for section, field in SECRET_FIELDS:
        raw = cfg.get(section, {}).get(field, "")
        cfg[section][field] = ""
        cfg[section][f"{field}_masked"] = mask_secret(raw)
        cfg[section][f"{field}_set"] = bool(raw)
    return cfg


def require_opencode(cfg: dict[str, Any] | None = None) -> tuple[str, str]:
    cfg = cfg or load_config()
    key = cfg["opencode"]["api_key"]
    if not key:
        raise ConfigError(
            "opencode.api_key vacío",
            user_message="Falta la llave de OpenCode Go. Pégala en Ajustes.",
        )
    return cfg["opencode"]["base_url"], key


def require_deepgram(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    key = cfg["deepgram"]["api_key"]
    if not key:
        raise ConfigError(
            "deepgram.api_key vacío",
            user_message="Falta la llave de Deepgram. Pégala en Ajustes.",
        )
    return key


def reset_cache() -> None:
    """Invalida la caché (usado por los tests)."""
    global _cache, _cache_stamp
    with _lock:
        _cache, _cache_stamp = None, None
