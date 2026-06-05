"""Plain-English health checks for the System Health page."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import db, profile_store

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str


def _check_profile() -> Check:
    p = profile_store.PROFILE_PATH
    if not p.exists():
        return Check("Profile file", "fail", f"{p} does not exist")
    if not os.access(p, os.W_OK):
        return Check("Profile file", "fail", f"{p} is not writable")
    sections = profile_store.read_all()
    filled = sum(1 for v in sections.values() if v.strip())
    total = len(profile_store.SECTIONS)
    if filled == 0:
        return Check("Profile file", "warn", f"writable, but 0/{total} sections filled")
    return Check("Profile file", "ok", f"writable, {filled}/{total} sections filled")


def _check_db() -> Check:
    if not db.DB_PATH.exists():
        return Check("Database", "fail", f"{db.DB_PATH} not initialized")
    try:
        with db.connect() as conn:
            tables = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
    except Exception as e:
        return Check("Database", "fail", f"connect failed: {e}")
    expected = {"jobs", "applications", "interviews", "transcripts"}
    missing = expected - set(tables)
    if missing:
        return Check("Database", "fail", f"missing tables: {sorted(missing)}")
    return Check("Database", "ok", f"{len(tables)} tables present")


def _check_env() -> Check:
    required = ["DASHBOARD_PASSWORD", "ENCRYPTION_KEY"]
    optional = ["ANTHROPIC_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "TELEGRAM_BOT_TOKEN"]

    missing_req = [k for k in required if not os.environ.get(k, "").strip()]
    if missing_req:
        return Check("Env vars", "fail", f"missing required: {missing_req}")

    missing_opt = [k for k in optional if not os.environ.get(k, "").strip()]
    if missing_opt:
        return Check(
            "Env vars",
            "warn",
            f"required set; Phase 2+ keys not yet set: {missing_opt}",
        )
    return Check("Env vars", "ok", "all set")


def _check_data_dirs() -> Check:
    needed = ["data/audio", "data/transcripts", "data/coaching-reports", "data/backups", "logs", "models"]
    missing = [d for d in needed if not (REPO_ROOT / d).is_dir()]
    if missing:
        return Check("Data dirs", "fail", f"missing: {missing}")
    return Check("Data dirs", "ok", "all present")


def run_all() -> list[Check]:
    return [_check_env(), _check_profile(), _check_db(), _check_data_dirs()]
