# Personal Notebook AI — Plan Parte 4: Podcast, Chatbot y Materiales de estudio

> Parte del plan master `2026-08-04-personal-notebook-ai.md`. Tareas T7.x (Fase 7), T8.x (Fase 8), T9.x (Fase 9).

---

## Fase 7 — Resumen de audio (podcast, edge-tts)

### Task 7.1: Guion del podcast (LLM)

**Files:**
- Create: `app/ai/podcast.py`
- Test: `tests/test_podcast_script.py`

- [ ] **Step 1: test que falla (LLM simulado)**

```python
def test_podcast_script_returns_lines(monkeypatch):
    from app.ai import podcast
    replies = iter([{"lines": [
        {"speaker": "A", "text": "Today we learned about welcome calls."},
        {"speaker": "B", "text": "Right, and the key metric is handle time."}]}])
    def fake(system, user, **kw):
        return next(replies)
    monkeypatch.setattr(podcast.opencode_client, "chat_json", fake)
    lines = podcast.make_script(topics=[{"title": "Welcome calls"}],
                                base_url="b", api_key="k", model="deepseek-v4-flash")
    assert len(lines) == 2 and lines[1]["speaker"] == "B"
```

- [ ] **Step 2: implementar `app/ai/podcast.py`**

```python
import json
from app.ai import opencode_client

SYSTEM = (
    "Write a natural 3-6 minute conversational recap in ENGLISH between two hosts, A and B, "
    "about the study session topics. Tone: warm, clear, like an audio documentary. Output "
    "JSON {\"lines\":[{\"speaker\":\"A\"|\"B\",\"text\":\"...\"}]}. Each line <= 3 sentences. "
    "End with one practical takeaway."
)

VOICE_A = "en-US-SaraNeural"
VOICE_B = "en-US-ChristopherNeural"


def make_script(*, topics: list[dict], base_url: str, api_key: str,
                model: str) -> list[dict]:
    user = json.dumps({"topics": topics}, ensure_ascii=False)
    return opencode_client.chat_json(SYSTEM, user, model=model, base_url=base_url,
                                     api_key=api_key)["lines"]
```

- [ ] **Step 3: ver pasar** — `pytest tests/test_podcast_script.py -v` → 1 PASSED.

- [ ] **Step 4: commit**

```bash
git add app tests
git commit -m "feat: guion del podcast con LLM"
```

### Task 7.2: Síntesis edge-tts + persistencia + endpoint

**Files:**
- Modify: `app/ai/podcast.py` (función async de audio)
- Create: `app/routers/generate.py`
- Test: `tests/test_podcast_render.py` (skip si sin red)

- [ ] **Step 1: `render_audio` async en `app/ai/podcast.py`**

```python
import asyncio
from pathlib import Path
import edge_tts
import imageio_ffmpeg
import subprocess


async def _line_mp3(text: str, voice: str, dst: Path) -> Path:
    com = edge_tts.Communicate(text, voice, rate="+4%")
    await com.save(str(dst))
    return dst


async def render_podcast(lines: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmps = []
    for i, ln in enumerate(lines):
        voice = VOICE_A if ln["speaker"] == "A" else VOICE_B
        tmp = out_dir / f"line_{i}.mp3"
        await _line_mp3(ln["text"], voice, tmp)
        tmps.append(tmp)
    out = out_dir / "podcast.mp3"
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    inputs = []
    for t in tmps:
        inputs += ["-i", str(t)]
    subprocess.run([ff, "-y", *inputs, "-filter_complex",
                    "concat=n=%d:v=0:a=1" % len(tmps), str(out)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for t in tmps:
        t.unlink(missing_ok=True)
    return out


def generate_podcast(*, topics: list[dict], out_dir: Path, base_url: str, api_key: str,
                     model: str) -> tuple[Path, str]:
    lines = make_script(topics=topics, base_url=base_url, api_key=api_key, model=model)
    out = asyncio.run(render_podcast(lines, out_dir))
    script = "\n".join(f"{ln['speaker']}: {ln['text']}" for ln in lines)
    return out, script
```

- [ ] **Step 2: endpoint en `app/routers/generate.py`**

```python
from fastapi import APIRouter, HTTPException
import json
from pathlib import Path
from app import db
from app.config import get_root, load_config
from app.ai import podcast

router = APIRouter(prefix="/api", tags=["generate"])


@router.post("/sessions/{sid}/podcast")
def make_podcast(sid: int):
    conn = db.get_conn()
    topics = [{"title": r["title"]} for r in conn.execute(
        "SELECT title FROM topics WHERE session_id=? AND status='final' ORDER BY sort_order", (sid,))]
    if not topics:
        conn.close()
        raise HTTPException(400, "Todavía no hay temas finales")
    conn.close()
    cfg = load_config()
    out_dir = get_root() / "data" / "sessions" / str(sid) / "podcast"
    clip, script = podcast.generate_podcast(
        topics=topics, out_dir=out_dir,
        base_url=cfg["opencode"]["base_url"], api_key=cfg["opencode"]["api_key"],
        model=cfg["opencode"]["models"]["podcast"])
    conn = db.get_conn()
    rel = str(clip.relative_to(get_root() / "data"))
    conn.execute("INSERT INTO audio_summaries (session_id, script, voice_a, voice_b, file_path, created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 (sid, script, podcast.VOICE_A, podcast.VOICE_B, rel, db.now_iso()))
    conn.commit()
    conn.close()
    return {"file_url": f"/data/{rel}", "script": script}


def serve(sid: int, name: str):
    return FileResponse(get_root() / "data" / "sessions" / str(sid) / "podcast" / safe(name))
```

> **Nota:** `safe(name)` sanea `name` (sin `/`, `..`, `://`) antes de resolver. El endpoint de audio del podcast se monta reutilizando `content.get_audio` con guarda de subcarpeta `podcast/`.

- [ ] **Step 3: test con red (condicional)**

```python
import pytest
pytest.importorskip("edge_tts")

def test_podcast_module_contracts():
    from app.ai import podcast, opencode_client
    assert podcast.VOICE_A and podcast.VOICE_B        # Sara + Christopher Neural
    assert podcast.VOICE_A != podcast.VOICE_B
    assert callable(opencode_client.chat_json)
    assert podcast.GPITCH_ALTS                     # ranuras para el barrido de tono
```

- [ ] **Step 4: SPA — pestaña "🎙️ Resumen de audio"** — botón "Generar podcast", reproductor `<audio controls src="...">`, texto del guion debajo.
- [ ] **Step 5: commit**

```bash
git add app static tests
git commit -m "feat: podcast con edge-tts y endpoint"
```

**Criterio de la Fase 7:** podcast de 3–5 min legible y natural; regenerable; reproductor en la web.

---

## Fase 8 — Chatbot de la sesión

### Task 8.1: BM25 retrieval

**Files:**
- Create: `app/ai/chat.py`
- Test: `tests/test_chat_retrieval.py`

- [ ] **Step 1: test que falla**

```python
def test_bm25_finds_relevant():
    from app.ai.chat import BM25, tokenize
    docs = [
        "handle time is the duration of a call",
        "we greet the customer and verify identity",
        "the teacher explained irregular verbs",
    ]
    bm = BM25.build(docs)
    hits = bm.top("how long should a handle time be?", k=1)
    assert hits[0] == 0

def test_tokenize():
    from app.ai.chat import tokenize
    assert tokenize("Handle-time, call!") == ["handle", "time", "call"]
```

- [ ] **Step 2: implementar `app/ai/chat.py`**

```python
import math
import re

STOP = set("the a an to of in on for and or is are was were how what when who why which where this that it".split())


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9]+", text.lower()) if t not in STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.doclen = [len(tokenize(d)) for d in docs]
        self.avgdl = (sum(self.doclen) / len(self.doclen)) if self.doclen else 0.0
        self.N = len(docs)
        self.dfs: dict[str, int] = {}
        self.freqs: list[dict[str, int]] = []
        for d in docs:
            f: dict[str, int] = {}
            for t in tokenize(d):
                f[t] = f.get(t, 0) + 1
                self.dfs[t] = self.dfs.get(t, 0) + 1
            self.freqs.append(f)

    def _idf(self, t: str) -> float:
        df = self.dfs.get(t, 0)
        return math.log(1 + (self.N - df + 0.5) / (df + 0.5))

    def score(self, q: str, i: int) -> float:
        s = 0.0
        dl = self.doclen[i] or 1
        for t in tokenize(q):
            f = self.freqs[i].get(t, 0)
            if f:
                s += self._idf(t) * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
        return s

    @classmethod
    def build(cls, docs: list[str]) -> "BM25":
        return cls(docs)

    def ranked(self, q: str) -> list[tuple[int, float]]:
        return sorted(range(self.N), key=lambda i: self.score(q, i), reverse=True)

    def top(self, q: str, k: int = 8) -> list[int]:
        return [i for i, _ in self.ranked(q)][:k]
```

- [ ] **Step 3: ver pasar** — `pytest tests/test_chat_retrieval.py -v` → 2 PASSED.

- [ ] **Step 4: commit**

```bash
git add app tests
git commit -m "feat: BM25 para retrieval del chat"
```

### Task 8.2: Endpoint de chat con contexto de sesión

**Files:**
- Modify: `app/ai/chat.py` (constructor de contexto + `answer`)
- Create: `app/routers/chat.py`
- Test: `tests/test_chat_answer.py`

- [ ] **Step 1: test que falla (LLM simulado)**

```python
def test_answer_builds_context(monkeypatch):
    from app.ai import chat
    seen = {}
    def fake_chat_text(system, user, **kw):
        seen["user"] = user
        return "Sure! Handle time means X."
    monkeypatch.setattr(chat.opencode_client, "chat_text", fake_chat_text)
    reply = chat.answer(
        segments=[(0.0, 0, "teacher explains handle time"), (5.0, 1, "student asks")],
        topics=[{"title": "Verizon", "points": ["handle time"], "spanish_notes": []}],
        question="¿qué es handle time?", history=[], base_url="b", api_key="k",
        model="deepseek-v4-flash")
    assert "handle time" in reply.lower()
    assert "Verizon" in seen["user"]
```

- [ ] **Step 2: implementar en `app/ai/chat.py`**

```python
from app.ai import opencode_client

SYS = (
    "You are an English-course tutor for a call-center agent (accounts like Yardi, Capital "
    "One, Verizon). Context per session: NOTES (JSON) and TRANSCRIPT segments (t, speaker, "
    "text). Rules: answer in the language the user writes in; explain new words in Spanish "
    "when useful; when relevant cite the time span that supports the answer; for 'tráduce X' "
    "give EN<->ES; for 'evalúame' ask 3 questions from the notes and then correct with "
    "feedback. Concise and clear."
)


def build_context(segments: list[tuple[float, int, str]], topics: list[dict],
                  question: str, k: int = 8) -> tuple[str, list[int]]:
    docs = [text for _, _, text in segments]
    bm = BM25.build(docs)
    idxs = bm.top(question, k=k)
    ctx = []
    for i in idxs:
        t, s, text = segments[i]
        ctx.append(f"[{t:.0f}s sp{s}] {text.strip()}")
    return "\n".join(ctx), idxs


def answer(*, segments: list[tuple[float, int, str]], topics: list[dict],
           question: str, history: list[dict], base_url: str, api_key: str,
           model: str) -> str:
    from app import db
    ctx, _ = build_context(segments, topics, question)
    import json as _json
    user = _json.dumps({
        "notes": topics,
        "relevant_transcript": ctx,
        "history": history[-6:],
        "question": question,
    }, ensure_ascii=False)
    return opencode_client.chat_text(SYS, user, model=model,
                                     base_url=base_url, api_key=api_key)
```

`app/routers/chat.py`:

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app import db
from app.ai import chat as chat_ai
from app.config import load_config

router = APIRouter(prefix="/api/sessions/{sid}", tags=["chat"])


class ChatMessage(BaseModel):
    message: str
    history: list[dict] = []


@router.post("/chat")
def chat(sid: int, body: ChatMessage):
    conn = db.get_conn()
    segs = [(r["start_t"], r["speaker_index"], r["text"]) for r in conn.execute(
        "SELECT start_t, speaker_index, text FROM transcript_segments WHERE session_id=?"
        " ORDER BY start_t", (sid,))]
    topics = [{"title": r["title"], "points": [], "spanish_notes": []} for r in
              conn.execute("SELECT title FROM topics WHERE session_id=? AND status='final'"
                           " ORDER BY sort_order", (sid,))]
    conn.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                 (sid, "user", body.message, db.now_iso()))
    conn.commit()
    conn.close()
    cfg = load_config()
    try:
        reply = chat_ai.answer(
            segments=segs, topics=topics, question=body.message,
            history=body.history, base_url=cfg["opencode"]["base_url"],
            api_key=cfg["opencode"]["api_key"], model=cfg["opencode"]["models"]["chat"])
    except Exception as e:
        raise HTTPException(502, f"Chat no disponible: {e}")
    conn = db.get_conn()
    conn.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                 (sid, "assistant", reply, db.now_iso()))
    conn.commit()
    conn.close()
    return {"reply": reply}
```

- [ ] **Step 3: ver pasar** — `pytest tests/test_chat_answer.py -v` → 1 PASSED.

- [ ] **Step 4: SPA — pestaña Chat** — lista de mensajes, input, historial en `body.history`, renders de markdown simple.
- [ ] **Step 5: commit**

```bash
git add app static tests
git commit -m "feat: chatbot de sesión con contexto BM25"
```

**Criterio de la Fase 8:** preguntas sobre una sesión devuelven respuestas correctas citando tramos; responde bilingüe; "evalúame" funciona.

---

## Fase 9 — Quiz, Flashcards y Mapa conceptual

### Task 9.1: Quiz (preguntas MCQ)

**Files:**
- Create: `app/ai/quiz.py`
- Create: `tests/test_quiz.py`

- [ ] **Step 1: test que falla (LLM simulado)**

```python
def test_quiz_generate_returns_questions(monkeypatch):
    from app.ai import quiz
    replies = iter([{"questions": [
        {"question": "What is handle time?", "options": ["A", "B", "C", "D"],
         "correct_index": 1, "explanation": "el tiempo de la llamada"}]}])
    monkeypatch.setattr(quiz.opencode_client, "chat_json", lambda *a, **k: next(replies))
    out = quiz.make_quiz(topics=[{"title": "Verizon", "points": ["handle time"]}],
                         n=1, base_url="b", api_key="k", model="deepseek-v4-flash")
    assert out[0]["correct_index"] == 1
```

- [ ] **Step 2: implementar `app/ai/quiz.py`**

```python
import json
from app.ai import opencode_client

SYSTEM = (
    "Given the class notes, generate MULTIPLE-CHOICE quiz questions in English appropriate "
    "for a call-center English trainee. Output JSON {\"questions\":[{"
    "'question':str,'options':[4],'correct_index':0..3,'explanation':str}]}."
)


def make_quiz(*, topics: list[dict], n: int, base_url: str, api_key: str,
              model: str) -> list[dict]:
    user = json.dumps({"n_questions": n, "topics": topics}, ensure_ascii=False)
    return opencode_client.chat_json(SYSTEM, user, model=model,
                                     base_url=base_url, api_key=api_key)["questions"]
```

- [ ] **Step 3: endpoint + persistencia**

`app/routers/generate.py`:

```python
from pydantic import BaseModel


class QuizRequest(BaseModel):
    n: int = 10


@router.post("/sessions/{sid}/quiz")
def make_quiz_endpoint(sid: int, body: QuizRequest):
    from app.ai import quiz as quiz_ai
    conn = db.get_conn()
    topics = [{"title": r["title"], "points": r["summary_md"]} for r in conn.execute(
        "SELECT title, summary_md FROM topics WHERE session_id=? AND status='final'"
        " ORDER BY sort_order", (sid,))]
    conn.close()
    cfg = load_config()
    questions = quiz_ai.make_quiz(topics=[{"title": t["title"], "points": t["points"]}
                                          for t in topics if t["points"]],
                                  n=body.n, base_url=cfg["opencode"]["base_url"],
                                  api_key=cfg["opencode"]["api_key"],
                                  model=cfg["opencode"]["models"]["chat"])
    conn = db.get_conn()
    conn.execute("DELETE FROM quiz_questions WHERE session_id=?", (sid,))
    for q in questions:
        conn.execute(
            "INSERT INTO quiz_questions (session_id, question, options_json, correct_index,"
            " explanation, created_at) VALUES (?,?,?,?,?,?)",
            (sid, q["question"], json.dumps(q["options"]), q["correct_index"],
             q.get("explanation", ""), db.now_iso()))
    conn.commit()
    conn.close()
    return questions


@router.get("/sessions/{sid}/quiz")
def list_quiz(sid: int):
    conn = db.get_conn()
    rows = conn.execute("SELECT id, question, options_json, correct_index, explanation"
                        " FROM quiz_questions WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    conn.close()
    return [{"id": r["id"], "question": r["question"],
             "options": json.loads(r["options_json"]),
             "correct_index": r["correct_index"], "explanation": r["explanation"]} for r in rows]
```

- [ ] **Step 4: SPA — pestaña Quiz** — selección de N, generar, responder, corregir (JS compara con `correct_index`) y mostrar `explanation`.
- [ ] **Step 5: ver pasar** — `pytest tests/test_quiz.py -v` → 1 PASSED.
- [ ] **Step 6: commit**

```bash
git add app static tests
git commit -m "feat: quiz de práctica MCQ"
```

### Task 9.2: Flashcards

**Files:**
- Create: `app/ai/flashcards.py`
- Test: `tests/test_flashcards.py`
- Modify: `app/routers/generate.py`

- [ ] **Step 1: test + implementación**

```python
def test_flashcards_from_vocab(monkeypatch):
    from app.ai import flashcards
    replies = iter([{"flashcards": [
        {"front": "handle time", "back": "duración de la llamada"}]}])
    monkeypatch.setattr(flashcards.opencode_client, "chat_json", lambda *a, **k: next(replies))
    out = flashcards.make_cards(topics=[], base_url="b", api_key="k",
                                model="deepseek-v4-flash")
    assert out[0]["front"] == "handle time"
```

```python
# app/ai/flashcards.py
import json
from app.ai import opencode_client

SYSTEM = (
    "From the class notes create spaced-repetition flashcards shipped JSON "
    "{\"flashcards\":[{'front':term/phrase EN,'back':meanING + example EN + short ES}]}."
)


def make_cards(*, topics: list[dict], base_url: str, api_key: str,
               model: str) -> list[dict]:
    user = json.dumps({"topics": topics}, ensure_ascii=False)
    return opencode_client.chat_json(SYSTEM, user, model=model, base_url=base_url,
                                     api_key=api_key)["flashcards"]
```

- [ ] **Step 2: endpoint (similar a quiz): `POST /api/sessions/{sid}/flashcards` + `GET`** — limpia `flashcards`, inserta `front/back_md`, devuelve lista.
- [ ] **Step 3: SPA — pestaña Flashcards** — grid, clic voltea, botón mezclar (shuffle en JS) y "domino".
- [ ] **Step 4: ver pasar** — `pytest tests/test_flashcards.py -v` → 1 PASSED.
- [ ] **Step 5: commit**

```bash
git add app static tests
git commit -m "feat: flashcards"
```

### Task 9.3: Mapa conceptual

**Files:**
- Create: `app/ai/concept_map.py`
- Test: `tests/test_concept_map.py`
- Modify: `app/routers/generate.py`, `static/app.js/css`

- [ ] **Step 1: test + implementación**

```python
def test_concept_map_nodes(monkeypatch):
    from app.ai import concept_map
    replies = iter([{"nodes": [{"id": "1", "label": "Call handling"},
                      "edges": []}])
    monkeypatch.setattr(concept_map.opencode_client, "chat_json", lambda *a, **k: next(replies))
    out = concept_map.make_map(topics=[{"title": "Verizon"}], base_url="b",
                               api_key="k", model="deepseek-v4-flash")
    assert out["nodes"][0]["id"] == "1"
```

```python
# app/ai/concept_map.py
import json
from app.ai import opencode_client

SYSTEM = (
    "Build a concept map of the session. Output JSON {\"nodes\":[{'id':str,'label':str}],"
    "\"edges\":[{'from':str,'to':str,'label':str}]}. Central node first. Max 14 nodes."
)


def make_map(*, topics: list[dict], base_url: str, api_key: str,
             model: str) -> dict:
    user = json.dumps({"topics": topics}, ensure_ascii=False)
    return opencode_client.chat_json(SYSTEM, user, model=model, base_url=base_url,
                                     api_key=api_key)
```

- [ ] **Step 2: endpoint `POST /api/sessions/{sid}/concept-map` + `GET`** — guarda `layout_json` en `concept_maps`, devuelve; SPA renderiza SVG (nodos en capas por profundidad BFS, aristas) dentro de una `<svg width=100% height=500>`.
- [ ] **Step 3: ver pasar** — `pytest tests/test_concept_map.py -v` → 1 PASSED.
- [ ] **Step 4: verificar manual** — mapa renderiza y conecta los temas.
- [ ] **Step 5: commit**

```bash
git add app static tests
git commit -m "feat: mapa conceptual"
```

**Criterio de la Fase 9:** quiz con corrección y explicación, flashcards volteables y barajables, mapa SVG render; todo regenerable y con persistencia por sesión.