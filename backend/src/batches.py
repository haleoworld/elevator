"""Batch formation — bundle scored jobs into batches for content generation.

The proposal's auto-trigger (9-12 scored OR 1 week elapsed) lands once we have
steady-state cadence. For now we form batches manually with a top-N selection.
"""
from __future__ import annotations

from . import db


def form_batch(
    *,
    name: str | None = None,
    top_n: int | None = None,
    job_ids: list[int] | None = None,
    trigger_reason: str = "manual",
) -> int | None:
    """Create a batch from EITHER the top-N scored jobs OR an explicit list of
    job ids (hand-picked). Does NOT generate content — call content.generate()
    separately when ready. Returns the new batch id, or None if no eligible jobs.

    Jobs already in another batch (stage != 'scored') are skipped so the same
    job doesn't land in two batches."""
    with db.connect() as conn:
        if job_ids:
            # Hand-picked: only accept ids that are currently 'scored' (not
            # already queued into another batch).
            ph = ",".join("?" * len(job_ids))
            rows = conn.execute(
                f"SELECT id FROM jobs WHERE id IN ({ph}) AND stage = 'scored' "
                f"AND deleted_at IS NULL",
                job_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM jobs "
                "WHERE stage = 'scored' AND deleted_at IS NULL "
                "ORDER BY fit_score DESC, ats_score DESC, discovered_at DESC "
                "LIMIT ?",
                (top_n or 5,),
            ).fetchall()
        if not rows:
            return None

        cur = conn.execute(
            "INSERT INTO batches (name, trigger_reason, job_count, status) "
            "VALUES (?, ?, ?, 'pending')",
            (name or None, trigger_reason, len(rows)),
        )
        batch_id = cur.lastrowid

        for row in rows:
            conn.execute(
                "INSERT INTO batch_jobs (batch_id, job_id) VALUES (?, ?)",
                (batch_id, row["id"]),
            )
            conn.execute(
                "UPDATE jobs SET stage = 'queued' WHERE id = ?",
                (row["id"],),
            )
        conn.commit()
        return batch_id


def move_jobs(job_ids: list[int], target_batch_id: int) -> int:
    """Move already-batched jobs into target_batch_id. Reassigns each job's
    batch_jobs link (a job lives in exactly one batch) and resyncs counts on
    every batch. Returns the number actually moved."""
    if not job_ids:
        return 0
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM batches WHERE id = ?", (target_batch_id,)).fetchone() is None:
            return 0
        ph = ",".join("?" * len(job_ids))
        valid = [r["id"] for r in conn.execute(
            f"SELECT id FROM jobs WHERE id IN ({ph}) AND deleted_at IS NULL", job_ids
        ).fetchall()]
        if not valid:
            return 0
        vph = ",".join("?" * len(valid))
        conn.execute(f"DELETE FROM batch_jobs WHERE job_id IN ({vph})", valid)
        for jid in valid:
            conn.execute(
                "INSERT OR IGNORE INTO batch_jobs (batch_id, job_id) VALUES (?, ?)",
                (target_batch_id, jid),
            )
            conn.execute("UPDATE jobs SET stage = 'queued' WHERE id = ?", (jid,))
        # Resync stored job_count on every batch from the link table (counting
        # only live jobs), so both source and destination stay accurate.
        conn.execute(
            "UPDATE batches SET job_count = "
            "  (SELECT COUNT(*) FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id "
            "   WHERE bj.batch_id = batches.id AND j.deleted_at IS NULL)"
        )
        conn.commit()
        return len(valid)


def scored_unbatched() -> list[dict]:
    """Scored jobs not yet in any batch — candidates for hand-picking."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, company, title, location, fit_score, ats_score "
            "FROM jobs WHERE stage = 'scored' AND deleted_at IS NULL "
            "ORDER BY fit_score DESC, discovered_at DESC LIMIT 500"
        ).fetchall()
        return [dict(r) for r in rows]


def list_batch_options() -> list[dict]:
    """Lightweight (id, name, job_count) list for batch-target dropdowns."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, job_count FROM batches ORDER BY triggered_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_jobs_to_batch(batch_id: int, job_ids: list[int]) -> int:
    """Add scored jobs to an EXISTING batch. Only jobs currently 'scored'
    (not already batched) are added. Returns the number actually added."""
    if not job_ids:
        return 0
    with db.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if exists is None:
            return 0
        ph = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT id FROM jobs WHERE id IN ({ph}) AND stage = 'scored' "
            f"AND deleted_at IS NULL",
            job_ids,
        ).fetchall()
        added = 0
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO batch_jobs (batch_id, job_id) VALUES (?, ?)",
                (batch_id, row["id"]),
            )
            conn.execute("UPDATE jobs SET stage = 'queued' WHERE id = ?", (row["id"],))
            added += 1
        # Refresh job_count from the link table (authoritative).
        conn.execute(
            "UPDATE batches SET job_count = "
            "  (SELECT COUNT(*) FROM batch_jobs WHERE batch_id = ?) "
            "WHERE id = ?",
            (batch_id, batch_id),
        )
        conn.commit()
        return added


def list_batches() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT b.id, b.name, b.triggered_at, b.trigger_reason, b.notified_at, b.status, "
            "  (SELECT COUNT(*) FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id "
            "   WHERE bj.batch_id = b.id AND j.deleted_at IS NULL) AS job_count "
            "FROM batches b ORDER BY b.triggered_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_batch(batch_id: int) -> dict | None:
    with db.connect() as conn:
        batch = conn.execute(
            "SELECT id, name, triggered_at, trigger_reason, job_count, notified_at, status "
            "FROM batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            return None
        jobs = conn.execute(
            "SELECT j.id, j.company, j.title, j.location, j.url, j.fit_score, "
            "       j.ats_score, j.fit_reason, j.stage, "
            "       a.cover_letter, a.why_company "
            "FROM batch_jobs bj "
            "JOIN jobs j ON j.id = bj.job_id "
            "LEFT JOIN applications a ON a.job_id = j.id "
            "WHERE bj.batch_id = ? AND j.deleted_at IS NULL "
            "ORDER BY j.fit_score DESC",
            (batch_id,),
        ).fetchall()
        return {"batch": dict(batch), "jobs": [dict(j) for j in jobs]}


def rename_batch(batch_id: int, name: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE batches SET name = ? WHERE id = ?",
            (name or None, batch_id),
        )
        conn.commit()


def mark_ready(batch_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE batches SET status = 'ready' WHERE id = ?", (batch_id,))
        conn.commit()


def mark_notified(batch_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE batches SET notified_at = CURRENT_TIMESTAMP WHERE id = ?",
            (batch_id,),
        )
        conn.commit()


def delete_batches(batch_ids: list[int]) -> int:
    """Delete batches by id. Cascades to batch_jobs and reverts the jobs'
    stage from 'queued' back to 'scored' so they can be re-batched.
    Applications (generated content) are kept. Returns number deleted."""
    if not batch_ids:
        return 0
    placeholders = ",".join("?" * len(batch_ids))
    with db.connect() as conn:
        conn.execute(
            f"UPDATE jobs SET stage = 'scored' WHERE stage = 'queued' AND id IN "
            f"(SELECT job_id FROM batch_jobs WHERE batch_id IN ({placeholders}))",
            batch_ids,
        )
        conn.execute(
            f"DELETE FROM batch_jobs WHERE batch_id IN ({placeholders})",
            batch_ids,
        )
        cur = conn.execute(
            f"DELETE FROM batches WHERE id IN ({placeholders})",
            batch_ids,
        )
        conn.commit()
        return cur.rowcount or 0
