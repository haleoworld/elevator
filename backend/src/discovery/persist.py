"""Persist raw jobs from sources into jobs.db.

INSERT OR IGNORE keeps re-runs idempotent: (source, external_id) is unique.
"""
from __future__ import annotations

import re

from .. import db


COLUMNS = (
    "source", "external_id", "company", "title", "location",
    "remote_type", "salary_min", "salary_max", "salary_currency",
    "url", "jd_text", "posted_at",
)

# Matches the format SQLite's PARSE_DECLTYPES TIMESTAMP parser accepts.
_TIMESTAMP_OK = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _normalize_timestamp(val):
    """Coerce loose date strings (ISO 8601, etc.) to 'YYYY-MM-DD HH:MM:SS'.
    Returns None for anything we can't parse — better a NULL than a row that
    crashes every read with SQLite's convert_timestamp ValueError."""
    if val is None:
        return None
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    # 2026-05-28T17:50:01Z, 2026-05-28T17:50:01.123Z, 2026-05-28T17:50:01+00:00
    if "T" in s:
        s = s.replace("T", " ", 1)
    s = s.rstrip("Z")
    s = re.sub(r"\.\d+", "", s)        # drop fractional seconds
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s)  # drop tz offset
    s = s.strip()
    if _TIMESTAMP_OK.match(s):
        return s[:19]
    return None


def save(raw_jobs: list[dict]) -> tuple[int, int]:
    """Insert each job; skip ones already present. Returns (inserted, skipped)."""
    if not raw_jobs:
        return 0, 0
    placeholders = ", ".join("?" for _ in COLUMNS)
    sql = (
        f"INSERT OR IGNORE INTO jobs ({', '.join(COLUMNS)}, stage, filter_status) "
        f"VALUES ({placeholders}, 'discovered', 'pending')"
    )
    inserted = 0
    with db.connect() as conn:
        for j in raw_jobs:
            if not j.get("external_id") or not j.get("company") or not j.get("title") or not j.get("url"):
                continue
            j["posted_at"] = _normalize_timestamp(j.get("posted_at"))
            values = tuple(j.get(col) for col in COLUMNS)
            cur = conn.execute(sql, values)
            if cur.rowcount:
                inserted += 1
        conn.commit()
    skipped = len(raw_jobs) - inserted
    return inserted, skipped
