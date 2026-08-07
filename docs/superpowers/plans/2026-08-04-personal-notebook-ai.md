# Personal Notebook AI — Implementation Plan (Master)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks se numeran secuencialmente (T0.1, T1.1, T2.1, …) y se ejecutan en orden por fase.

**Goal:** Construir la herramienta Personal Notebook AI — una app local de Windows (Python/FastAPI + gadget flotante pywebview) que captura el audio de clases de Zoom, transcribe con diarización (Deepgram), estructura en vivo y al final genera el "libro" de la sesión (timeline, temas, frases, vocabulario, roleplays) con podcast, quiz, flashcards, mapa y chatbot.

**Architecture:** Un solo proceso Python (`app/main.py`) sirve un API FastAPI en `localhost:8787`, una SPA vanilla (sin build) y un WebSocket hub. Un módulo de captura hace loopback WASAPI → chunks WAV de 90 s → cola persistente → Deepgram (pre-recorded + diarización) → integración incremental (OpenCode Go, DeepSeek V4 Flash) → pase final (DeepSeek V4 Pro/GLM-5.2) → libro + artefactos. Datos en SQLite (`data/app.db`) + carpetas `data/sessions/<id>/`. El gadget pywebview controla iniciar/detener.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, httpx, sqlite3 (stdlib), numpy (VAD), pywebview, pyaudiowpatch (loopback), edge-tts (podcast), imageio-ffmpeg (MP3), pytest.

**Spec de referencia:** `docs/superpowers/specs/2026-08-04-personal-notebook-ai-design.md`

---

## Índice de partes (leer en orden)

| Parte | Archivo | Fases |
|---|---|---|
| Master | este archivo | índice + estructura + convenciones |
| Parte 1 | `2026-08-04-notebook-ai-part1-foundations.md` | Fase 0 (cimientos), Fase 1 (cuadernos), Fase 2 (captura+gadget) |
| Parte 2 | `2026-08-04-notebook-ai-part2-transcription-live.md` | Fase 3 (transcripción+diarización), Fase 4 (estructuración en vivo) |
| Parte 3 | `2026-08-04-notebook-ai-part3-book-speakers.md` | Fase 5 (libro final + ¿Quién es quién?), Fase 6 (playback por segmento) |
| Parte 4 | `2026-08-04-notebook-ai-part4-podcast-chat-generate.md` | Fase 7 (podcast), Fase 8 (chatbot), Fase 9 (quiz/flashcards/mapa) |
| Parte 5 | `2026-08-04-notebook-ai-part5-robustness-beta.md` | Fase 10 (config/robustez/privacidad), Fase 11 (beta real + pulido UX) |

---

## File Structure (mapa completo)

```
notebook-ai/
├── requirements.txt
├── README.md
├── .gitignore
├── config.local.example.json
├── run.py                       # arranca backend + gadget
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, lifespan, routers, static
│   ├── config.py                # carga/guarda config.local.json
│   ├── db.py                    # sqlite3 init + schema + helpers
│   ├── schemas.py               # Pydantic modelos API
│   ├── ws.py                    # WebSocket hub (broadcast)
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── sessions.py          # CRUD cuadernos + start/stop (Fases 1-2)
│   │   ├── speakers.py          # panel "¿Quién es quién?" (Fase 5)
│   │   ├── content.py           # topics/timeline/transcript (Fases 4-5)
│   │   ├── chat.py              # chatbot (Fase 8)
│   │   ├── generate.py          # podcast/quiz/flashcards/map (Fases 7,9)
│   │   └── settings.py          # ajustes + llaves (Fase 10)
│   ├── capture/
│   │   ├── __init__.py
│   │   ├── loopback.py          # WASAPI loopback → chunks WAV (Fase 2)
│   │   └── silence.py           # VAD silencio → breaks (Fases 2,4)
│   ├── transcription/
│   │   ├── __init__.py
│   │   ├── queue.py             # cola pending_transcriptions + worker (Fase 3)
│   │   ├── deepgram_client.py   # cliente Deepgram pre-recorded (Fase 3)
│   │   ├── fusion.py            # fusión de hablantes entre chunks (Fase 3)
│   │   └── fallback.py          # stubs Gemini/Whisper (Fase 10)
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── opencode_client.py   # OpenAI-compatible client (Fase 1)
│   │   ├── live_integration.py  # estructuración incremental (Fase 4)
│   │   ├── polish.py            # pase final del libro (Fase 5)
│   │   ├── chat.py              # chat + BM25 retrieval (Fase 8)
│   │   ├── podcast.py           # guion + edge-tts (Fase 7)
│   │   ├── quiz.py              # preguntas MCQ (Fase 9)
│   │   ├── flashcards.py        # tarjetas (Fase 9)
│   │   └── concept_map.py       # nodos/aristas JSON (Fase 9)
│   ├── audio/
│   │   ├── __init__.py
│   │   ├── conversion.py        # wav→mp3 + clips por segmento (Fase 6)
│   │   └── trimming.py          # corte por timestamps
│   └── pipeline.py              # orquestador iniciar/detener sesión (Fases 2-5)
├── gadget/
│   ├── __init__.py
│   ├── gadget_app.py            # pywebview window (Fase 2)
│   ├── gadget.html / .css / .js
├── static/                      # SPA vanilla sin build
│   ├── index.html
│   ├── app.css
│   └── app.js
├── tests/
│   ├── conftest.py
│   ├── fixtures.py
│   └── test_*.py                # uno por módulo (ver cada parte)
└── data/                        # (gitignored) app.db + sessions/
```

---

## Convenciones transversales (obligatorias en todas las fases)

1. Rutas con `pathlib.Path`; root = `Path(__file__).resolve().parents[1]`.
2. Llaves de API SOLO en `config.local.json` (copia de `config.local.example.json`). Nunca en código.
3. Toda llamada LLM pasa por `app/ai/opencode_client.py`; toda llamada STT por `app/transcription/deepgram_client.py` (fallback detrás).
4. Timestamps de sesión en `REAL` (segundos desde `started_at`); hora absoluta = `started_at + segundos`.
5. Pruebas que requieren dispositivo de audio/red se marcan `skipif` salvo con mocks; las herramientas de prueba usan ficheros WAV sintéticos.
6. Número de sesión = `MAX(session_number del año)+1` (helper `db.next_session_number`).
7. Cada tarea termina con commit atómico; mensajes en inglés estilo `feat:`/`fix:`/`test:`.
8. Sin librerías nuevas sin añadirlas a `requirements.txt` y justificarlo aquí.

**Contrato de interfaces que comparten las fases** (definido una vez, usado en todas):

```python
# app/ai/opencode_client.py — firmas públicas
def chat_json(system: str, user: str, *, model: str, temperature: float = 0.3,
              json_schema: dict | None = None) -> dict: ...
def chat_text(system: str, user: str, *, model: str, temperature: float = 0.3) -> str: ...
# lanza AIError con .retryable si es 429/5xx (para reintentos)

# app/transcription/deepgram_client.py
def transcribe_file(path: Path, *, language: str = "en", diarize: bool = True) -> list[dict]:
    # -> [{"start": float, "end": float, "speaker": int, "text": str, "word": bool}]

# app/transcription/fusion.py
def merge_speakers(chunks: list[list[dict]]) -> list[dict]:
    # -> segmentos globales [{"start":, "end":, "speaker_global":, "text":}]

# app/ws.py
async def hub.broadcast(event: dict) -> None   # eventos: session/status, segment, structure
    
# app/pipeline.py
def start_session() -> int
def stop_session(sid: int) -> None
```

**Criterios de aceptación globales (Fase 11, ver parte 5) y privacidad:** borrar cuaderno = borrado total (base + audio); aviso legal en primera ejecución; captura = loopback del usuario.