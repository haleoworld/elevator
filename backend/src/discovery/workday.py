"""Workday public careers API scraper.

The list endpoint returns just title/location/path/posted-date — no JD text.
We do NOT fetch JD detail at discovery time (too many round-trips). Instead
the screen.py module lazily calls fetch_jd_text() for filter-passed jobs
right before the LLM call, so we only pay the detail cost on jobs that
survive Phase 2.

URL format expected in companies.yaml:
  workday_url: "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers"

That URL is enough to derive everything: subdomain, data-center, board, and
(by default) tenant = subdomain.
"""
from __future__ import annotations

import html as _html
import re
import time
from typing import Iterable

import httpx

from .. import companies_store

# https://<sub>.<dc>.myworkdayjobs.com/<board>  optionally with /en-US locale
_URL_RE = re.compile(
    r"^https?://(?P<sub>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com"
    r"(?:/(?:en-US|en-GB|en-CA))?"
    r"/(?P<board>[^/?#]+)"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

REQUEST_DELAY_S = 0.25  # polite spacing between Workday calls
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PER_COMPANY = 200


def parse_workday_url(url: str) -> tuple[str, str, str] | None:
    """Returns (subdomain, dc, board) or None if the URL doesn't match."""
    m = _URL_RE.match(url)
    if m is None:
        return None
    return m.group("sub"), m.group("dc"), m.group("board")


def _api_base(subdomain: str, dc: str, tenant: str, board: str) -> str:
    return f"https://{subdomain}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{board}"


def _public_base(subdomain: str, dc: str, board: str) -> str:
    return f"https://{subdomain}.{dc}.myworkdayjobs.com/{board}"


def pull(
    companies: Iterable[companies_store.Company] | None = None,
    *,
    max_per_company: int = DEFAULT_MAX_PER_COMPANY,
    page_size: int = DEFAULT_PAGE_SIZE,
    client: httpx.Client | None = None,
) -> list[dict]:
    if companies is None:
        companies = [
            c for c in companies_store.load_all() if c.enabled and c.workday_url
        ]
    out: list[dict] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0)
    try:
        for c in companies:
            parsed = parse_workday_url(c.workday_url or "")
            if parsed is None:
                print(f"  workday: cannot parse URL for {c.slug}: {c.workday_url}")
                continue
            subdomain, dc, board = parsed
            tenant = subdomain  # default; rarely different
            api_base = _api_base(subdomain, dc, tenant, board)
            public_base = _public_base(subdomain, dc, board)

            offset = 0
            n_company = 0
            while offset < max_per_company:
                try:
                    resp = client.post(
                        f"{api_base}/jobs",
                        json={
                            "appliedFacets": {},
                            "limit": page_size,
                            "offset": offset,
                            "searchText": "",
                        },
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "User-Agent": "elevator/1.0 (personal use)",
                        },
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    print(f"  workday error {c.slug} (offset {offset}): {e}")
                    break
                try:
                    data = resp.json()
                except ValueError:
                    print(f"  workday: non-JSON response from {c.slug}")
                    break

                postings = data.get("jobPostings") or []
                if not postings:
                    break
                for raw in postings:
                    out.append(_normalize(raw, c.name, public_base))
                n_company += len(postings)
                offset += page_size
                total = int(data.get("total") or 0)
                if total and offset >= total:
                    break
                time.sleep(REQUEST_DELAY_S)
            if n_company:
                print(f"  workday: {c.name} → {n_company} jobs")
    finally:
        if owns_client:
            client.close()
    return out


def _normalize(raw: dict, company_name: str, public_base: str) -> dict:
    external_path = (raw.get("externalPath") or "").strip()
    url = f"{public_base}{external_path}" if external_path else ""
    # External ID = the trailing requisition id after the underscore, e.g. R28763
    if "_" in external_path:
        rid = external_path.rsplit("_", 1)[-1].rstrip("/")
    else:
        rid = external_path or raw.get("title", "")
    return {
        "source": "workday",
        "external_id": rid,
        "company": company_name,
        "title": (raw.get("title") or "").strip(),
        "location": (raw.get("locationsText") or "").strip(),
        "remote_type": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "url": url,
        "jd_text": "",   # filled in lazily by screen.py via fetch_jd_text()
        # Workday's "postedOn" is a relative phrase ("Posted Today", "Posted 30+ Days Ago")
        # rather than a parseable timestamp — storing as NULL avoids sqlite3
        # convert_timestamp failures when the row is later SELECTed.
        "posted_at": None,
    }


def fetch_detail(public_url: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch a single job's full Workday detail and extract structured fields.

    Returns a dict with keys: jd_text, remote_type, job_type, location,
    posted_at. Any missing field comes back as "" or None. Called lazily from
    screen.py for jobs that passed Phase 2 — saves hundreds of detail calls
    on jobs that would be dropped anyway.
    """
    # Convert public URL like
    #   https://sub.wd5.myworkdayjobs.com/board/job/...
    # to the API detail endpoint
    #   https://sub.wd5.myworkdayjobs.com/wday/cxs/<tenant>/<board>/job/...
    empty = {"jd_text": "", "remote_type": None, "job_type": None,
             "location": None, "posted_at": None}
    m = re.match(
        r"^(https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)"
        r"(?:/(?:en-US|en-GB|en-CA))?"
        r"/([^/]+)(/job/.+)$",
        public_url,
    )
    if m is None:
        return empty
    origin, board, job_path = m.group(1), m.group(2), m.group(3)
    sub_m = re.match(r"^https?://([a-z0-9-]+)\.", origin)
    if sub_m is None:
        return empty
    tenant = sub_m.group(1)
    api_url = f"{origin}/wday/cxs/{tenant}/{board}{job_path}"

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=15.0)
    try:
        try:
            resp = client.get(
                api_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "elevator/1.0 (personal use)",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return empty
    finally:
        if owns_client:
            client.close()

    jp = data.get("jobPostingInfo") or {}

    html = jp.get("jobDescription") or ""
    jd_text = _WS_RE.sub(" ", _TAG_RE.sub(" ", _html.unescape(html))).strip() if html else ""

    # Locations — combine `location` (primary) + `additionalLocations` into a clean list.
    locs: list[str] = []
    primary = jp.get("location")
    if primary and isinstance(primary, str):
        locs.append(primary.strip())
    addl = jp.get("additionalLocations") or []
    if isinstance(addl, list):
        for x in addl:
            if isinstance(x, str) and x.strip():
                locs.append(x.strip())
    # dedupe preserving order
    seen: set[str] = set()
    locs = [x for x in locs if not (x in seen or seen.add(x))]
    location = "; ".join(locs) if locs else None

    # postedOn is usually relative ("Posted Yesterday") — useless.
    # startDate is sometimes a real ISO timestamp; pass it through to caller.
    posted_at = jp.get("startDate") or None

    return {
        "jd_text": jd_text,
        "remote_type": (jp.get("remoteType") or None),
        "job_type": (jp.get("timeType") or None),
        "location": location,
        "posted_at": posted_at,
    }


def fetch_jd_text(public_url: str, *, client: httpx.Client | None = None) -> str:
    """Backwards-compatible wrapper that returns only the JD text."""
    return fetch_detail(public_url, client=client).get("jd_text") or ""
