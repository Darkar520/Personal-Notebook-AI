"""Fixtures compartidas.

Aislamiento por variables de entorno en vez de `monkeypatch.setattr(db, "DB_PATH", ...)`
como proponía el plan: `app.paths` resuelve las rutas en cada llamada leyendo
`NOTEBOOK_HOME` / `NOTEBOOK_DATA_DIR`, así que basta apuntarlas a `tmp_path` y **todo**
(base, audio, config, logs) queda dentro del test. Ningún módulo necesita conocer el
truco, y no hay riesgo de que un test escriba en los datos reales del usuario.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import db, paths


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Cada test trabaja sobre su propio `NOTEBOOK_HOME` y `NOTEBOOK_DATA_DIR`."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / "static").mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.setenv(paths.ENV_DATA, str(data))
    app_config.reset_cache()
    db.reset_init_cache()
    db.init_db()
    yield {"home": home, "data": data}
    app_config.reset_cache()
    db.reset_init_cache()


@pytest.fixture()
def config_file(isolated_env):
    """Escribe un `config.local.json` con llaves de prueba y devuelve la config."""

    def _write(**overrides):
        payload = {
            "opencode": {
                "api_key": "sk-test-0123456789abcdef",
                "base_url": "https://llm.test/v1",
            },
            "deepgram": {"api_key": "dg-test-0123456789abcdef"},
            # Los tests usan el disco real del equipo. Fijamos el mínimo en 0 para que
            # pipeline.start_session no falle por espacio cuando el disco está justo.
            "settings": {"min_free_space_mb": 0},
        }
        for section, values in overrides.items():
            payload.setdefault(section, {}).update(values)
        paths.config_path().write_text(json.dumps(payload), encoding="utf-8")
        app_config.reset_cache()
        return app_config.load_config(force=True)

    return _write


@pytest.fixture()
def client(isolated_env):
    from app.main import create_app

    with TestClient(create_app(serve_static=False)) as test_client:
        yield test_client


@pytest.fixture()
def no_supervisor(monkeypatch):
    """Desactiva el hilo de fondo cuando el test controla el flujo a mano."""
    from app import runtime

    monkeypatch.setattr(runtime.supervisor, "start", lambda: None)
    monkeypatch.setattr(runtime.supervisor, "stop", lambda **_: None)


# ---------------------------------------------------------------------------
# Audio sintético
# ---------------------------------------------------------------------------


def write_wav(path: Path, samples: np.ndarray, sr: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def two_speaker_signal(seconds: float = 300.0, sr: int = 16000, seed: int = 7) -> np.ndarray:
    """Señal con dos 'voces' (tonos + ruido) alternando y un silencio largo en medio.

    No es voz real, pero sirve para verificar troceado, VAD, huellas distinguibles y
    offsets, que es lo que se prueba sin red ni hardware.
    """
    rng = np.random.default_rng(seed)
    total = int(seconds * sr)
    signal = rng.normal(0, 0.0008, total).astype(np.float32)
    t = np.arange(total, dtype=np.float32) / sr
    turn = 12.0
    for index in range(int(seconds // turn)):
        start, end = int(index * turn * sr), int((index + 1) * turn * sr)
        base = 165.0 if index % 2 == 0 else 240.0
        segment = (
            0.28 * np.sin(2 * np.pi * base * t[start:end])
            + 0.16 * np.sin(2 * np.pi * base * 2.1 * t[start:end])
            + 0.07 * rng.normal(0, 1, end - start)
        )
        signal[start:end] += segment.astype(np.float32)
    # Receso: 60 s de silencio a partir del segundo 120 (si la señal es larga).
    if seconds > 200:
        signal[int(120 * sr) : int(180 * sr)] = rng.normal(
            0, 0.0005, int(60 * sr)
        ).astype(np.float32)
    return np.clip(signal, -1.0, 1.0).astype(np.float32)


@pytest.fixture()
def sample_wav(tmp_path) -> Path:
    return write_wav(tmp_path / "sample.wav", two_speaker_signal(300.0))


@pytest.fixture()
def short_wav(tmp_path) -> Path:
    return write_wav(tmp_path / "short.wav", two_speaker_signal(30.0, seed=11))


# ---------------------------------------------------------------------------
# Dobles de los proveedores
# ---------------------------------------------------------------------------


class FakeLlm:
    """Sustituye `opencode_client.complete`, registrando las llamadas."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, system, user, **kwargs):
        from app.ai.opencode_client import LlmResponse

        self.calls.append({"system": system, "user": user, **kwargs})
        content = self.replies.pop(0) if self.replies else "{}"
        return LlmResponse(content=content, model=kwargs.get("model", "fake"),
                           tokens_in=100, tokens_out=50)


@pytest.fixture()
def fake_llm(monkeypatch):
    def _install(*replies: str) -> FakeLlm:
        double = FakeLlm([r if isinstance(r, str) else json.dumps(r) for r in replies])
        from app.ai import opencode_client

        monkeypatch.setattr(opencode_client, "complete", double)
        monkeypatch.setattr(
            opencode_client, "credentials",
            lambda cfg, role="live": {
                "model": f"fake-{role}", "base_url": "https://llm.test/v1",
                "api_key": "sk-test",
            },
        )
        return double

    return _install


@pytest.fixture()
def fake_stt(monkeypatch):
    """Sustituye la transcripción por utterances deterministas por chunk."""

    def _install(utterances_per_chunk=None):
        from app.transcription import deepgram_client, worker

        default = [
            {"start": 1.0, "end": 5.0, "speaker": 0, "text": "Good morning everyone. ",
             "confidence": 0.9},
            {"start": 6.0, "end": 9.0, "speaker": 1, "text": "Hi teacher, ready. ",
             "confidence": 0.9},
        ]
        calls: list[Path] = []

        def fake_transcribe(row, cfg):
            from app.transcription import queue as stt_queue

            path = stt_queue.absolute_path(row)
            calls.append(path)
            index = int(dict(row).get("chunk_index", 0) or 0)
            data = (
                utterances_per_chunk(index)
                if callable(utterances_per_chunk)
                else (utterances_per_chunk or default)
            )
            return deepgram_client.SttResult(
                utterances=[dict(u) for u in data],
                words=[],
                duration=90.0,
                model="fake-stt",
                provider="deepgram",
            )

        monkeypatch.setattr(worker, "transcribe", fake_transcribe)
        return calls

    return _install
