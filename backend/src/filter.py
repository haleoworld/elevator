"""Stage 2: cheap rules filter. Pure-Python; no LLM, no network.

Reads jobs that are stage=discovered and filter_status=pending, applies rules,
and writes filter_status=passed|dropped + drop_reason back to the row.

Rules:
- Drop if title implies a non-target level (intern/junior/manager/director/etc.)
- Drop if title lacks a frontend signal (frontend/react/angular/vue/UI engineer/etc.)
- Drop if location is not remote / CA / US
- Drop if company is in excluded_companies
- Drop if company doesn't match the curated list (case-insensitive normalized match).
  Curated list is the whitelist for Phase 2. Off-list companies are surfaced later
  (Phase 3 LLM screening) when we can semantically assess fit.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import companies_store, db

# Drop jobs older than this many days. NULL/unparseable posted_at is treated
# as neutral (pass through) — we don't know the date, so let Phase 3 / human judge.
_MAX_AGE_DAYS = 7

# Level + non-engineering function drops.
# Intentionally permissive on the FE-vs-BE distinction — Phase 3 LLM is what
# discriminates "Senior Engineer X" between frontend and backend after reading
# the JD. This filter only drops what's clearly out of scope.
_LEVEL_DROP_RE = re.compile(
    r"\b("
    r"intern|internship|junior|jr|associate|entry[- ]level|co[- ]?op|"
    r"manager|director|vp|head[- ]of|chief|founder|recruiter|"
    r"principal|architect|"
    r"sales|account executive|account manager|account development|account representative|customer success|"
    r"analyst|research scientist|"
    r"solutions engineer|solutions architect|sales engineer|pre[- ]?sales|"
    r"marketing|demand generation|designer|brand|copywriter|content writer|"
    r"finance|controller|accountant|paralegal|attorney|"
    r"hr|people ops|talent|partner|partnerships?|channel|"
    r"assistant|administrative|admin|secretary|receptionist|coordinator|"
    r"executive assistant|office manager|"
    r"engineer i{1,3}"
    r")\b",
    re.IGNORECASE,
)

# Positive title gate — title must contain at least one engineering / frontend
# signal. Prevents non-engineering roles (admin/sales/PM/etc.) from passing
# even when they happen to clear the level-drop negative list.
_TITLE_REQUIRE_RE = re.compile(
    r"\b("
    r"engineer|engineering|developer|programmer|"
    r"frontend|front[- ]end|fullstack|full[- ]stack|"
    r"react|angular|vue|svelte|nextjs|next\.?js|"
    r"typescript|javascript|"
    r"web|ui|ux engineer|"
    r"software|sde"
    r")\b",
    re.IGNORECASE,
)

# Engineering specialties that are unambiguously NOT frontend.
# Phase 3 LLM doesn't need to see hardware/embedded/SRE/CRM-specialist roles.
_NON_FE_DROP_RE = re.compile(
    r"\b("
    # Hardware / electrical / embedded
    r"embedded|firmware|hardware|optical|photonic|photonics|"
    r"semiconductor|silicon|asic|fpga|dsp|analog|rf|"
    r"mechanical|electrical|automotive|"
    # Ops / infra specialties
    r"site reliability|sre|devops|"
    r"netsuite|salesforce eng|sap|oracle eng|"
    r"business systems|gtm|revops|"
    r"perception|lidar|"
    r"validation engineer|test engineer|cad engineer|"
    r"reliability engineer|quality engineer|"
    # Backend (explicit non-frontend)
    r"backend|back[- ]end|"
    # AI / ML / data
    r"ai engineer|ml engineer|machine learning|"
    r"ai/ml|ai software|"
    r"gen ?ai|llm engineer|"
    r"ai platform|ml platform|ai trust|ai governance|"
    r"fine[- ]?tuning|"
    r"data engineer|data scientist|data architect|"
    r"applied scientist|research engineer|"
    # Field / customer-facing engineering
    r"forward deployed|solution developer|solution engineer|"
    r"field engineer|field service|implementation engineer|"
    r"professional services engineer|"
    r"consulting engineer|technical support|support engineer|"
    # Chip / hardware design (extends existing hardware coverage)
    r"physical design|timing engineer|sta engineer|"
    # Quality / manufacturing
    r"manufacturing|quality engineering|"
    # Networking (typically infra, not FE)
    r"network engineer|backbone engineering"
    r")\b",
    re.IGNORECASE,
)
# Deny list — drop only when location is *explicitly* outside North America.
# Empty / unknown locations pass through (let the human or Phase-3 LLM judge).
# Positive Canada signal — if any of these terms appear in the location,
# we treat the listing as Canada-compatible (even if it also lists US cities).
_CANADA_HINT_RE = re.compile(
    r"\b(canada|ontario|toronto|gta|greater toronto|"
    r"mississauga|markham|richmond hill|vaughan|north york|king city|"
    r"ottawa|montreal|vancouver|calgary|winnipeg|edmonton|halifax|"
    r"quebec|alberta|british columbia|manitoba|saskatchewan|"
    r"nova scotia|new brunswick|newfoundland)\b",
    re.IGNORECASE,
)

# US indicators — when these appear and there's no Canada signal, drop.
# Conservative: only matches well-known US cities + state full names + country.
# Avoids state abbreviations (would conflict with CA=California vs Canada).
_US_INDICATOR_RE = re.compile(
    r"\b(united states|usa|u\.s\.a\.?|u\.s\.|"
    r"new york city|nyc|san francisco|los angeles|seattle|austin|"
    r"boston|chicago|denver|atlanta|miami|portland|dallas|houston|"
    r"phoenix|san diego|san jose|"
    r"california|texas|florida|illinois|massachusetts|virginia|"
    r"colorado|oregon|new jersey|pennsylvania|north carolina|"
    r"south carolina|maryland|nevada|utah|tennessee|"
    r"missouri|wisconsin|minnesota|indiana|ohio|michigan|arizona)\b",
    re.IGNORECASE,
)

_LOCATION_DROP_RE = re.compile(
    r"\b(spain|germany|france|italy|portugal|netherlands|belgium|austria|"
    r"switzerland|sweden|denmark|finland|norway|iceland|ireland|poland|"
    r"czech|czechia|hungary|romania|greece|"
    r"berlin|munich|hamburg|cork|dublin|amsterdam|stockholm|barcelona|madrid|lisbon|"
    r"united kingdom|england|scotland|wales|london|manchester|edinburgh|"
    r"india|bangalore|bengaluru|hyderabad|mumbai|chennai|pune|delhi|gurgaon|gurugram|noida|"
    r"china|hong kong|singapore|malaysia|thailand|vietnam|indonesia|philippines|"
    r"japan|tokyo|osaka|korea|seoul|"
    r"australia|sydney|melbourne|new zealand|auckland|"
    r"israel|tel aviv|"
    r"brazil|mexico|argentina|chile|colombia|peru|"
    r"costa rica|panama|guatemala|honduras|el salvador|nicaragua|belize|"
    r"dominican republic|cuba|haiti|jamaica|puerto rico|"
    r"ecuador|bolivia|venezuela|uruguay|paraguay|"
    r"uae|dubai|saudi|qatar|"
    r"south africa|nigeria|kenya|egypt|morocco|"
    r"turkey|ukraine|russia|belarus)\b",
    re.IGNORECASE,
)


def _normalize_company(name: str) -> str:
    """Lowercase + strip common corp suffixes for fuzzy matching."""
    s = (name or "").lower().strip()
    s = re.sub(r"[,.]", " ", s)
    s = re.sub(
        r"\b(holdings?|inc|incorporated|corp|corporation|ltd|limited|llc|"
        r"plc|gmbh|s\.?a\.?|s\.?p\.?a\.?|co|the)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def _curated_names() -> dict[str, str]:
    """Returns {normalized_name: original_name} for enabled curated companies."""
    return {
        _normalize_company(c.name): c.name
        for c in companies_store.load_all()
        if c.enabled
    }


def _excluded_names(conn) -> set[str]:
    return {
        _normalize_company(row["company"])
        for row in conn.execute("SELECT company FROM excluded_companies")
    }


def _too_old(posted_at) -> bool:
    """True if posted_at is parseable and older than _MAX_AGE_DAYS days."""
    if posted_at is None:
        return False
    if isinstance(posted_at, str):
        try:
            posted_at = datetime.strptime(posted_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
    if not isinstance(posted_at, datetime):
        return False
    return (datetime.now() - posted_at) > timedelta(days=_MAX_AGE_DAYS)


def evaluate(job: dict, curated: dict[str, str], excluded: set[str]) -> tuple[bool, str | None]:
    """Returns (passes, drop_reason). drop_reason is None when passes=True."""
    title = job.get("title") or ""
    company_norm = _normalize_company(job.get("company") or "")
    location = job.get("location") or ""

    # Company checks first — cheapest drop, biggest bucket.
    if company_norm in excluded:
        return False, "company in excluded list"
    if company_norm not in curated:
        return False, "company not in curated list"
    if _LEVEL_DROP_RE.search(title):
        return False, "title level/function mismatch"
    if _NON_FE_DROP_RE.search(title):
        return False, "non-FE engineering specialty"
    if not _TITLE_REQUIRE_RE.search(title):
        return False, "title lacks engineering signal"
    if location and _LOCATION_DROP_RE.search(location):
        return False, f"location outside NA: {location[:60]}"
    if location and _US_INDICATOR_RE.search(location) and not _CANADA_HINT_RE.search(location):
        return False, f"location: US-only (no Canada signal): {location[:60]}"
    if _too_old(job.get("posted_at")):
        return False, f"posted >{_MAX_AGE_DAYS}d ago"
    return True, None


def run() -> dict:
    """Filter all pending jobs. Returns summary stats."""
    curated = _curated_names()
    stats = {"checked": 0, "passed": 0, "dropped": 0, "by_reason": {}}
    with db.connect() as conn:
        excluded = _excluded_names(conn)
        rows = conn.execute(
            "SELECT id, company, title, location, posted_at FROM jobs "
            "WHERE filter_status = 'pending' OR filter_status IS NULL"
        ).fetchall()
        for row in rows:
            stats["checked"] += 1
            passes, reason = evaluate(dict(row), curated, excluded)
            if passes:
                conn.execute(
                    "UPDATE jobs SET filter_status='passed', drop_reason=NULL, stage='filtered' WHERE id=?",
                    (row["id"],),
                )
                stats["passed"] += 1
            else:
                conn.execute(
                    "UPDATE jobs SET filter_status='dropped', drop_reason=?, stage='filtered' WHERE id=?",
                    (reason, row["id"]),
                )
                stats["dropped"] += 1
                stats["by_reason"][reason] = stats["by_reason"].get(reason, 0) + 1
        conn.commit()
    return stats
