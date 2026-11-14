from __future__ import annotations

from typing import Any

import sqlite3

_SELECT = """
    SELECT c.id, c.job_id, dj.name AS job_name,
           c.upstream_job_id, uj.name AS upstream_name,
           c.min_rows, c.required_field
    FROM output_contracts c
    JOIN jobs dj ON dj.id = c.job_id
    JOIN jobs uj ON uj.id = c.upstream_job_id
"""


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def list_contracts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(conn.execute(_SELECT + " ORDER BY c.job_id, c.upstream_job_id"))


def list_contracts_for_job(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    # Every configured contract reaches the gate as-is: the gate must be able to
    # enforce a min_rows floor of 1 (the empty-success case), so nothing is
    # filtered or clamped on the way in.
    return _rows(
        conn.execute(_SELECT + " WHERE c.job_id=? ORDER BY c.upstream_job_id", (job_id,))
    )


def get_contract(
    conn: sqlite3.Connection, job_id: int, upstream_job_id: int
) -> dict[str, Any] | None:
    rows = _rows(
        conn.execute(
            _SELECT + " WHERE c.job_id=? AND c.upstream_job_id=?",
            (job_id, upstream_job_id),
        )
    )
    return rows[0] if rows else None


def upsert_contract(
    conn: sqlite3.Connection,
    job_id: int,
    upstream_job_id: int,
    min_rows: int,
    required_field: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO output_contracts(job_id, upstream_job_id, min_rows, required_field)
        VALUES (?,?,?,?)
        ON CONFLICT(job_id, upstream_job_id) DO UPDATE SET
          min_rows=excluded.min_rows,
          required_field=excluded.required_field
        """,
        (job_id, upstream_job_id, min_rows, required_field),
    )


def delete_contract(conn: sqlite3.Connection, job_id: int, upstream_job_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM output_contracts WHERE job_id=? AND upstream_job_id=?",
        (job_id, upstream_job_id),
    )
    return cur.rowcount > 0
