"""Panel "¿Quién es quién?" y memoria de personas.

Mejoras sobre el plan original (Task 5.3):

- **Nunca se aplica un nombre sin confirmación** (requisito del spec §15): el `PUT` es el
  único camino por el que un `person_id` llega a la base.
- **La huella vocal se guarda en la persona** al confirmar, así la próxima clase con la
  misma teacher propone su nombre automáticamente (`auto_matched`). El plan proponía
  "mismo nº de hablantes y perfil de duración", que confunde a dos compañeros distintos.
- **Frase de muestra por hablante**, para que el usuario pueda reconocer quién es leyendo
  algo que dijo, en vez de adivinar por un color.
- `DELETE /api/people/{id}` para corregir un nombre mal guardado (el plan no lo tenía y
  la única salida era editar la base a mano).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app import db
from app.routers._common import get_session_or_404
from app.schemas import PersonIn, PersonOut, SpeakerOut, SpeakersConfirmIn
from app.transcription import voiceprint

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions/{sid}/speakers", tags=["speakers"])
people_router = APIRouter(prefix="/api/people", tags=["people"])

MIN_SAMPLE_CHARS = 25


def _sample_text(session_id: int, speaker_index: int) -> str:
    with db.read() as conn:
        row = conn.execute(
            "SELECT text FROM transcript_segments"
            " WHERE session_id=? AND speaker_index=? AND LENGTH(text)>?"
            " ORDER BY LENGTH(text) DESC LIMIT 1",
            (session_id, speaker_index, MIN_SAMPLE_CHARS),
        ).fetchone()
    return str(row["text"]).strip()[:220] if row else ""


@router.get("", response_model=list[SpeakerOut])
def list_speakers(sid: int):
    get_session_or_404(sid)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT s.speaker_index, s.person_id, s.suggested_name, s.suggested_role,"
            "       s.confirmed, s.auto_matched, s.is_me, s.talk_seconds, s.color,"
            "       p.name AS person_name, p.role AS person_role"
            " FROM session_speakers s LEFT JOIN people p ON p.id = s.person_id"
            " WHERE s.session_id=? ORDER BY s.talk_seconds DESC, s.speaker_index",
            (sid,),
        ).fetchall()
    out: list[SpeakerOut] = []
    for row in rows:
        index = int(row["speaker_index"])
        out.append(
            SpeakerOut(
                speaker_index=index,
                person_id=row["person_id"],
                name=row["person_name"],
                suggested_name=row["suggested_name"] or "",
                suggested_role=row["suggested_role"] or "",
                role=row["person_role"] or row["suggested_role"] or "other",
                confirmed=bool(row["confirmed"]),
                auto_matched=bool(row["auto_matched"]),
                is_me=bool(row["is_me"]),
                talk_seconds=round(float(row["talk_seconds"] or 0.0), 1),
                color=row["color"],
                sample_text=_sample_text(sid, index),
            )
        )
    return out


@router.put("", response_model=list[SpeakerOut])
def confirm_speakers(sid: int, body: SpeakersConfirmIn):
    """Confirma la identidad de los hablantes y la recuerda para futuras sesiones."""
    get_session_or_404(sid)
    now = db.now_iso()
    with db.write() as conn:
        for item in body.speakers:
            person_id = item.person_id
            name = (item.name or "").strip()
            voice_row = conn.execute(
                "SELECT voice_json FROM session_speakers"
                " WHERE session_id=? AND speaker_index=?",
                (sid, item.speaker_index),
            ).fetchone()
            voice_json = voice_row["voice_json"] if voice_row else None

            if person_id is None and name:
                existing = conn.execute(
                    "SELECT id FROM people WHERE name=? COLLATE NOCASE", (name,)
                ).fetchone()
                if existing:
                    person_id = int(existing["id"])
                elif item.remember:
                    cursor = conn.execute(
                        "INSERT INTO people (name, role, voice_json, created_at, updated_at)"
                        " VALUES (?,?,?,?,?)",
                        (name, item.role, voice_json, now, now),
                    )
                    person_id = int(cursor.lastrowid or 0)

            if person_id is not None:
                conn.execute(
                    "UPDATE people SET role=?, updated_at=? WHERE id=?",
                    (item.role, now, person_id),
                )
                if voice_json:
                    _merge_person_voice(conn, person_id, voice_json)

            conn.execute(
                "UPDATE session_speakers SET person_id=?, suggested_name=?,"
                " suggested_role=?, confirmed=1, is_me=? WHERE session_id=? AND"
                " speaker_index=?",
                (
                    person_id,
                    name or (item.name or ""),
                    item.role,
                    1 if item.role == "me" else 0,
                    sid,
                    item.speaker_index,
                ),
            )
        # `is_me` es único por sesión.
        me = [s for s in body.speakers if s.role == "me"]
        if me:
            conn.execute(
                "UPDATE session_speakers SET is_me=0"
                " WHERE session_id=? AND speaker_index<>?",
                (sid, me[0].speaker_index),
            )
    _sync_segment_flags(sid)
    return list_speakers(sid)


def _merge_person_voice(conn, person_id: int, voice_json: str) -> None:
    """Actualiza el centroide vocal de la persona con la muestra de esta sesión."""
    row = conn.execute("SELECT voice_json FROM people WHERE id=?", (person_id,)).fetchone()
    new_vector, new_seconds = voiceprint.from_json(db.json_loads(voice_json, None))
    if new_vector is None:
        return
    old_vector, old_seconds = voiceprint.from_json(
        db.json_loads(row["voice_json"] if row else None, None)
    )
    merged = voiceprint.combine(old_vector, old_seconds, new_vector, new_seconds)
    payload = voiceprint.to_json(merged, old_seconds + new_seconds)
    conn.execute(
        "UPDATE people SET voice_json=? WHERE id=?", (db.json_dumps(payload), person_id)
    )


def _sync_segment_flags(session_id: int) -> None:
    """Propaga `is_me` confirmado a los turnos del transcript."""
    with db.write() as conn:
        row = conn.execute(
            "SELECT speaker_index FROM session_speakers"
            " WHERE session_id=? AND is_me=1 AND confirmed=1",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE transcript_segments SET is_me=CASE WHEN speaker_index=? THEN 1 ELSE 0 END"
            " WHERE session_id=?",
            (int(row["speaker_index"]), session_id),
        )


# ---------------------------------------------------------------------------
# Personas conocidas
# ---------------------------------------------------------------------------


@people_router.get("", response_model=list[PersonOut])
def list_people():
    with db.read() as conn:
        rows = conn.execute(
            "SELECT p.id, p.name, p.role, COUNT(s.id) AS sessions"
            " FROM people p LEFT JOIN session_speakers s ON s.person_id = p.id"
            " GROUP BY p.id ORDER BY sessions DESC, p.name"
        ).fetchall()
    return [
        PersonOut(id=int(r["id"]), name=r["name"], role=r["role"], sessions=int(r["sessions"]))
        for r in rows
    ]


@people_router.post("", status_code=201, response_model=PersonOut)
def create_person(body: PersonIn):
    now = db.now_iso()
    with db.write() as conn:
        existing = conn.execute(
            "SELECT id FROM people WHERE name=? COLLATE NOCASE", (body.name,)
        ).fetchone()
        if existing:
            raise HTTPException(409, "Ya existe una persona con ese nombre")
        cursor = conn.execute(
            "INSERT INTO people (name, role, created_at, updated_at) VALUES (?,?,?,?)",
            (body.name, body.role, now, now),
        )
        person_id = int(cursor.lastrowid or 0)
    return PersonOut(id=person_id, name=body.name, role=body.role, sessions=0)


@people_router.delete("/{person_id}", status_code=204)
def delete_person(person_id: int):
    with db.write() as conn:
        cursor = conn.execute("DELETE FROM people WHERE id=?", (person_id,))
    if not cursor.rowcount:
        raise HTTPException(404, "Persona no encontrada")
