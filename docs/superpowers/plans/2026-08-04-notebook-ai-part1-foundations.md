# Personal Notebook AI — Plan Parte 1: Cimientos, Cuadernos y Captura

> Parte del plan master `2026-08-04-personal-notebook-ai.md`. Tareas T0.x (Fase 0), T1.x (Fase 1), T2.x (Fase 2).

---

## Fase 0 — Cimientos

### Task 0.1: Scaffold del repo

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `config.local.example.json`
- Create: `app/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: crear `requirements.txt`**

```
fastapi>=0.115
uvicorn[standard]>=0.30
httpx>=0.27
pydantic>=2.7
numpy>=1.26
pywebview>=5.0
pyaudiowpatch>=0.2.13
edge-tts>=6.1
imageio-ffmpeg>=0.5
pytest>=8.0
pytest-asyncio>=0.23
```

- [ ] **Step 2: crear `.gitignore`**

```
__pycache__/
*.pyc
.venv/
config.local.json
data/
logs/
*.wav
*.mp3
.pytest_cache/
```

- [ ] **Step 3: crear `config.local.example.json`** (el usuario lo copia como `config.local.json`)

```json
{
  "opencode": {
    "base_url": "https://opencode.ai/zen/go/v1",
    "api_key": "",
    "models": {
      "live": "deepseek-v4-flash",
      "polish": "deepseek-v4-pro",
      "chat": "deepseek-v4-flash",
      "podcast": "deepseek-v4-flash"
    }
  },
  "deepgram": {
    "api_key": "",
    "language": "en"
  },
  "audio": {
    "chunk_seconds": 90,
    "device_index": null
  },
  "settings": {
    "capture_mode": "loopback",
    "keep_raw_audio": false,
    "notes_language": "bilingue_inteligente",
    "auto_generate_all": false,
    "integration_interval_sec": 300,
    "min_free_space_mb": 1024
  }
}
```

- [ ] **Step 4: crear `app/__init__.py` y `tests/__init__.py`** vacíos, y `tests/conftest.py`:

```python
import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    with TestClient(create_app()) as c:
        yield c
```

- [ ] **Step 5: instalar y verificar**

Run: `python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt`
Run: `python -c "import fastapi, httpx, numpy; print('ok')"`
Expected: `ok`

- [ ] **Step 6: commit**

```bash
git init
git add -A
git commit -m "chore: scaffold repo, venv y config sample"
```

### Task 0.2: Config loader

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: test que falla**

```python
import json
from app.config import load_config, save_config, CONFIG_PATH

def test_load_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "config.local.json")
    cfg = load_config()
    assert cfg["settings"]["capture_mode"] == "loopback"
    assert cfg["deepgram"]["api_key"] == ""

def test_load_and_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "config.local.json")
    cfg = load_config()
    cfg["opencode"]["api_key"] = "sk-test"
    save_config(cfg)
    saved = json.loads((tmp_path / "config.local.json").read_text())
    assert saved["opencode"]["api_key"] == "sk-test"
```

- [ ] **Step 2: ver falla** — `pytest tests/test_config.py -v` → ImportError.

- [ ] **Step 3: implementar `app/config.py`**

```python
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.local.json"

DEFAULTS = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key": "",
        "models": {"live": "deepseek-v4-flash", "polish": "deepseek-v4-pro",
                   "chat": "deepseek-v4-flash", "podcast": "deepseek-v4-flash"},
    },
    "deepgram": {"api_key": "", "language": "en"},
    "audio": {"chunk_seconds": 90, "device_index": None},
    "settings": {
        "capture_mode": "loopback",
        "keep_raw_audio": False,
        "notes_language": "bilingue_inteligente",
        "auto_generate_all": False,
        "integration_interval_sec": 300,
        "min_free_space_mb": 1024,
    },
}


def get_root() -> Path:
    return ROOT


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return deep_merge(DEFAULTS, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return copy.deepcopy(DEFAULTS)


def save_config(cfg: dict) -> Path:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return CONFIG_PATH
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_config.py -v` → 2 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: config loader con defaults y roundtrip"
```

### Task 0.3: DB init + schema

**Files:**
- Create: `app/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: test que falla**

```python
from app import db

def test_init_creates_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "topics", "timeline_events", "transcript_segments",
            "session_speakers", "people", "pending_transcriptions",
            "messages", "quiz_questions", "flashcards", "concept_maps",
            "audio_summaries", "roleplays", "settings"} <= tables

def test_next_session_number(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    conn = db.get_conn()
    conn.execute("INSERT INTO sessions (title, session_number, status, started_at)"
                 " VALUES ('a', 1, 'done', '2026-08-04T08:00:00Z')")
    conn.commit()
    assert db.next_session_number(conn) == 2
```

- [ ] **Step 2: ver falla** — `pytest tests/test_db.py -v` → ImportError.

- [ ] **Step 3: implementar `app/db.py`** (schema completo, ver spec §5):

```python
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT, session_number INTEGER,
  started_at TEXT, ended_at TEXT,
  status TEXT DEFAULT 'recording',
  duration_sec INTEGER, status_detail TEXT,
  audio_root TEXT, polish_model TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE, role TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS session_speakers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  speaker_index INTEGER, person_id INTEGER REFERENCES people(id),
  suggested_name TEXT, suggested_role TEXT,
  confirmed BOOLEAN DEFAULT 0, color TEXT
);
CREATE TABLE IF NOT EXISTS topics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  sort_order INTEGER, status TEXT DEFAULT 'draft',
  title TEXT, start_t REAL, end_t REAL,
  summary_md TEXT, vocab_json TEXT, phrases_json TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS timeline_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  sort_order INTEGER, kind TEXT, start_t REAL, end_t REAL,
  label TEXT, note_md TEXT, topic_id INTEGER
);
CREATE TABLE IF NOT EXISTS transcript_segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  start_t REAL, end_t REAL,
  speaker_index INTEGER, text TEXT, topic_id INTEGER
);
CREATE TABLE IF NOT EXISTS pending_transcriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER, chunk_path TEXT, start_t REAL,
  status TEXT DEFAULT 'pending', retries INTEGER DEFAULT 0,
  error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS quiz_questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  question TEXT, options_json TEXT, correct_index INTEGER,
  explanation TEXT, kind TEXT DEFAULT 'mc', created_at TEXT
);
CREATE TABLE IF NOT EXISTS flashcards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  front TEXT, back_md TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS concept_maps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  layout_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS audio_summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  script TEXT, voice_a TEXT, voice_b TEXT,
  file_path TEXT, duration_sec REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS roleplays(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  title TEXT, context_md TEXT, your_role TEXT,
  participants_json TEXT, key_phrases_json TEXT,
  feedback_md TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def _dir_for_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    _dir_for_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def next_session_number(conn: sqlite3.Connection) -> int:
    year = now_iso()[:4]
    row = conn.execute(
        "SELECT COALESCE(MAX(session_number), 0) AS m FROM sessions WHERE started_at LIKE ?",
        (year + "%",),
    ).fetchone()
    return int(row["m"]) + 1
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_db.py -v` → 2 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: sqlite schema y helpers"
```

### Task 0.4: FastAPI bootstrap + health + SPA placeholder

**Files:**
- Create: `app/main.py`
- Create: `app/schemas.py`
- Create: `app/ws.py`
- Modify: `tests/conftest.py` (ya creado)
- Create: `static/index.html`
- Create: `tests/test_health.py`

- [ ] **Step 1: test que falla**

```python
from fastapi.testclient import TestClient
from app.main import create_app

def test_health():
    r = TestClient(create_app()).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 2: ver falla** — `pytest tests/test_health.py -v` → ImportError.

- [ ] **Step 3: implementar**

`app/ws.py`:

```python
from typing import Any, Dict, Set
from starlette.websockets import WebSocket


class Hub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, event: Dict[str, Any]) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


hub = Hub()
```

`app/schemas.py`:

```python
from pydantic import BaseModel


class SessionOut(BaseModel):
    id: int
    title: str
    session_number: int
    started_at: str | None = None
    ended_at: str | None = None
    status: str
    duration_sec: int | None = None
    status_detail: str | None = None


class SessionCreate(BaseModel):
    title: str | None = None


def row_to_session(row) -> SessionOut:
    return SessionOut(
        id=row["id"], title=row["title"], session_number=row["session_number"],
        started_at=row["started_at"], ended_at=row["ended_at"],
        status=row["status"], duration_sec=row["duration_sec"],
        status_detail=row["status_detail"],
    )
```

`app/main.py`:

```python
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from app import db
from app.ws import hub

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await hub.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(ws)

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()
```

`static/index.html`:

```html
<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>Personal Notebook AI</title></head>
<body><h1>Personal Notebook AI</h1><p>Bootstrap OK</p></body></html>
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_health.py tests/test_db.py -v` → 3 PASSED (el fixture `client` revisa DB aislada).

- [ ] **Step 5: smoke manual** — `uvicorn app.main:app --port 8787`; abrir `/health` y `/`.

- [ ] **Step 6: commit**

```bash
git add app static tests
git commit -m "feat: FastAPI bootstrap, health, WS hub y SPA placeholder"
```

**Criterio de la Fase 0:** `pytest` en verde + `/health` responde 200.

---

## Fase 1 — Cuadernos

### Task 1.1: Router de sesiones (CRUD)

**Files:**
- Create: `app/routers/__init__.py`, `app/routers/sessions.py`
- Modify: `app/main.py`
- Test: `tests/test_sessions_api.py`

- [ ] **Step 1: test que falla**

```python
def test_create_list_patch_delete(client):
    r = client.post("/api/sessions", json={"title": "Mi clase"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.get("/api/sessions").json()[0]["session_number"] == 1
    assert client.patch(f"/api/sessions/{sid}", json={"title": "Renombrada"}).json()["title"] == "Renombrada"
    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.get("/api/sessions").json() == []

def test_get_missing_404(client):
    assert client.get("/api/sessions/999").status_code == 404
```

- [ ] **Step 2: ver falla** — `pytest tests/test_sessions_api.py -v` → 404.

- [ ] **Step 3: implementar `app/routers/sessions.py`**

```python
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException
from app import db
from app.schemas import SessionCreate, SessionOut, row_to_session

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
ROOT = Path(__file__).resolve().parents[2]


@router.post("", status_code=201, response_model=SessionOut)
def create_session(body: SessionCreate):
    conn = db.get_conn()
    now = db.now_iso()
    num = db.next_session_number(conn)
    title = body.title or f"Sesión {num}"
    cur = conn.execute(
        "INSERT INTO sessions (title, session_number, started_at, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (title, num, now, "recording", now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return row_to_session(row)


@router.get("", response_model=list[SessionOut])
def list_sessions():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
    conn.close()
    return [row_to_session(r) for r in rows]


@router.get("/{sid}", response_model=SessionOut)
def get_session(sid: int):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Sesión no encontrada")
    return row_to_session(row)


@router.patch("/{sid}", response_model=SessionOut)
def patch_session(sid: int, body: SessionCreate):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Sesión no encontrada")
    if body.title:
        conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                     (body.title, db.now_iso(), sid))
        conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row_to_session(row)


@router.delete("/{sid}", status_code=204)
def delete_session(sid: int):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Sesión no encontrada")
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    if row["audio_root"]:
        folder = ROOT / "data" / row["audio_root"]
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
```

Modify `app/main.py`:

```python
from app.routers import sessions
...
def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(sessions.router)
    ...
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_sessions_api.py -v` → 2 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: CRUD de cuadernos (sesiones)"
```

### Task 1.2: SPA — lista y detalle de cuadernos

**Files:**
- Modify: `static/index.html` (reescribir)
- Create: `static/app.css`
- Create: `static/app.js`

- [ ] **Step 1: `static/index.html`** (SPA real)

```html
<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Personal Notebook AI</title><link rel="stylesheet" href="/app.css"></head>
<body>
<header><h1>📚 Personal Notebook AI</h1>
  <nav><button data-view="sessions">Cuadernos</button>
  <button data-view="settings">Ajustes</button></nav></header>
<main id="view"></main>
<script src="/app.js"></script></body></html>
```

- [ ] **Step 2: `static/app.css`** — variables de tema, tarjetas y toolbar (color primario `#4f46e5`, fondo `#f8fafc`, tarjetas blancas con borde `#e5e7eb`, radius 12px, tipografía del sistema).

- [ ] **Step 3: `static/app.js`** — lista, hash routing y detalle mínimo

```js
const API = "/api";
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 204) return null;
  return res.json();
}
function el(html) { const t = document.createElement("template");
  t.innerHTML = html.trim(); return t.content.firstChild; }

async function loadSessions() {
  const sessions = await api("/sessions");
  const view = document.getElementById("view");
  view.innerHTML = "";
  view.append(el(`<div class="toolbar"><button id="new-session">+ Nueva sesión</button></div>`));
  document.getElementById("new-session").onclick = async () => {
    await api("/sessions", { method: "POST", body: "{}" });
    loadSessions();
  };
  sessions.forEach(s => {
    const card = el(`<article class="card" data-id="${s.id}">
      <h3>${s.title}</h3><p>#${s.session_number} · ${(s.started_at || "").slice(0, 10)} · ${s.status}</p></article>`);
    card.onclick = () => { window.location.hash = `#/session/${s.id}`; };
    view.append(card);
  });
}

async function loadSessionDetail(id) {
  const s = await api(`/sessions/${id}`);
  const view = document.getElementById("view");
  view.innerHTML = "";
  view.append(el(`<header class="detail">
    <h2>${s.title}</h2><p>${s.status}</p>
    <button id="back">← Volver</button></header>`));
  document.getElementById("back").onclick = () => { window.location.hash = ""; };
}

function routes() {
  const m = window.location.hash.match(/^#\/session\/(\d+)$/);
  if (m) loadSessionDetail(Number(m[1]));
  else loadSessions();
}
window.addEventListener("hashchange", routes);
window.addEventListener("DOMContentLoaded", routes);
```

- [ ] **Step 4: verificar manual** — abrir `http://localhost:8787`; crear, renombrar, eliminar cuadernos.

- [ ] **Step 5: commit**

```bash
git add static
git commit -m "feat: SPA lista y detalle de cuadernos"
```

**Criterio de la Fase 1:** CRUD completo desde la web + API con tests.

---

## Fase 2 — Captura de audio + gadget

### Task 2.1: VAD de silencio + loopback WASAPI

**Files:**
- Create: `app/capture/__init__.py`
- Create: `app/capture/silence.py`
- Test: `tests/test_silence.py`

- [ ] **Step 1: test que falla**

```python
import numpy as np
from app.capture.silence import silence_ranges, segment_silences

def test_silence_ranges_detects_gap():
    sr = 16000
    rng = np.random.default_rng(0)
    x = np.full(sr * 10, 0.001, dtype=np.float32)
    x[sr*2:sr*4] = rng.normal(0, 0.3, sr*2).astype(np.float32)
    ranges = silence_ranges(x, sr=sr, min_silence_s=1.0)
    assert any(r[1] <= 2.0 for r in ranges)
    assert any(r[0] >= 4.0 for r in ranges)

def test_segment_silences_only_long_gaps():
    sr = 16000
    x = np.full(sr*300, 0.3, dtype=np.float32)
    x[sr*100:sr*103] = 0.001
    assert segment_silences(x, sr=sr, min_break_s=45) == []
    x2 = np.full(sr*300, 0.3, dtype=np.float32)
    x2[sr*100:sr*200] = 0.001
    assert len(segment_silences(x2, sr=sr, min_break_s=45)) == 1
```

- [ ] **Step 2: ver falla** — `pytest tests/test_silence.py -v` → ImportError.

- [ ] **Step 3: implementar `app/capture/silence.py`**

```python
import numpy as np


def _rms_profile(x: np.ndarray, sr: int, window_s: float = 0.2):
    win = int(sr * window_s)
    step = max(1, win // 2)
    n = (len(x) - win) // step + 1
    rms = np.empty(n, dtype=np.float64)
    for i in range(n):
        seg = x[i*step:i*step+win].astype(np.float64)
        rms[i] = np.sqrt(np.mean(seg**2))
    return rms, step


def silence_ranges(x: np.ndarray, sr: int, min_silence_s: float = 1.0,
                   thr: float = 0.01, window_s: float = 0.2) -> list[tuple[float, float]]:
    rms, step = _rms_profile(x, sr, window_s)
    silent = rms < thr
    if not silent.any():
        return []
    ranges, start = [], None
    for i, is_sil in enumerate(silent):
        if is_sil and start is None:
            start = i * step / sr
        elif not is_sil and start is not None:
            if i * step / sr - start >= min_silence_s:
                ranges.append((start, i * step / sr))
            start = None
    if start is not None and len(x) / sr - start >= min_silence_s:
        ranges.append((start, float(len(x)) / sr))
    return ranges


def segment_silences(x: np.ndarray, sr: int, min_break_s: float = 45.0) -> list[tuple[float, float]]:
    return silence_ranges(x, sr=sr, min_silence_s=min_break_s)
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_silence.py -v` → 2 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: VAD de silencio y detección de recesos"
```

### Task 2.2: Loopback recoder (pyaudiowpatch) con modo prueba WAV

**Files:**
- Create: `app/capture/loopback.py`
- Create: `tests/fixtures.py`
- Create: `tests/test_loopback_fake.py`

- [ ] **Step 1: `tests/fixtures.py`** (WAV sintético compartido)

```python
import wave
import numpy as np
import pytest


def gen_wav(path, seconds=300, sr=16000, seed=3, active_db=0.25):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, active_db, int(sr * seconds)).astype(np.float32)
    x[:sr*5] = 0.001
    x[-sr*5:] = 0.001
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())


@pytest.fixture()
def sample_wav(tmp_path):
    p = tmp_path / "sample.wav"
    gen_wav(p, seconds=300)
    return p
```

- [ ] **Step 2: diseño de `app/capture/loopback.py`** — exponer una interfaz única con dos modos:

```python
class FakeRecorder:   # modo prueba: lee WAV y produce chunks 90s
    def __init__(self, wav_path, chunk_seconds=90):
        import wave
        self._w = wave.open(str(wav_path), "rb")
        self.sr = self._w.getframerate()
        self.total_s = self._w.getnframes() // self.sr
        self.chunk_seconds = chunk_seconds

    def iter_chunks(self):
        start = 0
        while start < self.total_s:
            end = min(start + self.chunk_seconds, self.total_s)
            yield start, end
            start = end

    def write_chunk(self, start, out_dir, end=None):
        end = end or min(start + self.chunk_seconds, self.total_s)
        self._w.setpos(start * self.sr)
        frames = self._w.readframes((end - start) * self.sr)
        out = out_dir / f"chunk_{start:06d}.wav"
        import wave
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.sr)
            w.writeframes(frames)
        return out
```

- [ ] **Step 3: test que falla** (modo prueba)

```python
def test_fake_recorder_writes_chunks(tmp_path, sample_wav):
    from app.capture.loopback import FakeRecorder
    rec = FakeRecorder(sample_wav, chunk_seconds=90)
    total, n = rec.total_s, 0
    for start, end in rec.iter_chunks():
        rec.write_chunk(start, tmp_path)
        n += 1
    assert n == 3
    assert total == 300
    assert len(list(tmp_path.glob("chunk_*.wav"))) == 3
```

- [ ] **Step 4: `LoopbackRecorder` (producción)** — clase que **usará** `pyaudiowpatch`:

```python
class LoopbackRecorder:
    """Graba el audio del dispositivo de salida (loopback WASAPI).

    Implementación con pyaudiowpatch:
      1. p = pyaudiowpatch.PyAudio()
      2. host = p.get_host_api_info_by_type(pyaudiowpatch.paWASAPI)
      3. devices = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
         loopback_devices = [d for d in devices if d.get("isLoopbackDevice")]
      4. elegir loopback correspondiente a defaultOutputDevice (si config.audio.device_index
         es None) o al índice configurado; abrir p.open(format=paInt16…, rate, channels,
         input=True, input_device_index=loopback_index, frames_per_buffer=1024).
      5. leer por bloques, decodificar np.int16→float32, promediar canales a mono,
         resample a 16000 (numpy interp si rate != 16000), acumular y partir en chunks de
         chunk_seconds guardando con wave módulo.
      6. atéminar el hilo en stop(); al final devolver self.chunks = [(start_s, path)].
    Para la Task 2.2 basta la interfaz + FakeRecorder (prueba de unidad); el cuerpo
    LoopbackRecorder se implementa en la Task 2.4 / Fase 3 (validado con audio real).
    """
    def __init__(self, chunk_seconds=90):
        self.chunk_seconds = chunk_seconds

    def start(self, out_dir):
        raise NotImplementedError("Fase 3: pyaudiowpatch real")

    def stop(self):
        return []

    def run_until(self, seconds):
        """Helper de prueba: itera match FakeRecorder para fixtures."""
        raise NotImplementedError
```

- [ ] **Step 5: ver pasar** — `pytest tests/test_loopback_fake.py -v` → 1 PASSED.

- [ ] **Step 6: commit**

```bash
git add app tests
git commit -m "feat: recorder con modo prueba WAV e interfaz loopback"
```

### Task 2.3: Pipeline grabación (start/stop/poll)

**Files:**
- Create: `app/pipeline.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: test que falla**

```python
def test_start_creates_recording_session(client):
    from app import pipeline
    sid = pipeline.start_session()
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["status"] == "recording"
    from app.config import get_root
    assert (get_root() / "data" / "sessions" / str(sid) / "audio").exists()
```

- [ ] **Step 2: implementar `app/pipeline.py`**

```python
from app import db

# Estado en memoria (por proceso) de la captura activa.
ACTIVE: dict = {}  # sid -> {"source_type": "wav"|"loopback", "path": ..., "chunks": [...]}


def start_session() -> int:
    conn = db.get_conn()
    now = db.now_iso()
    num = db.next_session_number(conn)
    cur = conn.execute(
        "INSERT INTO sessions (title, session_number, started_at, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"Sesión {num}", num, now, "recording", now, now),
    )
    conn.commit()
    sid = cur.lastrowid
    audio_root = f"sessions/{sid}"
    conn.execute("UPDATE sessions SET audio_root=? WHERE id=?", (audio_root, sid))
    conn.commit()
    conn.close()
    from app.config import get_root
    (get_root() / "data" / audio_root / "audio").mkdir(parents=True, exist_ok=True)
    return sid


def poll_chunks(sid: int) -> list[Path]:
    """Chunks WAV ya escritos en disco (producción o prueba)."""
    from pathlib import Path
    from app.config import get_root
    d = get_root() / "data" / "sessions" / str(sid) / "audio"
    return sorted(d.glob("chunk_*.wav")) if d.exists() else []


def stop_session(sid: int, discard: bool = False) -> None:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise KeyError(sid)
    if discard:
        conn.close()
        return
    conn.execute(
        "UPDATE sessions SET status='processing', ended_at=? WHERE id=?",
        (db.now_iso(), sid),
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 3: ver pasar** — `pytest tests/test_pipeline.py -v` → 1 PASSED.

- [ ] **Step 4: commit**

```bash
git add app tests
git commit -m "feat: pipeline start/stop de sesión"
```

### Task 2.4: Gadget flotante + run.py

**Files:**
- Create: `gadget/__init__.py`, `gadget/gadget_app.py`, `gadget/gadget.html`, `gadget/gadget.css`, `gadget/gadget.js`
- Create: `run.py`
- Modify: `app/routers/sessions.py` (endpoints `start`/`stop`)
- Test: `tests/test_start_stop_api.py`

- [ ] **Step 1: test que falla (API start/stop)**

```python
def test_start_and_stop_api(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 200 and r.json()["status"] == "recording"
    r2 = client.post(f"/api/sessions/{sid}/stop")
    assert r2.status_code == 200 and r2.json()["status"] == "processing"
```

- [ ] **Step 2: exponer start/stop en `app/routers/sessions.py`**

```python
from app.pipeline import stop_session as _stop_pipeline

@router.post("/{sid}/start", response_model=SessionOut)
def start_capture(sid: int):
    # El hilo de grabación real se arranca con start_session() (Fase 3, Task 3.5);
    # este endpoint solo garantiza el estado 'recording' que usa el gadget.
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Sesión no encontrada")
    conn.execute("UPDATE sessions SET status='recording', updated_at=? WHERE id=?", (db.now_iso(), sid))
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row_to_session(row)


@router.post("/{sid}/stop", response_model=SessionOut)
def stop_capture(sid: int):
    _stop_pipeline(sid)
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row_to_session(row)
```

- [ ] **Step 3: `gadget/gadget.html`** (burbuja + menú)

```html
<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<link rel="stylesheet" href="/gadget.css"></head>
<body>
<div id="bubble" class="idle"></div>
<div id="menu" class="hidden">
  <button id="btn-main">▶ Iniciar sesión</button>
  <button id="btn-web">🌐 Abrir plataforma</button>
  <button id="btn-quit">🧹 Quitar gadget</button>
</div>
<div id="timer" class="hidden">0:00:00</div>
<script src="/gadget.js"></script></body></html>
```

- [ ] **Step 4: `gadget/gadget.css`** — burbuja 46px circular, sombra, drag como move (colores: `idle` gris, `recording` verde pulsante con keyframes, `processing` naranja, `error` rojo).

- [ ] **Step 5: `gadget/gadget.js`**

```js
let state = "idle";
const idleSid = () => null;
async function post(path, body) {
  return fetch("http://localhost:8787" + path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) });
}
function setState(s) {
  state = s;
  document.getElementById("bubble").className = s;
  const btn = document.getElementById("btn-main");
  btn.textContent = s === "recording" ? "⏹ Detener y finalizar" : "▶ Iniciar sesión";
}
function tick() {
  const t = document.getElementById("timer");
  if (state === "recording") {
    const parts = [];
    // duración: se sincroniza con /api/sessions/last (simple): placeholder
    t.textContent = "0:00:00";
  }
}
document.getElementById("btn-main").onclick = async () => {
  if (state !== "recording") {
    const s = await post("/api/sessions", {}).then(() => {});
    const list = await (await fetch("http://localhost:8787/api/sessions")).json();
    const newest = list[0];
    await post(`/api/sessions/${newest.id}/start`);
    setState("recording");
  } else {
    const list = await (await fetch("http://localhost:8787/api/sessions")).json();
    await post(`/api/sessions/${list[0].id}/stop`);
    setState("processing");
  }
};
document.getElementById("bubble").onclick = () => {
  document.getElementById("menu").classList.toggle("hidden");
};
document.getElementById("btn-web").onclick = () => {
  if (window.pywebview) window.pywebview.api.open_web();
  else window.open("http://localhost:8787");
};
document.getElementById("btn-quit").onclick = () => {
  if (window.pywebview) window.pywebview.api.quit();
};
tick();
setInterval(tick, 1000);
```

- [ ] **Step 6: `gadget/gadget_app.py`**

```python
import webbrowser
from pathlib import Path
import webview

ROOT = Path(__file__).resolve().parents[1]


class Api:
    def open_web(self):
        webbrowser.open("http://localhost:8787")

    def quit(self):
        for w in webview.windows:
            if not w.closed:
                w.destroy()


def run_gadget() -> None:
    api = Api()
    webview.create_window(
        "gadget", str(ROOT / "gadget" / "gadget.html"),
        width=240, height=140, frameless=True, easy_drag=True,
        on_top=True, transparent=True, js_api=api,
    )
    webview.start(api=api)
```

- [ ] **Step 7: `run.py`**

```python
import threading
import uvicorn
from app.main import app
from gadget.gadget_app import run_gadget


def main():
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info"),
        daemon=True,
    ).start()
    run_gadget()


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: ver pasar el test de API** — `pytest tests/test_start_stop_api.py -v` → 1 PASSED.

- [ ] **Step 9: smoke manual real** — `python run.py`; la burbuja flota, arrastra, abre la web, cambia estados con los botones.

- [ ] **Step 10: commit**

```bash
git add gadget run.py app tests
git commit -m "feat: gadget flotante, endpoints start/stop y run.py"
```

**Criterio de la Fase 2:** `python run.py` muestra la burbuja y controla sesiones; VAD detecta silencios; los chunks se graban (modo prueba verificado por tests).