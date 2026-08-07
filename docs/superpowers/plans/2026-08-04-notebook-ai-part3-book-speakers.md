# Personal Notebook AI — Plan Parte 3: Libro final + ¿Quién es quién? + Playback

> Parte del plan master `2026-08-04-personal-notebook-ai.md`. Tareas T5.x (Fase 5), T6.x (Fase 6).

---

## Fase 5 — Libro final + identificación de personas

### Task 5.1: Pase de pulido (`app/ai/polish.py`)

**Files:**
- Create: `app/ai/polish.py`
- Test: `tests/test_polish.py`

- [ ] **Step 1: test que falla (LLM simulado)**

```python
def test_finalize_session_builds_book(monkeypatch):
    from app.ai import polish
    transcript = [
        "0.0 0 Welcome everyone, I am Sara, your teacher.",
        "2.0 1 Hi teacher, I am Juan.",
        "5.0 0 Today we cover open-account welcome calls.",
        "40.0 1 What is handle time?",
        "50.0 0 Handle time is the duration of a call.",
    ]
    replies = iter([
        {"timeline": [{"start": 0.0, "end": 90.0, "kind": "topic", "label": "Welcome calls"}],
         "topics": [{"title": "Welcome calls", "points": ["greet and verify identity"],
                     "spanish_notes": ["handle time = duración de la llamada"]}],
         "roleplays": [{"title": "RI first call", "context": "practice", "your_role": "agent",
                        "participants": ["Juan"], "key_phrases": [], "feedback": "good tone"}]},
        {"speakers": [{"index": 0, "suggested_name": "Sara", "suggested_role": "teacher"},
                      {"index": 1, "suggested_name": "Juan", "suggested_role": "me"}]},
        {"title": "Welcome calls & KPI vocabulary"},
    ])
    def fake(system, user, **kw):
        return next(replies)
    monkeypatch.setattr(polish.opencode_client, "chat_json", fake)
    book = polish.finalize_session(transcript, draft_topics=[], breaks=[],
                                   base_url="b", api_key="k", model="deepseek-v4-pro")
    assert book["title"] == "Welcome calls & KPI vocabulary"
    assert len(book["timeline"]) == 1
    assert book["speakers"][0]["suggested_name"] == "Sara"
    assert book["topics"][0]["spanish_notes"][0].startswith("handle time")
```

- [ ] **Step 2: ver falla** — `pytest tests/test_polish.py -v` → ImportError.

- [ ] **Step 3: implementar `app/ai/polish.py`**

```python
from app.ai import opencode_client

SYS_MAIN = (
    "You turn a raw class transcript (English) into a rigorous bilingual study book. "
    "Lines are '<t_seconds> <speaker_index> <text>'. Input also has draft topics and "
    "candidate breaks. Output JSON with: "
    "timeline: [{start,end,kind,label}] kind in topic|break|activity|roleplay|closing; "
    "topics: [{title, points[EN concise, non-redundant], spanish_notes[], phrases[] "
    "({en,es,speaker_index}), vocab[] ({word,en_def,es,example_en})}]; "
    "roleplays: [{title, context, your_role, participants[], key_phrases[], feedback}]. "
    "Keep points in English; Spanish notes only for difficult concepts. Return nothing else."
)
SYS_SPEAKERS = (
    "Given a class transcript with speaker indices, infer each speaker's REAL name and "
    "role from what is said (self-introductions, being addressed by name, titles like "
    "'teacher'). Output JSON {'speakers':[{'index':int,'suggested_name':str,"
    "'suggested_role':'teacher'|'me'|'student'|'other'}]}. Use 'me' for the person the "
    "learner is most likely to be (the one told to practice), else 'student'."
)
SYS_TITLE = (
    "Given the main topics and timeline of a class session, output JSON "
    "{'title':'Concise title (Spanish or English, <8 words), mentioning the account/company "
    "if evident (e.g. Yardi, Capital One, Verizon)'}."
)


def _lines(segments: list[tuple[float, int, str]]) -> list[str]:
    return [f"{t:.0f} {s} {text.strip()}" for t, s, text in segments]


def finalize_session(*, segments: list[tuple[float, int, str]], draft_topics: list[dict],
                     breaks: list[tuple[float, float]], base_url: str, api_key: str,
                     model: str) -> dict:
    main_user = {
        "transcript": "\n".join(_lines(segments)),
        "draft_topics": draft_topics,
        "candidate_breaks": [{"start": b[0], "end": b[1]} for b in breaks],
    }
    import json as _json
    main = opencode_client.chat_json(
        SYS_MAIN, _json.dumps(main_user, ensure_ascii=False),
        model=model, base_url=base_url, api_key=api_key)
    speakers = opencode_client.chat_json(
        SYS_SPEAKERS, main_user["transcript"],
        model=model, base_url=base_url, api_key=api_key, temperature=0.1)
    title = opencode_client.chat_json(
        SYS_TITLE, _json.dumps({"topics": main["topics"], "timeline": main["timeline"]},
                               ensure_ascii=False),
        model=model, base_url=base_url, api_key=api_key, temperature=0.2)
    return {
        "timeline": main["timeline"],
        "topics": main["topics"],
        "roleplays": main["roleplays"],
        "speakers": speakers["speakers"],
        "title": title["title"],
    }
```

- [ ] **Step 3b: detección de breaks sobre WAV crudos (Fase 2 VAD reutilizado)**

```python
def detect_breaks_in_sessions(sid: int) -> list[tuple[float, float]]:
    """Lee los chunks WAV de la sesión, aplica silence.segment_silences con min_break_s=45,
    acumula offsets globales y devuelve lista ordenada de (start,end)."""
    from pathlib import Path
    import wave
    import numpy as np
    from app.config import get_root
    from app.capture.silence import segment_silences

    audio_dir = get_root() / "data" / "sessions" / str(sid) / "audio"
    breaks: list[tuple[float, float]] = []
    for wav in sorted(audio_dir.glob("chunk_*.wav")):
        start_s = int(wav.stem.split("_")[1])
        with wave.open(str(wav), "rb") as w:
            sr = w.getframerate()
            frames = w.readframes(w.getnframes())
            x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
        for (a, b) in segment_silences(x, sr=sr, min_break_s=45.0):
            breaks.append((start_s + a, start_s + b))
    breaks.sort()
    return breaks
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_polish.py -v` → 1 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: pase final del libro (timeline, temas, roleplays, nombres, título)"
```

### Task 5.2: Aplicar el libro en la base + disparo al detener

**Files:**
- Modify: `app/pipeline.py` (stop_session lanza polish en thread; guarda todo)
- Modify: `app/routers/sessions.py` (stop devuelve 'processing' y arranca tarea)
- Test: `tests/test_apply_book.py`

- [ ] **Step 1: aplicar el libro — función `apply_book(sid, book)` en `app/pipeline.py`**

```python
import json
from app import db


def apply_book(sid: int, book: dict) -> None:
    conn = db.get_conn()
    # timeline
    conn.execute("DELETE FROM timeline_events WHERE session_id=?", (sid,))
    for i, ev in enumerate(book.get("timeline", [])):
        conn.execute(
            "INSERT INTO timeline_events (session_id, sort_order, kind, start_t, end_t, label)"
            " VALUES (?,?,?,?,?,?)",
            (sid, i, ev.get("kind", "topic"), float(ev["start"]),
             float(ev["end"]), ev.get("label", "")))
    # temas finales
    conn.execute("DELETE FROM topics WHERE session_id=? AND status='final'", (sid,))
    for i, t in enumerate(book.get("topics", [])):
        conn.execute(
            "INSERT INTO topics (session_id, sort_order, status, title, start_t, end_t,"
            " summary_md, vocab_json, phrases_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, i, "final", t.get("title", ""),
             0.0, 0.0,
             json.dumps({"points": t.get("points", []),
                         "spanish_notes": t.get("spanish_notes", [])}),
             json.dumps(t.get("vocab", []), ensure_ascii=False),
             json.dumps(t.get("phrases", []), ensure_ascii=False),
             db.now_iso(), db.now_iso()))
    # roleplays
    conn.execute("DELETE FROM roleplays WHERE session_id=?", (sid,))
    for rp in book.get("roleplays", []):
        conn.execute(
            "INSERT INTO roleplays (session_id, title, context_md, your_role, participants_json,"
            " key_phrases_json, feedback_md, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, rp.get("title", ""), rp.get("context", ""), rp.get("your_role", ""),
             json.dumps(rp.get("participants", [])),
             json.dumps(rp.get("key_phrases", [])),
             rp.get("feedback", ""), db.now_iso()))
    # speakers sugeridos
    conn.execute("DELETE FROM session_speakers WHERE session_id=?", (sid,))
    for sp in book.get("speakers", []):
        conn.execute(
            "INSERT INTO session_speakers (session_id, speaker_index, suggested_name,"
            " suggested_role) VALUES (?,?,?,?)",
            (sid, sp["index"], sp.get("suggested_name", ""), sp.get("suggested_role", "")))
    # título + estado
    conn.execute("UPDATE sessions SET title=?, polish_model=?, status='done', updated_at=? WHERE id=?",
                 (book.get("title", ""), None, db.now_iso(), sid))
    conn.commit()
    conn.close()
```

- [ ] **Step 2: test `apply_book`**

```python
def test_apply_book_persists(tmp_path, monkeypatch):
    from app import db, pipeline
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    sid = pipeline.start_session()
    book = {
        "timeline": [{"start": 0, "end": 90, "kind": "topic", "label": "Intro"}],
        "topics": [{"title": "Intro", "points": ["p1"], "spanish_notes": ["s1"],
                    "vocab": [], "phrases": []}],
        "roleplays": [{"title": "r", "context": "c", "your_role": "agent",
                       "participants": [], "key_phrases": [], "feedback": "f"}],
        "speakers": [{"index": 0, "suggested_name": "Sara", "suggested_role": "teacher"}],
        "title": "Intro session",
    }
    pipeline.apply_book(sid, book)
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM timeline_events WHERE session_id=?", (sid,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM topics WHERE session_id=?", (sid,)).fetchone()[0] == 1
    assert conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM session_speakers WHERE session_id=?", (sid,)).fetchone()[0] == 1
```

- [ ] **Step 3: stop_session lanza polish en segundo plano**

```python
def stop_session(sid: int, discard: bool = False) -> None:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise KeyError(sid)
    if discard:
        conn.close()
        return
    conn.execute("UPDATE sessions SET status='processing', ended_at=?, duration_sec=?,"
                 " updated_at=? WHERE id=?",
                 (db.now_iso(),
                  max(0, int((__import__("datetime").datetime.now().timestamp()
                              - __import__("datetime").datetime.fromisoformat(
                                  row["started_at"].replace("Z", "+00:00")).timestamp()))),
                  db.now_iso(), sid))
    conn.commit()
    conn.close()
    _polish_async(sid)


def _polish_async(sid: int) -> None:
    import threading
    threading.Thread(target=_polish_worker, args=(sid,), daemon=True).start()


def _polish_worker(sid: int) -> None:
    from app.config import load_config
    from app.ai import polish
    cfg = load_config()
    try:
        conn = db.get_conn()
        segs = [(r["start_t"], r["speaker_index"], r["text"]) for r in conn.execute(
            "SELECT start_t, speaker_index, text FROM transcript_segments"
            " WHERE session_id=? ORDER BY start_t", (sid,))]
        drafts = [{"title": r["title"], "points": json.loads(r["summary_md"]).get("points", []),
                   "spanish_notes": json.loads(r["summary_md"]).get("spanish_notes", [])}
                  for r in conn.execute("SELECT title, summary_md FROM topics"
                                        " WHERE session_id=? AND status='draft' ORDER BY sort_order", (sid,))]
        conn.close()
        breaks = polish.detect_breaks_in_sessions(sid)
        book = polish.finalize_session(
            segments=segs, draft_topics=drafts, breaks=breaks,
            base_url=cfg["opencode"]["base_url"],
            api_key=cfg["opencode"]["api_key"],
            model=cfg["opencode"]["models"]["polish"])
        apply_book(sid, book)
        import asyncio
        from app.ws import hub
        asyncio.ensure_future(hub.broadcast({"type": "session/status", "session_id": sid,
                                             "status": "done"}))
    except Exception as e:
        conn = db.get_conn()
        conn.execute("UPDATE sessions SET status='error', status_detail=?, updated_at=? WHERE id=?",
                     (str(e)[:500], db.now_iso(), sid))
        conn.commit()
        conn.close()
```

- [ ] **Step 4: ver pasar** — `pytest tests/test_apply_book.py -v` → 1 PASSED.

- [ ] **Step 5: commit**

```bash
git add app tests
git commit -m "feat: aplicar libro y polish asíncrono al detener"
```

### Task 5.3: Panel "¿Quién es quién?" + memoria de personas

**Files:**
- Create: `app/routers/speakers.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_speakers_api.py`

- [ ] **Step 1: test que falla**

```python
def test_speakers_list_and_confirm(client):
    from app import db
    conn = db.get_conn()
    conn.execute("INSERT INTO sessions (title, session_number, status) VALUES ('s',1,'done')")
    conn.execute("INSERT INTO session_speakers (session_id, speaker_index, suggested_name, suggested_role)"
                 " VALUES (1,0,'Sara','teacher')")
    conn.commit()
    r = client.get("/api/sessions/1/speakers")
    assert r.status_code == 200 and r.json()[0]["suggested_name"] == "Sara"
    r2 = client.put("/api/sessions/1/speakers", json={
        "speakers": [{"speaker_index": 0, "person_id": None, "name": "Sara Rivera",
                      "role": "teacher"}]})
    assert r2.status_code == 200
    assert db.get_conn().execute("SELECT name FROM people").fetchone()[0] == "Sara Rivera"
    sp = client.get("/api/sessions/1/speakers").json()[0]
    assert sp["confirmed"] == 1
```

- [ ] **Step 2: implementar `app/routers/speakers.py`**

```python
from fastapi import APIRouter
from app import db
from pydantic import BaseModel

router = APIRouter(prefix="/api/sessions/{sid}/speakers", tags=["speakers"])


class SpeakerConfirm(BaseModel):
    speaker_index: int
    person_id: int | None = None
    name: str | None = None
    role: str = "other"


@router.get("")
def list_speakers(sid: int):
    conn = db.get_conn()
    rows = conn.execute("SELECT ss.*, p.name, p.role AS confirmed_role FROM session_speakers ss"
                        " LEFT JOIN people p ON p.id=ss.person_id"
                        " WHERE ss.session_id=? ORDER BY ss.speaker_index", (sid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.put("")
def confirm_speakers(sid: int, body: dict):
    speakers: list[SpeakerConfirm] = [SpeakerConfirm(**s) for s in body["speakers"]]
    conn = db.get_conn()
    for sc in speakers:
        person_id = sc.person_id
        if person_id is None and sc.name:
            existing = conn.execute("SELECT id FROM people WHERE name=?", (sc.name,)).fetchone()
            if existing:
                person_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO people (name, role, created_at) VALUES (?,?,?)",
                                   (sc.name, sc.role, db.now_iso()))
                person_id = cur.lastrowid
        conn.execute("UPDATE session_speakers SET person_id=?, confirmed=1 WHERE session_id=?"
                     " AND speaker_index=?", (person_id, sid, sc.speaker_index))
    conn.commit()
    conn.close()
    return list_speakers(sid)
```

Register en `main.py`. **Nota de orden:** `speakers.router` debe incluirse tras `sessions.router` (firma `/{sid}` no colisiona porque los prefijos difieren).

- [ ] **Step 3: "personas conocidas" reutilizables en próxima sesión**

`GET /api/people` → lista `people` (para el selector del panel). Añadir en `speakers.py` un segundo router:

```python
people_router = APIRouter(prefix="/api/people", tags=["people"])


@people_router.get("")
def list_people():
    conn = db.get_conn()
    rows = conn.execute("SELECT id, name, role FROM people ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: SPA — panel "¿Quién es quién?"** — en `loadSessionDetail`, si hay speakers sin confirmar, mostrar bloque con: color, input de nombre (sugerencia precargada), select de rol, botón "Usar personas conocidas" (rellena de `/api/people` con click en el nombre) y "Guardar".

- [ ] **Step 5: ver pasar** — `pytest tests/test_speakers_api.py -v` → 1 PASSED.

- [ ] **Step 6: commit**

```bash
git add app static tests
git commit -m "feat: panel ¿Quién es quién? y memoria de personas"
```

### Task 5.4: SPA — pestaña Notas finales (temas + timeline)

**Files:**
- Modify: `static/app.js`, `static/app.css`, `static/index.html` (tabs: Notas, Transcripción, Chat placeholder)

- [ ] **Step 1: tabs en `loadSessionDetail`** — barra de pestañas: `Notas | Transcripción | ¿Quién es quién?` (todas renderizan desde API). Chat/Quiz/etc. se añaden en sus fases.
- [ ] **Step 2: render Notas** — `GET /api/sessions/{id}/topics` (status final) con tarjetas: título, puntos (li), `spanish_notes` (bloque `.es`), frases con traducción y `vocab`. Además `GET /api/sessions/{id}/timeline` (nuevo endpoint en `content.py`: `SELECT * FROM timeline_events ORDER BY sort_order`) renderizado como línea de tiempo con horas absolutas (`started_at + start_t`).
- [ ] **Step 3: endpoint timeline en `app/routers/content.py`**

```python
@router.get("/timeline")
def timeline(sid: int):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM timeline_events WHERE session_id=? ORDER BY sort_order", (sid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: verificar manual** — tras una sesión procesada, Notas muestra temas finales + timeline con recesos.
- [ ] **Step 5: commit**

```bash
git add static app tests
git commit -m "feat: pestañas y render de notas finales con timeline"
```

**Criterio de la Fase 5:** al detener, el libro queda generado (timeline, temas, roleplays, título), con propuesta de nombres y confirmación persistente; personas reutilizables.

---

## Fase 6 — Playback por segmento

### Task 6.1: Conversión a MP3 + clips por segmento

**Files:**
- Create: `app/audio/__init__.py`, `app/audio/conversion.py`
- Create: `tests/test_conversion.py`

- [ ] **Step 1: test que falla (imageio-ffmpeg)**

```python
import subprocess, sys

def test_install_ffmpeg():
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    assert exe and sys.executable  # ffmpeg del paquete disponible

def test_chunk_mp3_created(tmp_path, sample_wav):
    from app.audio.conversion import to_mp3
    out = to_mp3(sample_wav, tmp_path / "out.mp3", bitrate_kbps=48)
    assert out.exists() and out.stat().st_size > 1000
```

- [ ] **Step 2: implementar `app/audio/conversion.py`**

```python
import subprocess
from pathlib import Path
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> None:
    subprocess.run([FFMPEG, "-y", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def to_mp3(src: Path, dst: Path, bitrate_kbps: int = 48) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(src), "-ac", "1", "-ar", "16000", "-b:a", f"{bitrate_kbps}k",
          str(dst)])
    return dst


def clip(src_wav: Path, start_s: float, end_s: float, dst: Path,
         bitrate_kbps: int = 48) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, end_s - start_s)
    _run(["-i", str(src_wav), "-ss", str(start_s), "-t", str(dur),
          "-ac", "1", "-ar", "16000", "-b:a", f"{bitrate_kbps}k", str(dst)])
    return dst
```

- [ ] **Step 3: script de render de sesión en `app/audio/session_audio.py`**

```python
from pathlib import Path
from app.audio.conversion import to_mp3, clip
from app.config import get_root


def concat_wavs(wavs: list[Path], dst: Path) -> Path:
    """Concatena WAV (mismo formato) a un único WAV usando imageio_ffmpeg."""
    import subprocess, imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    files = [str(w) for w in wavs]
    inputs = []
    for f in files:
        inputs += ["-i", f]
    subprocess.run([ff, "-y", *inputs, "-filter_complex",
                    "concat=n=%d:v=0:a=1" % len(files), str(dst)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def render_session(sid: int, timeline: list[dict]) -> dict[str, Path]:
    root = get_root() / "data" / "sessions" / str(sid)
    audio_dir = root / "audio"
    wavs = sorted(audio_dir.glob("chunk_*.wav"))
    session_wav = concat_wavs(wavs, root / "session.wav")
    session_mp3 = to_mp3(session_wav, root / "session.mp3")
    clips: dict[str, Path] = {}
    for i, ev in enumerate(timeline):
        start, end = float(ev["start_t"]), float(ev["end_t"])
        clips[f"seg_{i}"] = clip(session_wav, start, end, root / "clips" / f"seg_{i}.mp3")
    if not getattr(__import__("app.config", fromlist=[""]), "load_config")(). \
            get("settings", {}).get("keep_raw_audio", False):
        import shutil
        shutil.rmtree(audio_dir, ignore_errors=True)
    return {"session": session_mp3, "clips": clips}
```

> **Nota:** `concat_wavs` requiere que todos los chunks compartan formato (lo garantiza el grabador). `render_session` se llama en `_polish_worker` al final del `apply_book`.

- [ ] **Step 4: endpoints de entrega de audio en `app/routers/content.py`**

```python
from fastapi.responses import FileResponse
from app.config import get_root


@router.get("/audio/{name}")
def get_audio(sid: int, name: str):
    root = get_root() / "data" / "sessions" / str(sid)
    file = root / name if name.startswith(("session.mp3", "clips/")) else None
    if not file or not file.exists() or "://" in name or ".." in name:
        raise HTTPException(404, "audio no encontrado")
    return FileResponse(str(file), media_type="audio/mpeg")
```

- [ ] **Step 5: ver pasar** — `pytest tests/test_conversion.py -v` → 2 PASSED (el primero es smoke de instalación).

- [ ] **Step 6: commit**

```bash
git add app tests
git commit -m "feat: mp3 de sesión, clips por segmento y endpoints de audio"
```

### Task 6.2: Timeline clickeable en la SPA

**Files:**
- Modify: `static/app.js`, `static/app.css`

- [ ] **Step 1:** en el render de la timeline, cada tramo con botón ▶ que reproduce `/api/sessions/{id}/audio/clips/seg_{i}.mp3` en un `<audio>` compartido; el título muestra la hora absoluta (de `started_at` + `start_t`).
- [ ] **Step 2: verificar manual** — click en un tramo reproduce exactamente ese intervalo.
- [ ] **Step 3: commit**

```bash
git add static
git commit -m "feat: timeline reproducible por segmento"
```

**Criterio de la Fase 6:** audio de sesión + clips; reproducción por tramo desde la web; control de disco (`keep_raw_audio`).