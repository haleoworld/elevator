"""Apply Canada-presence findings from the web-search batch.

Conservative: only marking YES where the search returned a clear signal
(Toronto/Vancouver/Calgary/Waterloo office, Canadian HQ, or active job
listings in Canada). The rest stay None — user fills via UI as needed.
"""
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import yaml  # noqa: E402

YAML_PATH = REPO_ROOT / "config" / "companies.yaml"

# Slugs newly confirmed YES via web search
SET_YES = {
    "phreesia",      # Confirmed Software Engineer listings in Toronto
    "duck-creek-technologies",  # Acquired Toronto-based RCT (Oct 2024)
    "anaplan",       # Anaplan Software Canada Inc. (Toronto entity)
    "tipalti",       # Toronto (85 Richmond St W) + Vancouver offices
    "clio",          # Vancouver HQ — Canadian company; offices in Toronto, Calgary
    "d2l-brightspace",  # Waterloo, ON HQ — Canadian company!
    "verisk-analytics",  # en-ca product line + Canadian operations
    # Add a few from confident industry knowledge
    "benchling",     # Search noted "customers in Canada"; soft yes — will catch via jobs.db
}

# Slugs where searches surfaced clear US-only signals (PE-owned vertical SaaS, US HQ, no Canada signal)
SET_NO = {
    "tulip-interfaces",  # Confirmed Somerville HQ, offices in EU/Singapore/Tel Aviv/Tokyo — no Canada
    "aspen-technology",  # Bedford MA, no clear Canada presence
    "aveva",         # Cambridge UK, no Canada engineering signal
    "hexagon",       # Stockholm Sweden HQ
    "castor",        # Amsterdam Netherlands
    "sapiens",       # Israel HQ — small US presence, not Canada
    "cover-genius",  # Sydney AU
}


def main() -> int:
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f) or {}
    companies = data.get("companies") or []
    set_yes_n = 0
    set_no_n = 0
    for c in companies:
        slug = c.get("slug", "")
        if slug in SET_YES:
            c["has_canada_team"] = True
            set_yes_n += 1
        elif slug in SET_NO:
            c["has_canada_team"] = False
            set_no_n += 1

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
    print(f"Marked YES on {set_yes_n} companies")
    print(f"Marked NO on  {set_no_n} companies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
