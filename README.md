# Personal Notebook AI

Cuaderno inteligente para clases de inglés en línea. Graba el audio de la clase, lo
transcribe con diarización, escribe notas bilingües mientras la clase ocurre y, al
terminar, genera el **libro de la sesión**: línea de tiempo con horas reales, temas,
frases textuales, vocabulario, roleplays, podcast, quiz, flashcards, mapa conceptual y un
chatbot que conoce la clase.

Todo corre **en tu equipo**. El audio y las notas nunca salen de tu disco, salvo las
llamadas puntuales a los servicios de transcripción y de texto.

---

## Requisitos

| Requisito | Detalle |
|---|---|
| Sistema | Windows 10/11 (la captura usa WASAPI loopback) |
| Python | 3.11 o superior |
| Llave de OpenCode Go | Notas, libro final, chat y materiales de estudio |
| Llave de Deepgram | Transcripción con diarización (crédito gratuito de $200) |
| Disco | ~75 MB por clase (o ~475 MB si conservas el audio crudo) |

`edge-tts` (voces del podcast) y `ffmpeg` (vía `imageio-ffmpeg`) no necesitan llave.

---

## Instalación

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.local.example.json config.local.json
```

Abre `config.local.json` y pega tus llaves, o hazlo desde **Ajustes** en la propia app
(recomendado: valida las llaves con un clic).

---

## Uso

```bat
python run.py
```

Aparece una **burbuja flotante** siempre encima de Zoom y la plataforma queda disponible
en <http://127.0.0.1:8787>.

1. **Antes de la clase**: pulsa `▶ Iniciar clase` en la burbuja.
2. **Durante**: la transcripción aparece en vivo y las notas en borrador se actualizan
   cada pocos minutos. Puedes cerrar el navegador: la grabación no depende de él.
3. **Al terminar**: pulsa `⏹ Detener y finalizar`. En unos minutos tendrás el cuaderno.

Variantes útiles:

```bat
python run.py --no-gadget      :: solo el backend (si el gadget da problemas)
python run.py --open           :: abre además el navegador
```

### Ensayar sin clase real

Puedes recorrer el pipeline completo con un WAV cualquiera, sin gastar una clase:

```bat
curl -X POST http://127.0.0.1:8787/api/sessions/start ^
  -H "Content-Type: application/json" ^
  -d "{\"source_wav\": \"C:/ruta/a/tu/audio.wav\", \"realtime\": false}"
```

---

## Qué obtienes en cada cuaderno

| Pestaña | Contenido |
|---|---|
| 📝 Notas | Temas en orden, puntos en inglés, explicaciones en español solo donde hacen falta, frases textuales con traducción y vocabulario nuevo. Editable: **tus cambios nunca se sobrescriben**. |
| 🕒 Línea de tiempo | Horas reales de cada bloque y recesos detectados. Un clic reproduce ese tramo exacto. |
| 📜 Transcripción | Quién dijo qué, con búsqueda y salto al audio en cualquier turno. |
| 🧑‍🤝‍🧑 ¿Quién es quién? | La IA propone los nombres reales; tú confirmas una vez y la app los recuerda por su voz para las clases siguientes. |
| 💬 Chat | Tutor de esa clase: responde en tu idioma, cita el minuto que lo respalda y te evalúa. |
| 🎙️ Resumen de audio | Podcast de dos voces repasando la clase (gratis, con edge-tts). |
| ❓ Quiz | Preguntas de opción múltiple con corrección y explicación. |
| 🃏 Flashcards | Repetición espaciada (cajas de Leitner) con repaso programado. |
| 🗺️ Mapa | Mapa conceptual en SVG. |
| 🎭 Roleplays | Contexto, participantes, frases clave y una devolución constructiva. |

---

## Privacidad y datos

- Todo vive en `data/`: `app.db` (SQLite), `sessions/<id>/` (audio) y `logs/`.
- Las llaves solo están en `config.local.json`, fuera de git y con permisos restringidos.
  La API **nunca** las devuelve al navegador.
- Borrar un cuaderno borra sus notas, su transcript y su audio.
- `Exportar` genera un ZIP con la base del cuaderno, el audio y un `notes.md` legible sin
  la aplicación.
- El servidor solo escucha en loopback y rechaza peticiones de otros orígenes o con un
  `Host` que no sea local (defensa contra CSRF y DNS rebinding desde el navegador).
- **Aviso**: grabar una clase puede requerir permiso de la empresa o del proveedor del
  curso. La app te lo recuerda en el primer arranque.

## Costes aproximados por clase de 3,5 h

| Concepto | Coste |
|---|---|
| Deepgram Nova-3 con diarización | ~$1,20 (dentro del crédito de $200 ≈ 160 clases) |
| OpenCode Go (notas, libro, chat, materiales) | ~$0,20–0,40 |
| edge-tts (podcast) | $0 |

Cada cuaderno muestra su consumo real medido, no una estimación teórica.

---

## Desarrollo

```bat
.venv\Scripts\python.exe -m pytest -q          :: 294 pruebas, sin red ni micrófono
.venv\Scripts\python.exe -m pytest tests/test_pipeline.py -v
```

Estructura:

```
app/
  paths.py config.py db.py errors.py logging_setup.py security.py ws.py disk.py
  main.py runtime.py pipeline.py schemas.py backup.py
  capture/      devices, wavio, silence (VAD), loopback (grabadores)
  transcription/ deepgram_client, fusion, voiceprint, queue, worker, fallback
  ai/           opencode_client, models, jsonx, prompts, live_integration, polish,
                chat, podcast, study
  audio/        ffmpeg, session_audio
  routers/      sessions, content, speakers, chat, generate, media, settings
gadget/         burbuja flotante (pywebview)
static/         SPA vanilla con módulos ES, sin build
docs/           especificación, plan y decisiones de implementación
```

Documentos de referencia:

- `docs/superpowers/specs/2026-08-04-personal-notebook-ai-design.md` — diseño del producto.
- `docs/superpowers/plans/` — plan de implementación por fases.
- `docs/decisiones-de-implementacion.md` — **qué se hizo distinto al plan y por qué**.
- `docs/beta-checklist.md` — guion de validación con clases reales.
