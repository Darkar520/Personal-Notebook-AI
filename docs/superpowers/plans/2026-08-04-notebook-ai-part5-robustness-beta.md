# Personal Notebook AI — Plan Parte 5: Configuración, Robustez, Privacidad y Beta

> Parte del plan master `2026-08-04-personal-notebook-ai.md`. Tareas T10.x (Fase 10), T11.x (Fase 11).

---

## Fase 10 — Configuración, robustez y privacidad

### Task 10.1: Router de ajustes + test de conexión de llaves

**Files:**
- Create: `app/routers/settings.py`
- Modify: `app/main.py`, `static/app.js`
- Test: `tests/test_settings_api.py`

- [ ] **Step 1: test que falla**

```python
def test_settings_get_put(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "config.local.json")
    r = client.get("/api/settings")
    assert r.status_code == 200 and "opencode" in r.json()
    r2 = client.put("/api/settings", json={
        "opencode": {"api_key": "sk-x"},
        "deepgram": {"api_key": "dg-x"},
        "settings": {"capture_mode": "loopback"}})
    assert r2.status_code == 200
    from app.config import load_config
    assert load_config()["opencode"]["api_key"] == "sk-x"

def test_test_connection(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "config.local.json")
    from app.ai import opencode_client as oc
    state = {}
    def fake_chat_text(system, user, **kw):
        state["called"] = True
        return "ok"
    monkeypatch.setattr(oc, "chat_text", fake_chat_text)
    r = client.post("/api/settings/test", json={})
    assert r.status_code == 200 and r.json()["opencode"]["ok"] is True
```

- [ ] **Step 2: implementar `app/routers/settings.py`**

```python
from fastapi import APIRouter, HTTPException
from app.config import load_config, save_config
from app.ai import opencode_client

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings():
    cfg = load_config()
    # nunca devolver llaves completas: solo máscara
    mask = lambda k: (k[:4] + "…" + k[-4:]) if len(k) > 8 else ("set" if k else "")
    cfg["opencode"]["api_key_masked"] = mask(cfg["opencode"].get("api_key", ""))
    cfg["deepgram"]["api_key_masked"] = mask(cfg["deepgram"].get("api_key", ""))
    return cfg


@router.put("")
def put_settings(body: dict):
    cfg = load_config()
    for section in ("opencode", "deepgram", "audio", "settings"):
        if section in body:
            cfg[section].update({k: v for k, v in body[section].items()
                                 if v is not None})
    # vaciar llaves si llegan ""
    if body.get("opencode", {}).get("api_key") == "":
        cfg["opencode"]["api_key"] = ""
    if body.get("deepgram", {}).get("api_key") == "":
        cfg["deepgram"]["api_key"] = ""
    save_config(cfg)
    return get_settings()


@router.post("/test")
def test_connection():
    cfg = load_config()
    result = {"opencode": {"ok": False, "detail": ""},
              "deepgram": {"ok": False, "detail": ""}}
    if cfg["opencode"]["api_key"]:
        try:
            opencode_client.chat_text("Reply with exactly: ok", "hi",
                                      model=cfg["opencode"]["models"]["live"],
                                      base_url=cfg["opencode"]["base_url"],
                                      api_key=cfg["opencode"]["api_key"])
            result["opencode"]["ok"] = True
        except Exception as e:
            result["opencode"]["detail"] = str(e)[:200]
    else:
        result["opencode"]["detail"] = "llave vacía"
    result["deepgram"]["ok"] = bool(cfg["deepgram"]["api_key"])
    result["deepgram"]["detail"] = "" if result["deepgram"]["ok"] else "llave vacía"
    return result
```

- [ ] **Step 3: SPA — pestaña Ajustes** — formulario con campos (llaves, modelos, intervalos, modos), test de conexión, guardar.
- [ ] **Step 4: ver pasar** — `pytest tests/test_settings_api.py -v` → 2 PASSED.
- [ ] **Step 5: commit**

```bash
git add app static tests
git commit -m "feat: ajustes globales y test de llaves"
```

### Task 10.2: Fallback de transcripción (Whisper local / Gemini)

**Files:**
- Create: `app/transcription/fallback.py`
- Modify: `app/transcription/worker.py` (backend configurable)
- Test: `tests/test_fallback.py` (mocks)

- [ ] **Step 1: test que falla**

```python
def test_whisper_backend_no_diarization(monkeypatch):
    from app.transcription import fallback
    class Fake:
        def __init__(self, *a, **k): pass
        def transcribe(self, path, **k):
            return {"segments": [{"start": 0.0, "end": 5.0, "text": "hello world"}]}
    monkeypatch.setattr(fallback, "WhisperModel", Fake)
    out = fallback.transcribe_whisper("x.wav")
    assert len(out) == 1
    assert out[0]["speaker"] == 0
    assert "hello world" in out[0]["text"]
```

- [ ] **Step 2: implementar `app/transcription/fallback.py`**

```python
from pathlib import Path


def transcribe_whisper(path: Path, model_size: str = "small") -> list[dict]:
    from faster_whisper import WhisperModel  # import diferido (dependencia opcional)
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(path), language="en")
    return [{"start": float(s.start), "end": float(s.end), "speaker": 0,
             "text": s.text.strip() + " ", "word": False} for s in segments]


def transcribe_gemini(path: Path, api_key: str) -> list[dict]:
    # Fallback sin diarización; requiere paquete google-genai y red.
    from google import genai  # import diferido
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[genai.types.Part.from_bytes(data=path.read_bytes(),
                                              mime_type="audio/wav"),
                  "Transcribe this audio in English with segmentation. "
                  "Return plain text."])
    text = resp.text.strip()
    return [{"start": 0.0, "end": 0.0, "speaker": 0, "text": text + " ",
             "word": False}]
```

`app/transcription/worker.py` (modificar `process_chunk`):

```python
def process_chunk(row, cfg: dict) -> list[dict]:
    backend = cfg.get("settings", {}).get("stt_backend", "deepgram")
    path = __import__("pathlib").Path(row["chunk_path"])
    if backend == "whisper":
        from app.transcription import fallback
        return [{"start": u["start"], "end": u["end"],
                 "speaker_global": u["speaker"], "text": u["text"]} for u in
                fallback.transcribe_whisper(path)]
    if backend == "gemini":
        from app.transcription import fallback
        return [{"start": u["start"], "end": u["end"],
                 "speaker_global": u["speaker"], "text": u["text"]} for u in
                fallback.transcribe_gemini(path, cfg.get("deepgram", {}).get("gemini_key", ""))]
    words = deepgram_client.transcribe_file(path, api_key=cfg["deepgram"]["api_key"],
                                            language=cfg["deepgram"]["language"])
    utts = fusion.group_words(words)
    return [{"start": u["start"], "end": u["end"],
             "speaker_global": u["speaker"], "text": u["text"]} for u in utts]
```

Añadir al `config.local.example.json` `settings.stt_backend = "deepgram"`.

- [ ] **Step 3: ver pasar** — `pytest tests/test_fallback.py -v` → 1 PASSED.
- [ ] **Step 4: commit**

```bash
git add app tests
git commit -m "feat: fallback de transcripción senza diarización"
```

### Task 10.3: Crash recovery (sesión en curso al arrancar)

**Files:**
- Modify: `app/routers/sessions.py`, `app/main.py`, `static/app.js`
- Test: `tests/test_crash_recovery.py`

- [ ] **Step 1: test que falla**

```python
def test_pending_recording_returned_and_finalizable(client):
    from app import db
    conn = db.get_conn()
    conn.execute("INSERT INTO sessions (title, session_number, status, started_at)"
                 " VALUES ('cr', 1, 'recording', '2026-08-04T08:00:00Z')")
    conn.commit()
    r = client.get("/api/sessions/pending-recording")
    assert r.status_code == 200 and len(r.json()) == 1
    r2 = client.post("/api/sessions/1/finalize-recording")
    assert r2.status_code == 200 and r2.json()["status"] == "processing"
    r3 = client.post("/api/sessions/1/discard-recording")
    assert r3.status_code == 200
```

- [ ] **Step 2: endpoints en `app/routers/sessions.py`** (antes del `/{sid}` para evitar conflicto de rutas)

```python
@router.get("/pending-recording")
def pending_recording():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM sessions WHERE status='recording'").fetchall()
    conn.close()
    return [row_to_session(r) for r in rows]


@router.post("/{sid}/finalize-recording")
def finalize_recording(sid: int):
    conn = db.get_conn()
    conn.execute("UPDATE sessions SET status='processing', updated_at=? WHERE id=?", (db.now_iso(), sid))
    conn.commit()
    conn.close()
    from app.pipeline import _polish_async
    _polish_async(sid)
    return get_session(sid)


@router.post("/{sid}/discard-recording")
def discard_recording(sid: int):
    conn = db.get_conn()
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return {"ok": True}
```

- [ ] **Step 3: en `app/main.py` lifespan** — no auto-finalizar; el gadget y la SPA consultan `pending-recording` al cargar y muestran diálogo "Finalizar / Descartar / Continuar".
- [ ] **Step 4: UI** — en `loadSessions`, si hay pendientes, banner con las dos acciones.
- [ ] **Step 5: ver pasar** — `pytest tests/test_crash_recovery.py -v` → 1 PASSED.
- [ ] **Step 6: commit**

```bash
git add app static tests
git commit -m "feat: recuperación de sesión tras crash"
```

### Task 10.4: Privacidad, aviso legal, backup/restore

**Files:**
- Modify: `app/routers/settings.py` (endpoints backup/restore + flag legal)
- Create: `app/backup.py`
- Test: `tests/test_backup.py`

- [ ] **Step 1: test que falla (export→import de una sesión)**

```python
def test_export_and_restore(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    from app import pipeline
    sid = pipeline.start_session()
    from app.pipeline import apply_book
    apply_book(sid, {"timeline": [], "topics": [] , "roleplays": [], "speakers": [],
                     "title": "export me"})
    from app import backup
    monkeypatch.setattr(backup, "DATA", pipeline.DATA)
    zpath = tmp_path / "b.zip"
    backup.export_session(sid, zpath)
    assert zpath.exists()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app2.db")
    backup.restore_session(zpath, tmp_path / "sessions2")
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "app2.db"))
    rows = c.execute("SELECT id, title FROM sessions").fetchall()
    c.close()
    assert rows and rows[0][1] == "export me"
```

- [ ] **Step 2: implementar `app/backup.py`**

```python
import json, sqlite3, zipfile, shutil
from pathlib import Path
from app.config import get_root
from app import db, pipeline

DATA = get_root() / "data"
TABLES = ["sessions", "topics", "timeline_events", "transcript_segments",
          "session_speakers", "roleplays", "messages", "quiz_questions",
          "flashcards", "concept_maps", "audio_summaries"]


def export_session(sid: int, out_zip: Path) -> Path:
    conn = db.get_conn()
    payload = {}
    for t in TABLES:
        payload[t] = [dict(r) for r in conn.execute(
            f"SELECT * FROM {t} WHERE session_id=?", (sid,))] if t != "sessions" else \
            [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE id=?", (sid,))]
    conn.close()
    audio = DATA / "sessions" / str(sid)
    with zipfile.ZipFile(str(out_zip), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("session.json", json.dumps(payload, ensure_ascii=False))
        if audio.exists():
            for f in audio.rglob("*"):
                if f.is_file():
                    z.write(f, arcname=f"files/{f.relative_to(audio)}")
    return out_zip


def restore_session(zip_path: Path, target_audio_root: Path) -> int:
    with zipfile.ZipFile(str(zip_path)) as z:
        payload = json.loads(z.read("session.json"))
        z.extractall(str(target_audio_root.parent), members=[m for m in z.namelist()
                                                             if m.startswith("files/")])
    conn = db.get_conn()
    cur = conn.execute("INSERT INTO sessions (title, session_number, started_at, ended_at,"
                       " status, duration_sec, status_detail, audio_root, created_at, updated_at)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (payload["sessions"][0]["title"], db.next_session_number(conn),
                        payload["sessions"][0]["started_at"], payload["sessions"][0]["ended_at"],
                        "done", payload["sessions"][0]["duration_sec"], None,
                        str(target_audio_root.name), db.now_iso(), db.now_iso()))
    new_sid = cur.lastrowid
    id_map = {int(payload["sessions"][0]["id"]): new_sid}
    for t in ("topics", "timeline_events", "transcript_segments", "session_speakers",
              "roleplays", "messages", "quiz_questions", "flashcards", "concept_maps",
              "audio_summaries"):
        for r in payload.get(t, []):
            cols = [c for c in r.keys() if c != "id" and c != "session_id"]
            vals = [r[c] for c in cols]
            sql_cols = []
            for c in cols:
                if c == "session_id":
                    sql_cols.append("session_id")
                else:
                    sql_cols.append(c)
            placeholders = ",".join("?" for _ in sql_cols)
            vals = [id_map.get(int(v), v) if k == "session_id" else v for k, v in zip(sql_cols, vals)]
            conn.execute(f"INSERT INTO {t} ({','.join(sql_cols)}) VALUES ({placeholders})", vals)
    conn.commit()
    conn.close()
    # mover archivos del zip a data/sessions/<new_sid>
    shutil.copytree(str(target_audio_root), str(DATA / "sessions" / str(new_sid)), dirs_exist_ok=True)
    conn = db.get_conn()
    conn.execute("UPDATE sessions SET audio_root=? WHERE id=?",
                 (f"sessions/{new_sid}", new_sid))
    conn.commit()
    conn.close()
    return new_sid
```

- [ ] **Step 3: endpoints** — `POST /api/sessions/{sid}/export` → `FileResponse` zip; `POST /api/backup/restore` (sube zip vía `UploadFile`) → `restore_session`.
- [ ] **Step 4: aviso legal primera ejecución** — `settings` key `legal_notice_seen`; la SPA muestra el aviso (texto: responsabilidad sobre permisos de grabación y privacidad local) una vez.
- [ ] **Step 5: ver pasar** — `pytest tests/test_backup.py -v` → 1 PASSED.
- [ ] **Step 6: commit**

```bash
git add app static tests
git commit -m "feat: backup/restore y aviso legal"
```

### Task 10.5: Advertencia de espacio en disco + logs

**Files:**
- Modify: `app/main.py` (en `_session_loop`), `app/config.py` (cálculo de espacio)
- Create: `tests/test_disk_check.py`

- [ ] **Step 1: implementar check**

```python
def free_space_mb(path) -> int:
    import shutil
    usage = shutil.disk_usage(path)
    return usage.free // (1024 * 1024)


def should_warn(free_mb: int, min_free_mb: int) -> bool:
    return free_mb <= min_free_mb
```

- [ ] **Step 2: en `_session_loop`** — si `free_space_mb(get_root()) <= cfg["settings"]["min_free_space_mb"]`, broadcast `{"type":"warn","msg":"Disco casi lleno: borra sesiones o desactiva mantener audio"}` (evitar spam: broadcast solo si cambia de estado, con variable guard).
- [ ] **Step 3: logging** — `logs/app.log` con rotación simple (`logging` module, `RotatingFileHandler` 1 MB × 3), configurable DEBUG.
- [ ] **Step 4: ver pasar** — `pytest tests/test_disk_check.py -v` → 2 PASSED.
- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: aviso de disco y logging"
```

**Criterio de la Fase 10:** ajustes con test de llaves; fallback STT; recuperación de crash; borrado total; backup/restore; aviso legal; aviso de disco.

---

## Fase 11 — Beta real y pulido UX

### Task 11.1: Edición de notas con prioridad del usuario

**Files:**
- Modify: `app/db.py` (columna `user_edited` en `topics`), `app/routers/content.py` (PUT topic), `app/pipeline.py` (`apply_book` no sobreescribe `user_edited`)
- Test: `tests/test_user_edits.py`

- [ ] **Step 1: esquema** — añadir al `CREATE TABLE IF NOT EXISTS topics(...)` la columna `user_edited INTEGER DEFAULT 0`. Para instalaciones existentes (dev): migración simple en `init_db`: `ALTER TABLE topics ADD COLUMN user_edited INTEGER DEFAULT 0` dentro de try/except.
- [ ] **Step 2: `apply_book`** — al insertar temas finales, primero `DELETE ... WHERE session_id=? AND status='final' AND user_edited=0` (no toca los editados); conserva `sort_order` único recalculando.
- [ ] **Step 3: endpoint `PUT /api/sessions/{sid}/topics/{tid}`** — actualiza `title|summary_md|vocab_json|phrases_json` con `user_edited=1`.

```python
@router.put("/topics/{tid}")
def update_topic(sid: int, tid: int, body: dict):
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM topics WHERE id=? AND session_id=?", (tid, sid)).fetchone()
    if not row:
        raise HTTPException(404, "tema no encontrado")
    sets, vals = [], []
    for col in ("title", "summary_md", "vocab_json", "phrases_json"):
        if col in body and body[col] is not None:
            sets.append(f"{col}=?")
            vals.append(body[col] if isinstance(body[col], str) else __import__("json").dumps(body[col], ensure_ascii=False))
    sets.append("user_edited=1")
    sets.append("updated_at=?")
    vals.append(db.now_iso())
    vals.append(tid)
    conn.execute(f"UPDATE topics SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return get_topic(sid, tid)
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_user_edits.py -v`.
- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: edición de notas con prioridad del usuario"
```

### Task 11.2: Auto-generar materiales al finalizar

**Files:**
- Modify: `app/pipeline.py` (`_polish_worker`), `app/routers/generate.py` (helpers reusables)
- Test: `tests/test_auto_generate.py` (mocks)

- [ ] **Step 1:** al terminar `apply_book`, si `cfg["settings"]["auto_generate_all"]` (y hay llaves), encolar en un thread: generación de podcast, quiz (n=8), flashcards y mapa (reutilizando `make_podcast`, `make_quiz_endpoint` lógica en funciones helper puras en `generate.py`).
- [ ] **Step 2:** broadcast `{"type":"session/status","status":"done","generated":[...]}` y persistencia de cada artefacto.
- [ ] **Step 3: ver pasar** — test con mocks de las 4 generaciones.
- [ ] **Step 4: commit**

```bash
git add app tests
git commit -m "feat: auto-generación de materiales al finalizar"
```

### Task 11.3: Guía de primera ejecución

**Files:**
- Modify: `static/app.js`, `static/app.css`, `app/routers/settings.py` (flag `legal_notice_seen` + `onboarding_done`)

- [ ] **Step 1:** modal de bienvenida (una sola vez hasta marcarlo): pasos — 1) copiar `config.local.example.json` a `config.local.json` y pegar llaves en Ajustes; 2) en Zoom activar *Settings → Audio → "Hear my own voice"* (crucial para capturar tu voz); 3) iniciar sesión con el gadget antes de la clase; 4) detener al terminar y abrir el cuaderno.
- [ ] **Step 2:** asistente rápido en Ajustes: "Probar conexión" con resultado visual por proveedor.
- [ ] **Step 3: verificar manual** — primera apertura muestra el modal; al guardar ajustes el flag se pone.
- [ ] **Step 4: commit**

```bash
git add app static
git commit -m "feat: guía de primera ejecución"
```

### Task 11.4: Afinación de prompts + estimador de costo

**Files:**
- Modify: `app/ai/*.py` (prompts), `app/routers/content.py` (`GET /sessions/{id}/usage`)
- Test: `tests/test_usage.py`

- [ ] **Step 1: contador de uso** — tabla `settings` del tipo `usage:<session_id>` JSON con `{llm_calls, stt_minutes, chat_messages}`; incrementos en `worker`, `live_integration`, `polish` y `chat`.
- [ ] **Step 2: endpoint `GET /api/sessions/{sid}/usage`** — devuelve estimación: `stt_cost = stt_minutes * 0.0068` (Deepgram pre-recorded + diarización), `llm_cost = llm_calls * 0.005` (aprox.), `chat_cost = min(chat_messages*0.001, ...)`; total formateado.
- [ ] **Step 3: afinación** — tras la primera clase real, revisar: títulos muy largos, español_notes redundantes, duración del podcast; ajustar `SYSTEM_*` en `app/ai/*.py` y marcar cambios aquí (changelog del plan).
- [ ] **Step 4: ver pasar** — `pytest tests/test_usage.py -v`.
- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: medidor de uso y costo por sesión"
```

### Task 11.5: Beta real — validación end-to-end (checklist manual, 3 sesiones)

**Files:**
- Create: `docs/beta-checklist.md`
- Modify: prompts/menores según hallazgos

- [ ] **Step 1: escribir `docs/beta-checklist.md`** con el guion de 3 clases:
  - Clase 1: setup, llaves, test, Zoom "Hear my own voice", grabación de 30 min de prueba → revisar diarización, timeline, recesos, título.
  - Clase 2: sesión completa de 3,5 h → revisar notas concisas, sin duplicados, frases y vocabulario, nombres de personas, podcast, quiz, flashcards, mapa, chatbot.
  - Clase 3: crash/offline test (cerrar app a mitad, reiniciar → finalizar; quitar internet 5 min → cola reencola), backup/restore, borrado total.
- [ ] **Step 2: ejecutar checklist** con el usuario y registrar incidencias en `docs/beta-issues.md`; resolver las P0/P1 en commits.
- [ ] **Step 3: criterios de aceptación de la Fase 11** (✅ todos): claves en `config.local.json`; una clase real produce timeline+recesos+≥1 tema/bloque; nombres reales confirmados; notas bilingüe; podcast/quiz/flashcards/mapa regenerables; borrado total; aviso legal; recuperación de crash; backup/restore.
- [ ] **Step 4: commit final**

```bash
git add docs app static
git commit -m "feat: beta real validada y docs de checklist"
```

**Criterio de la Fase 11 (cierre del proyecto):** todos los criterios del spec §16 verificados en 3 sesiones reales y checklist documentado.