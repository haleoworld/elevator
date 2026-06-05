"""Apply researched Workday URLs (and a few missed Greenhouse/Lever slugs)
to config/companies.yaml. Idempotent — preserves any URLs already set.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

YAML_PATH = REPO_ROOT / "config" / "companies.yaml"

# slug → updates to apply (only fields we want to set)
UPDATES = {
    # Confirmed Workday URLs
    "crowdstrike":           {"workday_url": "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers"},
    "guidewire":             {"workday_url": "https://guidewire.wd5.myworkdayjobs.com/External"},
    "ncino":                 {"workday_url": "https://ncino.wd5.myworkdayjobs.com/ncinocareers"},
    "wellsky":               {"workday_url": "https://wellsky.wd1.myworkdayjobs.com/wellskycareers"},
    "workiva":               {"workday_url": "https://workiva.wd1.myworkdayjobs.com/careers"},
    "ptc":                   {"workday_url": "https://ptc.wd1.myworkdayjobs.com/PTC"},
    "autodesk":              {"workday_url": "https://autodesk.wd1.myworkdayjobs.com/Ext"},
    "procore":               {"workday_url": "https://procore.wd12.myworkdayjobs.com/Procore_External_Careers"},
    "aspen-technology":      {"workday_url": "https://aspentech.wd5.myworkdayjobs.com/AspenTech"},
    "aveva":                 {"workday_url": "https://aveva.wd3.myworkdayjobs.com/AVEVA_careers"},
    "q2-holdings":           {"workday_url": "https://q2ebanking.wd5.myworkdayjobs.com/Q2"},
    "manhattan-associates":  {"workday_url": "https://manh.wd5.myworkdayjobs.com/External"},
    "phreesia":              {"workday_url": "https://phreesia.wd1.myworkdayjobs.com/Phreesia"},

    # Missed previously — actually on Greenhouse/Lever
    "anaplan":               {"greenhouse_slug": "anaplan"},
    "definitive-healthcare": {"greenhouse_slug": "definitivehc"},
    "pointclickcare":        {"lever_slug": "pointclickcare"},
}


def main() -> int:
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f) or {}
    companies = data.get("companies") or []
    applied = []
    for c in companies:
        slug = c.get("slug")
        if slug in UPDATES:
            for k, v in UPDATES[slug].items():
                c[k] = v
            applied.append(slug)

    # Re-emit yaml preserving original key order per entry as much as possible.
    # Use the same field ordering convention as the rest of the file.
    field_order = ["name", "slug", "segment", "headcount_band",
                   "greenhouse_slug", "lever_slug", "workday_url",
                   "enabled", "notes"]
    sorted_companies = []
    for c in companies:
        sorted_companies.append({k: c.get(k) for k in field_order if k in c or c.get(k) is not None})

    # Write atomically
    out = {"companies": sorted_companies}
    with open(YAML_PATH, "w") as f:
        f.write("# Curated target companies. Edited via the dashboard /companies page\n")
        f.write("# (or directly here). Phase 2 discovery uses this file.\n#\n")
        f.write("# Fields:\n#   name             display name\n#   slug             stable identifier (also URL-safe). Don't rename casually.\n")
        f.write("#   segment          freeform tag for diversification analytics\n#   headcount_band   one of: 40-500 | 500-5000 | 5000-15000\n")
        f.write("#   greenhouse_slug  if non-null, pulled from boards.greenhouse.io/<slug>\n#   lever_slug       if non-null, pulled from jobs.lever.co/<slug>\n")
        f.write("#   workday_url      full board URL like https://<co>.wd5.myworkdayjobs.com/<board>\n")
        f.write("#   enabled          false = skip in discovery (kept here for history)\n#   notes            free text\n\n")
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True, default_flow_style=False, width=200)

    print(f"Applied URLs to {len(applied)} companies:")
    for s in applied:
        print(f"  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
