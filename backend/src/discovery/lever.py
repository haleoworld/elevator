"""Lever Postings API.

Public, unauthenticated. Per-company:
  GET https://api.lever.co/v0/postings/<slug>?mode=json
"""
from __future__ import annotations

from typing import Iterable

import httpx

from .. import companies_store

BASE = "https://api.lever.co/v0/postings"


def pull(
    companies: Iterable[companies_store.Company] | None = None,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    if companies is None:
        companies = [c for c in companies_store.load_all() if c.enabled and c.lever_slug]
    out: list[dict] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0)
    try:
        for c in companies:
            if not c.lever_slug:
                continue
            url = f"{BASE}/{c.lever_slug}"
            try:
                resp = client.get(url, params={"mode": "json"})
                resp.raise_for_status()
            except httpx.HTTPError as e:
                print(f"  lever error {c.slug}: {e}")
                continue
            for raw in resp.json() or []:
                out.append(_normalize(raw, c.name))
    finally:
        if owns_client:
            client.close()
    return out


def _normalize(raw: dict, company_name: str) -> dict:
    categories = raw.get("categories") or {}
    location = categories.get("location", "") or ""
    sal = raw.get("salaryRange") or {}
    return {
        "source": "lever",
        "external_id": str(raw.get("id") or ""),
        "company": company_name,
        "title": (raw.get("text") or "").strip(),
        "location": location.strip(),
        "remote_type": _infer_remote(raw),
        "salary_min": sal.get("min"),
        "salary_max": sal.get("max"),
        "salary_currency": sal.get("currency"),
        "url": raw.get("hostedUrl") or "",
        "jd_text": (raw.get("descriptionPlain") or raw.get("description") or "").strip(),
        "posted_at": _iso_or_none(raw.get("createdAt")),
    }


def _infer_remote(raw: dict) -> str | None:
    workplace = (raw.get("workplaceType") or "").lower()
    if workplace in ("remote", "hybrid", "on-site", "onsite"):
        return "onsite" if workplace == "on-site" else workplace
    return None


def _iso_or_none(ms) -> str | None:
    if ms is None:
        return None
    try:
        import datetime as dt
        return dt.datetime.utcfromtimestamp(int(ms) / 1000).isoformat() + "Z"
    except (TypeError, ValueError):
        return None
