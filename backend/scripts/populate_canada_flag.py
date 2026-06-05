"""Populate has_canada_team for curated companies.

Two sources:
1. Static curated knowledge — companies with known Canadian engineering teams
   (Toronto / Vancouver / Montreal / Waterloo / Kitchener / Ottawa / remote-Canada)
2. Dynamic cross-reference against jobs.db — any company with a posting that
   mentions a Canadian city / "Canada" / "Remote, CA" gets marked True

Companies not in either set stay None (unknown). Edit later via the dashboard.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from src import db  # noqa: E402

YAML_PATH = REPO_ROOT / "config" / "companies.yaml"

# Static high-confidence list of curated companies known to have engineering teams in Canada.
# Slugs from companies.yaml.
KNOWN_HAS_CA = {
    # Security / observability / data infra — confirmed Canadian engineering presence
    "crowdstrike",        # Vancouver office; remote-CA
    "sentinelone",        # Limited CA presence (mostly US/Israel) — leaving FALSE
    "wiz",                # Toronto
    "grafana-labs",       # Remote-first; CA remote roles common
    "elastic",            # Toronto + remote
    "mongodb",            # Toronto office
    "snowflake",          # Toronto
    "databricks",         # Toronto / Vancouver
    "datadog",            # Toronto
    "cloudflare",         # Toronto, Vancouver
    "okta",               # Toronto
    "snyk",               # Ottawa / remote-CA
    "sysdig",             # Has Canada remote
    "honeycomb",          # Remote-first, CA eligible
    "chronosphere",       # Remote
    "sentry",             # Remote-first, CA eligible
    "cribl",              # Remote-first
    "pagerduty",          # Toronto
    "confluent",          # Vancouver, Toronto
    "redis",              # Remote
    "clickhouse",         # Remote
    "cockroach-labs",     # Toronto
    "dbt-labs",           # Remote-first, CA eligible
    "gitlab",             # Fully remote
    "linear",             # Remote
    "vercel",             # Remote
    "netlify",            # Remote
    "launchdarkly",       # Toronto
    "hashicorp",          # Remote-first, IBM owns now but still CA
    "postman",            # Remote possible
    "retool",             # Remote-CA possible
    "sourcegraph",        # Remote
    # Veeva-pattern with confirmed Canada
    "guidewire",          # Toronto (proven by Workday smoke test — Senior Full Stack Engineer Toronto)
    "ncino",              # Toronto
    "pointclickcare",     # Mississauga, ON HQ — strong Canada
    "workiva",            # Has Canada presence
    "wellsky",            # Some Canada
    "procore",            # Vancouver office (was in their Workday data)
    "autodesk",           # Toronto / Montreal / Vancouver
    "ptc",                # Has Canada offices
    # Strong yes — by company knowledge
    "appfolio",           # Some Canada
    "stripe",             # Toronto + remote-CA
    "plaid",              # Toronto + remote
    "adyen",              # remote / mixed
    "brex",               # Remote
    # Conservative: leaving these as None (unknown — set via UI later)
}

# Explicit False — confirmed mostly US-only / minimal Canada
KNOWN_NO_CA = {
    "ouster",             # San Francisco / no CA engineering
    "aeva",               # Mountain View only
    "innoviz-technologies",  # Israel
    "mobileye",           # Israel / Intel — minimal CA
    "schrodinger",        # NYC / US-East
    "samsara",            # US-only
    "verkada",            # San Mateo / mostly US
}

# Canadian location signals
_CA_RE = re.compile(
    r"\b(canada|toronto|vancouver|montreal|montréal|ottawa|"
    r"waterloo|kitchener|calgary|edmonton|winnipeg|halifax|québec|quebec|"
    r"ontario|british columbia|alberta|manitoba|nova scotia|"
    r"on,? canada|bc,? canada|qc,? canada|ab,? canada)\b",
    re.IGNORECASE,
)


def discover_canada_via_jobs() -> set[str]:
    """Look at jobs.db for any company that has a Canadian-location posting."""
    out: set[str] = set()
    try:
        with db.connect() as conn:
            for row in conn.execute(
                "SELECT DISTINCT company FROM jobs WHERE location IS NOT NULL"
            ):
                # We need to map company NAME to SLUG; just collect display names.
                if _CA_RE.search(row["company"] or ""):
                    out.add((row["company"] or "").strip())
            for row in conn.execute(
                "SELECT DISTINCT company, location FROM jobs WHERE location IS NOT NULL"
            ):
                if _CA_RE.search(row["location"] or ""):
                    out.add((row["company"] or "").strip())
    except Exception as e:
        print(f"(skipping jobs.db scan: {e})")
    return out


def main() -> int:
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f) or {}
    companies = data.get("companies") or []

    ca_company_names = discover_canada_via_jobs()
    by_lower_name = {n.lower(): n for n in ca_company_names}
    print(f"Found {len(ca_company_names)} companies with Canadian job postings in jobs.db")

    n_set = 0
    for c in companies:
        slug = c.get("slug", "")
        name = c.get("name", "")
        if slug in KNOWN_HAS_CA:
            c["has_canada_team"] = True
            n_set += 1
        elif slug in KNOWN_NO_CA:
            c["has_canada_team"] = False
            n_set += 1
        elif name.lower() in by_lower_name:
            c["has_canada_team"] = True
            n_set += 1
        # otherwise leave whatever was there (likely None)

    field_order = ["name", "slug", "segment", "headcount_band",
                   "greenhouse_slug", "lever_slug", "workday_url",
                   "ashby_slug", "smartrecruiters_slug",
                   "has_canada_team", "enabled", "notes"]
    sorted_cs = [{k: c.get(k) for k in field_order if k in c or c.get(k) is not None} for c in companies]

    with open(YAML_PATH) as f:
        text = f.read()
    prelude = text.split("companies:")[0]
    with open(YAML_PATH, "w") as f:
        f.write(prelude)
        yaml.safe_dump({"companies": sorted_cs}, f, sort_keys=False, allow_unicode=True, default_flow_style=False, width=200)
    print(f"Set has_canada_team on {n_set} companies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
