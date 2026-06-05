"""Adzuna job board pulls.

Adzuna doesn't filter by company directly. We do broad role/location queries and
let the rules filter (filter.py) match against the curated company list. Free
tier rate limit is generous enough for daily 2am pulls.

Docs: https://developer.adzuna.com/docs/search
"""
from __future__ import annotations

import os
import time
from typing import Iterable

import httpx

from .. import companies_store

BASE = "https://api.adzuna.com/v1/api/jobs"

# Countries Adzuna supports as ISO codes. We default to user's geo (Ontario, CA) + US.
DEFAULT_COUNTRIES = ("ca", "us")

# Generic role queries — broad, low volume. Most results are at non-curated companies
# and will drop at the filter; kept as a thin baseline + signal for "what companies
# am I missing?" if we ever want to expand the curated list.
GENERIC_QUERIES = (
    "senior frontend engineer",
)

RESULTS_PER_PAGE = 50
DEFAULT_PAGES = 1  # 1 page = top-50 most relevant per query
REQUEST_DELAY_S = 0.4  # be polite to Adzuna


def _app_creds() -> tuple[str, str]:
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        raise RuntimeError("ADZUNA_APP_ID and ADZUNA_APP_KEY must be set in .env")
    return app_id, app_key


def _curated_queries() -> tuple[str, ...]:
    """One Adzuna query per enabled curated company: '<name> frontend'.
    This is how we surface postings at companies that don't have a Greenhouse
    or Lever board (CrowdStrike, Wiz, Guidewire, etc.)."""
    return tuple(
        f"{c.name} frontend"
        for c in companies_store.load_all()
        if c.enabled
    )


def pull(
    *,
    countries: Iterable[str] = DEFAULT_COUNTRIES,
    queries: Iterable[str] | None = None,
    pages: int = DEFAULT_PAGES,
    client: httpx.Client | None = None,
) -> list[dict]:
    app_id, app_key = _app_creds()
    if queries is None:
        queries = tuple(GENERIC_QUERIES) + _curated_queries()
    out: list[dict] = []
    seen_ids: set[str] = set()
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0)
    try:
        for country in countries:
            for query in queries:
                for page in range(1, pages + 1):
                    url = f"{BASE}/{country}/search/{page}"
                    params = {
                        "app_id": app_id,
                        "app_key": app_key,
                        "results_per_page": RESULTS_PER_PAGE,
                        "what": query,
                        "category": "it-jobs",
                        "content-type": "application/json",
                    }
                    try:
                        resp = client.get(url, params=params)
                        resp.raise_for_status()
                    except httpx.HTTPError as e:
                        print(f"  adzuna error {country}/{query} p{page}: {e}")
                        continue
                    for raw in resp.json().get("results", []):
                        rid = str(raw.get("id") or "")
                        if not rid or rid in seen_ids:
                            continue
                        seen_ids.add(rid)
                        out.append(_normalize(raw, country))
                    time.sleep(REQUEST_DELAY_S)
    finally:
        if owns_client:
            client.close()
    return out


def _normalize(raw: dict, country: str) -> dict:
    """Shape an Adzuna result into the common RawJob dict."""
    company = (raw.get("company") or {}).get("display_name", "") or ""
    location = (raw.get("location") or {}).get("display_name", "") or ""
    return {
        "source": "adzuna",
        "external_id": str(raw.get("id") or ""),
        "company": company.strip(),
        "title": (raw.get("title") or "").strip(),
        "location": location.strip(),
        "remote_type": None,  # Adzuna doesn't expose this reliably
        "salary_min": _int_or_none(raw.get("salary_min")),
        "salary_max": _int_or_none(raw.get("salary_max")),
        "salary_currency": "USD" if country == "us" else ("CAD" if country == "ca" else None),
        "url": raw.get("redirect_url") or "",
        "jd_text": (raw.get("description") or "").strip(),
        "posted_at": raw.get("created"),  # ISO 8601 string
    }


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
