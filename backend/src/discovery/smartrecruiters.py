"""SmartRecruiters public postings API.

Endpoint: GET https://api.smartrecruiters.com/v1/companies/<slug>/postings
Paginated via limit + offset. Each posting has only summary fields here —
descriptions live at .../postings/<id> which we don't fetch eagerly (similar
strategy to Workday: lazy-fetch in screen.py for filter-passed jobs).
"""
from __future__ import annotations

import html as _html
import re
import time
from typing import Iterable

import httpx

from .. import companies_store

BASE = "https://api.smartrecruiters.com/v1/companies"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
PAGE_SIZE = 100
REQUEST_DELAY_S = 0.2


def pull(
    companies: Iterable[companies_store.Company] | None = None,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    if companies is None:
        companies = [c for c in companies_store.load_all() if c.enabled and c.smartrecruiters_slug]
    out: list[dict] = []
    owns = client is None
    if owns:
        client = httpx.Client(timeout=20.0)
    try:
        for c in companies:
            slug = c.smartrecruiters_slug
            if not slug:
                continue
            offset = 0
            n_company = 0
            while True:
                try:
                    r = client.get(
                        f"{BASE}/{slug}/postings",
                        params={"limit": PAGE_SIZE, "offset": offset},
                    )
                    r.raise_for_status()
                except httpx.HTTPError as e:
                    print(f"  smartrecruiters error {c.slug}: {e}")
                    break
                data = r.json()
                postings = data.get("content") or []
                if not postings:
                    break
                for raw in postings:
                    out.append(_normalize(raw, c.name))
                n_company += len(postings)
                if len(postings) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
                time.sleep(REQUEST_DELAY_S)
            if n_company:
                print(f"  smartrecruiters: {c.name} → {n_company} jobs")
    finally:
        if owns:
            client.close()
    return out


def _normalize(raw: dict, company_name: str) -> dict:
    location = raw.get("location") or {}
    loc_parts = [p for p in (location.get("city"), location.get("region"), location.get("country")) if p]
    apply_url = (
        raw.get("applyUrl")
        or raw.get("url")
        or (raw.get("ref") or {}).get("url", "")
    )
    return {
        "source": "smartrecruiters",
        "external_id": str(raw.get("id") or raw.get("uuid") or ""),
        "company": company_name,
        "title": (raw.get("name") or "").strip(),
        "location": ", ".join(loc_parts).strip(),
        "remote_type": "remote" if location.get("remote") else None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "url": apply_url,
        "jd_text": "",   # lazy-fetched in screen.py
        "posted_at": raw.get("releasedDate") or raw.get("createdOn"),
    }


def fetch_jd_text(posting_url: str, slug: str, *, client: httpx.Client | None = None) -> str:
    """Fetch JD text for a single posting (lazy)."""
    # apply URL looks like https://jobs.smartrecruiters.com/<slug>/<id>
    # we need to call api endpoint with the posting ID
    m = re.search(r"smartrecruiters\.com/[^/]+/([^/?#]+)", posting_url or "")
    if not m:
        return ""
    posting_id = m.group(1)
    owns = client is None
    if owns:
        client = httpx.Client(timeout=15.0)
    try:
        try:
            r = client.get(f"{BASE}/{slug}/postings/{posting_id}")
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return ""
    finally:
        if owns:
            client.close()
    sections = data.get("jobAd", {}).get("sections", {})
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        text = sec.get("text") or ""
        if text:
            parts.append(_TAG_RE.sub(" ", _html.unescape(text)))
    return _WS_RE.sub(" ", " ".join(parts)).strip()
