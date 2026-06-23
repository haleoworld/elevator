"""Phase 3: Haiku-based JD screen + resume-alignment scoring.

For each job at filter_status='passed' AND stage='filtered', call Haiku once
with a forced tool call. Updates the job row with fit_score, fit_reason,
ats_score, ats_missing_keywords, stage='scored'. Logs cost to api_costs.

Combines the proposal's stages 3 (LLM JD screen) and 4 (ATS-like scoring)
into a single call — Haiku is cheap enough that two round-trips weren't
worth the extra latency or code.
"""
from __future__ import annotations

import json
import os
import re

import anthropic

from . import db, filter as _rules_filter, profile_store
from .discovery import workday as _workday  # for lazy Workday JD fetch

MODEL = "claude-haiku-4-5"

# Per-million pricing for Haiku 4.5 (cached from skill 2026-04-29).
# Local cost-tracking only — Anthropic bills off their side.
PRICE_INPUT_PER_M = 1.00
PRICE_OUTPUT_PER_M = 5.00
PRICE_CACHE_READ_PER_M = 0.10    # cache reads ~10% of base input
PRICE_CACHE_WRITE_PER_M = 1.25   # 5-min TTL writes ~125% of base input

# Ordered list of perspectives the LLM must score. Display labels live in
# main.py / templates so they can be tweaked without touching the LLM contract.
PERSPECTIVES = (
    "tech_stack",
    "fe_be_breakdown",
    "requirements",
    "role_expectations",
    "team_culture",
    "company_culture",
    "company_value",
    "company_business_model",
)


_PERSPECTIVE_DESCRIPTIONS = {
    "tech_stack": (
        "Frameworks/languages/tools the JD asks for. the candidate's core is "
        "Angular + React + TypeScript; Vue/Svelte/Next.js are good; Node and "
        "Python are mid; others are gaps."
    ),
    "fe_be_breakdown": (
        "What share of day-to-day work is frontend vs backend/infra based on "
        "the JD? the candidate's minimum is roughly 50% frontend."
    ),
    "requirements": (
        "Hard requirements (years of experience, must-have skills, "
        "certifications) and how each maps onto the candidate's resume."
    ),
    "role_expectations": (
        "Seniority level (IC vs lead/manager/architect), on-call/pager-duty "
        "burden, leadership scope. the candidate wants senior IC ~5+ YOE with no "
        "rotating on-call."
    ),
    "team_culture": (
        "Collaboration model, agile cadence, code-quality and review "
        "expectations as visible from the JD. the candidate values calm, technical, "
        "methodical teams."
    ),
    "company_culture": (
        "Company pace, growth stage, hype level. the candidate wants post-chaos "
        "mature B2B — not consumer hype or startup grind."
    ),
    "company_value": (
        "Strategic value: who the buyer is and how serious the problem is. "
        "the candidate favors companies whose product solves real complex problems "
        "for sophisticated buyers (regulated industries, enterprise depth)."
    ),
    "company_business_model": (
        "Vertical SaaS vs horizontal SaaS vs infra vs consumer. the candidate's best "
        "fit is B2B vertical SaaS (Veeva pattern); B2B horizontal SaaS "
        "(Veeva-adjacent) acceptable; B2B infra/security (CrowdStrike "
        "pattern) acceptable; consumer is out."
    ),
}


def _perspective_schema(name: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "description": (
                    "0-100 alignment with the candidate's profile for this perspective. "
                    "Be honest. 0=no signal/clear mismatch, 100=ideal."
                ),
            },
            "analysis": {
                "type": "string",
                "description": (
                    "2-3 sentences citing specific JD/profile evidence. Don't be "
                    "generic. Note when JD detail is sparse on this perspective. "
                    f"Perspective context: {_PERSPECTIVE_DESCRIPTIONS[name]}"
                ),
            },
        },
        "required": ["score", "analysis"],
        "additionalProperties": False,
    }


# Note: strict tool mode rejects `minimum`/`maximum`/`maxItems` etc. Ranges are
# communicated via description and clamped client-side after parse.
SCORE_TOOL = {
    "name": "score_job",
    "description": "Score a single job against the candidate's profile across 8 perspectives.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "is_frontend": {
                "type": "boolean",
                "description": (
                    "Is this fundamentally a frontend engineering role? Even if the title is "
                    "generic like 'Senior Engineer', infer from the JD whether day-to-day work "
                    "is primarily building user interfaces or frontend infrastructure."
                ),
            },
            "fit_score": {
                "type": "integer",
                "description": (
                    "Overall fit for the candidate as an integer 0-100, weighted across the 8 "
                    "perspectives below. Tech-stack, fe_be_breakdown, role_expectations, and "
                    "company_business_model carry the most weight (they're deal-breakers in "
                    "the candidate's criteria). Reserve 80+ for genuine senior-frontend matches at "
                    "curated B2B vertical SaaS or B2B infra companies. Clear backend, hardware, "
                    "or non-engineering roles → fit_score below 30 + is_frontend=false."
                ),
            },
            "resume_alignment": {
                "type": "number",
                "description": (
                    "How well the candidate's resume matches the JD's requirements, as a float from 0.0 "
                    "to 1.0. 0=no overlap, 1=resume covers everything the JD asks for."
                ),
            },
            "missing_requirements": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Up to 5 important JD requirements that the candidate's resume does NOT clearly "
                    "demonstrate. Empty array if resume covers everything. Keep each item short "
                    "(<= 8 words)."
                ),
            },
            "tech_stack":             _perspective_schema("tech_stack"),
            "fe_be_breakdown":        _perspective_schema("fe_be_breakdown"),
            "requirements":           _perspective_schema("requirements"),
            "role_expectations":      _perspective_schema("role_expectations"),
            "team_culture":           _perspective_schema("team_culture"),
            "company_culture":        _perspective_schema("company_culture"),
            "company_value":          _perspective_schema("company_value"),
            "company_business_model": _perspective_schema("company_business_model"),
        },
        "required": [
            "is_frontend", "fit_score", "resume_alignment", "missing_requirements",
            *PERSPECTIVES,
        ],
        "additionalProperties": False,
    },
}


# The 4 deal-breaker perspectives that get 2x weight. Floor logic also runs
# on these: if any of them scores below DEAL_BREAKER_THRESHOLD, the overall
# fit_score is capped at PRACTICE_CEILING so it can't reach apply tier.
_DEAL_BREAKER_PERSPECTIVES = (
    "tech_stack", "fe_be_breakdown", "role_expectations", "company_business_model",
)
_NON_DEAL_BREAKER_PERSPECTIVES = (
    "requirements", "team_culture", "company_culture", "company_value",
)
_DEAL_BREAKER_THRESHOLD = 40   # below this on a deal-breaker → cap
_PRACTICE_CEILING = 65         # the cap value (top of practice tier)


def _compute_fit_score(perspectives: dict) -> int | None:
    """Deterministic overall fit_score from per-perspective scores.

      weighted_avg = (2 * sum(deal_breakers) + sum(others)) / 12
      if min(deal_breakers) < _DEAL_BREAKER_THRESHOLD:
          fit_score = min(weighted_avg, _PRACTICE_CEILING)
      else:
          fit_score = weighted_avg

    Returns None if any required perspective score is missing — callers
    should fall back to the LLM's own fit_score in that case."""
    db_scores: list[int] = []
    other_scores: list[int] = []
    for k in _DEAL_BREAKER_PERSPECTIVES:
        s = (perspectives.get(k) or {}).get("score")
        if not isinstance(s, int):
            return None
        db_scores.append(s)
    for k in _NON_DEAL_BREAKER_PERSPECTIVES:
        s = (perspectives.get(k) or {}).get("score")
        if not isinstance(s, int):
            return None
        other_scores.append(s)

    weighted_avg = (2 * sum(db_scores) + sum(other_scores)) / 12
    rounded = round(weighted_avg)
    if min(db_scores) < _DEAL_BREAKER_THRESHOLD:
        return min(rounded, _PRACTICE_CEILING)
    return max(0, min(100, rounded))


def _clamp_int(v, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = lo
    return max(lo, min(hi, n))


def _clamp_float(v, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        x = lo
    return max(lo, min(hi, x))


def _system_prompt() -> list[dict]:
    """Static system prompt with the user's profile. `cache_control` is set so
    that once the profile grows past Haiku 4.5's 4096-token minimum, jobs 2..N
    in a batch read at ~10% of base input cost. Below the minimum it silently
    no-ops — no error, just no savings."""
    bodies = profile_store.read_all()
    criteria = bodies.get("Criteria", "").strip()
    resume = bodies.get("Resume", "").strip()
    career_bio = bodies.get("Career Bio", "").strip()
    work_history = bodies.get("Work History Detail", "").strip()

    text = (
        "You are screening senior frontend engineering jobs for a candidate. Use the "
        "candidate's criteria, resume, and career history below as the source of truth — "
        "judge fit from that evidence, not assumptions.\n\n"
        f"CANDIDATE'S CRITERIA:\n{criteria}\n\n"
        f"CANDIDATE'S RESUME:\n{resume}\n\n"
        f"CANDIDATE'S CAREER BIO:\n{career_bio}\n\n"
        f"CANDIDATE'S WORK HISTORY DETAIL:\n{work_history}\n\n"
        "SCORING METHODOLOGY — call the score_job tool exactly once. Analyze the JD across "
        "these 8 perspectives in order, scoring each 0-100 with a 2-3 sentence analysis that "
        "cites specific evidence from the JD and the candidate's profile:\n\n"
        "1. tech_stack — frameworks/languages/tools required; map to the candidate's expertise.\n"
        "2. fe_be_breakdown — what share of day-to-day is FE vs BE/infra?\n"
        "3. requirements — hard requirements and how each maps to the candidate's resume.\n"
        "4. role_expectations — seniority (IC vs lead/manager/architect), on-call burden, "
        "leadership scope. the candidate wants senior IC ~5+ YOE, no rotating on-call.\n"
        "5. team_culture — collaboration/agile cadence/code quality from JD signals.\n"
        "6. company_culture — pace, growth stage, hype level.\n"
        "7. company_value — who buys this, how serious the problem solved is.\n"
        "8. company_business_model — vertical SaaS / horizontal SaaS / infra / consumer.\n\n"
        "When the user message includes a CURATED-LIST FACTS block, those facts override "
        "your training-data guesses for the relevant perspectives (especially "
        "company_business_model and company_culture). When facts aren't provided, use your "
        "general knowledge of the company plus the JD.\n\n"
        "After scoring all 8 perspectives, set the overall fit_score as a weighted judgment. "
        "Heavier weight on tech_stack + fe_be_breakdown + role_expectations + "
        "company_business_model — those are deal-breakers in the candidate's criteria. Be honest, "
        "don't pad. Reserve 80+ for genuine senior-FE matches at curated B2B vertical SaaS "
        "or B2B infra companies. Clear backend, hardware, sales, or non-engineering roles → "
        "fit_score below 30 + is_frontend=false."
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _curated_facts(company_name: str) -> str:
    """Return a CURATED-LIST FACTS block for the given company name, or "" if
    the company isn't on the candidate's curated list. Uses normalized fuzzy matching."""
    if not company_name:
        return ""
    from . import companies_store
    norm = re.sub(r"[^a-z0-9]+", " ", company_name.lower()).strip()
    if not norm:
        return ""
    for c in companies_store.load_all():
        cn = re.sub(r"[^a-z0-9]+", " ", c.name.lower()).strip()
        if cn == norm or norm in cn or cn in norm:
            pattern = companies_store.classify_pattern(c.segment, c.slug)
            parts = [
                f"- Pattern (the candidate's classification): {pattern}",
                f"- Segment: {c.segment or '(none)'}",
                f"- Headcount band: {c.headcount_band or '(unknown)'}",
            ]
            if c.has_canada_team is True:
                parts.append("- Confirmed Canada team: yes")
            elif c.has_canada_team is False:
                parts.append("- Confirmed Canada team: no (US-only or other)")
            if c.notes:
                parts.append(f"- the candidate's notes: {c.notes}")
            return "CURATED-LIST FACTS:\n" + "\n".join(parts)
    return ""


def _user_message(job: dict) -> str:
    jd = (job.get("jd_text") or "").strip()
    if not jd:
        jd = "(no JD text available — score conservatively based on title alone)"
    facts = _curated_facts(job.get("company") or "")
    facts_block = f"\n\n{facts}" if facts else ""
    return (
        f"{job['company']} — {job['title']} "
        f"({job.get('location') or 'location unknown'})"
        f"{facts_block}\n\n"
        f"JOB DESCRIPTION:\n{jd}"
    )


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.Anthropic(api_key=key)


def _score_one(
    client: anthropic.Anthropic, job: dict, system: list[dict]
) -> tuple[dict, dict] | None:
    """Returns (parsed_tool_input, usage_dict) or None on error."""
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,  # 8 perspective analyses ~50-100 tok each → ~700-1k tok plus other fields
            system=system,
            messages=[{"role": "user", "content": _user_message(job)}],
            tools=[SCORE_TOOL],
            tool_choice={"type": "tool", "name": "score_job"},
        )
    except anthropic.BadRequestError as e:
        print(f"  [job {job['id']}] bad request: {e.message}")
        return None
    except anthropic.RateLimitError:
        print(f"  [job {job['id']}] rate limited — skipping (will retry on next run)")
        return None
    except anthropic.APIStatusError as e:
        print(f"  [job {job['id']}] API error {e.status_code}: {e.message}")
        return None

    if resp.stop_reason == "refusal":
        print(f"  [job {job['id']}] LLM refused — skipping")
        return None

    tool_block = next(
        (b for b in resp.content if b.type == "tool_use" and b.name == "score_job"),
        None,
    )
    if tool_block is None:
        print(f"  [job {job['id']}] no score_job in response (stop_reason={resp.stop_reason})")
        return None

    usage = {
        "input_tokens": resp.usage.input_tokens or 0,
        "output_tokens": resp.usage.output_tokens or 0,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return dict(tool_block.input), usage


def _cost_usd(usage: dict) -> float:
    return (
        usage["input_tokens"] * PRICE_INPUT_PER_M / 1_000_000
        + usage["output_tokens"] * PRICE_OUTPUT_PER_M / 1_000_000
        + usage["cache_read_input_tokens"] * PRICE_CACHE_READ_PER_M / 1_000_000
        + usage["cache_creation_input_tokens"] * PRICE_CACHE_WRITE_PER_M / 1_000_000
    )


def _log_cost(conn, job_id: int, usage: dict, cost: float) -> None:
    conn.execute(
        "INSERT INTO api_costs (provider, operation, job_id, input_tokens, output_tokens, "
        "cached_input_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "anthropic", "fit_screen", job_id,
            usage["input_tokens"], usage["output_tokens"],
            usage["cache_read_input_tokens"], cost,
        ),
    )


def run(*, limit: int | None = None, job_ids: list[int] | None = None) -> dict:
    """Screen passed-but-not-yet-scored jobs. Returns summary stats.
    Pass job_ids to restrict to specific rows (used by manual-paste flow)."""
    client = _client()
    system = _system_prompt()

    stats = {"checked": 0, "scored": 0, "errors": 0, "total_cost_usd": 0.0}
    with db.connect() as conn:
        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            sql = (
                f"SELECT id, company, title, location, jd_text FROM jobs "
                f"WHERE id IN ({placeholders}) AND filter_status='passed' "
                f"ORDER BY id"
            )
            rows = conn.execute(sql, tuple(job_ids)).fetchall()
        else:
            sql = (
                "SELECT id, company, title, location, jd_text FROM jobs "
                "WHERE filter_status='passed' AND stage='filtered' "
                "ORDER BY id"
            )
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
        stats["checked"] = len(rows)
        if not rows:
            return stats

        for i, row in enumerate(rows, 1):
            job = dict(row)
            # Lazy detail fetch for Workday jobs — the list scraper deliberately
            # left jd_text empty (plus remote_type, posted_at, sometimes
            # location) so we don't pay detail-call cost on jobs that would be
            # dropped by Phase 2. Now that this one passed, fill those in.
            if not (job.get("jd_text") or "").strip() and "myworkdayjobs.com" in (job.get("url") or ""):
                from .discovery.persist import _normalize_timestamp  # noqa: E402
                detail = _workday.fetch_detail(job["url"])
                # Resolve the real location independently of jd_text: the list
                # view shows an "N Locations" placeholder that the Phase-2 filter
                # can't evaluate, and the detail call may resolve the city even
                # when JD-text extraction fails.
                updates: dict = {}
                new_loc = detail.get("location")
                cur_loc = (job.get("location") or "").strip()
                if new_loc and (not cur_loc or re.match(r"^\d+\s+Locations?$", cur_loc, re.IGNORECASE)):
                    updates["location"] = new_loc
                if detail.get("jd_text"):
                    updates["jd_text"] = detail["jd_text"]
                    if detail.get("remote_type") and not job.get("remote_type"):
                        updates["remote_type"] = detail["remote_type"]
                    if detail.get("posted_at") and not job.get("posted_at"):
                        norm = _normalize_timestamp(detail["posted_at"])
                        if norm:
                            updates["posted_at"] = norm
                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(
                        f"UPDATE jobs SET {set_clause} WHERE id = ?",
                        (*updates.values(), job["id"]),
                    )
                    conn.commit()
                    for k, v in updates.items():
                        job[k] = v

            # If the location is still an "N Locations" placeholder (or empty),
            # derive the primary city from the Workday URL path — reliable even
            # when the detail fetch fails or also returns the placeholder.
            cur = (job.get("location") or "").strip()
            if (not cur or re.match(r"^\d+\s+Locations?$", cur, re.IGNORECASE)) \
                    and "myworkdayjobs.com" in (job.get("url") or ""):
                url_loc = _workday._location_from_url(job["url"])
                if url_loc and url_loc != cur:
                    job["location"] = url_loc
                    conn.execute("UPDATE jobs SET location = ? WHERE id = ?",
                                 (url_loc, job["id"]))
                    conn.commit()

            # Re-apply the location gate now that the real city is known — drops
            # multi-location Workday postings that turn out to be non-Canada
            # (they sailed past Phase-2 with an "N Locations" placeholder).
            loc_reason = _rules_filter.location_disqualified(job.get("location"))
            if loc_reason:
                conn.execute(
                    "UPDATE jobs SET filter_status = 'dropped', drop_reason = ?, "
                    "stage = 'filtered' WHERE id = ?",
                    (loc_reason, job["id"]),
                )
                conn.commit()
                stats["dropped_location"] = stats.get("dropped_location", 0) + 1
                continue  # skip the (paid) LLM screen for an out-of-scope job

            result = _score_one(client, job, system)
            if result is None:
                stats["errors"] += 1
                continue
            output, usage = result
            cost = _cost_usd(usage)
            stats["total_cost_usd"] += cost
            stats["scored"] += 1

            missing = output.get("missing_requirements") or []
            if not isinstance(missing, list):
                missing = []
            missing = [str(x) for x in missing[:5]]

            # Extract perspectives and clamp sub-scores.
            perspectives: dict[str, dict] = {}
            for p in PERSPECTIVES:
                blk = output.get(p) or {}
                if isinstance(blk, dict):
                    perspectives[p] = {
                        "score": _clamp_int(blk.get("score"), 0, 100),
                        "analysis": str(blk.get("analysis") or "").strip(),
                    }

            # Synthesize a short fit_reason from the two lowest-scoring + one
            # highest-scoring perspective — surfaces the main concern and the
            # main strength in one sentence for list/preview UIs.
            fit_reason = ""
            if perspectives:
                ranked = sorted(perspectives.items(), key=lambda kv: kv[1]["score"])
                low = ranked[0]
                high = ranked[-1]
                fit_reason = (
                    f"Strongest: {high[0].replace('_', ' ')} ({high[1]['score']}). "
                    f"Weakest: {low[0].replace('_', ' ')} ({low[1]['score']}) — "
                    f"{low[1]['analysis'][:160]}"
                )

            # Override the LLM's own fit_score with a deterministic value
            # computed from the per-perspective scores — fixes the chronic
            # inconsistency where the LLM's overall judgment drifts from its
            # analytical breakdown. Falls back to the LLM's number only if a
            # perspective is missing (shouldn't happen, but be safe).
            computed = _compute_fit_score(perspectives)
            fit_score_final = (
                computed if computed is not None
                else _clamp_int(output.get("fit_score"), 0, 100)
            )

            conn.execute(
                "UPDATE jobs SET fit_score=?, fit_reason=?, ats_score=?, "
                "ats_missing_keywords=?, stage='scored' WHERE id=?",
                (
                    fit_score_final,
                    fit_reason,
                    _clamp_float(output.get("resume_alignment"), 0.0, 1.0),
                    json.dumps({
                        "is_frontend": bool(output.get("is_frontend")),
                        "missing_requirements": missing,
                        "perspectives": perspectives,
                    }),
                    job["id"],
                ),
            )
            _log_cost(conn, job["id"], usage, cost)

            if i % 10 == 0 or i == len(rows):
                conn.commit()
                print(f"  [{i}/{len(rows)}] scored — running cost ${stats['total_cost_usd']:.4f}")

        conn.commit()
    return stats


if __name__ == "__main__":
    import time
    print(f"[screen]   Haiku JD screen + resume alignment")
    t0 = time.time()
    stats = run()
    print(f"           checked={stats['checked']}  scored={stats['scored']}  errors={stats['errors']}")
    print(f"           cost=${stats['total_cost_usd']:.4f}  ({time.time() - t0:.1f}s)")
