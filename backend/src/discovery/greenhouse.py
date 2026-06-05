"""Greenhouse Job Board API.

Public, unauthenticated. Per-company:
  GET https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true
"""
from __future__ import annotations

import re
from typing import Iterable

import httpx

from .. import companies_store

BASE = "https://boards-api.greenhouse.io/v1/boards"
_TAG_RE = re.compile(r"<[^>]+>")


def pull(
    companies: Iterable[companies_store.Company] | None = None,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    if companies is None:
        companies = [c for c in companies_store.load_all() if c.enabled and c.greenhouse_slug]
    out: list[dict] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0)
    try:
        for c in companies:
            if not c.greenhouse_slug:
                continue
            url = f"{BASE}/{c.greenhouse_slug}/jobs"
            try:
                resp = client.get(url, params={"content": "true"})
                resp.raise_for_status()
            except httpx.HTTPError as e:
                print(f"  greenhouse error {c.slug}: {e}")
                continue
            for raw in resp.json().get("jobs", []):
                out.append(_normalize(raw, c.name))
    finally:
        if owns_client:
            client.close()
    return out


def _normalize(raw: dict, company_name: str) -> dict:
    offices = raw.get("offices") or []
    location_str = (raw.get("location") or {}).get("name", "") or ", ".join(
        o.get("name", "") for o in offices if o.get("name")
    )
    content = raw.get("content") or ""
    # content is HTML-escaped HTML — unescape and strip tags for a plain-text JD.
    import html as _html
    text = _TAG_RE.sub(" ", _html.unescape(content))
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "source": "greenhouse",
        "external_id": str(raw.get("id") or ""),
        "company": company_name,
        "title": (raw.get("title") or "").strip(),
        "location": location_str.strip(),
        "remote_type": _infer_remote(location_str),
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "url": raw.get("absolute_url") or "",
        "jd_text": text,
        "posted_at": raw.get("updated_at"),
    }


def _infer_remote(location: str) -> str | None:
    lo = (location or "").lower()
    if "remote" in lo:
        return "remote"
    return None
