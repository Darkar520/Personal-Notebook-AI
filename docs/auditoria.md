# Auditoría — Personal Notebook AI

> Documento vivo. Se actualiza después de cada clase real y cada fase de auditoría.

## Estado de tests

- Fecha: 2026-08-06
- Resultado: 291/294 pasando en la 2ª ejecución; 294/294 en la 1ª
- Causa de los 3 fallos: **ambiental, no de código**. El disco pasó de >1024 MB libres a ~1000 MB, por debajo del umbral `min_free_space_mb // 2 = 1024` que `pipeline.start_session` exige (assert de espacio). Los 3 tests fallan con `NotebookError: Espacio insuficiente`. No se aplicaron fixes (pendiente de autorización).
- Warning no bloqueante: deprecación `httpx`/`starlette.testclient`.

## Fase 1 — Seguridad

### Hallazgos

| ID | Severidad | Archivo:Línea | Descripción | Estado |
|---|---|---|---|---|
| S1 | Crítica | app/main.py:103 (`/ws`) | `LocalGuardMiddleware` (BaseHTTPMiddleware) no envuelve WebSockets en Starlette. El endpoint `/ws` quedaba sin validación de Host/Origin: una página maliciosa con DNS rebinding podía **leer** la transcripción en vivo. | ✅ Corregido |
| S2 | Alta | app/routers/settings.py:243 (`/backup/restore`) | El endpoint de restauración no limitaba el tamaño del ZIP subido: un archivo enorme podía llenar el disco. | ✅ Corregido |
| S3 | Media | app/backup.py (export) / app/routers/settings.py:224 | Los ZIPs de `data/exports/` se acumulaban sin límite ni retención (una clase ≈ 75 MB). | ✅ Corregido |
| S4 | Baja | app/ai/opencode_client.py:141-186 | Los `snippet` de respuesta del proveedor (hasta 300 chars) se registran en logs en errores 4xx/5xx. No contienen la llave (va en header), pero podrían contener datos de la clase. Riesgo teórico bajo. | ⏳ Pendiente de decisión |
| S5 | Baja | app/transcription/deepgram_client.py:157-200 | Igual que S4 para Deepgram: el cuerpo de la respuesta se registra en logs de error. | ⏳ Pendiente de decisión |
| S6 | Baja | app/routers/media.py:118-137 (`/clip`) | `export_clip` genera MP3 bajo demanda en `sessions/<id>/clips/` sin limpieza. El nombre deriva de `start/end` (floats validados), sin path traversal posible. | ⏳ Pendiente de decisión |

### Fixes aplicados

1. **S1 — WebSocket protegido (Crítica)**
   - `app/security.py`: nueva función `check_websocket_origin(host, origin, port)` que replica la política del middleware (Host loopback + Origin en whitelist local).
   - `app/main.py`: el handshake de `/ws` valida Host y Origin antes de aceptar la conexión; si falla, cierra con código 1008.
   - Archivos: `app/security.py`, `app/main.py`.

2. **S2 — Límite de 500 MB en restore (Alta)**
   - `app/routers/settings.py`: constante `MAX_RESTORE_BYTES = 500 * 1024 * 1024`; el streaming de subida aborta con HTTP 413 y mensaje claro si se supera.
   - Archivo: `app/routers/settings.py`.

3. **S3 — Limpieza de exports (Media)**
   - `app/backup.py`: nueva función `cleanup_exports(max_age_seconds=7 días)` que borra ZIPs antiguos de `data/exports/`.
   - `app/main.py`: se invoca en el `lifespan` al arrancar (nunca impide el arranque si falla).
   - Archivos: `app/backup.py`, `app/main.py`.

### Verificaciones sin cambios necesarios

- **Path traversal en media.py**: `session_audio.session_mp3_path(sid)` y `export_clip(sid, start, end)` usan `session_id` (int validado por FastAPI) y nombres fijos (`session.mp3`, `clip_<start>_<end>.mp3` con floats validados `ge=0`/`gt=0`). El podcast usa `paths.from_data()` que rechaza rutas fuera de `data/`. **Sin vulnerabilidad.**
- **Path traversal en export**: el nombre del ZIP se sanitiza (`isalnum` + ` -_`, máx. 60 chars). **Sin vulnerabilidad.**
- **Llaves en logs/respuestas**: `public_config()` vacía los campos de llave y solo expone `_masked`/`_set`. Los `user_message` de errores son genéricos ("Falta la llave...", "La llave no es válida..."). Los headers `Authorization`/`Token` nunca se registran. **Sin filtración de llaves.**

## Fases pendientes (esperando primera clase real)

- Fase 2 — Concurrencia
- Fase 3 — Privacidad y datos
- Fase 4 — Robustez y recuperación
- Fase 5 — Calidad y mantenibilidad

## Registro de clases reales

| Fecha | Duración | Hallazgos encontrados |
|---|---|---|
| (pendiente) | — | — |