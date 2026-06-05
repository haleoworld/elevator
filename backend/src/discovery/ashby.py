"""Ashby public job board API.

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true
Returns JSON with a `jobs` array. Each job has descriptionHtml so no detail call needed.
"""
from __future__ import annotations

import html as _html
import re
from typing import Iterable

import httpx

from .. import companies_store

BASE = "https://api.ashbyhq.com/posting-api/job-board"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def pull(
    companies: Iterable[companies_store.Company] | None = None,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    if companies is None:
        companies = [c for c in companies_store.load_all() if c.enabled and c.ashby_slug]
    out: list[dict] = []
    owns = client is None
    if owns:
        client = httpx.Client(timeout=20.0)
    try:
        for c in companies:
            if not c.ashby_slug:
                continue
            try:
                r = client.get(f"{BASE}/{c.ashby_slug}", params={"includeCompensation": "true"})
                r.raise_for_status()
            except httpx.HTTPError as e:
                print(f"  ashby error {c.slug}: {e}")
                continue
            data = r.json() if r.content else {}
            for raw in data.get("jobs", []) or []:
                out.append(_normalize(raw, c.name))
            if data.get("jobs"):
                print(f"  ashby: {c.name} → {len(data['jobs'])} jobs")
    finally:
        if owns:
            client.close()
    return out


def _normalize(raw: dict, company_name: str) -> dict:
    desc_html = raw.get("descriptionHtml") or raw.get("descriptionPlain") or ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", _html.unescape(desc_html))).strip() if desc_html else ""
    return {
        "source": "ashby",
        "external_id": str(raw.get("id") or ""),
        "company": company_name,
        "title": (raw.get("title") or "").strip(),
        "location": (raw.get("location") or "").strip(),
        "remote_type": "remote" if raw.get("isRemote") else None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "url": raw.get("jobUrl") or raw.get("applyUrl") or "",
        "jd_text": text,
        "posted_at": raw.get("publishedDate"),
    }
