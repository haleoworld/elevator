"""SQLite schema + connection helper.

Phase 1 only creates the tables; Phase 2-5 will start writing to them.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Discovered jobs + their progression through the pipeline.
CREATE TABLE IF NOT EXISTS jobs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source                TEXT NOT NULL,        -- adzuna, greenhouse, lever, manual
    external_id           TEXT,
    company               TEXT NOT NULL,
    title                 TEXT NOT NULL,
    location              TEXT,
    remote_type           TEXT,                  -- remote, hybrid, onsite, unknown
    salary_min            INTEGER,
    salary_max            INTEGER,
    salary_currency       TEXT,
    url                   TEXT NOT NULL,
    jd_text               TEXT,
    posted_at             TIMESTAMP,
    discovered_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    filter_status         TEXT,                  -- pending, passed, dropped
    drop_reason           TEXT,

    fit_score             INTEGER,               -- 0-100, Haiku output
    fit_reason            TEXT,

    ats_score             REAL,                  -- 0-1 cosine sim
    ats_missing_keywords  TEXT,                  -- JSON array

    stage                 TEXT NOT NULL DEFAULT 'discovered',
        -- discovered, filtered, screened, scored, queued,
        -- generating, ready, approved, submitted, rejected, withdrawn

    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_stage   ON jobs(stage);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

-- A batch = 9-12 qualified jobs (or 1-week-elapsed) bundled for content gen.
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trigger_reason  TEXT,                        -- queue_full | week_elapsed
    job_count       INTEGER,
    notified_at     TIMESTAMP,
    status          TEXT DEFAULT 'pending'       -- pending, generating, ready, completed
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_id  INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    job_id    INTEGER NOT NULL REFERENCES jobs(id)    ON DELETE CASCADE,
    PRIMARY KEY (batch_id, job_id)
);

-- Generated application content per job.
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    cover_letter  TEXT,
    why_company   TEXT,
    submitted_at  TIMESTAMP,
    decision      TEXT,                          -- pending, rejected, advanced, offer, accepted, declined
    decision_at   TIMESTAMP,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS screening_answers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    ord             INTEGER,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    edited_answer   TEXT
);

-- Interviews — real ones, recorded on your phone.
CREATE TABLE IF NOT EXISTS interviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id    INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    round             TEXT,                       -- recruiter, hm, technical, panel, etc.
    scheduled_at      TIMESTAMP,
    occurred_at       TIMESTAMP,
    audio_path        TEXT,                       -- relative to data/audio/
    audio_kept        INTEGER NOT NULL DEFAULT 0, -- 1 if pinned past auto-delete
    audio_deleted_at  TIMESTAMP,
    notes             TEXT
);

-- Practice question bank + per-attempt sessions.
CREATE TABLE IF NOT EXISTS practice_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT,        -- behavioral, technical, system_design, fe_specific
    question    TEXT NOT NULL,
    source      TEXT,        -- seed | from_interview | coaching_gap
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS practice_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id       INTEGER NOT NULL REFERENCES practice_questions(id),
    occurred_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    audio_path        TEXT,
    audio_deleted_at  TIMESTAMP,
    notes             TEXT
);

-- Transcripts attach to either an interview or a practice session.
CREATE TABLE IF NOT EXISTS transcripts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id         INTEGER REFERENCES interviews(id)        ON DELETE CASCADE,
    practice_session_id  INTEGER REFERENCES practice_sessions(id) ON DELETE CASCADE,
    path                 TEXT NOT NULL,         -- relative to data/transcripts/
    word_count           INTEGER,
    wpm                  REAL,
    filler_count         INTEGER,
    filler_rate          REAL,
    talk_ratio           REAL,
    pause_p50            REAL,
    pause_p90            REAL,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (interview_id IS NOT NULL OR practice_session_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS coaching_reports (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id         INTEGER REFERENCES interviews(id)        ON DELETE CASCADE,
    practice_session_id  INTEGER REFERENCES practice_sessions(id) ON DELETE CASCADE,
    transcript_id        INTEGER REFERENCES transcripts(id)       ON DELETE SET NULL,
    path                 TEXT NOT NULL,         -- relative to data/coaching-reports/
    summary              TEXT,
    top_patterns_json    TEXT,
    next_practice        TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Companies you've decided you don't want — used by the cheap filter.
CREATE TABLE IF NOT EXISTS excluded_companies (
    company    TEXT PRIMARY KEY,
    reason     TEXT,
    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- API spend tracking for the cost cap.
CREATE TABLE IF NOT EXISTS api_costs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    provider            TEXT NOT NULL,           -- anthropic, adzuna, ...
    operation           TEXT,                    -- discovery, fit_screen, content_gen, coaching, transcription
    job_id              INTEGER REFERENCES jobs(id),
    interview_id        INTEGER REFERENCES interviews(id),
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cached_input_tokens INTEGER,
    cost_usd            REAL
);

CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    channel   TEXT NOT NULL,                    -- telegram, gmail
    event     TEXT NOT NULL,
    payload   TEXT,
    success   INTEGER
);

-- Generic key-value for app metadata (last batch time, version, etc.)
CREATE TABLE IF NOT EXISTS app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def connect() -> sqlite3.Connection:
    # 30s Python-level timeout — covers the wait while another writer holds
    # the DB lock (e.g. background screen.run subprocess hammering UPDATEs).
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode: readers never block on writers and vice versa; writes queue
    # rather than throwing 'database is locked'. WAL is sticky — once set on
    # the DB file, it persists across connections.
    conn.execute("PRAGMA journal_mode = WAL")
    # Belt-and-suspenders C-level timeout (milliseconds). When the DB is busy,
    # SQLite retries internally for this long before raising OperationalError.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for migration in (
            "ALTER TABLE jobs ADD COLUMN deleted_at TIMESTAMP",
            "ALTER TABLE jobs ADD COLUMN job_type TEXT",
            "ALTER TABLE jobs ADD COLUMN job_board TEXT",
            "ALTER TABLE jobs ADD COLUMN reference_id TEXT",
            "ALTER TABLE batches ADD COLUMN name TEXT",
            "ALTER TABLE interviews ADD COLUMN company TEXT",
            "ALTER TABLE interviews ADD COLUMN interview_date TEXT",
            "ALTER TABLE interviews ADD COLUMN name TEXT",
            "ALTER TABLE transcripts ADD COLUMN duration_s REAL",
            "ALTER TABLE coaching_reports ADD COLUMN question_matches_json TEXT",
            "ALTER TABLE practice_questions ADD COLUMN core_id INTEGER REFERENCES practice_questions(id)",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass
        conn.commit()


def purge_old_deleted_jobs(days: int = 5) -> int:
    """Hard-delete jobs that have been soft-deleted longer than `days`.
    Cascades to applications + batch_jobs. Returns rows removed."""
    with connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM jobs WHERE deleted_at IS NOT NULL "
            f"AND deleted_at < datetime('now', '-{int(days)} days')"
        )]
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        conn.execute(f"UPDATE api_costs   SET job_id = NULL WHERE job_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM applications WHERE job_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM batch_jobs   WHERE job_id IN ({ph})", ids)
        cur = conn.execute(f"DELETE FROM jobs   WHERE id     IN ({ph})", ids)
        conn.commit()
        return cur.rowcount or 0
