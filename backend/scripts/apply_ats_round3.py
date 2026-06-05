"""Apply round-3 ATS findings + corrections.

Adds: Relativity, Blue Yonder, Health Catalyst (Workday); Mitratech (SmartRecruiters);
Ironclad (Ashby - if it works).

Drops failed: BlackLine and Verisk SmartRecruiters slugs (API returned 0 — they
don't expose postings via the public endpoint).
"""
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import yaml  # noqa: E402

YAML_PATH = REPO_ROOT / "config" / "companies.yaml"

# slug → updates to apply
UPDATES = {
    "relativity":      {"workday_url": "https://kcura.wd1.myworkdayjobs.com/External_Career_Site"},
    "blue-yonder":     {"workday_url": "https://jda.wd5.myworkdayjobs.com/JDA_Careers"},
    "health-catalyst": {"workday_url": "https://healthcatalyst.wd5.myworkdayjobs.com/healthcatalystcareers"},
    "mitratech":       {"smartrecruiters_slug": "Mitratech"},
    "ironclad":        {"ashby_slug": "ironcladhq"},  # may be transiently broken but config it
    # Remove the SmartRecruiters slugs that returned 0 jobs from the public API
    "blackline":       {"smartrecruiters_slug": None},
    "verisk-analytics":{"smartrecruiters_slug": None},
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

    field_order = ["name", "slug", "segment", "headcount_band",
                   "greenhouse_slug", "lever_slug", "workday_url",
                   "ashby_slug", "smartrecruiters_slug",
                   "enabled", "notes"]
    sorted_cs = [{k: c.get(k) for k in field_order if k in c or c.get(k) is not None} for c in companies]

    with open(YAML_PATH) as f:
        text = f.read()
    prelude = text.split("companies:")[0]
    with open(YAML_PATH, "w") as f:
        f.write(prelude)
        yaml.safe_dump({"companies": sorted_cs}, f, sort_keys=False, allow_unicode=True, default_flow_style=False, width=200)

    print(f"Applied: {', '.join(applied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
