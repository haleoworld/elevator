"""SerpAPI Google Jobs source — broad keyword discovery (TEST).

Google for Jobs aggregates LinkedIn / Indeed / Glassdoor / company sites, so
this surfaces jobs beyond the curated-company ATSes. Free tier = 100 searches/
month, ~10 results each. Set SERPAPI_KEY in .env (get one at serpapi.com).

run_test() persists results and marks them 'passed' so they show in the Passed
tab for eyeballing — it deliberately bypasses the curated-company gate so you
can judge the raw quality of broad discovery.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # backend/.env

from .. import db  # noqa: E402
from . import persist  # noqa: E402

API = "https://serpapi.com/search.json"

# Google for Jobs returns ~one page (~10-13) per query and rarely paginates, so
# breadth comes from a SET of distinct natural-language queries (each = 1 billed
# search), not from paging. Queries stay natural (Google Jobs chokes on heavy
# boolean/negation); the strict level exclusions, FE requirement, and location
# checks run in the Phase-2 filter afterward — everything EXCEPT the curated gate.
QUERIES = [
    "senior frontend engineer",
    "senior front end developer",
    "frontend engineer",
    "senior full stack engineer",
    "senior ui engineer",
    "senior web developer",
    "senior software engineer frontend",
    "senior react engineer",
]
LOCATION = "Ontario, Canada"


def key_configured() -> bool:
    return bool(os.environ.get("SERPAPI_KEY", "").strip())


def _key() -> str:
    k = os.environ.get("SERPAPI_KEY", "").strip()
    if not k:
        raise RuntimeError("SERPAPI_KEY not set in .env — get a free key at serpapi.com")
    return k


# Meta-aggregators / scraper-reposters we never want to apply through. A job
# offered ONLY through these is skipped entirely.
_AGGREGATORS = (
    "whatjobs", "recruit.net", "recruit net", "jobleads", "bebee", "learn4good",
    "expertini", "jooble", "talent.com", "jobrapido", "neuvoo", "trabajo",
    "jobget", "simplyhired", "jobcase", "joblist", "lensa", "ladders",
    "the org", "theorg", "jobspresso", "jobspikr", "jobilize", "snagajob",
    "careerjet", "jobboard", "myjobsearch", "jobg8", "adzuna",
    "jobsora", "jobtome", "joblift", "resume-library",
    "jobmesh", "clickajobs", "clickajob", "click a jobs",
    "goremotejob", "go remote", "getcanadianjob", "get canadian job",
    "randstad",   # staffing agency (posts client roles, not its own)
)
# Real sources, ranked best-first when a job is available through several.
_PREFERRED = ("linkedin", "indeed", "glassdoor")


def _is_aggregator(title: str, link: str) -> bool:
    t = (title or "").strip().lower()
    if t == "jobs":                      # generic aggregator labeled just "Jobs"
        return True
    blob = t + " " + (link or "").lower()
    return any(a in blob for a in _AGGREGATORS)


def _pick_source(job: dict) -> tuple[str | None, str] | None:
    """Best (board_name, apply_url) for this job, preferring the real source
    (company site / LinkedIn / Indeed) over aggregators. Returns None when the
    job is only available through aggregators — caller skips it."""
    cands = []
    for o in (job.get("apply_options") or []):
        title = (o.get("title") or "").strip()
        link = (o.get("link") or "").strip()
        if link and not _is_aggregator(title, link):
            cands.append((title, link))
    if not cands:
        return None

    def rank(title: str) -> int:
        tl = title.lower()
        for i, p in enumerate(_PREFERRED):
            if p in tl:
                return i
        return len(_PREFERRED)   # company career sites & other real boards next

    cands.sort(key=lambda c: rank(c[0]))
    name, link = cands[0]
    return (name.replace("via ", "").strip() or None), link


def _to_raw(job: dict) -> dict | None:
    jid = job.get("job_id")
    title = (job.get("title") or "").strip()
    company = (job.get("company_name") or "").strip()
    if not (jid and title and company):
        return None
    picked = _pick_source(job)
    if picked is None:
        return None   # aggregator-only listing — skip
    board, url = picked
    return {
        "source": "serpapi",
        "external_id": str(jid)[:300],
        "company": company,
        "title": title,
        "location": (job.get("location") or "").strip() or None,
        "remote_type": None,
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "url": url,
        "jd_text": (job.get("description") or "")[:20000],
        "posted_at": None,  # Google Jobs gives relative dates ("2 days ago")
        "_via": board,      # real source (LinkedIn/Indeed/company) → job_board
    }


def search(query: str, location: str, *, pages: int = 1) -> list[dict]:
    """Run one Google Jobs query (optionally paginated). Returns raw job dicts."""
    key = _key()
    out: list[dict] = []
    next_token = None
    with httpx.Client(timeout=30.0) as client:
        for _ in range(max(1, pages)):
            params = {"engine": "google_jobs", "q": query,
                      "location": location, "hl": "en", "api_key": key}
            if next_token:
                params["next_page_token"] = next_token
            r = client.get(API, params=params)
            r.raise_for_status()
            data = r.json()
            for j in (data.get("jobs_results") or []):
                raw = _to_raw(j)
                if raw:
                    out.append(raw)
            next_token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not next_token:
                break
    return out


def run_test(*, recent: bool = True, apply_filter: bool = True,
             queries: list[str] | None = None) -> dict:
    """Run a SET of Google Jobs queries (each = 1 billed search), aggregate +
    dedupe, drop aggregator-only listings, then run survivors through the
    Phase-2 filter (role/level/location) minus the curated gate. Flags the
    survivors 'passed'. recent=True adds a last-7-days filter (note: it tends to
    suppress LinkedIn results). Returns {searches, found, passed, dropped, inserted}."""
    key = _key()
    queries = queries or QUERIES
    uniq: list[dict] = []
    seen: set[str] = set()
    searches = 0
    with httpx.Client(timeout=30.0) as client:
        for q in queries:
            params = {"engine": "google_jobs", "q": q, "location": LOCATION,
                      "hl": "en", "gl": "ca", "api_key": key}
            if recent:
                params["chips"] = "date_posted:week"
            r = client.get(API, params=params)
            r.raise_for_status()
            data = r.json()
            searches += 1
            if data.get("error"):
                continue   # Google returned nothing for this query — skip it
            for j in (data.get("jobs_results") or []):
                raw = _to_raw(j)
                if raw and raw["external_id"] not in seen:
                    seen.add(raw["external_id"])
                    uniq.append(raw)

    # How many of the found results are brand-new to the DB (pre-filter).
    new_found = 0
    if uniq:
        ext = [j["external_id"] for j in uniq]
        ph = ",".join("?" * len(ext))
        with db.connect() as conn:
            existing = {r[0] for r in conn.execute(
                f"SELECT external_id FROM jobs WHERE source='serpapi' "
                f"AND external_id IN ({ph})", ext)}
        new_found = sum(1 for e in ext if e not in existing)

    # Apply discovery criteria (role/level/location) minus the curated gate.
    passed = uniq
    dropped = 0
    if apply_filter:
        from .. import filter as _rules
        with db.connect() as conn:
            excluded = _rules._excluded_names(conn)
        kept = []
        for j in uniq:
            ok, _reason = _rules.evaluate(j, {}, excluded, require_curated=False)
            if ok:
                kept.append(j)
            else:
                dropped += 1
        passed = kept

    # Dedup by URL. Google Jobs hands back a fresh external_id (job_id token)
    # for the SAME posting on every run, so external_id-only dedup lets one URL
    # pile up across nights. Drop any job whose URL already exists in the DB
    # (incl. deleted, so we don't resurrect removed rows) and collapse same-URL
    # repeats within this run.
    if passed:
        urls = [j["url"] for j in passed]
        ph = ",".join("?" * len(urls))
        with db.connect() as conn:
            seen_urls = {r[0] for r in conn.execute(
                f"SELECT url FROM jobs WHERE url IN ({ph})", urls)}
        deduped = []
        for j in passed:
            if j["url"] in seen_urls:
                continue
            seen_urls.add(j["url"])
            deduped.append(j)
        url_dropped = len(passed) - len(deduped)
        if url_dropped:
            print(f"[serpapi] url-dedup dropped {url_dropped} already-known/repeat URLs")
        passed = deduped

    inserted, _ = persist.save(passed)
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET filter_status = 'passed', stage = 'filtered' "
            "WHERE source = 'serpapi' AND filter_status = 'pending'"
        )
        for j in passed:
            if j.get("_via"):
                conn.execute(
                    "UPDATE jobs SET job_board = ? WHERE source = 'serpapi' "
                    "AND external_id = ? AND (job_board IS NULL OR job_board = '')",
                    (j["_via"], j["external_id"]),
                )
        conn.commit()
    print(f"[serpapi] searches={searches} found={len(uniq)} passed={len(passed)} "
          f"dropped={dropped} inserted={inserted}")
    return {"searches": searches, "found": len(uniq), "new_found": new_found,
            "passed": len(passed), "dropped": dropped, "inserted": inserted}


def main() -> int:
    """Nightly entry point: run the broad search, then Telegram-ping the count
    of new jobs (separate from the regular discovery's nudge)."""
    res = run_test()
    db.record_run("serpapi", new_jobs=res["new_found"], new_passed=res["inserted"])
    base = os.environ.get("ELEVATOR_DASHBOARD_URL", "http://localhost:8742").rstrip("/")
    try:
        from .. import notifier
        ok, msg = notifier.notify_serpapi(res["inserted"], f"{base}/jobs?tab=passed")
        print(f"[serpapi] notify: {msg}")
    except Exception as e:  # noqa: BLE001
        print(f"[serpapi] notify failed (non-fatal): {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
