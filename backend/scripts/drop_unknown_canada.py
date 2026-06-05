"""Drop curated companies where has_canada_team is None (unknown).

After web-search round we have:
  ~45 confirmed YES, ~13 confirmed NO, ~60 unknown.
User chose to remove the unknowns. Result: ~58 companies remain.
"""
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import yaml  # noqa: E402

YAML_PATH = REPO_ROOT / "config" / "companies.yaml"


def main() -> int:
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f) or {}
    companies = data.get("companies") or []
    before = len(companies)
    kept = [c for c in companies if c.get("has_canada_team") in (True, False)]
    dropped = [c for c in companies if c.get("has_canada_team") not in (True, False)]
    after = len(kept)

    field_order = ["name", "slug", "segment", "headcount_band",
                   "greenhouse_slug", "lever_slug", "workday_url",
                   "ashby_slug", "smartrecruiters_slug",
                   "has_canada_team", "enabled", "notes"]
    sorted_cs = [{k: c.get(k) for k in field_order if k in c or c.get(k) is not None} for c in kept]

    with open(YAML_PATH) as f:
        text = f.read()
    prelude = text.split("companies:")[0]
    with open(YAML_PATH, "w") as f:
        f.write(prelude)
        yaml.safe_dump({"companies": sorted_cs}, f, sort_keys=False, allow_unicode=True, default_flow_style=False, width=200)

    print(f"Before: {before}  After: {after}  Dropped: {len(dropped)}")
    print("\nDropped companies:")
    for c in dropped:
        print(f"  - {c['name']:30s}  ({c.get('segment')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
