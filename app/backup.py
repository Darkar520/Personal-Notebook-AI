"""Exportar / restaurar un cuaderno completo en un ZIP.

Mejoras sobre el plan original (Task 10.4):

1. **Sin *zip-slip*.** El plan hacía `z.extractall(...)` con los nombres tal cual vienen
   dentro del ZIP. Un archivo con la entrada `../../../AppData/…` escribe fuera de la
   carpeta de destino. Aquí cada entrada se valida y se copia una por una.
2. **Copia de tablas por introspección** (`PRAGMA table_info`), así añadir una columna al
   esquema no rompe el backup ni obliga a mantener una lista a mano.
3. **Remapeo de claves ajenas.** El plan insertaba `topic_id` con el id de la máquina de
   origen: las notas del transcript apuntaban a temas de otra sesión. Aquí se traducen
   `session_id` y `topic_id`.
4. **Manifiesto con versión de esquema**: restaurar un backup de una versión futura avisa
   en vez de corromper la base.
5. **Exportación legible** (`notes.md`) dentro del ZIP: si algún día no existe la app, el
   cuaderno sigue siendo consultable. Es el seguro de vida del contenido del usuario.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from app import __version__, db, paths
from app.errors import NotebookError

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
PAYLOAD = "session.json"
NOTES = "notes.md"
FILES_PREFIX = "files/"

CHILD_TABLES = (
    "topics",
    "timeline_events",
    "transcript_segments",
    "session_speakers",
    "roleplays",
    "messages",
    "quiz_questions",
    "flashcards",
    "concept_maps",
    "audio_summaries",
    "usage_events",
)


class BackupError(NotebookError):
    """El ZIP no es un cuaderno válido o no se pudo restaurar."""


def _columns(conn, table: str) -> list[str]:
    return [str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------------------
# Exportar
# ---------------------------------------------------------------------------


def export_session(session_id: int, out_zip: Path | str) -> Path:
    target = Path(out_zip)
    target.parent.mkdir(parents=True, exist_ok=True)
    with db.read() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise BackupError(
                f"La sesión {session_id} no existe",
                user_message="Ese cuaderno ya no existe.",
            )
        payload: dict[str, Any] = {"sessions": [dict(session)]}
        for table in CHILD_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE session_id=?", (session_id,)
            ).fetchall()
            payload[table] = [dict(r) for r in rows]
        people_ids = {
            r["person_id"] for r in payload["session_speakers"] if r.get("person_id")
        }
        payload["people"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM people WHERE id IN (%s)"
                % ",".join("?" * len(people_ids)),
                tuple(people_ids),
            )
        ] if people_ids else []

    manifest = {
        "app_version": __version__,
        "schema_version": db.SCHEMA_VERSION,
        "exported_at": db.now_iso(),
        "session_title": session["title"],
        "session_number": session["session_number"],
    }
    folder = paths.session_dir(session_id)
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(PAYLOAD, json.dumps(payload, ensure_ascii=False))
        archive.writestr(NOTES, render_notes(payload))
        if folder.exists():
            for file in sorted(folder.rglob("*")):
                if file.is_file():
                    archive.write(file, arcname=FILES_PREFIX + file.relative_to(folder).as_posix())
    log.info("Cuaderno %s exportado a %s (%.1f MB)",
             session_id, target, target.stat().st_size / 1e6)
    return target


def render_notes(payload: dict[str, Any]) -> str:
    """Versión markdown del cuaderno, legible sin la aplicación."""
    session = payload["sessions"][0]
    lines = [
        f"# {session.get('title') or 'Sesión'}",
        "",
        f"- Sesión nº {session.get('session_number')}",
        f"- Inicio: {session.get('started_at')}",
        f"- Duración: {round((session.get('duration_sec') or 0) / 60)} min",
        "",
    ]
    speakers = {
        int(s["speaker_index"]): (s.get("suggested_name") or f"Speaker {int(s['speaker_index']) + 1}")
        for s in payload.get("session_speakers", [])
    }
    for topic in sorted(payload.get("topics", []), key=lambda t: t.get("sort_order") or 0):
        if topic.get("status") != "final" and any(
            t.get("status") == "final" for t in payload.get("topics", [])
        ):
            continue
        summary = db.json_loads(topic.get("summary_md"), {})
        lines.append(f"## {topic.get('title')}")
        for point in summary.get("points", []):
            lines.append(f"- {point}")
        for note in summary.get("spanish_notes", []):
            lines.append(f"  > ES: {note}")
        vocab = db.json_loads(topic.get("vocab_json"), [])
        if vocab:
            lines += ["", "**Vocabulary**"]
            for item in vocab:
                lines.append(
                    f"- **{item.get('word')}** — {item.get('en_def')} _({item.get('es')})_"
                )
        phrases = db.json_loads(topic.get("phrases_json"), [])
        if phrases:
            lines += ["", "**Phrases**"]
            for item in phrases:
                who = speakers.get(int(item.get("speaker_index", -1) or -1), "")
                suffix = f" — {who}" if who else ""
                lines.append(f"- “{item.get('en')}” · {item.get('es')}{suffix}")
        lines.append("")
    for roleplay in payload.get("roleplays", []):
        lines += [f"## Roleplay · {roleplay.get('title')}", roleplay.get("context_md") or ""]
        if roleplay.get("feedback_md"):
            lines.append(f"> {roleplay['feedback_md']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Restaurar
# ---------------------------------------------------------------------------


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Entradas de `files/` seguras (sin rutas absolutas ni `..`)."""
    safe: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        if info.is_dir() or not info.filename.startswith(FILES_PREFIX):
            continue
        relative = info.filename[len(FILES_PREFIX):]
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or relative.startswith(("/", "\\")):
            log.warning("Entrada de ZIP descartada por insegura: %r", info.filename)
            continue
        safe.append(info)
    return safe


def restore_session(zip_path: Path | str) -> int:
    """Crea un cuaderno nuevo a partir de un ZIP exportado. Devuelve el id nuevo."""
    source = Path(zip_path)
    if not source.exists():
        raise BackupError(
            f"No existe {source}", user_message="No encuentro el archivo de backup."
        )
    try:
        archive = zipfile.ZipFile(str(source))
    except zipfile.BadZipFile as exc:
        raise BackupError(
            f"ZIP corrupto: {exc}", user_message="El archivo no es un backup válido."
        ) from exc

    with archive:
        try:
            payload = json.loads(archive.read(PAYLOAD))
        except KeyError as exc:
            raise BackupError(
                "El ZIP no contiene session.json",
                user_message="El archivo no es un backup de Personal Notebook AI.",
            ) from exc
        manifest = {}
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except (KeyError, json.JSONDecodeError):
            pass
        schema = int(manifest.get("schema_version", db.SCHEMA_VERSION))
        if schema > db.SCHEMA_VERSION:
            raise BackupError(
                f"Backup de esquema {schema} (esta versión soporta {db.SCHEMA_VERSION})",
                user_message=(
                    "Este backup viene de una versión más nueva de la app. "
                    "Actualiza antes de restaurarlo."
                ),
            )
        if not payload.get("sessions"):
            raise BackupError(
                "Backup sin sesión", user_message="El backup no contiene ningún cuaderno."
            )

        new_id = _insert_payload(payload)
        destination = paths.session_dir(new_id)
        destination.mkdir(parents=True, exist_ok=True)
        for info in _safe_members(archive):
            relative = info.filename[len(FILES_PREFIX):]
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    with db.write() as conn:
        conn.execute(
            "UPDATE sessions SET audio_root=? WHERE id=?", (f"sessions/{new_id}", new_id)
        )
    log.info("Cuaderno restaurado como sesión %s", new_id)
    return new_id


def _insert_payload(payload: dict[str, Any]) -> int:
    original = payload["sessions"][0]
    now = db.now_iso()
    with db.write() as conn:
        people_map = _restore_people(conn, payload.get("people", []), now)

        columns = [c for c in _columns(conn, "sessions") if c != "id"]
        values = []
        for column in columns:
            if column == "session_number":
                values.append(db.next_session_number(conn))
            elif column == "updated_at":
                values.append(now)
            else:
                values.append(original.get(column))
        cursor = conn.execute(
            f"INSERT INTO sessions ({','.join(columns)})"
            f" VALUES ({','.join('?' * len(columns))})",
            values,
        )
        new_id = int(cursor.lastrowid or 0)

        topic_map: dict[int, int] = {}
        for table in CHILD_TABLES:
            rows = payload.get(table) or []
            if not rows:
                continue
            columns = [c for c in _columns(conn, table) if c != "id"]
            for row in rows:
                data = []
                for column in columns:
                    value = row.get(column)
                    if column == "session_id":
                        value = new_id
                    elif column == "topic_id" and value is not None:
                        value = topic_map.get(int(value))
                    elif column == "person_id" and value is not None:
                        value = people_map.get(int(value))
                    data.append(value)
                inserted = conn.execute(
                    f"INSERT INTO {table} ({','.join(columns)})"
                    f" VALUES ({','.join('?' * len(columns))})",
                    data,
                )
                if table == "topics" and row.get("id") is not None:
                    topic_map[int(row["id"])] = int(inserted.lastrowid or 0)
    return new_id


def _restore_people(conn, people: list[dict[str, Any]], now: str) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for person in people:
        name = str(person.get("name") or "").strip()
        if not name:
            continue
        existing = conn.execute(
            "SELECT id FROM people WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone()
        if existing:
            mapping[int(person["id"])] = int(existing["id"])
            continue
        cursor = conn.execute(
            "INSERT INTO people (name, role, voice_json, notes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                name,
                person.get("role", "other"),
                person.get("voice_json"),
                person.get("notes"),
                person.get("created_at") or now,
                now,
            ),
        )
        mapping[int(person["id"])] = int(cursor.lastrowid or 0)
    return mapping
