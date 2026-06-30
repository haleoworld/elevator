# Plan: Re-host source tagging + jobs-page filter

**Status:** Implemented 2026-06-29. Approved approach = Option A (flag, don't drop).

## Goal
SerpAPI/Google-Jobs discovery surfaces listings re-hosted on third-party sites
(resume tools, personal pages, small aggregators) that re-list jobs scraped from
elsewhere. Tag these so they can be reviewed in one batch — without dropping them,
because a re-host occasionally surfaces a legit direct-employer role (e.g. NEOGOV
came in via davidneevel.com).

## Why not reuse the existing `_AGGREGATORS` drop-list
`src/discovery/serpapi.py:_AGGREGATORS` skips listings that are ONLY available
through an aggregator (drop-at-discovery = Option C). Adding these domains there
would have dropped the NEOGOV-type catches. So this is a SEPARATE, display-only
tag list — discovery pipeline is untouched.

## Design (flag-only; no DB migration, no pipeline change)
- `src/filter.py`: `REHOST_DOMAINS` tuple + `is_rehost(url)` helper.
- `src/main.py` `jobs_view`: select `url`, compute `is_rehost` per row, add
  `q_rehost` query param (`only` / `hide`) applied SQL-side in `append_filters`.
- `src/templates/jobs.html`: "re-host" amber badge (`badge status-warn`) on the
  Board cell when tagged; a **Re-host** dropdown (any / only / hide) in the filter
  form; `q_rehost` wired into the clear-filters condition.

## Seed list
cvcraft.roynex.com, roynex.com, davidneevel.com, thecareerwallet.com,
careerwallet.com, sercanto, jobspider, jobzmall. Append as new re-hosts appear.

## Verification
- py_compile (main.py, filter.py) + jobs.html Jinja parse: pass.
- `is_rehost` unit checks: re-hosts True, LinkedIn/Greenhouse False.
- SQL filter matches exactly the 6 known re-host jobs (incl. NEOGOV, tagged not dropped).
- Server restart clean; `/jobs` + `?q_rehost=only|hide` return 303 (auth), no 500.
- Pending: in-browser visual confirmation (badge + dropdown) — behind login.
