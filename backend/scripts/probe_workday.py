"""Probe common Workday URL patterns for our curated companies.

Workday URLs follow `<slug>.<dc>.myworkdayjobs.com/<board>` where dc is wd1..wd103
and board is usually one of External, Careers, <slug>careers, or Recruiting.

We POST to the /jobs endpoint with a tiny query and consider a 200-with-postings
or 200-with-total>=0 as confirmation. Output is yaml-ready for hand-editing
into config/companies.yaml.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from src import companies_store  # noqa: E402

# Candidate (slug variant, dc, board) tuples to try per company.
DC_TIERS = ["wd1", "wd5", "wd3", "wd2", "wd103"]
BOARD_TIERS_BY_SLUG = [
    lambda slug: ["External", "Careers", f"{slug}careers", f"{slug}-careers", f"{slug}careers-ext", "Recruiting"],
]

PRIORITY_SLUGS = [
    # Already verified
    "crowdstrike",
    # Veeva-pattern targets (where the unlock matters most)
    "guidewire", "duck-creek-technologies", "applied-systems",
    "ncino", "q2-holdings", "alkami-technology", "blackline",
    "workiva", "anaplan", "avidxchange", "bill-com", "jack-henry",
    "definitive-healthcare", "pointclickcare", "phreesia", "wellsky",
    "nextgen-healthcare", "healthequity", "inovalon", "innovaccer",
    "intapp", "procore", "appfolio", "autodesk", "ptc",
    "aspen-technology", "bentley-systems", "aveva", "hexagon",
    "instructure", "powerschool", "anthology", "ellucian",
    "manhattan-associates", "verisk-analytics",
    "relativity",
    # CrowdStrike-pattern that are commonly Workday
    "sentinelone", "okta", "cloudflare", "samsara", "verkada",
]


def slug_to_workday_candidates(slug: str) -> list[str]:
    base = slug.replace("-", "")
    return list({slug, base, slug.replace("-", ""), slug.split("-")[0]})


def probe_one(slug: str) -> tuple[str, str | None]:
    """Try common (subdomain, dc, board) combos. Return (slug, working_url_or_None)."""
    candidates: list[str] = []
    for sub in slug_to_workday_candidates(slug):
        for dc in DC_TIERS:
            for boards_fn in BOARD_TIERS_BY_SLUG:
                for board in boards_fn(sub):
                    candidates.append(
                        f"https://{sub}.{dc}.myworkdayjobs.com/{board}"
                    )
    seen = set()
    deduped = [c for c in candidates if not (c in seen or seen.add(c))]
    with httpx.Client(timeout=4.0) as client:
        for url in deduped:
            sub = url.split("//")[1].split(".")[0]
            dc = url.split(f"{sub}.")[1].split(".")[0]
            board = url.rsplit("/", 1)[-1]
            tenant = sub
            api = f"https://{sub}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
            try:
                r = client.post(
                    api,
                    json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    continue
                total = data.get("total")
                if isinstance(total, int) and total > 0:
                    return slug, url
    return slug, None


def main() -> int:
    all_companies = {c.slug: c for c in companies_store.load_all()}
    slugs_to_probe = [s for s in PRIORITY_SLUGS if s in all_companies]
    missing = [s for s in PRIORITY_SLUGS if s not in all_companies]
    if missing:
        print(f"NOTE: not in companies.yaml (skipped): {missing}")
        print()

    print(f"Probing Workday URLs for {len(slugs_to_probe)} priority companies...\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(probe_one, s): s for s in slugs_to_probe}
        results = {}
        for fut in as_completed(futs):
            slug, url = fut.result()
            results[slug] = url
            marker = "✓" if url else "·"
            print(f"  {marker} {slug:35s}  {url or '(no URL found via common patterns)'}")

    found = {s: u for s, u in results.items() if u}
    print(f"\n=== Found {len(found)}/{len(slugs_to_probe)} via probing ===")
    for slug, url in found.items():
        print(f"  {slug}: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
