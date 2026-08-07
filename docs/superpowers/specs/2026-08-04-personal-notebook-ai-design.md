# Especificación de diseño — Personal Notebook AI

**Fecha:** 2026-08-04
**Estado:** Diseño aprobado, pendiente de revisión del usuario
**Autor:** Sesión de brainstorming con el usuario

---

## 1. Propósito

Herramienta personal, tipo **Notebook LM**, para un curso de inglés virtual de formación
para atención al cliente con cuentas como Capital One, Yardi y Verizon. La herramienta:

1. Captura el **audio de la clase en tiempo real** (Zoom, app de escritorio en Windows)
   mediante un **gadget flotante** siempre encima de todo.
2. Transcribe, identifica **quién dijo qué** (nombre real de la persona) y **estructura en
   vivo** los temas hablados.
3. Al detener la clase, genera el **"libro" de la sesión**: línea de tiempo con horas,
   temas ordenados, frases textuales, vocabulario nuevo, reseña de roleplays.
4. Ofrece, por sesión: **chatbot**, **resumen de audio** (podcast de 2 voces),
   **quiz**, **flashcards**, **mapa conceptual** y **transcripción completa**.

**Regla de producto clave:** "que sume y no que reste" — nada de duplicados, notas
concisas, directas y bien explicadas, y cero complejidad visible para el usuario final.

---

## 2. Contexto del usuario

| Dato | Valor |
|---|---|
| Horario de clases | 08:00 – 11:30 (3.5 horas por sesión) |
| Plataforma | Zoom (aplicación de escritorio) |
| Sistema operativo | Windows (10 u 11), WebView2 disponible |
| Idiomas | Clases en inglés; usuario nativo de español |
| Llave disponible | OpenCode Go (API de texto vía `https://opencode.ai/zen/go/v1`) |
| Frecuencia | ~1 clase por día |

**Dolor principal:** al ser estudiante de un segundo idioma, retener los temas del día
anterior es difícil; se pierden frases, conceptos y detalles de los roleplays. La
herramienta debe permitir repasar "el día de ayer" de forma rápida y clara.

---

## 3. Decisiones de producto acordadas

1. **Un cuaderno = una sesión.** Cada clase genera un cuaderno fechado. El título se
   autogenera al detener y es editable.
2. **Idioma bilingüe inteligente:** notas, títulos y lineamientos en **inglés**; los
   conceptos difíciles llevan una explicación breve en **español** embebida. El chatbot
   responde en el idioma en que se le escriba.
3. **Estructura por temas** dentro del cuaderno, ordenados cronológicamente, con línea de
   tiempo (horas) y detección automática de **recesos**.
4. **Identificación de hablantes por nombre real** (no "Speaker 1/2"): propuesta de la IA
   a partir del transcript + panel de confirmación "¿Quién es quién?" + memoria de
   personas conocidas.
5. Funcionalidades post-clase: resumen por temas, línea de tiempo, chatbot, resumen de
   audio (podcast), quiz, flashcards, mapa conceptual, recap de roleplays, vocabulario y
   frases por tema, transcripción completa.
6. **Todo local** (datos y audio en la PC del usuario). Laves de API guardadas en equipo.
7. Modelo de costos: **$0 extra de bolsillo** mientras dure el crédito de Deepgram.

---

## 4. Arquitectura general

**Enfoque elegido:** *Python todo-en-uno* (opción A).

```
┌───────────────────────────────────────────────────────────────┐
│                       TU PC (Windows)                          │
│                                                                │
│   ┌─────────────┐   HTTP/WS   ┌───────────────────────┐        │
│   │ GADGET      │◄──────────►│  BACKEND  (Python)     │        │
│   │ flotante    │             │  FastAPI  :8787        │        │
│   │ (pywebview) │             │                        │        │
│   └─────────────┘             │  · Captura WASAPI      │        │
│                               │  · Cola de transcripción│       │
│   ┌─────────────┐             │  · Integración en vivo │        │
│   │ NAVEGADOR   │◄──────────►│  · Pase final "libro"   │        │
│   │  (web SPA)  │   HTTP/WS   │  · Podcast (edge-tts)  │        │
│   │ :8787       │             │  · Chat / Quiz / Mapas │        │
│   └─────────────┘             └───────────┬────────────┘        │
│                                           │                    │
│                          ┌────────────────▼───────────────┐    │
│                          │  Almacenamiento local          │    │
│                          │  · SQLite (estructura)          │    │
│                          │  · data/sessions/<id>/ (audio,  │    │
│                          │    mp3, transcripts)            │    │
│                          │  · config.local.json (llaves)   │    │
│                          └────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────┘

   Servicios en la nube (solo HTTP, sin base de datos en la nube):
   · DEEPGRAM  → transcripción + diarización (Nova-3, crédito $200 gratis)
   · OPENCODE GO → LLM de texto (DeepSeek V4 Flash/Pro, GLM-5.2)
   · EDGE-TTS  → síntesis de voz del podcast (gratis, sin llave)
```

### Roles de cada componente

- **Gadget flotante (pywebview):** ventana sin bordes, siempre al frente, arrastrable.
  Controla iniciar/detener sesión, muestra estado, abre la plataforma y la configuración.
- **Backend (FastAPI):** único proceso; expone API REST + WebSocket (eventos en vivo a la
  web: nuevo transcript, temas actualizados, estado de sesión). Dispara el pipeline de
  captura/transcripción/estructuración/finalización y sirve los archivos estáticos de la SPA.
- **SPA en navegador:** lista de cuadernos, detalle de cuaderno (pestañas), panel de
  speckers, configuración. Sin framework de build: HTML/CSS/JS vanilla (sin paso de
  compilación).
- **Almacenamiento:** SQLite para estructura; sistema de archivos para audio y MP3.

---

## 5. Modelo de datos (SQLite)

```sql
sessions(
  id INTEGER PK,
  title TEXT,               -- autogenerado o manual
  session_number INTEGER,   -- correlativo por año
  started_at TEXT,          -- ISO
  ended_at TEXT,            -- ISO (NULL mientras grabando)
  status TEXT,              -- 'recording'|'processing'|'done'|'error'
  duration_sec INTEGER,
  status_detail TEXT,       -- mensaje para el usuario
  audio_root TEXT,          -- ruta relativa a data/ (carpeta de la sesión)
  polish_model TEXT,        -- modelo usado en el pase final
  created_at TEXT, updated_at TEXT
)

people(
  id INTEGER PK,
  name TEXT UNIQUE,
  role TEXT,                -- 'me'|'teacher'|'student'|'other'
  created_at TEXT
)

session_speakers(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  speaker_index INTEGER,    -- índice global (fusionado entre chunks)
  person_id INTEGER REFERENCES people(id),   -- NULL hasta confirmar
  suggested_name TEXT,      -- propuesta de la IA
  suggested_role TEXT,
  confirmed BOOLEAN DEFAULT 0,
  color TEXT
)

topics(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  sort_order INTEGER,       -- orden cronológico
  status TEXT,              -- 'draft' (en vivo) | 'final'
  title TEXT,
  start_t REAL, end_t REAL, -- segundos desde inicio de sesión
  summary_md TEXT,          -- puntos clave concisos (inglés + notas en español)
  vocab_json TEXT,          -- [{word, en_def, es, example_en, example_es}]
  phrases_json TEXT,        -- [{en, es, speaker_index?}]
  created_at TEXT, updated_at TEXT
)

timeline_events(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  sort_order INTEGER,
  kind TEXT,                -- 'topic'|'break'|'activity'|'roleplay'|'closing'
  start_t REAL, end_t REAL,
  label TEXT,               -- p.ej. "Tema 1 · Irregular verbs"
  note_md TEXT,
  topic_id INTEGER REFERENCES topics(id)  -- NULL si no aplica
)

transcript_segments(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  start_t REAL, end_t REAL,
  speaker_index INTEGER,    -- índice global de persona
  text TEXT,
  topic_id INTEGER REFERENCES topics(id)  -- NULL hasta el pase final
)

pending_transcriptions(     -- cola persistente (robustez sin-red)
  id INTEGER PK,
  session_id INTEGER,
  chunk_path TEXT,
  start_t REAL,
  status TEXT,              -- 'pending'|'ok'|'failed'
  retries INTEGER DEFAULT 0,
  error TEXT,
  created_at TEXT
)

messages(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  role TEXT,                -- 'user'|'assistant'
  content TEXT,
  created_at TEXT
)

quiz_questions(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  question TEXT, options_json TEXT, correct_index INTEGER,
  explanation TEXT, kind TEXT DEFAULT 'mc', created_at TEXT
)

flashcards(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  front TEXT, back_md TEXT, created_at TEXT
)

concept_maps(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  layout_json TEXT, created_at TEXT
)

audio_summaries(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  script TEXT, voice_a TEXT, voice_b TEXT,
  file_path TEXT, duration_sec REAL, created_at TEXT
)

roleplays(
  id INTEGER PK,
  session_id INTEGER REFERENCES sessions(id),
  title TEXT, context_md TEXT, your_role TEXT,
  participants_json TEXT,  -- nombres reales
  key_phrases_json TEXT,   -- frases que debías usar
  feedback_md TEXT,
  created_at TEXT
)

settings(key TEXT PK, value TEXT)   -- preferencias de UI (no llaves secretas)
```

**Llaves secretas** viven SOLO en `config.local.json` (excluido de git, permisos
restringidos), nunca en la base ni en código.

---

## 6. Flujo de una sesión (detallado)

### 6.1 Iniciar
1. Usuario presiona **▶ Iniciar session** en el gadget.
2. Backend crea `session` (`status='recording'`, `session_number` = correlativo,
   `started_at` = ahora), crea la carpeta `data/sessions/<id>/audio/`.
3. Se abre el **stream de loopback WASAPI** del dispositivo de salida (captura todo lo
   que el usuario escucha: teacher, compañeros, videos, y la propia voz del usuario si
   el "hear my own voice" de Zoom está activo; si no, la voz del usuario). *(Ver 6.6.)*
4. Se abre un **WebSocket** a la SPA si ya está abierta; el gadget pasa a estado
   ⏺ grabando (verde, con cronómetro `0:00:00`).

### 6.2 Captura y chunks
1. El audio se convierte a **16 kHz mono 16-bit PCM** (parámetro estándar de Deepgram).
2. Se acumula en **chunks de 90 segundos** → `audio/chunk_<start_s>.wav`.
3. Cada chunk completo entra en la cola `pending_transcriptions`.
4. Detección de **silencios** (RMS de energía por ventana de 200 ms): si hay ≥ 45 s de
   silencio continuo dentro de un chunk o entre chunks, se registra un candidato a
   `timeline_events` de tipo `break` (marca temporal, se confirma en el pase final).

### 6.3 Transcripción (Deepgram Nova-3 + diarización)
1. El worker de transcripción toma el chunk pendiente y lo envía al endpoint
   **pre-recorded** de Deepgram (`model=nova-3`, `language=en`, `diarize=true`,
   `punctuate=true`, `timestamps=true`).
   - Uso _pre-recorded_ (no streaming) por chunk de 90 s: más barato ($0.0048/min) y con
     latencia de 2–5 s por chunk, suficiente para "en vivo casi inmediato".
2. El resultado se normaliza en `transcript_segments`:
   - Cada palabra/locución con `start_t`, `end_t` y `speaker_index` local del chunk.
   - **Fusión de hablantes entre chunks:** se mantiene un mapeo global
     `speaker_local(chunk) → speaker_global`; si el hablante al final del chunk N es el
     mismo que al inicio del chunk N+1 (comprobando solape de turnos y continuidad de
     sentencia), se reutiliza el mismo índice global. Con 2–4 hablantes esto es estable.
3. El backend **emite por WebSocket** a la SPA: `{type:'segment', data:{...}}` para
   actualizar la transcripción en vivo.

### 6.4 Estructuración en vivo (integración incremental)
- Cada **5 minutos** (o al acumular ~1.500 palabras nuevas sin integrar), se ejecuta una
  pasada con **DeepSeek V4 Flash** (modelo barato):
  - Entrada: (a) estructura borradora actual (`topics` draft con puntos en JSON),
    (b) texto nuevo desde la última pasada, con sus timestamps y hablantes.
  - Instrucciones: _"Integra los puntos nuevos sin repetir, agrega temas nuevos al final,
    mantén la concisión. Devuelve la estructura completa actualizada en JSON."_
  - Corrección de duplicados, reagrupación ligera y anotación en español solo cuando un
    punto se considera concepto difícil.
- La SPA refresca la vista "Notas (borrador)" con marca de que aún es borrador vivo.
- **Control de costo:** solo se envía la estructura + transcript nuevo; nunca se reenvía
  todo el transcript histórico en esta fase.

### 6.5 Pase final (al presionar ▶ Detener)
Se marca `status='processing'` y se lanza el **pase de pulido** en segundo plano con
**DeepSeek V4 Pro** (o **GLM-5.2**, configurable):
1. Entrada: transcript completo + timestamps + hablantes + candidatos de receso + la
   estructura borradora.
2. `solicitud` devuelve en llamadas JSON separadas:
   - **Línea de tiempo** (`timeline_events`): segmentos con horas absolutas y etiquetas
     (`topic`, `break`, `activity`, `roleplay`, `closing`).
   - **Temas finales** (`topics` final): orden, título, resumen conciso en inglés con
     explicaciones difíciles en español, frases textuales (`phrases_json`), vocabulario
     nuevo (`vocab_json`), hablantes clave.
   - **Roleplays** (`roleplays`): contexto, participantes (nombres), qué dijo el usuario,
     feedback constructivo.
   - **Propuesta de nombres de hablantes** (`session_speakers.suggested_name`).
   - **Título de la sesión** (si el usuario no lo editó).
3. Se actualizan los `transcript_segments.topic_id` según la línea de tiempo.
4. Se genera la versión **MP3** de la sesión y los **recortes de audio por segmento** de
   la línea de tiempo (para el playback por tramo). *(Ver 6.7.)*
5. Se marca `status='done'` y se notifica por WebSocket y en el gadget (estado 🟠 durante
   "processing", también en la SPA).

> El borrador en vivo queda sustituido por el libro final; la edición manual posterior
> del usuario tiene siempre prioridad (no se sobreescribe lo que el usuario editó).

### 6.6 Captura de la voz propia (matiz importante)
- El loopback captura el audio de salida. En Zoom, la **"voz del usuario"** se escucha
  solo si el usuario activa *Settings → Audio → "Hear my own voice"* y habla por el
  micrófono.
- **Solución A (recomendada):** el usuario activa "Hear my own voice" durante las clases
  (guía en pantalla de primera ejecución). El loopback captura todo y los dos hablantes
  entran bien diferenciados.
- **Solución B (sin micrófono):** si el usuario no quiere activar esa opción, se captura
  solo el audio entrante (teacher/compis) y el transcript de "lo que dijo el usuario" se
  infiere por contexto donde la teacher repite o responde; la línea de tiempo lo marca
  como `"[tu voz no capturada]"`. La calidad de diarización dependerá de eso.
- Configurable en Ajustes: `capture_mode = loopback | loopback+mic`.

### 6.7 Almacenamiento y playback del audio
- Chunks WAV crudos: `data/sessions/<id>/audio/chunk_<start_s>.wav` — 16 kHz mono 16-bit
  = 32 KB/s → **≈ 2,9 MB por chunk de 90 s** y **≈ 400 MB por clase de 3,5 h**.
  (16 kHz es el formato nativo de Deepgram y mucho más ligero que 44,1 kHz.)
- Al finalizar: se genera `session.mp3` (mono 48 kbps, ≈ 75 MB por clase de 3,5 h) y clips
  `seg_<start_s>.mp3` por segmento de la línea de tiempo (para el playback por tramo).
- Ajuste `keep_raw_audio` (por defecto **OFF**): los WAV crudos se borran tras generar el
  MP3 (notas y clips se conservan; la re-transcripción usará el MP3, soportado por
  Deepgram). Con OFF, el disco por clase es ≈ 75 MB.
- El "ir a la línea de tiempo" reproduce el segmento desde la web (el navegador toma el
  clip MP3 local servido por FastAPI).

---

## 7. Especificación del gadget flotante

- **Tecnología:** pywebview (WebView2 de Windows) → ventana **sin bordes, transparente,
  siempre al frente (always-on-top)**.
- **Apariencia:** burbuja compacta (≈ 44 px) tipo chat-floating; arrastrable (drag por
  toda la burbuja, mmove con HTML/CSS); click → menú.
- **Estados visuales:**
  - ⚪ Gris = sin sesión activa.
  - 🔴 Verde (idle usuario: "recording") con cronómetro y puntos animados = grabando.
  - 🟠 Naranja con spinner = procesando el libro final.
  - ⚠️ Rojo con ! = error (p.ej. dispositivo de audio cerrado, cuota agotada); tooltip
    con el mensaje.
- **Menú (click):**
  - ▶ Iniciar sesión / ⏹ Detener y finalizar (botón principal, alterna).
  - 🌐 Abrir plataforma (abre `http://localhost:8787`).
  - ⚙️ Configuración (abre la pestaña de ajustes de la SPA).
  - 🧹 Quitar (oculta el gadget; se restaura desde el ícono del sistema / comando).
- **Comunicación:** HTTP + WebSocket al backend local. El gadget no guarda estado propio.

---

## 8. Plataforma web (SPA) — pantallas y funcionalidades

### 8.1 Pantalla "Mis cuadernos"
- Lista ordenada por fecha: título, número de sesión, fecha, duración, temas detectados,
  estado (grabando/procesando/completa) y badge de la cuenta (opcional, campo editable).
- Acciones: **abrir**, **renombrar**, **eliminar** (borra todo, incl. audio, con confirmación).

### 8.2 Pantalla de cuaderno (pestañas)
1. **📝 Notas** — temas en tarjetas ordenadas; cada tema: título, puntos clave concisos
   (inglés), explicaciones difíciles (español, destacadas), frases textuales con
   traducción, vocabulario. Editable (markdown); el usuario marca lo que ha dominado.
   Incluye la **Línea de tiempo** con horas absolutas, recesos detectados y **playback
   por segmento**.
2. **💬 Chat** — chatbot con todo el contexto de la sesión (ver §9).
3. **🎙️ Resumen de audio** — botón "Generar podcast" + reproductor + guardado (ver §10).
4. **❓ Quiz** — botones de generar N preguntas, responder, corregir, regenerar.
5. **🃏 Flashcards** — generación, barajado, voltear, "domino/repaso".
6. **🗺️ Mapa conceptual** — generación y render visual (SVG), nodos/aristas desde JSON.
7. **🎭 Roleplays** — tarjetas con contexto, participantes, frases clave y feedback.
8. **📜 Transcripción** — transcript completo segmentado por hablante, con búsqueda y
   click para saltar al audio.

### 8.3 Panel "¿Quién es quién?"
- Se muestra tras cada pase final y al abrir un cuaderno sin confirmar.
- Para cada `session_speakers`: color, **nombre propuesto por la IA**, selector de
  "Personas conocidas" + campo libre, rol (`me`, `teacher`, `student`).
- Botón "Guardar (y recordar para próximas sesiones)". Las confirmaciones se persisten en
  `people` y se reutilizan automáticamente en la próxima sesión cuando coincide el patrón
  de hablantes (mismo nº de hablantes y perfil de duración); se avisa "¿Usar los nombres
  guardados?" para confirmar.

### 8.4 Configuración ⚙️
- Llaves: **OpenCode Go** (autocompletado del endpoint base) y **Deepgram**. Se guardan en
  `config.local.json`. Indicador visual de "conectado" (test call: 1 chat completion
  mínima + 1 petición de saldo de Deepgram cuando exista).
- Modelos: en vivo (Flash), libro final (Pro/GLM), chat, podcast.
- Preferencias: idioma de notas, `capture_mode`, `keep_raw_audio`, `auto_generate_all`
  (genera podcast/quiz/flashcards/mapa automáticamente al finalizar), autostart.
- **Backup/exportar** sesión (zip con DB export + audio) y **restaurar**.

---

## 9. Chatbot

- **Contexto:** (a) libro final del cuaderno (temas+frases+vocabulario+tiempo), (b)
  `transcript_segments` con hablantes, (c) persona del curso (rol del usuario, cuenta
  actual si se indica).
- **Selección de contexto (RAG-lite):** BM25 simple (Python puro) sobre los segmentos por
  tokens de la pregunta → se envían los N (por defecto 8) segmentos más relevantes + la
  estructura del libro completa en formato compacto + últimos 6 mensajes.
- **Modelo:** DeepSeek V4 Flash (barato) por defecto; GLM-5.2 opcional.
- **Comportamientos:**
  - Responde en el idioma en que se le pregunta.
  - `"explícame"` / `"qué significa"` → explica en español con ejemplo.
  - `"tráduce ..."` → inglés ↔ español.
  - `"hazme un quiz"` → genera quiz rápido interactivo desde el contexto.
  - `"evalúame"` → hace preguntas del cuaderno y corrige con feedback.
  - Referencia siempre qué parte de la clase sustenta la respuesta (tramo de tiempo).

---

## 10. Resumen de audio tipo podcast

1. Botón **"Generar podcast"** (o automático si `auto_generate_all`).
2. **Guion:** LLM (Flash) redacta una conversación natural en inglés entre un host A y un
   host B (3–6 min, puntos clave de la sesión, entonación de audio-documental). Salida
   JSON: `[{speaker:'A'|'B', text:...}]`.
3. **TTS:** edge-tts con dos voces distintas de inglés (p.ej.
   `en-US-SaraNeural` / `en-US-ChristopherNeural`); concatenación MP3 48 kbps.
4. Se guarda `audio_summaries` y se expone en la pestaña "🎙️". Regenerable.
5. Costo: $0 (edge-tts).

---

## 11. Robustez, errores y casos límite

| Caso | Comportamiento |
|---|---|
| Sin internet durante la clase | La captura local nunca se detiene; los chunks en cola se mantienen en `pending_transcriptions` y se procesan al reconectar. La integración en vivo se salta; el pase final usa el transcript completo (los puntos no estructurados se incluyen igual). |
| Falla/cuota de Deepgram | Reintentos con backoff exponencial (3). Si cae la cuota, switch manual a fallback: Whisper local (sin diarización) o Gemini free-tier — configurable en Ajustes. Se marca el transcript "sin diarización". |
| Falla de OpenCode Go | La pasada se pospone y se reintenta; nunca se pierde transcript. |
| Crash a mitad de sesión | Al relanzar, se detecta `sessions.status='recording'` → diálogo: **Finalizar / Descartar / Continuar grabando**. |
| Cierre inesperado del gadget | El backend sigue grabando (el gadget es solo control); al reabrir el gadget recupera el estado real. |
| Error de dispositivo de audio (Zoom sin salida / sin mic) | Detección y aviso en el gadget; la clase puede seguir con el transcript parcial hasta que se restaure. |
| Dos clases el mismo día | Cada inicio crea una sesión nueva (diseño por sesión). Se vigilan los límites de uso de Go (5 h rodantes, $12). |
| Disco lleno | Umbral de aviso (ajuste `min_free_space_mb`, aviso al 10 % restante). |

**Logging:** `logs/app.log` con rotación; nivel DEBUG opcional en Ajustes.

---

## 12. Privacidad y consideraciones legales

- Todo el contenido (audio, transcript, notas) reside **localmente**.
- Llaves de API guardadas en `config.local.json` (fuera de git, permisos restrictivos).
- Deepgram: no entrena con datos de clientes; el crédito $200 no vence.
- OpenCode Go (fuente del proveedor vía Zen): políticas de **zero-retention** para los
  modelos listados.
- **AVISO al usuario:** la grabación de clases puede requerir permiso de la empresa/proveedor
  del curso. La herramienta implementa **borrado total** de un cuaderno (1 clic, con
  confirmación) y en Ajustes se puede desactivar el guardado de audio crudo.
- Se incluirá una pantalla de "Aviso de uso responsable" en la primera ejecución.

---

## 13. Costos estimados

| Concepto | Costo por clase (3,5 h) | Nota |
|---|---|---|
| Deepgram Nova-3 pre-recorded + diarización | ~$1,43–2,00 | Dentro del crédito **$200 gratis** ≈ 100–140 clases (~5 meses a razón de 1 clase/día). Sin tarjeta. |
| OpenCode Go (texto: en vivo + final + chat) | ~$0,20–0,40 | Dentro de la suscripción de $10/mes. Modelo dominante: DeepSeek V4 Flash. |
| edge-tts (podcast) | $0 | Gratis, sin llave. |
| **Total bolsillo** | ≈ **$0** | Hasta ~140 clases; después, ~$1,5–2/clase o Whisper local gratis. |

---

## 14. Fases de implementación (granuladas)

Cada fase concluye con **criterios de verificación** ejecutables y se entrega en el
repositorio con su propia prueba de humo.

- **Fase 0 — Cimientos.** Estructura de carpetas, `venv`, `requirements.txt`, `git init`,
  `config.local.example.json`, FastAPI con `/health` y página estática "Hola".
  *Verificar:* `uvicorn` arranca, `/health` responde 200, SPA se sirve en `:8787`.
- **Fase 1 — Cuadernos.** CRUD de `sessions` + lista y detalle básico (sin audio).
  *Verificar:* crear/abrir/renombrar/eliminar cuaderno desde la web (SQLite correcta).
- **Fase 2 — Captura + gadget.** Loopback WASAPI → chunks WAV con timestamps; gadget
  burbuja (iniciar/detener/estado/abrir web). *Verificar:* 3,5 h simuladas con un archivo
  de prueba generan chunks correctos; el gadget draga y cambia estados.
- **Fase 3 — Transcripción + diarización.** Cola `pending_transcriptions`, Deepgram
  pre-recorded, fusión de hablantes entre chunks, WebSocket a la SPA, transcript en vivo.
  *Verificar:* un audio de prueba de 2 hablantes produce segmentos con hablantes estables
  y timestamps; sin-red reencola.
- **Fase 4 — Estructuración en vivo.** Integración incremental (Flash), borrador de temas
  con refresco en la SPA, detección de silencio. *Verificar:* sobre 30 min de audio de
  prueba, aparecen 3–5 temas consistente y sin duplicados al pausar/retomar.
- **Fase 5 — Libro final + ¿Quién es quién?.** Pase de pulido (Pro/GLM), línea de tiempo,
  temas finales, roleplays, propuesta de nombres, memoria de personas.
  *Verificar:* cuaderno de prueba contiene timeline con horas y recesos; nombres
  propuestos coinciden al 80 % con los reales de un audio de control; persistencia de
  personas.
- **Fase 6 — Playback por segmento.** MP3 de sesión y clips; reproducción desde la web.
  *Verificar:* click en tramo de la timeline reproduce exactamente ese intervalo.
- **Fase 7 — Podcast (edge-tts).** Guion + 2 voces + MP3. *Verificar:* audio de 3–5 min
  legible y natural, regenerable.
- **Fase 8 — Chatbot.** RAG-lite BM25 + chat con contexto del cuaderno + funciones
  "explícame/evalúame/quiz". *Verificar:* preguntas sobre un cuaderno de prueba con
  respuestas correctas referenciando el tramo.
- **Fase 9 — Quiz, flashcards, mapa conceptual.** Generación + interacción visual.
  *Verificar:* 10 preguntas razonables; flashcards volterán correctamente; mapa renderiza.
- **Fase 10 — Configuración, robustez y privacidad.** Ajustes completos (llaves + test de
  conexión), crash-recovery, aviso legal en primera ejecución, borrado total, backup/restaurar.
  *Verificar:* simulación de crash/norede/falla Deepgram; backup/restaura conserva
  cuadernos.
- **Fase 11 — Beta real y pulido UX.** Orden editorial de notas, edición manual con
  prioridad de usuario, notificaciones, guías de primera ejecución, afinación de prompts,
  mediciones de costo en UI. *Verificar:* 3 clases reales completas de punta a punta.

---

## 15. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Diarización imprecisa con voz de usuario por mic no capturada | Guía de activación "Hear my own voice"; fallback claro (`[tu voz no capturada]`); etiquetado manual de 1 clic. |
| La IA fusiona/crea hablantes fantasma | El pase final valida con el transcript; el panel "¿Quién es quién?" permite corregir; los nombres nunca se aplican sin confirmación. |
| Costo de OpenCode Go por uso en vivo de ~3,5 h | Modelo Flash para todo lo repetitivo; ventana de contexto sólo con delta; vigilancia del límite en UI; opción de no integrar tan seguido (intervalo configurable). |
| Privacidad del audio corporativo | Todo local; borrado total; aviso legal; Deepgram sin entrenamiento con datos. |
| Calidad del audio (eco, volumen bajo) | Loopback del dispositivo correcto; normalización; monitoreo de RMS con alerta si la señal es casi silencio. |
| cambio de precio/crédito Deepgram | Fallback a Whisper local (gratis) o Gemini; los puntos de integración de STT quedan aislados tras una interfaz. |

---

## 16. Criterios de aceptación generales (done definition)

1. Una clase real de 3,5 h genera: línea de tiempo con horas, recesos detectados, ≥1 tema
   bien delimitado por bloque, transcript completo, frases textuales, vocabulario,
   roleplays identificados cuando existan.
2. Los hablantes aparecen **con nombre real** (confirmados por el usuario una vez por
   persona) en notas, timeline y transcript.
3. Notas: concisas, sin duplicados, en inglés con explicaciones difíciles en español.
4. Podcast, quiz, flashcards y mapa son **regenerables**; ver lo ya generado funciona
   offline (todo es local), pero **generarlos** requiere conexión (LLM/Deepgram/edge-tts).
5. El borrado de un cuaderno elimina todos sus datos (base + audio).
6. Configuración con test de conexión de llaves funcional.