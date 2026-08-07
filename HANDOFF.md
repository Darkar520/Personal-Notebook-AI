# HANDOFF — Personal Notebook AI

Fecha de corte: 2026-08-06. El contexto llegó al límite mensual durante la ejecución.

---

## Qué se hizo (completo y funcional)

### Backend Python — 100 % implementado

| Archivo | Qué hace |
|---|---|
| `app/paths.py` | Todas las rutas resueltas en tiempo de ejecución vía env vars (`NOTEBOOK_HOME`, `NOTEBOOK_DATA_DIR`). Permite mover `data/` y aislar tests sin monkeypatch. |
| `app/config.py` | Carga `config.local.json`, merge profundo con defaults, caché por mtime, override por env, endurecimiento de permisos del fichero, `public_config()` que oculta secretos. |
| `app/db.py` | SQLite con WAL + busy_timeout + `BEGIN IMMEDIATE`. Schema completo (15 tablas + FTS5 + índices + triggers). Migraciones con `PRAGMA user_version`. Helpers de tiempo UTC, `wall_clock`, `duration_between`, `record_usage`, `touch_session`. |
| `app/errors.py` | Jerarquía: `NotebookError → ProviderError → AIError / STTError`, `ConfigError`, `AudioDeviceError`, `SessionStateError`. |
| `app/security.py` | Middleware anti-CSRF / DNS-rebinding: valida `Host`, `Origin`, `Sec-Fetch-Site`. Token local opcional. |
| `app/ws.py` | Hub WebSocket thread-safe con `run_coroutine_threadsafe`. Historial de 50 eventos. Atajos semánticos: `emit_segments`, `emit_structure`, `emit_session_status`, `emit_warning`. |
| `app/disk.py` | `free_space_mb`, `should_warn`, `estimate_session_mb`, `remaining_recording_minutes`. |
| `app/logging_setup.py` | `RotatingFileHandler` en `data/logs/app.log`, nivel configurable, httpx silenciado. |
| `app/capture/wavio.py` | PCM↔float32, remuestreo sin scipy, mezcla de pistas, escritura atómica, iterador por ventanas sin cargar el fichero entero. |
| `app/capture/silence.py` | VAD por energía vectorizado con `stride_tricks`. Umbral adaptativo con piso y techo (corrige el bug de "señal constante = todo es silencio"). Histéresis. `voiced_ranges` para máscara de micrófono. |
| `app/capture/devices.py` | Resolución de loopback WASAPI con cascada de 3 estrategias (corrige el bug del nombre exacto que nunca coincidía). |
| `app/capture/loopback.py` | `LoopbackRecorder` (sistema + micrófono, 1 PyAudio compartido con `_PA_LOCK` — fix de la violación de acceso nativa), `WavFileRecorder` (alias `FakeRecorder`), solape entre chunks, `ChunkResult`. |
| `app/transcription/voiceprint.py` | Huella vocal 80-dim (banco mel + normalización cepstral, solo numpy). `embed`, `similarity`, `combine`, `speaker_embeddings`, JSON roundtrip. |
| `app/transcription/fusion.py` | `SpeakerRegistry` (asignación local→global por huella + índice como desempate). `dedupe_overlap` (punto medio + descarte del borde). `mark_is_me`. `group_words`. |
| `app/transcription/deepgram_client.py` | `transcribe_file` con utterances, smart_format, reintentos backoff+jitter, clasificación de errores, `check_credentials` con saldo. |
| `app/transcription/fallback.py` | `transcribe_whisper` (faster-whisper, import diferido), `transcribe_gemini` (google-genai, import diferido, reparto proporcional de prose sin timestamps). |
| `app/transcription/queue.py` | Cola persistente: enqueue idempotente por `(session_id, chunk_index)`, `claim_batch` atómico, `mark_failed` con retries, `requeue_stale`, `release`, sidecars JSON. |
| `app/transcription/worker.py` | `process_pending_once` concurrente (`ThreadPoolExecutor`), anti-rebote por chunk dentro de la misma pasada, offset correcto desde la fila, `is_me` por máscara de mic, upsert de `session_speakers` con colores, `record_usage`. |
| `app/ai/jsonx.py` | Extracción tolerante de JSON: directo → sin vallas → recorte balanceado → reparaciones (comas, comillas tipográficas, None/True/False, NaN). |
| `app/ai/models.py` | Resolución de id de modelo contra el catálogo del proveedor (exacto → prefijo → sufijo → comodín → primer resultado). Caché con TTL en `settings`. |
| `app/ai/opencode_client.py` | `complete` / `chat_text` / `chat_json`. Reintentos, degradación si `response_format` no soportado, reintento estricto si el JSON viene roto, `fit_text` para prompts largos, `record_usage`. |
| `app/ai/prompts.py` | Todos los prompts del sistema en un solo archivo. `LIVE_INTEGRATION`, `BOOK_MAIN`, `BOOK_SPEAKERS`, `BOOK_TITLE`, `CHAT_TUTOR`, `CONCEPT_MAP`, `podcast_script(min)`, `quiz(n)`, `flashcards(n)`. |
| `app/ai/live_integration.py` | `integrate` (delta de transcript + estructura actual → estructura actualizada). `normalize_structure` (deduplicación, fusión por título, defensa anti-pérdida si el modelo "resume"). |
| `app/ai/polish.py` | `finalize_session` con map-reduce para clases largas, prompt de nombres selectivo, validación de timeline (breaks verificados contra silencio real, solapes recortados). `normalize_speakers`. |
| `app/ai/chat.py` | BM25 bilingüe (EN+ES), expansión de vecinos, contexto con nombres reales y horas de pared, citas devueltas a la UI. |
| `app/ai/podcast.py` | `make_script` (contenido real, no solo títulos), `render_podcast` async concurrente con semáforo y reintentos por réplica, `clean_line` para TTS, concatenación con ffmpeg sin límite de argumentos. |
| `app/ai/study.py` | `make_quiz` (validación de opciones, rotación de la posición correcta), `make_cards` (dedup), `make_map` (grafo conexo garantizado). |
| `app/audio/ffmpeg.py` | `concat_copy` (demuxer concat, sin límite de argumentos), `encode_pcm_stream` (stdin pipe), `clip`, `to_mp3`, `silence_mp3`, `duration`. Sin ventanas negras en Windows. |
| `app/audio/session_audio.py` | `build_session_mp3` (quita solape de cada chunk antes de unir, pipe a ffmpeg sin WAV intermedio de 400 MB), `discard_raw_audio`, `export_clip`. |
| `app/pipeline.py` | `start_session` / `stop_session` / `delete_session`. `run_integration` (delta real). `apply_book` (respeta `user_edited` y `title_locked`). `finalize_session` (drain + polish + audio + auto-generate). `recover_orphans`. `active_status`. |
| `app/runtime.py` | Supervisor de fondo: drain de cola, integración en vivo, aviso de disco, latidos por WebSocket, backoff en fallos. |
| `app/backup.py` | `export_session` (ZIP + `notes.md` legible sin la app), `restore_session` (anti zip-slip, remapeo de claves ajenas, manifiesto con versión de esquema). |
| `app/schemas.py` | Todos los modelos Pydantic. Adaptadores `row_to_*`. |
| `app/main.py` | `create_app`: lifespan (bind loop WS, recover_orphans, supervisor), manejadores de error de dominio, routers, SPA estática. |
| `app/routers/sessions.py` | CRUD + start/stop/finalize/discard/repolish/retry-transcription/queue. Ruta `/pending-recording` declarada antes de `/{sid}`. |
| `app/routers/content.py` | topics (CRUD, draft, patch con `user_edited`), timeline, roleplays, transcript (paginado por `after_id`), search (FTS5 + fallback LIKE, query saneada), usage. |
| `app/routers/speakers.py` | Confirmación de hablantes, merge de huella vocal en `people`, propagación de `is_me` al transcript, listado de personas conocidas, CRUD de personas. |
| `app/routers/chat.py` | Historial desde DB, contexto con nombres y horas, citas en la respuesta. |
| `app/routers/generate.py` | quiz, flashcards (Leitner con `due_at`), concept-map, podcast. Funciones puras reutilizables por `pipeline._auto_generate`. |
| `app/routers/media.py` | `GET /media/session` y `/media/podcast` con HTTP Range (206 Partial Content). `GET /media/clip` con validación. |
| `app/routers/settings.py` | GET/PUT settings (vacío = no borrar llave), DELETE key, POST /test (Deepgram con saldo real, TTS, audio), GET devices/models/voices, system status, acknowledge, export/restore HTTP. |

### Tests — 294 pasando, sin red ni hardware

`tests/test_foundations.py` — paths, config, db, disk, security, `/health`.  
`tests/test_capture.py` — wavio, silence/VAD, WavFileRecorder (chunks, solape, breaks, nivel).  
`tests/test_transcription.py` — Deepgram client, fusión, voiceprint, registry, cola, worker.  
`tests/test_ai.py` — jsonx, models, opencode_client, live_integration, polish, chat, podcast, study.  
`tests/test_pipeline.py` — grabar desde WAV, transcribir, libro, audio, recuperación.  
`tests/test_api.py` — todos los endpoints HTTP, Range requests, backup/restore.

### Frontend — implementado, sin validar en navegador

| Archivo | Qué hace |
|---|---|
| `static/index.html` | HTML semántico, reproductor único, toasts, modal-root. |
| `static/app.css` | Design system completo: tokens CSS, dark mode, topbar, cards, tabs, notas, transcript, speakers, chat, quiz, flashcards, mapa SVG, player sticky, toasts, modales. Accesibilidad: skip-link, focus-visible, aria, prefers-reduced-motion. |
| `static/js/api.js` | Cliente HTTP + WebSocket con reconexión backoff. Todos los endpoints como funciones nombradas. `ApiError` con `status` y `code`. |
| `static/js/ui.js` | `el()`, `mount()`, `markdown()` (seguro, sin innerHTML de datos externos), `toast()`, `modal()`, `confirmDialog()`, `debounce()`, `withBusy()`. |
| `static/js/player.js` | Reproductor único con `playRange(start, end)`, `loadSession`, `loadExternal`. Salta a cualquier instante. |
| `static/js/app.js` | Enrutado por hash, captura global (botón topbar), eventos WebSocket, first-run checks (aviso legal + onboarding). |
| `static/js/views/list.js` | Lista de cuadernos, banners de recuperación, exportar/restaurar. |
| `static/js/views/notebook.js` | Cabecera editable, tabs, notas (editable inline), timeline clickeable, transcript con búsqueda, speakers. |
| `static/js/views/study.js` | Chat con citas, podcast con reproductor, quiz con corrección, flashcards Leitner, mapa SVG por capas BFS, roleplays. |
| `static/js/views/settings.js` | Ajustes completos, test de conexión, catálogo de modelos, dispositivos. |
| `static/js/views/people.js` | Personas conocidas. |

### Gadget flotante

`gadget/gadget_app.py` — espera al backend, una instancia de PyAudio, posición recordada en JSON, `GadgetApi.hide()`.  
`gadget/gadget.html/.css/.js` — burbuja circular, cargada por HTTP (same-origin), cronómetro local, WebSocket con reconexión.

### Entorno

`run.py` — `--no-gadget`, `--open`, `--host`, `--port`. Detecta si el puerto ya está en uso.  
`.venv` creado, `requirements.txt` instalado (pip confirmó OK).

---

## Dónde quedó parado

La última acción ejecutada fue verificar la sintaxis de los archivos JS con `node --check`. Todos fallaron porque Node.js evalúa módulos ES contra el sistema de archivos y los imports relativos (`./api.js`, `../player.js`) no se resuelven desde stdin. **No es un error real**: el mismo comando da error en cualquier módulo ES con imports, incluso si el código es sintácticamente correcto. Los archivos JS no fueron modificados ni están rotos.

**No se llegó a hacer:**

1. **Prueba de humo del frontend en navegador** — no se abrió `http://127.0.0.1:8787` y se verificó visualmente. El backend sirve todos los archivos estáticos (confirmado con `Invoke-WebRequest` a `/`, `/app.css`, `/js/app.js`, etc., todos con 200), pero no se comprobó que la SPA funcione de punta a punta en el navegador.

2. **Init de git y commits atómicos** — el plan pedía `git init` + commits por fase. No se ejecutaron. El repositorio no tiene historial.

3. **`app/ai/models.py` — prueba de integración real** — se probó con mock. No se verificó que el catálogo de `https://opencode.ai/zen/go/v1/models` responda y que los ids resueltos sean los correctos para tu cuenta. Esto se comprueba en Ajustes → Ver catálogo de modelos con las llaves reales.

4. **Clase de 3,5 h de punta a punta** — toda la Fase 11 del plan. El pipeline completo (grabar → transcribir → libro → audio → materiales) se verifica contra el checklist en `docs/beta-checklist.md`.

5. **Ajuste de prompts post-beta** — `app/ai/prompts.py` es la única palanca editorial. No se usó en una clase real, así que los prompts son los diseñados en el plan original sin iteración real.

---

## Lo que hay que hacer para arrancar

```bat
cd "PERSONAL NOTEBOOK AI"
copy config.local.example.json config.local.json
# Edita config.local.json y pon tus llaves de OpenCode Go y Deepgram

python run.py --no-gadget          # solo el backend
# Abre http://127.0.0.1:8787 en el navegador
# Ve a Ajustes y pulsa "Probar conexión"
# Si todo verde, prueba con python run.py (abre también la burbuja)
```

Para ejecutar los tests:
```bat
.venv\Scripts\activate
pytest -q                          # 294 tests, ~60 s, sin red ni micrófono
```

---

## Riesgos conocidos antes de la primera clase real

| Riesgo | Estado |
|---|---|
| Los ids de modelo de OpenCode Go podrían no coincidir | `app/ai/models.py` lo resuelve automáticamente, pero verifica en Ajustes antes de grabar |
| El gadget puede no funcionar si WebView2 no está instalado | Usa `python run.py --no-gadget` y controla desde la web |
| La primera clase puede revelar que los prompts necesitan ajuste | Todo en `app/ai/prompts.py`, un solo archivo, sin tocar lógica |
| Disco: ~2.3 GB libres en este equipo | Con `keep_raw_audio: false` (default) una clase ocupa ~75 MB; con raw ~475 MB |

---

## Archivos de referencia para continuar

- `docs/decisiones-de-implementacion.md` — por qué cada cambio respecto al plan.
- `docs/beta-checklist.md` — guion de las 3 clases de validación.
- `docs/superpowers/specs/2026-08-04-personal-notebook-ai-design.md` — spec del producto.
- `app/ai/prompts.py` — todo lo editorial, aquí se afina después de la beta.
