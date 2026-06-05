"""Phase 4 stage 6: per-job application content generation.

Haiku + cached profile produces a cover letter + why-this-company paragraph
for every job in a batch. Output is structured via forced tool call.

Cost target (per proposal): ~$0.02/job. Actual Haiku cost on profile+JD
sits around $0.008-0.015 depending on JD length.
"""
from __future__ import annotations

import os

import anthropic

from . import db, profile_store

MODEL = "claude-haiku-4-5"

PRICE_INPUT_PER_M = 1.00
PRICE_OUTPUT_PER_M = 5.00
PRICE_CACHE_READ_PER_M = 0.10
PRICE_CACHE_WRITE_PER_M = 1.25

CONTENT_TOOL = {
    "name": "draft_application",
    "description": "Draft a cover letter and why-this-company paragraph for a single job.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "cover_letter": {
                "type": "string",
                "description": (
                    "A 280-340 word cover letter in the candidate's voice. First person. "
                    "Structure: (1) opening hook tied to a specific signal from this JD "
                    "or company — NOT 'I am writing to apply for…'; (2) 2-3 paragraphs "
                    "of his most relevant experience for THIS job, citing real outcomes "
                    "from his resume / career bio (Angular, React, RxJS, performance "
                    "scale, data-heavy enterprise UIs); (3) brief close that explains "
                    "why this role fits his current trajectory. "
                    "Use the tone from his Hiring Manager POV Summaries — direct, "
                    "specific, technical, no fluff. Avoid clichés like 'passionate', "
                    "'rockstar', 'fast-paced environment'. Avoid em-dashes (—) and "
                    "AI-typical punctuation. Plain prose, short sentences when possible."
                ),
            },
            "why_this_company": {
                "type": "string",
                "description": (
                    "A 120-180 word paragraph answering 'why this company' specifically. "
                    "Cite real things about THIS company from the JD — product, segment, "
                    "engineering culture, technical challenges mentioned in the posting. "
                    "Avoid generic statements that would fit any company. Connect to "
                    "the candidate's stated target (B2B vertical SaaS / B2B infrastructure, calm "
                    "technical engineering cultures). No fluff."
                ),
            },
        },
        "required": ["cover_letter", "why_this_company"],
        "additionalProperties": False,
    },
}


def _system_prompt() -> list[dict]:
    """Profile context for content generation. Cached — across a batch run
    every call after the first reads at ~10% of base input cost (once the
    profile is large enough to hit Haiku 4.5's 4096-token cache minimum)."""
    bodies = profile_store.read_all()
    text = (
        "You are drafting first-person application materials for the candidate, a senior "
        "frontend / full-stack software engineer. Use ONLY his real background. "
        "Do not invent projects, employers, or metrics he hasn't documented.\n\n"
        f"CANDIDATE'S CRITERIA (what he's looking for):\n{bodies.get('Criteria', '').strip()}\n\n"
        f"CANDIDATE'S RESUME:\n{bodies.get('Resume', '').strip()}\n\n"
        f"CANDIDATE'S CAREER BIO:\n{bodies.get('Career Bio', '').strip()}\n\n"
        f"CANDIDATE'S WORK HISTORY DETAIL:\n{bodies.get('Work History Detail', '').strip()}\n\n"
        f"CANDIDATE'S WRITING TONE (match this voice):\n{bodies.get('Tone Samples', '').strip()}\n\n"
        "Always call the draft_application tool exactly once. Quality bar: "
        "(a) the cover letter must reference at least two concrete experiences "
        "from his resume / bio that map to the JD; (b) the why-this-company "
        "paragraph must cite something specific from the JD, not boilerplate."
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _user_message(job: dict) -> str:
    jd = (job.get("jd_text") or "").strip()
    if not jd:
        jd = "(no JD text available — draft based on title + company alone)"
    fit_reason = (job.get("fit_reason") or "").strip()
    fit_block = f"\n\nLLM SCREEN SAID:\n{fit_reason}\n" if fit_reason else ""
    return (
        f"Draft for: {job['company']} — {job['title']} "
        f"({job.get('location') or 'location unknown'})"
        f"{fit_block}\n\n"
        f"JOB DESCRIPTION:\n{jd}"
    )


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.Anthropic(api_key=key)


def _draft_one(
    client: anthropic.Anthropic, job: dict, system: list[dict]
) -> tuple[dict, dict] | None:
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": _user_message(job)}],
            tools=[CONTENT_TOOL],
            tool_choice={"type": "tool", "name": "draft_application"},
        )
    except anthropic.BadRequestError as e:
        print(f"  [job {job['id']}] bad request: {e.message}")
        return None
    except anthropic.RateLimitError:
        print(f"  [job {job['id']}] rate limited — will retry on next run")
        return None
    except anthropic.APIStatusError as e:
        print(f"  [job {job['id']}] API error {e.status_code}: {e.message}")
        return None

    if resp.stop_reason == "refusal":
        print(f"  [job {job['id']}] LLM refused — skipping")
        return None

    tool_block = next(
        (b for b in resp.content if b.type == "tool_use" and b.name == "draft_application"),
        None,
    )
    if tool_block is None:
        print(f"  [job {job['id']}] no draft_application in response (stop_reason={resp.stop_reason})")
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


def generate(batch_id: int) -> dict:
    """Generate content for every job in the given batch. Returns stats."""
    client = _client()
    system = _system_prompt()

    stats = {"checked": 0, "drafted": 0, "errors": 0, "total_cost_usd": 0.0}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT j.id, j.company, j.title, j.location, j.url, j.jd_text, j.fit_reason "
            "FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id "
            "WHERE bj.batch_id = ? AND j.stage IN ('queued', 'generating') "
            "ORDER BY j.fit_score DESC",
            (batch_id,),
        ).fetchall()
        stats["checked"] = len(rows)
        if not rows:
            return stats

        conn.execute(
            "UPDATE batches SET status = 'generating' WHERE id = ?",
            (batch_id,),
        )
        conn.commit()

        for i, row in enumerate(rows, 1):
            job = dict(row)
            conn.execute("UPDATE jobs SET stage = 'generating' WHERE id = ?", (job["id"],))

            result = _draft_one(client, job, system)
            if result is None:
                stats["errors"] += 1
                continue
            output, usage = result
            cost = _cost_usd(usage)
            stats["total_cost_usd"] += cost
            stats["drafted"] += 1

            conn.execute(
                "INSERT INTO applications (job_id, cover_letter, why_company) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "  cover_letter = excluded.cover_letter, "
                "  why_company  = excluded.why_company",
                (job["id"], output["cover_letter"], output["why_this_company"]),
            )
            conn.execute("UPDATE jobs SET stage = 'ready' WHERE id = ?", (job["id"],))
            conn.execute(
                "INSERT INTO api_costs (provider, operation, job_id, input_tokens, "
                "output_tokens, cached_input_tokens, cost_usd) VALUES (?,?,?,?,?,?,?)",
                ("anthropic", "content_gen", job["id"],
                 usage["input_tokens"], usage["output_tokens"],
                 usage["cache_read_input_tokens"], cost),
            )
            conn.commit()
            print(f"  [{i}/{len(rows)}] drafted {job['company']} — {job['title'][:50]} "
                  f"(cost so far ${stats['total_cost_usd']:.4f})")

        conn.execute("UPDATE batches SET status = 'ready' WHERE id = ?", (batch_id,))
        conn.commit()
    return stats
