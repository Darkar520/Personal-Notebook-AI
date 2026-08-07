"""Capa de datos (SQLite).

Decisiones que corrigen el plan original:
1. **WAL + busy_timeout**: la app escribe desde 3 hilos (grabador, worker de STT,
   pase final) mientras la SPA lee por HTTP. Con el `journal_mode=delete` por defecto
   y sin `busy_timeout` una clase de 3,5 h termina en `database is locked`.
2. **Conexión por operación** (no global, no por hilo): SQLite crea conexiones en
   microsegundos y así no hay afinidad de hilo ni fugas. Los escritores usan
   `BEGIN IMMEDIATE` para no fallar al promover un lock compartido.
3. **Migraciones versionadas** con `PRAGMA user_version` en vez de `ALTER TABLE` dentro
   de try/except: reproducible y auditable.
4. **FTS5 con contenido externo + triggers** para el retrieval del chatbot: índice real
   con ranking BM25 nativo, sin reconstruir nada en cada pregunta.
5. **Índices** en las columnas por las que realmente se filtra.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Sequence

from app import paths

_init_lock = threading.Lock()
_initialized_for: str | None = None

# ---------------------------------------------------------------------------
# Migraciones
# ---------------------------------------------------------------------------

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  title          TEXT    NOT NULL DEFAULT '',
  title_locked   INTEGER NOT NULL DEFAULT 0,   -- 1 = el usuario lo renombró: la IA no lo toca
  session_number INTEGER NOT NULL DEFAULT 1,
  started_at     TEXT,                         -- ISO-8601 UTC con sufijo Z
  ended_at       TEXT,
  tz_offset_min  INTEGER NOT NULL DEFAULT 0,   -- offset local al grabar (para la hora de pared)
  status         TEXT    NOT NULL DEFAULT 'recording',
  status_detail  TEXT,
  progress       REAL    NOT NULL DEFAULT 0,
  duration_sec   INTEGER,
  audio_root     TEXT,                         -- relativo a data/
  capture_mode   TEXT    NOT NULL DEFAULT 'loopback',
  polish_model   TEXT,
  account_tag    TEXT,                         -- Capital One / Yardi / Verizon…
  created_at     TEXT    NOT NULL,
  updated_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  role        TEXT NOT NULL DEFAULT 'other',   -- me | teacher | student | other
  voice_json  TEXT,                            -- centroide de huella vocal + nº de muestras
  notes       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_speakers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id     INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  speaker_index  INTEGER NOT NULL,             -- índice global fusionado entre chunks
  person_id      INTEGER REFERENCES people(id) ON DELETE SET NULL,
  suggested_name TEXT,
  suggested_role TEXT,
  confirmed      INTEGER NOT NULL DEFAULT 0,
  auto_matched   INTEGER NOT NULL DEFAULT 0,   -- 1 = identificado por huella vocal
  is_me          INTEGER NOT NULL DEFAULT 0,   -- 1 = confirmado por el track de micrófono
  talk_seconds   REAL    NOT NULL DEFAULT 0,
  color          TEXT,
  voice_json     TEXT,
  UNIQUE (session_id, speaker_index)
);

CREATE TABLE IF NOT EXISTS topics (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  sort_order   INTEGER NOT NULL DEFAULT 0,
  status       TEXT    NOT NULL DEFAULT 'draft',  -- draft (en vivo) | final
  title        TEXT    NOT NULL DEFAULT '',
  start_t      REAL,
  end_t        REAL,
  summary_md   TEXT,        -- JSON {points:[], spanish_notes:[]}
  vocab_json   TEXT,        -- JSON [{word, en_def, es, example_en, example_es}]
  phrases_json TEXT,        -- JSON [{en, es, speaker_index}]
  user_edited  INTEGER NOT NULL DEFAULT 0,
  mastered     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  kind       TEXT    NOT NULL DEFAULT 'topic',  -- topic|break|activity|roleplay|closing
  start_t    REAL    NOT NULL DEFAULT 0,
  end_t      REAL    NOT NULL DEFAULT 0,
  label      TEXT    NOT NULL DEFAULT '',
  note_md    TEXT,
  topic_id   INTEGER REFERENCES topics(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS transcript_segments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  chunk_index   INTEGER NOT NULL DEFAULT 0,
  start_t       REAL    NOT NULL,
  end_t         REAL    NOT NULL,
  speaker_index INTEGER NOT NULL DEFAULT 0,
  is_me         INTEGER NOT NULL DEFAULT 0,
  confidence    REAL,
  text          TEXT    NOT NULL,
  topic_id      INTEGER REFERENCES topics(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pending_transcriptions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  chunk_path  TEXT    NOT NULL,               -- relativo a data/
  start_t     REAL    NOT NULL DEFAULT 0,
  duration    REAL    NOT NULL DEFAULT 0,
  overlap_pre REAL    NOT NULL DEFAULT 0,     -- segundos de solape con el chunk anterior
  status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|claimed|failed|done
  retries     INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  claimed_at  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (session_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role       TEXT NOT NULL,                   -- user | assistant
  content    TEXT NOT NULL,
  meta_json  TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quiz_questions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  question      TEXT NOT NULL,
  options_json  TEXT NOT NULL,
  correct_index INTEGER NOT NULL DEFAULT 0,
  explanation   TEXT,
  kind          TEXT NOT NULL DEFAULT 'mc',
  topic_title   TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flashcards (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  front       TEXT NOT NULL,
  back_md     TEXT NOT NULL,
  box         INTEGER NOT NULL DEFAULT 1,     -- Leitner 1..5
  due_at      TEXT,
  reviews     INTEGER NOT NULL DEFAULT 0,
  lapses      INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_maps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  layout_json TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_summaries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  script       TEXT NOT NULL,
  voice_a      TEXT,
  voice_b      TEXT,
  file_path    TEXT,                          -- relativo a data/
  duration_sec REAL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roleplays (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  title             TEXT NOT NULL DEFAULT '',
  context_md        TEXT,
  your_role         TEXT,
  participants_json TEXT,
  key_phrases_json  TEXT,
  feedback_md       TEXT,
  start_t           REAL,
  end_t             REAL,
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  kind       TEXT NOT NULL,      -- stt | llm | tts
  provider   TEXT NOT NULL DEFAULT '',
  model      TEXT NOT NULL DEFAULT '',
  purpose    TEXT NOT NULL DEFAULT '',
  minutes    REAL NOT NULL DEFAULT 0,
  tokens_in  INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd   REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_seg_session_time  ON transcript_segments(session_id, start_t);
CREATE INDEX IF NOT EXISTS idx_seg_topic         ON transcript_segments(topic_id);
CREATE INDEX IF NOT EXISTS idx_topics_session    ON topics(session_id, status, sort_order);
CREATE INDEX IF NOT EXISTS idx_timeline_session  ON timeline_events(session_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_pending_status    ON pending_transcriptions(status, session_id);
CREATE INDEX IF NOT EXISTS idx_messages_session  ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_usage_session     ON usage_events(session_id, kind);
CREATE INDEX IF NOT EXISTS idx_cards_due         ON flashcards(session_id, due_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status   ON sessions(status);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  text,
  content='transcript_segments',
  content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS transcript_fts_ai AFTER INSERT ON transcript_segments BEGIN
  INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS transcript_fts_ad AFTER DELETE ON transcript_segments BEGIN
  INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS transcript_fts_au AFTER UPDATE OF text ON transcript_segments BEGIN
  INSERT INTO transcript_fts(transcript_fts, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


# ---------------------------------------------------------------------------
# Conexiones
# ---------------------------------------------------------------------------


def connect(*, readonly: bool = False) -> sqlite3.Connection:
    """Abre una conexión configurada. Cada llamada devuelve una conexión nueva."""
    path = paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


# Alias retro-compatible con el plan original.
def get_conn() -> sqlite3.Connection:
    return connect()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """Conexión de solo lectura, siempre cerrada al salir."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """Transacción de escritura con `BEGIN IMMEDIATE` (evita SQLITE_BUSY al promover)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover
            pass
        raise
    finally:
        conn.close()


def init_db(*, force: bool = False) -> None:
    """Crea/actualiza el esquema. Idempotente y seguro entre hilos."""
    global _initialized_for
    key = str(paths.db_path())
    with _init_lock:
        if _initialized_for == key and not force:
            return
        conn = connect()
        try:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for version, script in MIGRATIONS:
                if current < version:
                    conn.executescript(script)
                    conn.execute(f"PRAGMA user_version={version}")
                    current = version
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()
        _initialized_for = key


def reset_init_cache() -> None:
    """Fuerza que el próximo `init_db` vuelva a ejecutar migraciones (tests)."""
    global _initialized_for
    with _init_lock:
        _initialized_for = None


# ---------------------------------------------------------------------------
# Tiempo
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """ISO-8601 en UTC con sufijo `Z` y precisión de segundos."""
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """Parsea ISO-8601 admitiendo `Z`; devuelve siempre datetime con tzinfo UTC."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_offset_minutes() -> int:
    """Offset local actual en minutos (p. ej. -300 para UTC-5)."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


def wall_clock(started_at: str | None, offset_seconds: float, tz_offset_min: int = 0) -> str:
    """Hora de pared local `HH:MM:SS` para un desplazamiento en segundos de la sesión."""
    base = parse_iso(started_at)
    if base is None:
        total = int(max(0.0, offset_seconds))
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    local = base + timedelta(minutes=tz_offset_min, seconds=offset_seconds)
    return local.strftime("%H:%M:%S")


def duration_between(started_at: str | None, ended_at: str | None) -> int:
    a, b = parse_iso(started_at), parse_iso(ended_at)
    if not a or not b:
        return 0
    return max(0, int((b - a).total_seconds()))


# ---------------------------------------------------------------------------
# Helpers de dominio
# ---------------------------------------------------------------------------


def next_session_number(conn: sqlite3.Connection | None = None) -> int:
    """Correlativo por año natural (basado en `started_at`, con fallback a created_at)."""
    year = now_iso()[:4]
    sql = (
        "SELECT COALESCE(MAX(session_number), 0) AS m FROM sessions "
        "WHERE COALESCE(started_at, created_at) LIKE ?"
    )
    if conn is not None:
        row = conn.execute(sql, (f"{year}%",)).fetchone()
    else:
        with read() as c:
            row = c.execute(sql, (f"{year}%",)).fetchone()
    return int(row["m"] if row else 0) + 1


def setting_get(key: str, default: Any = None, *, conn: sqlite3.Connection | None = None) -> Any:
    sql = "SELECT value FROM settings WHERE key=?"
    if conn is not None:
        row = conn.execute(sql, (key,)).fetchone()
    else:
        with read() as c:
            row = c.execute(sql, (key,)).fetchone()
    if not row or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def setting_set(key: str, value: Any, *, conn: sqlite3.Connection | None = None) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    sql = (
        "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
    )
    args = (key, payload, now_iso())
    if conn is not None:
        conn.execute(sql, args)
    else:
        with write() as c:
            c.execute(sql, args)


def setting_delete(prefix: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM settings WHERE key LIKE ?", (f"{prefix}%",))


def record_usage(
    *,
    session_id: int | None,
    kind: str,
    provider: str = "",
    model: str = "",
    purpose: str = "",
    minutes: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Registra consumo real para el estimador de costos (Fase 11)."""
    try:
        with write() as conn:
            conn.execute(
                "INSERT INTO usage_events (session_id, kind, provider, model, purpose,"
                " minutes, tokens_in, tokens_out, cost_usd, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    kind,
                    provider,
                    model,
                    purpose,
                    float(minutes),
                    int(tokens_in),
                    int(tokens_out),
                    float(cost_usd),
                    now_iso(),
                ),
            )
    except sqlite3.Error:  # pragma: no cover - la telemetría nunca rompe el flujo
        pass


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def touch_session(
    session_id: int,
    *,
    status: str | None = None,
    status_detail: str | None = None,
    progress: float | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Actualiza estado/progreso de una sesión en una sola sentencia."""
    sets: list[str] = ["updated_at=?"]
    vals: list[Any] = [now_iso()]
    if status is not None:
        sets.append("status=?")
        vals.append(status)
    if status_detail is not None:
        sets.append("status_detail=?")
        vals.append(status_detail)
    if progress is not None:
        sets.append("progress=?")
        vals.append(float(progress))
    vals.append(session_id)
    sql = f"UPDATE sessions SET {', '.join(sets)} WHERE id=?"
    if conn is not None:
        conn.execute(sql, vals)
    else:
        with write() as c:
            c.execute(sql, vals)


def fetch_session(session_id: int) -> sqlite3.Row | None:
    with read() as conn:
        return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def fetch_segments(session_id: int) -> list[sqlite3.Row]:
    with read() as conn:
        return conn.execute(
            "SELECT id, chunk_index, start_t, end_t, speaker_index, is_me, text"
            " FROM transcript_segments WHERE session_id=? ORDER BY start_t, id",
            (session_id,),
        ).fetchall()


def executemany(sql: str, params: Sequence[Sequence[Any]]) -> None:
    if not params:
        return
    with write() as conn:
        conn.executemany(sql, params)
