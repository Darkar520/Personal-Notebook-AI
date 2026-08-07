# Decisiones de implementación: dónde y por qué me separé del plan

**Fecha:** 2026-08-05
**Alcance:** implementación completa de las Fases 0–11 del plan
`docs/superpowers/plans/2026-08-04-personal-notebook-ai.md`.

El plan se siguió fase por fase y su arquitectura se respetó por completo: un proceso
Python con FastAPI en `localhost:8787`, SPA vanilla sin build, SQLite + ficheros en `data/`,
gadget pywebview, Deepgram para STT y OpenCode Go para texto. Lo que sigue son los cambios
concretos, cada uno con **el problema real que resuelve**. No hay ninguno "estético": si el
plan ya resolvía algo bien, se dejó tal cual (por ejemplo el modelo de datos, la cola
persistente, el troceado en chunks de 90 s, la integración incremental solo con el delta del
transcript, o el uso del endpoint *pre-recorded* de Deepgram en vez de streaming).

Los cambios marcados con 🔴 corrigen algo que **habría impedido usar la aplicación**.

---

## 1. Errores que habrían roto la app en la primera clase

### 🔴 1.1 El dispositivo de loopback nunca se habría encontrado

El plan elegía el dispositivo comparando nombres con igualdad exacta:

```python
if dev["isLoopbackDevice"] and dev["name"] == default_speakers["name"]:
```

En esta misma máquina, el dispositivo real se llama
`Speaker / Headphone (Realtek(R) Audio) [Loopback]` y la salida por defecto
`Speaker / Headphone (Realtek(R) Audio)`. La igualdad **nunca** se cumple, así que
`RuntimeError("No hay dispositivo en bucle")` y no se graba nada.

`app/capture/devices.py` usa una cascada: `get_default_wasapi_loopback()` si existe →
coincidencia por prefijo/subcadena tolerando el sufijo `[Loopback]` y el truncado a 31
caracteres de Windows → cualquier loopback disponible con aviso en el log. Verificado
contra el hardware real.

### 🔴 1.2 Violación de acceso nativa al grabar con micrófono

PortAudio no es reentrante al crear su contexto ni al abrir streams. El diseño de dos
pistas (sistema + micrófono) abría los dos streams desde hilos distintos y el proceso
moría con `Windows fatal exception: access violation` — reproducido y capturado con
`faulthandler`. Ahora hay **una sola** instancia de `PyAudio` por grabador y un cerrojo
(`_PA_LOCK`) que serializa `init`/`open`/`close`; la lectura sigue siendo concurrente.

### 🔴 1.3 `join()` fallaba justo al detener la grabación

`_StreamReader` hereda de `threading.Thread` y guardaba el evento de parada en
`self._stop`, que colisiona con el método interno `Thread._stop`. Al detener:
`TypeError: 'Event' object is not callable`, con la sesión a medio cerrar. Renombrado a
`_stop_event` y documentado en el propio código.

### 🔴 1.4 Los eventos en vivo no llegaban al navegador

El plan emitía por WebSocket desde hilos con `asyncio.ensure_future(...)` (no hace nada si
no hay loop corriendo) y, en la Task 3.5, con `asyncio.run(...)` (crea un loop nuevo que
**no puede** escribir en sockets del loop de uvicorn). Resultado: transcripción en vivo
muerta. `app/ws.py` guarda el loop principal en el `lifespan` y publica con
`run_coroutine_threadsafe`, que es la forma correcta de cruzar de hilo a loop.

### 🔴 1.5 Guardar cualquier ajuste borraba las llaves de API

`PUT /api/settings` del plan interpretaba `api_key: ""` como "borrar", pero el formulario
enviaba el objeto completo y el campo de contraseña siempre viaja vacío si no se reescribe.
Abrir Ajustes, cambiar el intervalo y guardar dejaba la app sin llaves. Ahora vacío
significa "no tocar" y hay un endpoint explícito `DELETE /api/settings/keys/{provider}`.

### 🔴 1.6 `/api/sessions/pending-recording` era inalcanzable

El plan declaraba `GET /{sid}` antes que `/pending-recording`, así que FastAPI intentaba
convertir `"pending-recording"` en `int` y devolvía 422: la recuperación tras un cierre
inesperado no funcionaba. Las rutas literales se declaran primero (hay un test que lo fija).

### 🔴 1.7 La duración de la sesión salía desplazada varias horas

`stop_session` mezclaba `datetime.now().timestamp()` (hora local, *naive*) con `started_at`
en UTC. Con UTC−5 toda clase duraba 5 h de más. Se centralizó en `db.duration_between` /
`db.parse_iso`, siempre en UTC, y la hora de pared se calcula con el `tz_offset_min`
guardado al grabar (así una clase sigue mostrando "08:00" aunque cambie el horario de
verano).

### 🔴 1.8 Un identificador de modelo equivocado dejaba la app sin IA

El plan fija `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`. Si el proveedor los nombra
distinto (`deepseek/deepseek-v4-flash`, sufijos de fecha…), *toda* llamada devuelve 404 y
no hay notas, ni libro, ni chat, ni materiales, sin pista del motivo. `app/ai/models.py`
consulta `GET /models`, cachea el catálogo y resuelve por coincidencia exacta → sufijo/
prefijo → comodines de `model_fallbacks`; si no hay catálogo usa el id tal cual. Ajustes
muestra qué modelo se usará de verdad para cada rol.

### 🔴 1.9 `json.loads` directo sobre la respuesta del modelo

Todos los generadores hacían `json.loads(content)`. Los modelos devuelven vallas de
código, prosa alrededor, comas sobrantes o JSON truncado por `max_tokens`; cualquiera de
esas cosas tiraba el pase final de una clase de 3,5 h y el dinero de la llamada.
`app/ai/jsonx.py` recupera el JSON en cuatro pasadas (directo → sin vallas → recorte
balanceado → reparaciones) y `chat_json` reintenta una vez con instrucción estricta.

### 🔴 1.10 SQLite se habría bloqueado a mitad de clase

Con el `journal_mode` por defecto y sin `busy_timeout`, tres hilos escribiendo (grabador,
worker de STT, pase final) mientras la SPA lee acaban en `database is locked`. `app/db.py`
usa WAL, `busy_timeout=15000`, `BEGIN IMMEDIATE` para escrituras y conexión por operación.

### 1.11 Otros fallos puntuales del pseudocódigo del plan

- `pipeline.run_integration` terminaba con `return list_topics(sid)`, función que no existe
  en ese módulo.
- `content.replace_draft` acababa en `return db.get_conn and list_topics(sid)`.
- `session_audio.render_session` invocaba
  `getattr(__import__("app.config", fromlist=[""]), "load_config")()...`.
- `queue.claim_one` hacía `SELECT` + `UPDATE` sin transacción: con `stt_concurrency > 1`,
  dos workers reclaman el mismo chunk y se paga dos veces la transcripción.
- `test_loopback_fake` afirmaba 3 chunks para 300 s con troceado de 90 s (son 4).
- El `test_concept_map` del plan tiene JSON sintácticamente inválido.
- `LoopbackRecorder.run` del plan hacía `stream.close() if 'stream' in dir() else None`,
  que nunca cierra el stream (`dir()` sin argumento devuelve nombres del *scope*, y la
  comprobación se evalúa mal en el `finally`).

---

## 2. Cambios que mejoran el resultado del producto

### 2.1 La voz propia se graba de verdad (`capture_mode="loopback+mic"`)

El plan ofrecía dos salidas para "lo que dices tú": pedirle al usuario que active
*"Hear my own voice"* en Zoom (Solución A) o renunciar y marcar `[tu voz no capturada]`
(Solución B). Grabar el micrófono como **segunda pista** en paralelo resuelve el problema
de raíz: la voz propia entra siempre, sin tocar Zoom.

El beneficio extra es mayor: la pista de micrófono dice **con certeza local** en qué
instantes hablaste. Esa máscara temporal marca `is_me` en el transcript
(`fusion.mark_is_me`) y le dice al modelo qué hablante eres tú en el pase final, en vez de
pedirle que lo intuya. Coste: cero llamadas extra.

### 2.2 Identidad de hablantes por huella vocal, no por índice

El plan fusionaba los hablantes entre chunks con *sticky-by-index*: "si el chunk N+1 tiene
los mismos M hablantes, reutiliza los índices". Deepgram numera los hablantes **por orden
de aparición en cada petición**: si en el chunk 3 habla primero la teacher es `speaker 0`,
y en el chunk 4 habla primero un compañero, ese compañero pasa a ser `speaker 0`. Con
sticky-by-index los nombres y los colores se intercambian a mitad de clase.

`app/transcription/voiceprint.py` calcula un descriptor de 80 dimensiones (banco mel
logarítmico resumido en media y desviación, con normalización cepstral) usando solo numpy;
`fusion.SpeakerRegistry` empareja cada hablante local con el global más parecido, usando el
índice solo para desempatar. Sin torch, sin speechbrain, milisegundos por chunk.

El mismo descriptor se guarda en `people.voice_json` al confirmar una persona, así la
segunda clase con la misma teacher **propone su nombre automáticamente** (`auto_matched`).
El plan proponía reconocerla por "mismo número de hablantes y perfil de duración", que
confunde a dos compañeros cualesquiera. Los nombres siguen sin aplicarse nunca sin
confirmación (requisito del spec §15).

### 2.3 Solape entre chunks para no partir frases

Cortar en seco cada 90 s parte palabras y Deepgram pierde la frase del borde ~una vez por
minuto y medio. Cada chunk arrastra 8 s del anterior (`overlap_seconds`) y
`fusion.dedupe_overlap` deja cada turno **una sola vez y completo** con dos reglas: punto
medio ≥ inicio útil, y descarte de lo que termina pegado al corte (salvo en el último
chunk, que no tiene sucesor). Los tiempos absolutos se mantienen exactos porque el fichero
sabe cuánto pre-roll lleva.

### 2.4 Un solo `session.mp3` con HTTP Range en vez de un clip por tramo

El plan generaba, además del MP3 de la sesión, un `seg_<i>.mp3` por cada tramo de la línea
de tiempo. Eso son 20–40 ejecuciones de ffmpeg por clase, el doble de disco, y solo permite
escuchar lo que la IA decidió cortar.

`app/routers/media.py` sirve un único MP3 con `206 Partial Content` y el reproductor de la
SPA hace `currentTime = start` y para en `end`. Mismo comportamiento visible, un solo
ffmpeg, ~75 MB en vez de ~150 MB, y se puede saltar a **cualquier** instante: un tema, un
receso, una frase textual del libro, una cita del chat o un turno concreto del transcript
(esto último lo pedía el spec §8.2 y con clips por tramo era imposible). `export_clip`
sigue existiendo para descargar un fragmento.

Además, `session.mp3` se construye **quitando el solape de cada chunk** y enviando el PCM a
ffmpeg por `stdin`, sin materializar un WAV intermedio de 400 MB.

### 2.5 El pase final aguanta una clase de 3,5 h

El plan metía el transcript completo en un solo prompt. Una clase real son ~27 000 palabras
y, con la estructura borrador y los recesos, el prompt roza o supera el límite del modelo:
la respuesta se trunca **en silencio** y el libro se queda sin la última hora.
`app/ai/polish.py` mide el tamaño y, si no cabe, procesa por ventanas solapadas y fusiona
los resultados en Python (determinista, sin tokens extra).

El prompt de nombres es aparte y **selectivo**: la apertura de la clase más todas las
líneas con indicios de nombre (presentaciones, vocativos, "teacher"). Más barato y más
certero que mandar 27 000 palabras donde el nombre sale tres veces.

### 2.6 La línea de tiempo es verificable, no una alucinación

Los recesos que propone el modelo se aceptan solo si hay **silencio real detectado
localmente** que los respalde (VAD sobre el audio); si no, se degradan a `activity`. Y los
silencios largos que el modelo olvidó se añaden. Además se ordena, se recorta a la duración
real y se eliminan solapes. Sin esto, la timeline puede afirmar un receso que no existió.

### 2.7 Notas realmente sin duplicados

El plan confiaba la regla "sin duplicados" a que el modelo obedeciera el prompt.
`live_integration.normalize_structure` la impone en código: normaliza el texto, descarta
puntos repetidos o contenidos en otros, fusiona temas con el mismo título y limita puntos y
notas por tema. Y si el modelo "resume" y se come temas anteriores, el borrador **no los
pierde**: la estructura en vivo solo puede crecer o refinarse.

### 2.8 Materiales de estudio que se pueden usar

- **Quiz**: se valida cardinalidad y rango, se eliminan opciones duplicadas y se **rota la
  posición de la respuesta correcta** (los LLM tienden a poner la buena en A/B). El plan
  insertaba `correct_index` sin comprobar nada: un `correct_index: 4` con 4 opciones deja
  una pregunta imposible de acertar.
- **Flashcards**: repetición espaciada real (cajas de Leitner con `due_at`); acertar sube de
  caja, fallar vuelve a la primera. El plan solo guardaba anverso y reverso.
- **Mapa conceptual**: se garantiza un grafo conexo y sin aristas colgantes, si no el SVG
  sale con nodos flotando sueltos.
- **Podcast**: síntesis concurrente con semáforo (de ~2 min a ~20 s), reintento por réplica
  ante los `NoAudioReceived` intermitentes de edge-tts, limpieza del guion para TTS (nada de
  markdown, emojis ni acotaciones leídas en voz alta) y concatenación con `-c copy` más
  silencios entre turnos. Al modelo se le pasa el contenido real de la clase (puntos,
  vocabulario, roleplays), no solo los títulos: el plan enviaba `[{"title": ...}]` y el
  podcast salía genérico.

### 2.9 Chat que cita de verdad

El plan enviaba el contexto como `sp0`, `sp1`, así que el modelo no podía cumplir el
requisito del spec §9 ("referencia siempre qué parte de la clase sustenta la respuesta").
Ahora el contexto lleva **nombres reales y hora de pared**, la respuesta devuelve las citas
con su instante y la interfaz ofrece un botón para escuchar ese momento. Las stopwords son
bilingües (las preguntas llegan en español) y se incluyen los turnos vecinos al mejor
resultado, que es donde suele estar la explicación completa. El historial vive en la base,
no en el navegador: recargar la página ya no borra la conversación.

### 2.10 Concatenación de audio que no se rompe con clases largas

El plan unía audio con `-filter_complex concat=n=N` y un `-i` por fichero. Con ~140 chunks
la línea de comandos se acerca al límite de Windows (32 768 caracteres) y, además,
`filter_complex` recodifica todo. Se usa el demuxer `concat` con lista en fichero y
`-c copy`: segundos en vez de minutos y sin pérdida. Con respaldo a recodificación si los
formatos difieren.

---

## 3. Seguridad y privacidad (añadidos, no estaban en el plan)

### 3.1 El servidor local no era inocuo

`localhost:8787` exponía una API sin autenticación con transcripciones de clases
corporativas. Dos vectores reales desde el navegador:

- **CSRF simple**: cualquier página abierta puede hacer `POST http://localhost:8787/api/...`
  (las peticiones "simples" no llevan preflight), por ejemplo para borrar cuadernos.
- **DNS rebinding**: un dominio atacante que resuelva a `127.0.0.1` se vuelve *same-origin*
  y puede **leer** las respuestas, es decir, todo el transcript.

`app/security.py` exige `Host` loopback, valida `Origin` contra una lista blanca, rechaza
`Sec-Fetch-Site: cross-site` en escrituras y añade `no-store` + `nosniff`. Opcionalmente
exige un token local (`data/api.token`) para clientes que no son el navegador.

### 3.2 Zip-slip en la restauración de backups

`restore_session` del plan hacía `z.extractall(...)` con los nombres tal cual vienen dentro
del ZIP. Una entrada `../../../AppData/...` escribe fuera de la carpeta de destino. Ahora
cada entrada se valida y se copia una a una.

### 3.3 Las llaves no vuelven al navegador

`GET /api/settings` del plan devolvía el `cfg` completo, con `api_key` en claro, y añadía
una máscara al lado. `config.public_config()` vacía el campo y expone solo `..._masked` y
`..._set`. Y `config.local.json` se escribe con permisos restringidos (`icacls`).

### 3.4 Restauración con claves ajenas remapeadas

El plan reinsertaba `topic_id` con el identificador de la máquina de origen: las notas del
transcript restaurado apuntaban a temas de otra sesión. Ahora se traducen `session_id`,
`topic_id` y `person_id`, y el ZIP lleva un manifiesto con la versión de esquema (restaurar
un backup de una versión futura avisa en vez de corromper la base). El ZIP incluye además
un `notes.md` legible sin la aplicación: es el seguro de vida del contenido del usuario.

---

## 4. Cambios de estructura

| Cambio | Motivo |
|---|---|
| `app/paths.py` resuelve rutas **en cada llamada** (`NOTEBOOK_HOME`, `NOTEBOOK_DATA_DIR`) en vez de constantes de módulo | El plan congelaba `DB_PATH`/`CONFIG_PATH` al importar y los tests tenían que parchearlas módulo a módulo (frágil, y un olvido escribe en los datos reales del usuario). Además permite mover `data/` a otro disco. |
| `app/errors.py` con la jerarquía de excepciones | El plan definía `AIError` en `app/transcription/__init__.py`, así que el cliente LLM tenía que importar el paquete de transcripción. |
| `app/ai/prompts.py` centraliza todos los prompts | La Fase 11 consiste en afinar prompts; repartidos en siete módulos, cada ajuste toca siete ficheros y no hay forma de ver el estilo editorial completo. |
| `app/runtime.py` con el supervisor de fondo | En el plan el bucle vivía en `main.py` con `except Exception: pass`, que se traga cualquier fallo sin rastro. Ahora registra errores, aplica backoff y emite latidos. |
| `app/ai/study.py` unifica quiz, flashcards y mapa | Eran tres módulos de ~15 líneas que hacían lo mismo; la sustancia está en la validación, que sí es compartida. |
| `app/schemas.py` con un modelo Pydantic por operación | El plan usaba `body: dict` en casi todos los endpoints de escritura: la validación queda en cada handler y un payload inesperado da 500 en vez de 422. |
| `app/audio/session_audio.py` separado de `conversion.py` | Separa "ejecutar ffmpeg" de "construir el audio de una sesión"; el primero se prueba sin sesiones y el segundo sin ffmpeg. |
| Migraciones con `PRAGMA user_version` | El plan proponía `ALTER TABLE` dentro de `try/except` (Task 11.1). Con versión explícita el esquema es reproducible y auditable. |
| FTS5 con contenido externo y triggers | La búsqueda en el transcript usa el índice nativo con ranking BM25 en vez de `LIKE` sobre miles de filas. El BM25 en Python puro se mantiene para el retrieval del chat, donde hace falta puntuar y expandir vecinos. |
| Tabla `usage_events` | El plan guardaba el consumo como JSON en `settings` con contadores aproximados (`llm_calls * 0.005`). Con una tabla se registran tokens y minutos **reales** por propósito, que es lo que hace útil el estimador de costes. |
| SPA en módulos ES (`static/js/...`) | Sigue sin paso de compilación (el requisito del plan), pero un único `app.js` de ~2 000 líneas es inmantenible. Se sirve por HTTP, así que los módulos funcionan sin más. |
| El gadget se carga por HTTP, no por `file://` | Cargarlo como fichero lo deja en un origen opaco (`Origin: null`), lo que complica la guardia local y el WebSocket. |

---

## 5. Lo que el plan pedía y quedó igual

Para que quede claro qué **no** se cambió: el modelo de datos de la spec §5 (ampliado, no
alterado), el troceado en chunks de 90 s, el uso del endpoint pre-recorded de Deepgram con
`diarize`, la integración incremental cada 5 minutos enviando solo el delta, el pase final
en llamadas JSON separadas (timeline+temas+roleplays / nombres / título), `keep_raw_audio`
por defecto en `false`, el borrado total de un cuaderno, la memoria de personas, el aviso
legal en el primer arranque, la recuperación tras caída con las tres opciones
(finalizar/descartar/continuar), el aviso de disco, el backup/restore en ZIP, y el criterio
editorial "que sume y no que reste": inglés con explicaciones puntuales en español.

---

## 6. Estado y qué falta

- **294 pruebas** en verde, sin red ni micrófono (proveedores con dobles, audio sintético).
- Captura WASAPI verificada contra el hardware real de esta máquina: loopback + micrófono
  simultáneos, chunks con solape correcto y cierre limpio.
- Pendiente y solo verificable contigo: **la Fase 11 con clases reales**. El guion está en
  `docs/beta-checklist.md`. Lo que no se puede validar sin una clase de verdad es la
  calidad editorial de las notas, el acierto de los nombres propuestos y la naturalidad del
  podcast; los tres se ajustan editando `app/ai/prompts.py`, sin tocar el resto del código.
- **Aviso de disco**: en este equipo quedan ~2,2 GB libres. Una clase con audio crudo ocupa
  ~475 MB; con el ajuste por defecto (`keep_raw_audio: false`) unos 75 MB. Conviene liberar
  espacio antes de la primera clase de 3,5 h.
