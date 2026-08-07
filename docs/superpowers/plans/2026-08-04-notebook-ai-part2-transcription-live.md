# Personal Notebook AI — Plan Parte 2: Transcripción + Estructuración en vivo

> Parte del plan master `2026-08-04-personal-notebook-ai.md`. Tareas T3.x (Fase 3), T4.x (Fase 4).

---

## Fase 3 — Transcripción con diarización (Deepgram)

### Task 3.1: Cliente Deepgram (pre-recorded)

**Files:**
- Create: `app/transcription/__init__.py`
- Create: `app/transcription/deepgram_client.py`
- Create: `tests/test_deepgram_client.py`

- [ ] **Step 1: test que falla (mock de httpx)**

```python
from pathlib import Path
import pytest

def test_transcribe_file_parses_words(monkeypatch, tmp_path):
    import app.transcription.deepgram_client as dg
    wav = tmp_path / "c.wav"
    payload = {
        "results": {"channels": [{"alternatives": [{
            "words": [
                {"start": 0.0, "end": 0.4, "speaker": 0, "word": "Hello"},
                {"start": 0.5, "end": 1.0, "speaker": 1, "word": "there"},
            ]}]}]},
    }
    calls = {}
    def fake_post(url, headers, params, json):  # noqa: ARG001
        calls["url"] = url
        calls["params"] = params
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return payload
        return R()
    monkeypatch.setattr(dg.httpx, "post", fake_post)
    out = dg.transcribe_file(wav, api_key="k-test", diarize=True)
    assert out[0]["word"] is True
    assert out[0]["text"] == "Hello "
    assert len(out) == 2
    assert calls["params"]["model"] == "nova-3"
    assert calls["params"]["diarize"] == "true"
```

- [ ] **Step 2: ver falla** — `pytest tests/test_deepgram_client.py -v` → ImportError.

- [ ] **Step 3: implementar `app/transcription/deepgram_client.py`**

```python
from pathlib import Path
import httpx
from app.transcription import AIError

DEEPGRAM_BASE = "https://api.deepgram.com/v1/listen"


def transcribe_file(path: Path, *, api_key: str, language: str = "en",
                    diarize: bool = True) -> list[dict]:
    if not api_key:
        raise AIError("LLave de Deepgram no configurada", retryable=False)
    r = httpx.post(
        DEEPGRAM_BASE,
        headers={"Authorization": f"Token {api_key}"},
        params={"model": "nova-3", "language": language,
                "diarize": "true" if diarize else "false",
                "punctuate": "true", "timestamps": "true"},
        content=path.read_bytes(),
        timeout=120.0,
    )
    if r.status_code >= 500:
        raise AIError(f"Deepgram 5xx: {r.status_code}", retryable=True)
    if r.status_code == 401:
        raise AIError("Deepgram: llave inválida", retryable=False)
    r.raise_for_status()
    words = r.json()["results"]["channels"][0]["alternatives"][0].get("words", [])
    return [
        {"start": float(w["start"]), "end": float(w["end"]),
         "speaker": int(w.get("speaker", 0)), "text": str(w["word"]) + " ", "word": True}
        for w in words
    ]
```

`app/transcription/__init__.py`:

```python
class AIError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_deepgram_client.py -v` → 1 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: cliente Deepgram pre-recorded con diarización"
```

### Task 3.2: Fusión de hablantes entre chunks

**Files:**
- Create: `app/transcription/fusion.py`
- Create: `tests/test_fusion.py`

- [ ] **Step 1: test que falla**

```python
from app.transcription.fusion import group_words, merge_speakers

def test_group_words_into_utterances():
    words = [
        {"start": 0.0, "end": 0.4, "speaker": 0, "text": "Hello ", "word": True},
        {"start": 0.5, "end": 1.0, "speaker": 0, "text": "world ", "word": True},
        {"start": 1.2, "end": 1.7, "speaker": 1, "text": "Hi ", "word": True},
    ]
    segs = group_words(words, gap_s=0.8)
    assert len(segs) == 2
    assert segs[0]["text"] == "Hello world "

def test_merge_speakers_sticky_by_index():
    chunk_a = [{"start": 0.0, "end": 5.0, "speaker": 0, "text": "A "},
               {"start": 5.0, "end": 9.0, "speaker": 1, "text": "B "}]
    chunk_b = [{"start": 0.0, "end": 4.0, "speaker": 0, "text": "C "},
               {"start": 4.0, "end": 8.0, "speaker": 1, "text": "D "}]
    global_segs, mapping = merge_speakers([chunk_a, chunk_b])
    # speaker 0 y 1 del chunk B mapean a los mismos globales del A (sticky)
    assert mapping[1][0] == mapping[0][0]  # chunk B sp0 == chunk A sp0
    assert mapping[1][1] == mapping[0][1]
    assert all(s["speaker_global"] in (0, 1) for s in global_segs)
```

- [ ] **Step 2: ver falla** — `pytest tests/test_fusion.py -v` → ImportError.

- [ ] **Step 3: implementar `app/transcription/fusion.py`**

```python
def group_words(words: list[dict], gap_s: float = 0.8) -> list[dict]:
    if not words:
        return []
    segs, cur = [], None
    for w in words:
        if cur is None:
            cur = {"start": w["start"], "end": w["end"], "speaker": w["speaker"],
                   "text": w["text"]}
        else:
            if w["start"] - cur["end"] <= gap_s and w["speaker"] == cur["speaker"]:
                cur["end"] = w["end"]
                cur["text"] += w["text"]
            else:
                segs.append(cur)
                cur = {"start": w["start"], "end": w["end"], "speaker": w["speaker"],
                       "text": w["text"]}
    if cur:
        segs.append(cur)
    return segs


def merge_speakers(chunks: list[list[dict]]) -> tuple[list[dict], dict]:
    """Fusiona utterances por chunk en segmentos globales.

    mapping: dict[chunk_index] -> dict[locaal_speaker -> global_speaker].
    Estrategia sticky-by-index: si un chunk tiene M hablantes y ya existen M globales,
    reutiliza índices; si aparecen más hablantes, crea nuevos.
    """
    global_segs: list[dict] = []
    mapping: dict[int, dict[int, int]] = {}
    next_global = 0
    seen_global: set[int] = set()
    for ci, utts in enumerate(chunks):
        m: dict[int, int] = {}
        for u in utts:
            if u["speaker"] not in m:
                if len(m) < len(seen_global) and len(mapping) > 0:
                    # reusar el índice libre de globales ya usados por este chunk
                    used = set(m.values())
                    free = [g for g in sorted(seen_global) if g not in used]
                    if free:
                        m[u["speaker"]] = free[0]
                    else:
                        m[u["speaker"]] = next_global
                        seen_global.add(next_global)
                        next_global += 1
                else:
                    m[u["speaker"]] = next_global
                    seen_global.add(next_global)
                    next_global += 1
            global_segs.append({"start": u["start"], "end": u["end"],
                                "speaker_global": m[u["speaker"]], "text": u["text"]})
        mapping[ci] = m
    return global_segs, mapping
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_fusion.py -v` → 2 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: fusión de hablantes entre chunks"
```

### Task 3.3: Cola persistente de transcripciones + worker

**Files:**
- Create: `app/transcription/queue.py`
- Modify: `app/pipeline.py` (usar cola en `poll_chunks`)
- Test: `tests/test_queue.py`

- [ ] **Step 1: test que falla**

```python
def test_enqueue_and_claim(tmp_path, monkeypatch):
    from app import db, pipeline
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    from app.transcription import queue
    monkeypatch.setattr(queue, "DB_PATH", db.DB_PATH)
    db.init_db()
    sid = pipeline.start_session()
    queue.enqueue(sid, "sessions/1/audio/chunk_000000.wav", 0.0)
    queue.enqueue(sid, "sessions/1/audio/chunk_000090.wav", 90.0)
    row = queue.claim_one()
    assert row["status"] == "claimed" or row["status"] == "pending"
    # claim_one devuelve y marca pending de forma aislada (script de prueba)
    assert row["start_t"] == 0.0
```

- [ ] **Step 2: implementar `app/transcription/queue.py`**

```python
import sqlite3
from pathlib import Path
from app.config import get_root

DB_PATH = get_root() / "data" / "app.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def enqueue(session_id: int, chunk_path: str, start_t: float) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO pending_transcriptions (session_id, chunk_path, start_t, status, created_at)"
        " VALUES (?,?,?,'pending', datetime('now'))",
        (session_id, chunk_path, start_t),
    )
    conn.commit()
    conn.close()


def claim_one() -> sqlite3.Row | None:
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM pending_transcriptions WHERE status='pending' ORDER BY start_t LIMIT 1"
    ).fetchone()
    if row:
        conn.execute("UPDATE pending_transcriptions SET status='claimed' WHERE id=?",
                     (row["id"],))
        conn.commit()
    conn.close()
    return row


def mark_ok(pending_id: int) -> None:
    conn = _conn()
    conn.execute("DELETE FROM pending_transcriptions WHERE id=?", (pending_id,))
    conn.commit()
    conn.close()


def mark_failed(pending_id: int, error: str) -> None:
    conn = _conn()
    conn.execute("UPDATE pending_transcriptions SET status='failed', retries=retries+1, error=?"
                 " WHERE id=?", (error, pending_id))
    conn.commit()
    conn.close()


def retry_failed(max_retries: int = 3) -> None:
    conn = _conn()
    conn.execute("UPDATE pending_transcriptions SET status='pending' WHERE status='failed'"
                 " AND retries < ?", (max_retries,))
    conn.commit()
    conn.close()
```

- [ ] **Step 3: ver pasar** — `pytest tests/test_queue.py -v` → 1 PASSED.

- [ ] **Step 4: worker que procesa un chunk (test con mock de Deepgram)**

`tests/test_queue_worker.py`:

```python
def test_worker_processes_chunk_sync(tmp_path, monkeypatch, sample_wav):
    from app import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    from app.transcription import queue
    monkeypatch.setattr(queue, "DB_PATH", db.DB_PATH)
    from app import pipeline
    monkeypatch.setattr(pipeline, "queue", queue)
    db.init_db()
    sid = pipeline.start_session()
    chunk = sample_wav  # fuente
    out_dir = tmp_path / "audio"
    out_dir.mkdir()
    from app.capture.loopback import FakeRecorder
    FakeRecorder(sample_wav)._w  # abre sin usar para fixture simple
    # encolamos el chunk y procesamos con transcripción simulada
    from app.transcription import worker
    monkeypatch.setattr(worker, "transcribe_file",
                        lambda p, **k: [{"start": 0.0, "end": 1.0, "speaker": 0,
                                         "text": "test audio ", "word": True}])
    queue.enqueue(sid, str(sample_wav), 0.0)
    processed, errors = worker.process_pending_once()
    assert processed == 1 and errors == 0
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0]
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM pending_transcriptions").fetchone()[0] == 0
```

`app/transcription/worker.py`:

```python
from typing import Any
from app.config import load_config
from app import db
from app.transcription import deepgram_client, fusion, queue
from app.ws import hub


def process_pending_once(config: dict | None = None) -> tuple[int, int]:
    cfg = config or load_config()
    processed, errors = 0, 0
    while True:
        row = queue.claim_one()
        if not row:
            break
        try:
            segments = process_chunk(row, cfg)
            insert_segments(row["session_id"], segments, str(row["chunk_path"]))
            queue.mark_ok(row["id"])
            processed += 1
        except deepgram_client.AIError as e:
            queue.mark_failed(row["id"], str(e))
            errors += 1
            if not e.retryable:
                break
    return processed, errors


def process_chunk(row, cfg: dict) -> list[dict]:
    words = deepgram_client.transcribe_file(
        __import__("pathlib").Path(row["chunk_path"]),
        api_key=cfg["deepgram"]["api_key"],
        language=cfg["deepgram"]["language"],
    )
    utts = fusion.group_words(words)
    # chunk aislado => 1 chunk: speaker_global = speaker local
    return [{"start": u["start"], "end": u["end"],
             "speaker_global": u["speaker"], "text": u["text"]} for u in utts]


def insert_segments(session_id: int, segments: list[dict], chunk_path: str) -> None:
    conn = db.get_conn()
    offset = float(__import__("pathlib").Path(chunk_path).stem.split("_")[1]) if "_" in \
        __import__("pathlib").Path(chunk_path).stem else 0.0
    for s in segments:
        conn.execute(
            "INSERT INTO transcript_segments (session_id, start_t, end_t, speaker_index, text)"
            " VALUES (?,?,?,?,?)",
            (session_id, offset + s["start"], offset + s["end"],
             s["speaker_global"], s["text"]),
        )
    conn.commit()
    conn.close()
    # broadcast (fire-and-forget; si el loop no es async se ignora el errror)
    try:
        import asyncio
        asyncio.ensure_future(hub.broadcast({"type": "segments", "session_id": session_id,
                                             "segments": segments}))
    except Exception:
        pass
```

- [ ] **Step 5: ver pasar** — `pytest tests/test_queue_worker.py -v`.

**Nota de offsets:** el nombre del chunk es `chunk_<start_s>.wav`; para chunks producidos por el `FakeRecorder`/loopback real, el start absoluto se pasa explícitamente en `enqueue(sid, path, start_t)`. El parseo del nombre es solo respaldo; el offset correcto lo da `start_t` de la fila. Ajustar `insert_segments` para recibir `offset` explícito:

```python
def process_pending_once(...):
    ...
    offset = row["start_t"]
    ...
def insert_segments(session_id, segments, offset): ...
```

- [ ] **Step 6: commit**

```bash
git add app tests
git commit -m "feat: cola persistente y worker de transcripción"
```

### Task 3.4: Integrar cola con loop + pestaña Transcripción en la SPA

**Files:**
- Modify: `app/main.py` (thread de loop de sesiones activas)
- Create: `app/routers/content.py` (GET transcript)
- Modify: `static/app.js` (pestaña Transcripción)
- Test: `tests/test_transcript_api.py`

- [ ] **Step 1: test de API transcript**

```python
def test_transcript_list(client):
    from app import db
    conn = db.get_conn()
    conn.execute("INSERT INTO transcript_segments (session_id, start_t, end_t, speaker_index, text)"
                 " VALUES (1,0,5,0,'Hello '),(1,6,9,1,'Hi ')")
    conn.commit()
    r = client.get("/api/sessions/1/transcript")
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert r.json()[0]["text"] == "Hello "
```

- [ ] **Step 2: `app/routers/content.py`**

```python
from fastapi import APIRouter
from app import db

router = APIRouter(prefix="/api/sessions/{sid}", tags=["content"])


@router.get("/transcript")
def list_transcript(sid: int):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, start_t, end_t, speaker_index, text FROM transcript_segments"
        " WHERE session_id=? ORDER BY start_t", (sid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

Register en `main.py`: `app.include_router(content.router)`.

- [ ] **Step 3: loop de procesado en `app/main.py` (lifespan)**

```python
import threading, time
from app.transcription import worker

_loop_stop = threading.Event()


def _session_loop():
    from app import db as _db
    while not _loop_stop.is_set():
        try:
            conn = _db.get_conn()
            rows = conn.execute("SELECT id FROM sessions WHERE status='recording'").fetchall()
            conn.close()
            if rows:
                worker.process_pending_once()
                worker.retry_failed()
        except Exception:
            pass
        _loop_stop.wait(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    t = threading.Thread(target=_session_loop, daemon=True)
    t.start()
    yield
    _loop_stop.set()
```

- [ ] **Step 4: SPA — pestaña Transcripción en `loadSessionDetail`** (fetch `/api/sessions/{id}/transcript`, agrupa por speaker y renderiza; autoscroll).

- [ ] **Step 5: ver pasar** — `pytest tests/test_transcript_api.py -v` → 1 PASSED.

- [ ] **Step 6: smoke manual** — iniciar sesión, reproducir audio del sistema 1 min → los segmentos aparecen en vivo en la pestaña.

- [ ] **Step 7: commit**

```bash
git add app static tests
git commit -m "feat: loop de transcripción y pestaña en vivo"
```

**Criterio de la Fase 3:** transcripción automaticada con diarización; en vivo via WebSocket (broadcast `segments`); recuperación sin-red (cola persistente + `retry_failed`).

### Task 3.5: Producer real (loopback) + fusión de hablantes entre chunks

Cierra el hueco entre la grabación y la cola. En producción `pipeline.start_session` arranca un hilo `LoopbackRecorder` (WASAPI loopback vía pyaudiowpatch) que rellena chunks de `chunk_seconds` como WAV 16 kHz mono `chunk_0000.wav...` en `audio/`, detectando también silencios ≥ `min_silence_s` para marcar recesos tempranamente. Cada chunk terminado se **encola** con `enqueue(sid, path, start_s, channel_text_stream=False)` (el broadcast del borrador lo dispara el hilo de glosario de Fase 4).

La **fusión entre chunks** reemplaza el cifrado por moción de la Task 3.3 (su worker quedaba "aislado por chunk"). Se mantiene un mapeo por sesión `local→global(número, display)` que persiste en `settings(speaker_map_<sid>)` como JSON; los `speaker_global` de nuevos chunks se asignan con sticky-by-index sobre ese mapeo, y se reutiliza el nombre `display` ya conocido hasta que el pase final (Fase 5) los renombre — así el índice del hablante y su color NO cambian a mitad de clase.

```python
# app/capture/loopback.py
import wave, threading
import numpy as np
import pyaudiowpatch as pyaudio

class LoopbackRecorder:
    def __init__(self, session_dir, chunk_seconds=90, min_silence_s=45, rate=16000):
        self.session_dir = session_dir
        self.chunk_seconds = chunk_seconds
        self.min_silence_s = min_silence_s
        self.rate = rate
        self._stop = threading.Event()
        self._thread = None

    def _pick_loopback(self, p):
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev["isLoopbackDevice"] and dev["name"] == default_speakers["name"]:
                return dev
        raise RuntimeError("No hay dispositivo en bucle (necesario: altavoces activos)")

    def close(self): self._stop.set()

    def run(self, on_chunk):
        # on_chunk(index, wav_path, start_s, silence_periods) — llamado por el hilo
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        try:
            dev = self._pick_loopback(p)
            rate = int(dev["defaultSampleRate"])
            stream = p.open(format=pyaudio.paInt16, channels=dev["maxInputChannels"],
                            rate=rate, input=True, frames_per_buffer=1024,
                            input_device_index=dev["index"])
            buf = b""; t0 = time.time(); idx = 0; last_voice = time.time()
            while not self._stop.is_set():
                data = stream.read(1024, exception_on_overflow=False)
                buf += data
                now = time.time()
                if now - t0 >= self.chunk_seconds:
                    path = self._write_chunk(p, dev, buf, idx, rate)
                    on_chunk(idx, path, t0, self._silences)
                    self._silences = []
                    buf = b""; idx += 1; t0 = now
            if buf:  # cola final (flujo cortado con Ctrl+Stop)
                self._write_chunk(p, dev, buf, idx, rate)
                on_chunk(idx, self._last_path, t0, [])
        finally:
            stream.close() if 'stream' in dir() else None
            p.terminate()
```

Regla de receso durante la captura: el hilo marca `self._silences.append((start_s, end_s))` cada vez que lleva ≥ `min_silence_s` sin audio; el producer los pasa a `pipeline` que los suma a `breaks` de la sesión (idempotente, dedupe por overlap [T2.3]).

- [ ] **Step 1: test (sin dispositivo — con FakeRecorder)**
```python
def test_producer_maps_loopback_onto_queue(tmp_path):
    from app import pipeline, worker
    sid = 101
    pipeline.enqueue_test_chunk = None
    got = []
    class FakeRec:
        def run(self, on_chunk):
            for i in range(2):
                on_chunk(i, tmp_path / f"chunk_000{i}.wav", i * 90, [])
    pipeline.LoopbackRecorder = FakeRec   # inyectable, real pyaudiowpatch no se prueba en CI
    sid2 = pipeline.start_session(capture_mode="loopback")  # arranca productor
    pipeline.stop_session(sid2)
    from app.ai.transcribe_queue import pending
    assert pending  # los chunks quedaron encolados
```
- [ ] **Step 2: implementación** — siguiendo el código de arriba, en `app/pipeline.py`:
  - `pipeline.pipeline_cfg["demo"] = "spawn_demo"` en `start_session` (solo para el modo demo ya existente) se mantiene; para `capture_mode="loopback"` se arranca `th = threading.Thread(target=rec.run, kwargs={"on_chunk": partial(self._on_chunk, sid)})`.
  - `_on_chunk(sid, idx, path, start_s, silences)`: `enqueue(sid, path, start_s, channel_text_stream=False)` + `breaks.extend(silences)`.
  - Thread daemon; en `stop_session` `rec.close()` + `th.join(timeout=3)`.
- [ ] **Step 3: fusión entre chunks** — en `app/ai/fusion.py` se añade:
```python
def speaker_map_load(sid):
    from app import db
    raw = settings_get(f"speaker_map_{sid}", "{}")
    return json.loads(raw)

def speaker_map_save(sid, mapping):  # {local_id: {"n": n, "display": name}}
    settings_set(f"speaker_map_{sid}", json.dumps(mapping))
```
Y el worker de la Task 3.3 deja de inventar `display` perdidos por chunk: al asignar `speaker_global` usa `sticky(sid, local_id)` que devuelve el `n`/`display` persistido, o crea el siguiente `n` global y lo guarda. El broadcast de segmentos del worker usa `asyncio.run(hub.broadcast(...))` (loop propio del hilo, seguro).
- [ ] **Step 4: pestaña en vivo** confirmada (la Task 3.4 ya la creó); se verifica con demo que el `display` de un hablante no cambia de chunk a chunk.
- [ ] **Step 5: commit** — `feat: producer loopback y fusión de hablantes entre chunks`.

---

## Fase 4 — Estructuración en vivo

### Task 4.1: Cliente OpenCode Go (LLM texto)

**Files:**
- Create: `app/ai/__init__.py`
- Create: `app/ai/opencode_client.py`
- Test: `tests/test_opencode_client.py`

- [ ] **Step 1: test que falla (mock httpx)**

```python
def test_chat_json_parses(monkeypatch):
    from app.ai import opencode_client as oc
    calls = {}
    def fake_post(url, headers, json, timeout):
        calls["json"] = json
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": '{"topics": []}'}}]}
        return R()
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    out = oc.chat_json(system="sys", user="usr", model="deepseek-v4-flash",
                       base_url="https://opencode.ai/zen/go/v1", api_key="k")
    assert out == {"topics": []}
    assert calls["json"]["model"] == "deepseek-v4-flash"
    assert calls["json"]["stream"] is False
```

- [ ] **Step 2: implementar `app/ai/opencode_client.py`**

```python
import json
import httpx
from app.transcription import AIError


def _post(base_url: str, api_key: str, model: str, system: str, user: str,
          temperature: float, json_schema: dict | None) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "stream": False,
    }
    if json_schema:
        payload["response_format"] = {
            "type": "json_object" if not json_schema.get("strict") else "json_schema",
            **({"schema": json_schema} if json_schema.get("strict") else {}),
        }
        payload["response_format"]["strict"] = False
    r = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"},
                   json=payload, timeout=180.0)
    if r.status_code >= 500 or r.status_code == 429:
        raise AIError(f"OpenCode {r.status_code}", retryable=True)
    if r.status_code == 401:
        raise AIError("OpenCode: llave inválida", retryable=False)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def chat_json(system: str, user: str, *, model: str, base_url: str, api_key: str,
              temperature: float = 0.3) -> dict:
    content = _post(base_url, api_key, model, system, user, temperature, None)
    return json.loads(content)


def chat_text(system: str, user: str, *, model: str, base_url: str, api_key: str,
              temperature: float = 0.3) -> str:
    return _post(base_url, api_key, model, system, user, temperature, None)
```

- [ ] **Step 3: ver pasar** — `pytest tests/test_opencode_client.py -v` → 1 PASSED.

- [ ] **Step 4: commit**

```bash
git add app tests
git commit -m "feat: cliente OpenCode Go (LLM texto)"
```

### Task 4.2: Integración incremental de estructura

**Files:**
- Create: `app/ai/live_integration.py`
- Modify: `app/routers/content.py` (GET/PUT topics draft)
- Test: `tests/test_live_integration.py`

- [ ] **Step 1: test que falla (LLM simulado vía monkeypatch de `chat_json`)**

```python
def test_integrate_returns_updated_structure(monkeypatch):
    from app.ai import live_integration as li
    new_strings = ["1.0 0 Teacher talks about help desk vocabulary.",
                   "30.0 1 Student asks what handle time means."]
    calls = {}
    def fake_chat_json(system, user, **kw):
        calls["user"] = user
        return {"topics": [
            {"title": "Help desk vocabulary", "points": ["terms & definitions"],
             "spanish_notes": ["definición de handle time"]}]}
    monkeypatch.setattr(li.opencode_client, "chat_json", fake_chat_json)
    out = li.integrate(current={"topics": []}, new_text=new_strings, model="deepseek-v4-flash",
                       base_url="b", api_key="k")
    assert out["topics"][0]["title"] == "Help desk vocabulary"
    assert "Help desk vocabulary" in calls["user"]
```

- [ ] **Step 2: implementar `app/ai/live_integration.py`**

```python
import json
from app.ai import opencode_client

SYSTEM = (
    "You are a bilingual meeting-notes synthesizer. Given (A) the current structure "
    "(JSON) of a class session and (B) new transcript lines 'time speaker text', update "
    "the structure. Rules: concisely integrate new points WITHOUT repeating; add new "
    "topics at the end; keep points in English; add a Spanish note ONLY for difficult "
    "concepts. Return the FULL updated structure JSON {\"topics\":[{"
    "\"title\":\"...\",\"points\":[\"...\"],\"spanish_notes\":[\"...\"]}]}."
)


def integrate(*, current: dict, new_text: list[str], model: str, base_url: str,
              api_key: str) -> dict:
    user = json.dumps({
        "current_structure": current,
        "new_transcript": "\n".join(new_text),
    }, ensure_ascii=False)
    return opencode_client.chat_json(SYSTEM, user, model=model,
                                     base_url=base_url, api_key=api_key)
```

- [ ] **Step 3: persistir borrador en `topics` (status='draft')** — `app/routers/content.py`:

```python
@router.get("/topics")
def list_topics(sid: int):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM topics WHERE session_id=? ORDER BY sort_order", (sid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.put("/topics/draft")
def replace_draft(sid: int, body: dict):
    topics = body["topics"]
    conn = db.get_conn()
    conn.execute("DELETE FROM topics WHERE session_id=? AND status='draft'", (sid,))
    for i, t in enumerate(topics):
        conn.execute(
            "INSERT INTO topics (session_id, sort_order, status, title, summary_md, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, i, "draft", t.get("title", ""),
             json.dumps({"points": t.get("points", []), "spanish_notes": t.get("spanish_notes", [])}),
             db.now_iso(), db.now_iso()),
        )
    conn.commit()
    conn.close()
    return db.get_conn and list_topics(sid)
```

- [ ] **Step 4: test de API borrador**

```python
def test_put_draft_topics(client):
    r = client.put("/api/sessions/1/topics/draft", json={
        "topics": [{"title": "A", "points": ["p1"], "spanish_notes": ["s1"]}]})
    assert r.status_code == 200
    assert r.json()[0]["status"] == "draft"
```

- [ ] **Step 5: ver pasar** — `pytest tests/test_live_integration.py tests/test_transcript_api.py -v`.

- [ ] **Step 6: commit**

```bash
git add app tests
git commit -m "feat: integración incremental y borrador de temas"
```

### Task 4.3: Disparador de integración en el loop + broadcast + UI borrador

**Files:**
- Modify: `app/main.py` (en `_session_loop`: integrar si debe)
- Create: `app/pipeline.py` (función `integration_due` + `run_integration`)
- Modify: `static/app.js` (pestaña "Notas (borrador)")

- [ ] **Step 1: implementar disparador en `app/pipeline.py`**

```python
from app.config import get_root, load_config

_last_integration: dict[int, float] = {}


def integration_due(sid: int, interval_sec: int) -> bool:
    import time
    now = time.monotonic()
    due = _last_integration.get(sid) is None or (now - _last_integration[sid]) >= interval_sec
    return due


def mark_integrated(sid: int) -> None:
    import time
    _last_integration[sid] = time.monotonic()


def run_integration(sid: int, cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    from app import db
    from app.ai import live_integration
    conn = db.get_conn()
    row = conn.execute("SELECT MAX(id) AS mi, MIN(start_t) AS min_t FROM transcript_segments WHERE session_id=?", (sid,)).fetchone()
    last_id = conn.execute("SELECT value FROM settings WHERE key='last_integrated_seg'").fetchone()
    from_id = int(last_id["value"]) if last_id else 0
    segs = conn.execute("SELECT start_t, speaker_index, text FROM transcript_segments"
                        " WHERE session_id=? AND id>? ORDER BY start_t", (sid, from_id)).fetchall()
    max_id = conn.execute("SELECT MAX(id) AS m FROM transcript_segments WHERE session_id=?", (sid,)).fetchone()["m"]
    if not segs:
        conn.close()
        return False
    lines = [f"{s['start_t']:.0f} {s['speaker_index']} {s['text'].strip()}" for s in segs]
    existing = conn.execute("SELECT summary_md, title, sort_order FROM topics WHERE session_id=? AND status='draft'"
                            " ORDER BY sort_order", (sid,)).fetchall()
    current = {"topics": [
        {"title": t["title"], "points": json.loads(t["summary_md"])["points"],
         "spanish_notes": json.loads(t["summary_md"]).get("spanish_notes", [])}
        for t in existing]}
    conn.close()
    try:
        structure = live_integration.integrate(
            current=current, new_text=lines,
            model=cfg["opencode"]["models"]["live"],
            base_url=cfg["opencode"]["base_url"],
            api_key=cfg["opencode"]["api_key"])
    except Exception:
        return False
    # persistir
    conn = db.get_conn()
    conn.execute("DELETE FROM topics WHERE session_id=? AND status='draft'", (sid,))
    for i, t in enumerate(structure.get("topics", [])):
        conn.execute(
            "INSERT INTO topics (session_id, sort_order, status, title, summary_md, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, i, "draft", t.get("title", ""),
             json.dumps({"points": t.get("points", []), "spanish_notes": t.get("spanish_notes", [])}),
             db.now_iso(), db.now_iso()))
    conn.execute("INSERT INTO settings(key, value) VALUES ('last_integrated_seg', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(max_id or 0),))
    conn.commit()
    conn.close()
    mark_integrated(sid)
    return list_topics(sid)
    # (el broadcast del borrador se hace en el caller de la Task 4.3)
```

> **Nota implementación:** importar `json` en la cabecera de `pipeline.py`; mover `_last_integration` fuera del módulo si hace falta reset por sesión al cerrar.

- [ ] **Step 2: llamar en `_session_loop` (Task 3.4, Step 3)**

```python
from app.config import load_config
from app import pipeline
cfg = None
...
if rows and pipeline.integration_due(row_sid, cfg["settings"]["integration_interval_sec"]):
    pipeline.run_integration(row_sid, cfg)
```

- [ ] **Step 3: UI borrador** — en `loadSessionDetail`, tras cargar transcripción, hacer `GET /api/sessions/{id}/topics` y renderizar tarjetas con `summary_md` (puntos + `spanish_notes` en un bloque destacado); refresco: suscribirse al WebSocket `ws://localhost:8787/ws` y en `structure` recargar.

```js
function connectWS(id) {
  const ws = new WebSocket(`ws://localhost:8787/ws`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "structure" && m.session_id === Number(id)) loadTopics(id);
    if (m.type === "segments" && m.session_id === Number(id)) loadTranscript(id);
  };
}
```

- [ ] **Step 4: verificar manual** — reproducir 30 min de audio que toque 3 temas → aparecen 3–5 temas vivos sin duplicados al pausar/retomar.

- [ ] **Step 5: commit**

```bash
git add app static tests
git commit -m "feat: disparo periódico de integración en vivo y UI de borrador"
```

**Criterio de la Fase 4:** en una clase simulada de 30 min, los temas aparecen en vivo, se integran sin duplicar, y la SPA los muestra con el WebSocket. Costo controlado (intervalo de 300 s por defecto).