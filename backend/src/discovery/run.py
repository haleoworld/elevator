"""CLI: run the pipeline end-to-end (discovery → filter → screen).

Usage:
    jarun                       # all stages
    jarun --no-filter           # pull only
    jarun --no-screen           # pull + filter, skip LLM screen
    jarun --filter-only         # re-filter existing jobs
    jarun --screen-only         # re-screen passed-but-unscored jobs
    jarun --screen-limit 10     # cap LLM calls (cost guard during testing)

Phase 5 will wrap this in a LaunchAgent for daily 2am execution.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

import os  # noqa: E402

from .. import batches, content, db, filter as rules_filter, notifier, screen  # noqa: E402
from . import adzuna, ashby, greenhouse, lever, persist, smartrecruiters, workday  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-filter", action="store_true", help="skip the rules filter stage")
    ap.add_argument("--no-screen", action="store_true", help="skip the Phase 3 LLM screen stage")
    ap.add_argument("--filter-only", action="store_true", help="skip pulls; just (re-)filter")
    ap.add_argument("--screen-only", action="store_true", help="skip pulls + filter; just (re-)screen")
    ap.add_argument("--prune-expired", action="store_true",
                    help="drop active jobs whose posting was removed from the board")
    ap.add_argument("--record-run", action="store_true",
                    help="log this run's new-found / new-passed counts to the activity history")
    ap.add_argument("--screen-limit", type=int, default=None,
                    help="cap how many jobs the LLM screens (useful for cost-controlled tests)")
    ap.add_argument("--batch-now", action="store_true",
                    help="after screen, form a batch from top-N scored and generate content")
    ap.add_argument("--batch-size", type=int, default=5,
                    help="how many jobs to include when --batch-now is set (default 5)")
    args = ap.parse_args()

    do_pull = not (args.filter_only or args.screen_only)
    do_filter = not (args.no_filter or args.screen_only)
    do_screen = not args.no_screen
    ins = 0

    if do_pull:
        t0 = time.time()
        print("[discover] Adzuna pull")
        adzuna_jobs = adzuna.pull()
        print(f"           {len(adzuna_jobs)} jobs from Adzuna")

        print("[discover] Greenhouse + Lever pulls")
        gh_jobs = greenhouse.pull()
        lv_jobs = lever.pull()
        print(f"           {len(gh_jobs)} jobs from Greenhouse, {len(lv_jobs)} from Lever")

        print("[discover] Workday pulls")
        wd_jobs = workday.pull()
        print(f"           {len(wd_jobs)} jobs from Workday")

        print("[discover] Ashby + SmartRecruiters pulls")
        ab_jobs = ashby.pull()
        sr_jobs = smartrecruiters.pull()
        print(f"           {len(ab_jobs)} jobs from Ashby, {len(sr_jobs)} from SmartRecruiters")

        all_raw = adzuna_jobs + gh_jobs + lv_jobs + wd_jobs + ab_jobs + sr_jobs
        ins, skip = persist.save(all_raw)
        print(f"[discover] Persisted: {ins} new, {skip} already in DB ({time.time() - t0:.1f}s)")

    filter_passed = None
    if do_filter:
        print("[filter]")
        stats = rules_filter.run()
        filter_passed = stats["passed"]
        print(f"           checked={stats['checked']}  passed={stats['passed']}  dropped={stats['dropped']}")
        for reason, n in sorted(stats["by_reason"].items(), key=lambda x: -x[1])[:8]:
            print(f"             {n:4d}  {reason}")

    if args.record_run and do_filter:
        db.record_run("discovery", new_jobs=ins, new_passed=filter_passed or 0)
        print(f"[record]   discovery run logged (new={ins}, passed={filter_passed or 0})")

    if args.prune_expired:
        print("[prune]    checking postings still exist on the board")
        from .. import expire
        pstats = expire.prune()
        print(f"           checked={pstats['checked']}  expired/dropped={pstats['expired']}")

    if do_screen:
        print("[screen]   Haiku JD screen + resume alignment")
        t0 = time.time()
        stats = screen.run(limit=args.screen_limit)
        print(f"           checked={stats['checked']}  scored={stats['scored']}  errors={stats['errors']}")
        print(f"           cost=${stats['total_cost_usd']:.4f}  ({time.time() - t0:.1f}s)")

    if args.batch_now:
        print(f"[batch]    forming batch from top {args.batch_size} scored")
        batch_id = batches.form_batch(top_n=args.batch_size, trigger_reason="cli")
        if batch_id is None:
            print("           no scored jobs available — skipping")
        else:
            print(f"           batch #{batch_id} formed")
            print("[content]  Haiku cover letter + why-this-company")
            t0 = time.time()
            cstats = content.generate(batch_id)
            print(f"           drafted={cstats['drafted']}  errors={cstats['errors']}")
            print(f"           cost=${cstats['total_cost_usd']:.4f}  ({time.time() - t0:.1f}s)")
            bundle = batches.get_batch(batch_id)
            url = os.environ.get(
                "ELEVATOR_DASHBOARD_URL", "http://localhost:8742"
            ).rstrip("/") + f"/batches/{batch_id}"
            ok, msg = notifier.notify_batch_ready(
                batch_id, bundle["jobs"] if bundle else [], url
            )
            print(f"[notify]   {msg}")

    # Cheap nightly nudge — fires when the filter stage passed 1+ jobs and we
    # didn't already send a richer batch-ready ping.
    if not args.batch_now and filter_passed:
        base = os.environ.get("ELEVATOR_DASHBOARD_URL", "http://localhost:8742").rstrip("/")
        ok, msg = notifier.notify_filter_pass(filter_passed, f"{base}/jobs?tab=passed")
        print(f"[notify]   {msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
