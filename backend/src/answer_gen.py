"""Generate a standard practice answer + keyword triggers, grounded in the
candidate's profile (resume, career bio, work history, tone samples).

The answer is meant to be rehearsed and reused across companies. Keywords are
short triggers for on-the-go / flash-card practice — not the full sentences.
"""
from __future__ import annotations

import os

import anthropic

from . import profile_store

ANSWER_MODEL = "claude-sonnet-4-6"
KEYWORD_MODEL = "claude-haiku-4-5"

KEYWORD_TOOL = {
    "name": "emit_keywords",
    "description": "Emit the short keyword triggers a speaker would glance at to recall this answer.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "6-12 short triggers (1-4 words each), in the order the answer flows. "
                               "Each is a memory hook, not a full sentence.",
            },
        },
        "required": ["keywords"],
        "additionalProperties": False,
    },
}


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.Anthropic(api_key=key)


def _answer_system() -> str:
    bodies = profile_store.read_all()
    # Life-Pattern Awareness is private — deliberately excluded from outward content.
    return (
        "You write a first-person interview answer for the candidate to rehearse and reuse. "
        "He is a senior frontend / full-stack engineer with 10+ years of experience. "
        "Write in HIS voice using only the facts below — do not invent employers, "
        "titles, metrics, or projects.\n\n"
        "LEVEL HONESTY: he is a senior individual contributor, not a principal, staff, "
        "architect, or manager. Do not inflate the title or use manager-speak like "
        "'directing', 'owned the org', or 'led the team' unless the facts say so. "
        "Confident and concrete, never bluffing a level above his.\n\n"
        "TONE (match his tone samples): straightforward conversational voice, short "
        "sentences, no jargon, no em dashes (use '...' where a dash-pause is wanted), no "
        "excessive emoji. Frame motivation around taking on harder problems and going deeper "
        "in his craft from a position of strength, never around boredom, energy constraints, "
        "or catching up.\n\n"
        "LENGTH: a spoken answer of about 60-90 seconds (roughly 150-200 words). "
        "Tight and natural, the way someone actually talks. No headings, no bullet "
        "points, no preamble. Output only the answer text.\n\n"
        f"CANDIDATE CRITERIA / TARGET ROLES:\n{bodies.get('Criteria', '').strip()}\n\n"
        f"CANDIDATE RESUME:\n{bodies.get('Resume', '').strip()}\n\n"
        f"CANDIDATE CAREER BIO:\n{bodies.get('Career Bio', '').strip()}\n\n"
        f"CANDIDATE WORK HISTORY DETAIL:\n{bodies.get('Work History Detail', '').strip()}\n\n"
        f"CANDIDATE TONE SAMPLES (mirror this voice):\n{bodies.get('Tone Samples', '').strip()}"
    )


def generate_answer(question: str) -> str:
    """Draft a rehearsable answer to `question`, grounded in the profile."""
    client = _client()
    resp = client.messages.create(
        model=ANSWER_MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": _answer_system(),
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content":
                   f"Interview question:\n{question}\n\n"
                   "Write the answer he should rehearse. Output only the answer."}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def generate_keywords(answer: str) -> list[str]:
    """Extract short keyword triggers from a standard answer."""
    client = _client()
    resp = client.messages.create(
        model=KEYWORD_MODEL,
        max_tokens=512,
        system=[{"type": "text", "text":
                 "You distill a rehearsed answer into the short keyword triggers a speaker "
                 "would glance at to recall it — in flow order. Each trigger is 1-4 words, a "
                 "memory hook, not a full sentence. Call emit_keywords exactly once."}],
        messages=[{"role": "user", "content": f"Answer:\n{answer}"}],
        tools=[KEYWORD_TOOL],
        tool_choice={"type": "tool", "name": "emit_keywords"},
    )
    block = next((b for b in resp.content
                  if getattr(b, "type", None) == "tool_use" and b.name == "emit_keywords"), None)
    if block is None:
        return []
    kws = block.input.get("keywords") or []
    return [k.strip() for k in kws if isinstance(k, str) and k.strip()]
