"""Read/write the curated companies list (config/companies.yaml).

Edited via the /companies dashboard page or the file directly. Phase 2 discovery
consumes load_all() to know which Greenhouse/Lever boards to pull and which
Adzuna company-name matches to boost.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

COMPANIES_PATH = Path(__file__).resolve().parent.parent / "config" / "companies.yaml"

HEADCOUNT_BANDS = ("40-500", "500-5000", "5000-15000")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass
class Company:
    name: str
    slug: str
    segment: str = ""
    headcount_band: str = "500-5000"
    greenhouse_slug: str | None = None
    lever_slug: str | None = None
    # Full board URL like "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers"
    # The Workday scraper derives subdomain, dc, tenant, board from it.
    workday_url: str | None = None
    ashby_slug: str | None = None         # jobs.ashbyhq.com/<slug>
    smartrecruiters_slug: str | None = None  # api.smartrecruiters.com/v1/companies/<slug>/postings
    # True = confirmed has engineering presence in Canada (remote-Canada eligible
    # or office in Toronto/Vancouver/Montreal/Waterloo/Ottawa/Kitchener).
    # False = US/Europe/elsewhere only. None = unknown.
    has_canada_team: bool | None = None
    enabled: bool = True
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "segment": self.segment,
            "headcount_band": self.headcount_band,
            "greenhouse_slug": self.greenhouse_slug or None,
            "lever_slug": self.lever_slug or None,
            "workday_url": self.workday_url or None,
            "ashby_slug": self.ashby_slug or None,
            "smartrecruiters_slug": self.smartrecruiters_slug or None,
            "has_canada_team": self.has_canada_team,
            "enabled": bool(self.enabled),
            "notes": self.notes or "",
        }


def _normalize(raw: dict) -> Company:
    def s(key: str, default: str = "") -> str:
        v = raw.get(key, default)
        return "" if v is None else str(v).strip()

    return Company(
        name=s("name"),
        slug=s("slug"),
        segment=s("segment"),
        headcount_band=s("headcount_band") or "500-5000",
        greenhouse_slug=s("greenhouse_slug") or None,
        lever_slug=s("lever_slug") or None,
        workday_url=s("workday_url") or None,
        ashby_slug=s("ashby_slug") or None,
        smartrecruiters_slug=s("smartrecruiters_slug") or None,
        has_canada_team=raw.get("has_canada_team"),
        enabled=bool(raw.get("enabled", True)),
        notes=s("notes"),
    )


# Pattern classification — Veeva / CrowdStrike / Ouster / Other
_VEEVA_KEYWORDS = (
    "health", "life-sciences", "clinical", "real-world",
    "insurance", "risk-management", "embedded-insurance", "insurance-claims", "insurance-data",
    "banking", "finance-saas", "professional-services", "regulatory", "enterprise-planning",
    "ap-automation", "payments-finance", "financial-services-tech",
    "legal", "contract-lifecycle", "document-saas", "ops-saas",
    "education",
    "construction", "real-estate", "design-engineering", "industrial",
    "infrastructure-engineering", "predictive-maintenance", "manufacturing",
    "compliance", "governance", "audit", "privacy", "ethics",
    "logistics-visibility", "supply-chain", "telecom-bss",
)
_CROWDSTRIKE_KEYWORDS = (
    "security", "identity", "threat", "privileged", "vulnerability", "developer-security",
    "observability", "error-tracking", "incident-response", "observability-pipeline",
    "data-warehouse", "data-lakehouse", "streaming", "data-transformation", "data-integration",
    "data-orchestration", "distributed-database", "in-memory", "nosql", "graph-database",
    "managed-data", "product-analytics", "olap-database", "database-infra", "runtime",
    "devops", "ci-cd", "feature-flags", "frontend-platform", "engineering-pm",
    "api-tools", "internal-tools", "code-intelligence", "infrastructure-automation",
    "payments-infra", "fintech-infra", "card-issuing", "fintech",
)
_OUSTER_KEYWORDS = (
    "lidar", "perception", "fleet-iot", "physical-security", "video-security",
)


# Explicit slug allowlist for the "Veeva-adjacent" pattern: calm-culture
# mature B2B companies whose segments don't hit the Veeva keyword set
# (because Veeva-adjacent is horizontal rather than regulated-vertical),
# but which still fit the overall Veeva-style target bucket for the 70%
# weighting (vs CrowdStrike 25% / Ouster 5%).
_VEEVA_ADJACENT_SLUGS = frozenset({
    "atlassian", "salesforce", "intuit", "hubspot", "zapier",
    "shopify", "cisco", "adobe", "dropbox", "servicenow", "workday",
    "benevity", "freshbooks",
})


def classify_pattern(segment: str, slug: str = "") -> str:
    # Explicit per-company override comes first — segment keyword matching
    # can't capture "horizontal-B2B-but-calm" without false positives.
    if slug and slug in _VEEVA_ADJACENT_SLUGS:
        return "Veeva-adjacent"
    s = (segment or "").lower()
    if any(k in s for k in _VEEVA_KEYWORDS):
        return "Veeva"
    if any(k in s for k in _CROWDSTRIKE_KEYWORDS):
        return "CrowdStrike"
    if any(k in s for k in _OUSTER_KEYWORDS):
        return "Ouster"
    return "Other"


def load_all() -> list[Company]:
    if not COMPANIES_PATH.exists():
        return []
    with open(COMPANIES_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    items = raw.get("companies") or []
    return [_normalize(r) for r in items]


def get(slug: str) -> Company | None:
    for c in load_all():
        if c.slug == slug:
            return c
    return None


def validate(c: Company, *, existing_slugs: Iterable[str] = (), updating_slug: str | None = None) -> list[str]:
    """Return a list of error strings; empty means valid."""
    errs: list[str] = []
    if not c.name:
        errs.append("Name is required.")
    if not c.slug:
        errs.append("Slug is required.")
    elif not _SLUG_RE.match(c.slug):
        errs.append("Slug must be lowercase letters, digits, or hyphens (1-64 chars).")
    if c.headcount_band and c.headcount_band not in HEADCOUNT_BANDS:
        errs.append(f"Headcount band must be one of: {', '.join(HEADCOUNT_BANDS)}.")
    if c.slug and c.slug != updating_slug and c.slug in existing_slugs:
        errs.append(f"Slug '{c.slug}' already exists.")
    return errs


def upsert(c: Company, *, updating_slug: str | None = None) -> list[str]:
    """Add new or update existing (matched by updating_slug, or by c.slug if not given).
    Returns validation errors; on success, writes file atomically."""
    all_companies = load_all()
    existing_slugs = {x.slug for x in all_companies}
    if updating_slug is not None:
        existing_slugs.discard(updating_slug)
    errs = validate(c, existing_slugs=existing_slugs, updating_slug=updating_slug)
    if errs:
        return errs

    key = updating_slug if updating_slug is not None else c.slug
    out: list[Company] = []
    replaced = False
    for x in all_companies:
        if x.slug == key:
            out.append(c)
            replaced = True
        else:
            out.append(x)
    if not replaced:
        out.append(c)

    _write_all(out)
    return []


def delete(slug: str) -> bool:
    all_companies = load_all()
    remaining = [x for x in all_companies if x.slug != slug]
    if len(remaining) == len(all_companies):
        return False
    _write_all(remaining)
    return True


def _write_all(companies: list[Company]) -> None:
    payload = {"companies": [c.to_dict() for c in companies]}
    COMPANIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".companies.", suffix=".yaml.tmp", dir=str(COMPANIES_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                payload, f, sort_keys=False, allow_unicode=True, default_flow_style=False
            )
        os.replace(tmp, COMPANIES_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
