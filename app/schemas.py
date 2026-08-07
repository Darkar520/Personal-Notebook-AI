"""Modelos Pydantic de la API.

Mejora sobre el plan original: el plan pasaba `body: dict` en casi todos los endpoints
de escritura, lo que deja la validación en manos de cada handler (y produce 500 en vez
de 422 cuando el payload es inesperado). Aquí cada operación tiene su modelo, con lo
que FastAPI valida, documenta (`/docs`) y rechaza con el código correcto.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import db

SessionStatus = Literal["recording", "processing", "done", "error", "empty"]
SpeakerRole = Literal["me", "teacher", "student", "other"]
TimelineKind = Literal["topic", "break", "activity", "roleplay", "closing"]


# ---------------------------------------------------------------------------
# Sesiones
# ---------------------------------------------------------------------------


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""
    title_locked: bool = False
    session_number: int = 1
    started_at: str | None = None
    ended_at: str | None = None
    tz_offset_min: int = 0
    status: str = "recording"
    status_detail: str | None = None
    progress: float = 0.0
    duration_sec: int | None = None
    capture_mode: str = "loopback"
    account_tag: str | None = None
    polish_model: str | None = None
    # Derivados (no están en la tabla): se calculan al serializar.
    topics_count: int = 0
    segments_count: int = 0
    has_audio: bool = False
    speakers_pending: bool = False


class SessionCreate(BaseModel):
    title: str | None = None
    account_tag: str | None = None


class SessionPatch(BaseModel):
    title: str | None = None
    account_tag: str | None = None
    status: SessionStatus | None = None


class StartRequest(BaseModel):
    """Arranque de captura. `source_wav` permite ensayar el pipeline sin clase real."""

    capture_mode: Literal["loopback", "loopback+mic", "mic"] | None = None
    title: str | None = None
    account_tag: str | None = None
    source_wav: str | None = None
    realtime: bool = True


class StopRequest(BaseModel):
    discard: bool = False
    finalize: bool = True


# ---------------------------------------------------------------------------
# Contenido
# ---------------------------------------------------------------------------


class SegmentOut(BaseModel):
    id: int
    start_t: float
    end_t: float
    speaker_index: int
    is_me: bool = False
    text: str
    topic_id: int | None = None
    wall_clock: str | None = None


class TopicOut(BaseModel):
    id: int
    sort_order: int
    status: str
    title: str
    start_t: float | None = None
    end_t: float | None = None
    points: list[str] = Field(default_factory=list)
    spanish_notes: list[str] = Field(default_factory=list)
    vocab: list[dict[str, Any]] = Field(default_factory=list)
    phrases: list[dict[str, Any]] = Field(default_factory=list)
    user_edited: bool = False
    mastered: bool = False
    wall_clock: str | None = None


class TopicPatch(BaseModel):
    title: str | None = None
    points: list[str] | None = None
    spanish_notes: list[str] | None = None
    vocab: list[dict[str, Any]] | None = None
    phrases: list[dict[str, Any]] | None = None
    mastered: bool | None = None


class DraftTopic(BaseModel):
    title: str = ""
    points: list[str] = Field(default_factory=list)
    spanish_notes: list[str] = Field(default_factory=list)


class DraftTopicsIn(BaseModel):
    topics: list[DraftTopic] = Field(default_factory=list)


class TimelineEventOut(BaseModel):
    id: int
    sort_order: int
    kind: str
    start_t: float
    end_t: float
    label: str
    note_md: str | None = None
    topic_id: int | None = None
    wall_clock: str | None = None


class RoleplayOut(BaseModel):
    id: int
    title: str
    context_md: str | None = None
    your_role: str | None = None
    participants: list[str] = Field(default_factory=list)
    key_phrases: list[Any] = Field(default_factory=list)
    feedback_md: str | None = None
    start_t: float | None = None
    end_t: float | None = None


# ---------------------------------------------------------------------------
# Hablantes / personas
# ---------------------------------------------------------------------------


class SpeakerOut(BaseModel):
    speaker_index: int
    person_id: int | None = None
    name: str | None = None
    suggested_name: str | None = None
    suggested_role: str | None = None
    role: str | None = None
    confirmed: bool = False
    auto_matched: bool = False
    is_me: bool = False
    talk_seconds: float = 0.0
    color: str | None = None
    sample_text: str | None = None


class SpeakerConfirm(BaseModel):
    speaker_index: int
    person_id: int | None = None
    name: str | None = None
    role: SpeakerRole = "other"
    remember: bool = True


class SpeakersConfirmIn(BaseModel):
    speakers: list[SpeakerConfirm]


class PersonOut(BaseModel):
    id: int
    name: str
    role: str
    sessions: int = 0


class PersonIn(BaseModel):
    name: str
    role: SpeakerRole = "other"

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("El nombre no puede estar vacío")
        return v


# ---------------------------------------------------------------------------
# Chat / materiales
# ---------------------------------------------------------------------------


class ChatIn(BaseModel):
    message: str
    reset: bool = False

    @field_validator("message")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("El mensaje no puede estar vacío")
        return v.strip()


class ChatOut(BaseModel):
    reply: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    meta: dict[str, Any] = Field(default_factory=dict)


class QuizRequest(BaseModel):
    n: int = Field(default=10, ge=1, le=30)


class QuizQuestionOut(BaseModel):
    id: int
    question: str
    options: list[str]
    correct_index: int
    explanation: str | None = None
    topic_title: str | None = None


class FlashcardOut(BaseModel):
    id: int
    front: str
    back_md: str
    box: int = 1
    due_at: str | None = None


class FlashcardReview(BaseModel):
    correct: bool


class PodcastOut(BaseModel):
    id: int
    script: str
    voice_a: str | None = None
    voice_b: str | None = None
    audio_url: str | None = None
    duration_sec: float | None = None
    created_at: str | None = None


class ConceptMapOut(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Ajustes
# ---------------------------------------------------------------------------


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    opencode: dict[str, Any] | None = None
    deepgram: dict[str, Any] | None = None
    gemini: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    pricing: dict[str, Any] | None = None


class ProviderCheck(BaseModel):
    ok: bool = False
    detail: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class ConnectionReport(BaseModel):
    opencode: ProviderCheck = Field(default_factory=ProviderCheck)
    deepgram: ProviderCheck = Field(default_factory=ProviderCheck)
    tts: ProviderCheck = Field(default_factory=ProviderCheck)
    audio: ProviderCheck = Field(default_factory=ProviderCheck)


class UsageOut(BaseModel):
    stt_minutes: float = 0.0
    stt_cost_usd: float = 0.0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_cost_usd: float = 0.0
    total_usd: float = 0.0
    by_purpose: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Adaptadores fila → modelo
# ---------------------------------------------------------------------------


def _b(value: Any) -> bool:
    return bool(value)


def row_to_session(row: sqlite3.Row | dict[str, Any], **extra: Any) -> SessionOut:
    data = dict(row)
    data["title_locked"] = _b(data.get("title_locked"))
    return SessionOut(**{**data, **extra})


def row_to_segment(row: sqlite3.Row | dict[str, Any], started_at: str | None = None,
                   tz_offset_min: int = 0) -> SegmentOut:
    data = dict(row)
    return SegmentOut(
        id=data["id"],
        start_t=data["start_t"],
        end_t=data["end_t"],
        speaker_index=data["speaker_index"],
        is_me=_b(data.get("is_me")),
        text=data["text"],
        topic_id=data.get("topic_id"),
        wall_clock=db.wall_clock(started_at, data["start_t"], tz_offset_min)
        if started_at
        else None,
    )


def row_to_topic(row: sqlite3.Row | dict[str, Any], started_at: str | None = None,
                 tz_offset_min: int = 0) -> TopicOut:
    data = dict(row)
    summary = db.json_loads(data.get("summary_md"), {})
    if isinstance(summary, list):  # tolerancia a formatos viejos
        summary = {"points": summary, "spanish_notes": []}
    return TopicOut(
        id=data["id"],
        sort_order=data.get("sort_order", 0),
        status=data.get("status", "draft"),
        title=data.get("title", ""),
        start_t=data.get("start_t"),
        end_t=data.get("end_t"),
        points=list(summary.get("points", [])),
        spanish_notes=list(summary.get("spanish_notes", [])),
        vocab=db.json_loads(data.get("vocab_json"), []),
        phrases=db.json_loads(data.get("phrases_json"), []),
        user_edited=_b(data.get("user_edited")),
        mastered=_b(data.get("mastered")),
        wall_clock=db.wall_clock(started_at, data.get("start_t") or 0.0, tz_offset_min)
        if started_at and data.get("start_t") is not None
        else None,
    )


def row_to_timeline(row: sqlite3.Row | dict[str, Any], started_at: str | None = None,
                    tz_offset_min: int = 0) -> TimelineEventOut:
    data = dict(row)
    return TimelineEventOut(
        id=data["id"],
        sort_order=data.get("sort_order", 0),
        kind=data.get("kind", "topic"),
        start_t=data.get("start_t", 0.0),
        end_t=data.get("end_t", 0.0),
        label=data.get("label", ""),
        note_md=data.get("note_md"),
        topic_id=data.get("topic_id"),
        wall_clock=db.wall_clock(started_at, data.get("start_t", 0.0), tz_offset_min)
        if started_at
        else None,
    )


def row_to_roleplay(row: sqlite3.Row | dict[str, Any]) -> RoleplayOut:
    data = dict(row)
    return RoleplayOut(
        id=data["id"],
        title=data.get("title", ""),
        context_md=data.get("context_md"),
        your_role=data.get("your_role"),
        participants=db.json_loads(data.get("participants_json"), []),
        key_phrases=db.json_loads(data.get("key_phrases_json"), []),
        feedback_md=data.get("feedback_md"),
        start_t=data.get("start_t"),
        end_t=data.get("end_t"),
    )


def topic_summary_json(points: list[str], spanish_notes: list[str]) -> str:
    return json.dumps({"points": points, "spanish_notes": spanish_notes}, ensure_ascii=False)
