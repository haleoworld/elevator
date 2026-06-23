"""Bridge: push elevator jobs into the jmc app (Phone Screen Conversion Tool).

jmc keeps its entire state in one private GitHub Gist file
(`phone_screen_data.json`): {jobs[], batches[], nextJobId, nextBatchId, ...}.
We pull that state, append the selected elevator jobs into a batch matched by
NAME (creating the batch if it doesn't exist), then push the whole state back.

jmc sync is last-write-wins, so we pull-modify-push back to back to keep the
race window tiny. Dedup is by URL within the target batch, so re-sending the
same jobs is a no-op.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx

GIST_FILE = "phone_screen_data.json"
_API = "https://api.github.com/gists"


def _creds() -> tuple[str, str]:
    gid = (os.environ.get("JMC_GIST_ID") or "").strip()
    tok = (os.environ.get("JMC_GIST_TOKEN") or "").strip()
    if not gid or not tok:
        raise RuntimeError("JMC_GIST_ID / JMC_GIST_TOKEN not set in .env")
    return gid, tok


def _headers(tok: str) -> dict:
    return {"Authorization": "token " + tok, "Accept": "application/vnd.github+json"}


def _now_iso() -> str:
    # Mirrors JS new Date().toISOString(): 2026-06-22T18:00:00.000Z
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_state() -> dict:
    """Pull and parse jmc's full state from the Gist (handling GitHub's
    truncation of large file content)."""
    gid, tok = _creds()
    h = _headers(tok)
    with httpx.Client(timeout=60) as c:
        g = c.get(f"{_API}/{gid}", headers=h).json()
        f = g["files"][GIST_FILE]
        content = c.get(f["raw_url"], headers=h).text if f.get("truncated") else f["content"]
    return json.loads(content)


def save_state(state: dict) -> None:
    """Write the full state back to the Gist (matches jmc's PATCH format)."""
    gid, tok = _creds()
    body = {
        "description": "Phone Screen Conversion Tool data — synced " + _now_iso() + " (via elevator)",
        "files": {GIST_FILE: {"content": json.dumps(state, ensure_ascii=False, indent=2)}},
    }
    with httpx.Client(timeout=60) as c:
        r = c.patch(f"{_API}/{gid}", headers={**_headers(tok), "Content-Type": "application/json"},
                    json=body)
        r.raise_for_status()


def _to_jmc_job(ej: dict, job_id: str, batch_id: str) -> dict:
    """Map an elevator job row (dict) to a jmc job object with safe defaults."""
    loc = (ej.get("location") or "").strip()
    locations = [p.strip() for p in loc.split(",") if p.strip()] if loc else []
    posted = ej.get("posted_at")
    posted_date = str(posted)[:10] if posted else ""
    return {
        "id": job_id,
        "added_at": _now_iso(),
        "url": ej.get("url") or "",
        "raw_text": ej.get("jd_text") or "",
        "title": ej.get("title") or "",
        "company": ej.get("company") or "",
        "reference_id": ej.get("reference_id") or "",
        "salary_min": ej.get("salary_min"),
        "salary_max": ej.get("salary_max"),
        "currency": (ej.get("salary_currency") or "CAD"),
        "company_size": "",
        "industry": "",
        "locations": locations,
        "work_arrangement": ej.get("remote_type") or "",
        "job_type": ej.get("job_type") or "",
        "tech_stack": [],
        "requirements": [],
        "interview_process": "",
        "fe_percent": None,
        "fe_claude": None,
        "status": "batched",
        "batch_id": batch_id,
        "applied_at": None,
        "response_at": None,
        "phone_screen_at": None,
        "onsite_at": None,
        "outcome": "Pending",
        "custom_questions": [],
        "qa": [],
        "interviews": None,            # jmc lazily seeds preset stages
        "company_id": None,
        "notes": "",
        "screening": {"qualified": None, "reasons_pass": [], "reasons_fail": [], "target_match": []},
        "screening_all": {},
        "screened_against": None,
        "posted_date": posted_date,
        "ai_fit": None,
        "ai_fit_all": {},
    }


def _new_batch(state: dict, name: str) -> dict:
    bid = "batch_" + str(state.get("nextBatchId", 1)).zfill(3)
    state["nextBatchId"] = state.get("nextBatchId", 1) + 1
    batch = {
        "id": bid,
        "name": name,
        "created_at": _now_iso(),
        "status": "draft",
        "job_ids": [],
        "resume_version_id": None,
        "analysis": None,
        "criteria_id": state.get("active_criteria_id"),
        "submitted_at": None,
    }
    state.setdefault("batches", []).append(batch)
    return batch


def send_jobs(jobs_by_batch: dict[str, list[dict]]) -> dict:
    """jobs_by_batch: {batch_name: [elevator_job_dict, ...]}.

    For each batch name: find the jmc batch by exact name (or create it), then
    append the elevator jobs, deduping by URL within that batch. Returns a
    summary {added, skipped, created_batches, batches: {name: added_count}}."""
    state = load_state()
    jobs = state.setdefault("jobs", [])
    by_name = {b.get("name"): b for b in state.get("batches", [])}

    summary = {"added": 0, "skipped": 0, "created_batches": [], "batches": {}}

    for name, ejobs in jobs_by_batch.items():
        batch = by_name.get(name)
        if batch is None:
            batch = _new_batch(state, name)
            by_name[name] = batch
            summary["created_batches"].append(name)
        existing_urls = {j.get("url") for j in jobs
                         if j.get("id") in set(batch.get("job_ids", []))}
        added_here = 0
        for ej in ejobs:
            url = ej.get("url") or ""
            if url and url in existing_urls:
                summary["skipped"] += 1
                continue
            job_id = "job_" + str(state.get("nextJobId", 1)).zfill(4)
            state["nextJobId"] = state.get("nextJobId", 1) + 1
            jobs.append(_to_jmc_job(ej, job_id, batch["id"]))
            batch.setdefault("job_ids", []).append(job_id)
            existing_urls.add(url)
            added_here += 1
            summary["added"] += 1
        summary["batches"][name] = added_here

    if summary["added"] or summary["created_batches"]:
        save_state(state)
    return summary
