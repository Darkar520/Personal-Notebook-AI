"""Cola persistente de chunks pendientes de transcribir.

Es la pieza que hace que una clase sin internet no se pierda: el grabador solo escribe
WAV y encola; el worker consume cuando hay red.

Diferencias con el plan original (Task 3.3):

- **Sin `DB_PATH` congelado al importar.** El plan definía `DB_PATH = get_root()/"data"/
  "app.db"` en el módulo y los tests tenían que parchearlo. Aquí se usa `app.db`, que
  resuelve la ruta en cada llamada.
- **Reclamo atómico.** El plan hacía `SELECT` y luego `UPDATE` en dos sentencias sin
  transacción: con `stt_concurrency > 1` dos workers reclamaban el mismo chunk y se
  pagaba dos veces. Aquí el reclamo va dentro de `BEGIN IMMEDIATE`.
- **Reintentos con estado.** `mark_failed` devuelve la fila a `pending` mientras queden
  intentos y solo la marca `failed` al agotarlos, en vez de exigir un `retry_failed`
  externo (que se conserva para el botón "reintentar" de la UI).
- **`requeue_stale`.** Si el proceso muere con chunks en `claimed`, al arrancar vuelven a
  `pending` en vez de quedarse colgados para siempre.
- **Metadatos del chunk** (`duration`, `overlap_pre`, `chunk_index`) viajan en la fila: el
  worker no tiene que adivinar el offset parseando el nombre del fichero.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from app import db, paths

log = logging.getLogger(__name__)

STALE_CLAIM_SECONDS = 15 * 60


def enqueue(
    session_id: int,
    chunk_path: str | Path,
    start_t: float,
    *,
    chunk_index: int = 0,
    duration: float = 0.0,
    overlap_pre: float = 0.0,
) -> int:
    """Registra un chunk. Idempotente por `(session_id, chunk_index)`."""
    rel = paths.rel_to_data(chunk_path) if not isinstance(chunk_path, str) else chunk_path
    if isinstance(chunk_path, str) and Path(chunk_path).is_absolute():
        rel = paths.rel_to_data(chunk_path)
    now = db.now_iso()
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO pending_transcriptions"
            " (session_id, chunk_index, chunk_path, start_t, duration, overlap_pre,"
            "  status, retries, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?, 'pending', 0, ?, ?)"
            " ON CONFLICT(session_id, chunk_index) DO UPDATE SET"
            "  chunk_path=excluded.chunk_path, start_t=excluded.start_t,"
            "  duration=excluded.duration, overlap_pre=excluded.overlap_pre,"
            "  updated_at=excluded.updated_at",
            (session_id, chunk_index, rel, float(start_t), float(duration),
             float(overlap_pre), now, now),
        )
        return int(cur.lastrowid or 0)


def claim_one(*, session_id: int | None = None) -> sqlite3.Row | None:
    """Reclama el chunk pendiente más antiguo de forma atómica."""
    rows = claim_batch(1, session_id=session_id)
    return rows[0] if rows else None


def claim_batch(limit: int = 1, *, session_id: int | None = None) -> list[sqlite3.Row]:
    """Reclama hasta `limit` chunks pendientes (orden cronológico)."""
    sql = (
        "SELECT * FROM pending_transcriptions WHERE status='pending'"
        + (" AND session_id=?" if session_id is not None else "")
        + " ORDER BY session_id, start_t, chunk_index LIMIT ?"
    )
    args: list[Any] = [session_id, limit] if session_id is not None else [limit]
    now = db.now_iso()
    with db.write() as conn:
        rows = conn.execute(sql, args).fetchall()
        if rows:
            conn.executemany(
                "UPDATE pending_transcriptions SET status='claimed', claimed_at=?,"
                " updated_at=? WHERE id=?",
                [(now, now, r["id"]) for r in rows],
            )
    return list(rows)


def release(pending_ids: list[int]) -> None:
    """Devuelve a `pending` filas reclamadas que no se van a procesar ahora."""
    if not pending_ids:
        return
    with db.write() as conn:
        conn.executemany(
            "UPDATE pending_transcriptions SET status='pending', claimed_at=NULL,"
            " updated_at=? WHERE id=? AND status='claimed'",
            [(db.now_iso(), pid) for pid in pending_ids],
        )


def mark_ok(pending_id: int) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM pending_transcriptions WHERE id=?", (pending_id,))


def mark_failed(pending_id: int, error: str, *, max_retries: int = 3,
                retryable: bool = True) -> str:
    """Devuelve el nuevo estado: `pending` (quedan intentos) o `failed`."""
    with db.write() as conn:
        row = conn.execute(
            "SELECT retries FROM pending_transcriptions WHERE id=?", (pending_id,)
        ).fetchone()
        retries = int(row["retries"] if row else 0) + 1
        status = "pending" if retryable and retries < max(1, max_retries) else "failed"
        conn.execute(
            "UPDATE pending_transcriptions SET status=?, retries=?, error=?, claimed_at=NULL,"
            " updated_at=? WHERE id=?",
            (status, retries, error[:500], db.now_iso(), pending_id),
        )
    return status


def retry_failed(*, max_retries: int = 3, session_id: int | None = None) -> int:
    """Reactiva chunks fallidos (botón "reintentar" / vuelta de la red)."""
    sql = "UPDATE pending_transcriptions SET status='pending', updated_at=? WHERE status='failed'"
    args: list[Any] = [db.now_iso()]
    if session_id is not None:
        sql += " AND session_id=?"
        args.append(session_id)
    with db.write() as conn:
        cur = conn.execute(sql, args)
        return int(cur.rowcount or 0)


def reset_retries(session_id: int | None = None) -> int:
    sql = "UPDATE pending_transcriptions SET retries=0, status='pending', error=NULL, updated_at=?"
    args: list[Any] = [db.now_iso()]
    if session_id is not None:
        sql += " WHERE session_id=?"
        args.append(session_id)
    with db.write() as conn:
        cur = conn.execute(sql, args)
        return int(cur.rowcount or 0)


def requeue_stale(*, timeout_seconds: int = STALE_CLAIM_SECONDS) -> int:
    """Devuelve a `pending` lo que quedó `claimed` por un cierre inesperado."""
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE pending_transcriptions SET status='pending', claimed_at=NULL, updated_at=?"
            " WHERE status='claimed' AND (claimed_at IS NULL OR"
            "       (julianday('now') - julianday(claimed_at)) * 86400 >= ?)",
            (db.now_iso(), int(timeout_seconds)),
        )
        count = int(cur.rowcount or 0)
    if count:
        log.info("Reencolados %d chunks que quedaron reclamados", count)
    return count


def counts(session_id: int | None = None) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM pending_transcriptions"
    args: list[Any] = []
    if session_id is not None:
        sql += " WHERE session_id=?"
        args.append(session_id)
    sql += " GROUP BY status"
    with db.read() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = {"pending": 0, "claimed": 0, "failed": 0}
    for row in rows:
        out[str(row["status"])] = int(row["n"])
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def has_work(session_id: int | None = None) -> bool:
    c = counts(session_id)
    return c["pending"] > 0 or c["claimed"] > 0


def purge_session(session_id: int) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM pending_transcriptions WHERE session_id=?", (session_id,))


def absolute_path(row: sqlite3.Row | dict[str, Any]) -> Path:
    """Ruta absoluta del chunk (las filas guardan rutas relativas a `data/`)."""
    raw = str(dict(row)["chunk_path"])
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return paths.from_data(raw)


# ---------------------------------------------------------------------------
# Metadatos del chunk (sidecar JSON junto al WAV)
# ---------------------------------------------------------------------------
#
# La máscara de micrófono y los silencios se calculan en el momento de grabar (es
# cuando tenemos las muestras en RAM) pero se consumen después, en el worker y en el
# pase final. Guardarlos en un JSON hermano del WAV tiene tres ventajas sobre meterlos
# en la fila de la cola: sobreviven al borrado de la fila cuando el chunk se procesa,
# sobreviven a un `data/app.db` recreado, y permiten reprocesar una carpeta de sesión
# copiada de otra máquina.

import json as _json


def meta_path(chunk_path: Path | str) -> Path:
    return Path(chunk_path).with_suffix(".json")


def write_meta(chunk_path: Path | str, data: dict[str, Any]) -> Path:
    target = meta_path(chunk_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.part")
    tmp.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


def read_meta(chunk_path: Path | str) -> dict[str, Any]:
    target = meta_path(chunk_path)
    try:
        return _json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
