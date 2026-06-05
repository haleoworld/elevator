"""Apply round-2 ATS findings:
- 7 more Workday URLs
- AvidXchange's actual Greenhouse slug
- Ashby slugs (Sapiens, Instructure)
- SmartRecruiters slugs (BlackLine, Verisk)
"""
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import yaml  # noqa: E402

YAML_PATH = REPO_ROOT / "config" / "companies.yaml"

UPDATES = {
    # Round 2 Workday URLs (verified via web search)
    "duck-creek-technologies": {"workday_url": "https://duckcreek.wd1.myworkdayjobs.com/duckcreekcareers"},
    "applied-systems":         {"workday_url": "https://applied.wd501.myworkdayjobs.com/Applied"},
    "alkami-technology":       {"workday_url": "https://alkami.wd12.myworkdayjobs.com/Alkami"},
    "intapp":                  {"workday_url": "https://intapp.wd1.myworkdayjobs.com/Intapp"},
    "fis":                     {"workday_url": "https://fis.wd5.myworkdayjobs.com/SearchJobs"},
    "temenos":                 {"workday_url": "https://temenos.wd103.myworkdayjobs.com/Temenoscareers"},
    "nextgen-healthcare":      {"workday_url": "https://nextgen.wd5.myworkdayjobs.com/NextGen_Careers"},
    # Greenhouse slug fix (we had wrong slug originally)
    "avidxchange":             {"greenhouse_slug": "avidxchangeinc"},
    # Ashby
    "sapiens":                 {"ashby_slug": "sapien"},
    "instructure":             {"ashby_slug": "instructure"},
    # SmartRecruiters
    "blackline":               {"smartrecruiters_slug": "BlackLine"},
    "verisk-analytics":        {"smartrecruiters_slug": "Verisk"},
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

    print(f"Applied updates to {len(applied)} companies: {', '.join(applied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
