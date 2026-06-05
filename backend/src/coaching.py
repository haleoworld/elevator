"""Coaching synthesis — Sonnet reads the transcript + metrics and writes a
structured report. Audio never goes to the LLM; only text + numbers.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from . import db, profile_store

MODEL = "claude-sonnet-4-6"

PRICE_INPUT_PER_M = 3.00
PRICE_OUTPUT_PER_M = 15.00
PRICE_CACHE_READ_PER_M = 0.30
PRICE_CACHE_WRITE_PER_M = 3.75

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "data" / "coaching-reports"

COACHING_TOOL = {
    "name": "write_coaching_report",
    "description": "Write a structured coaching report based on the candidate's interview or practice transcript.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "overall_summary": {
                "type": "string",
                "description": "1-2 sentences capturing the overall impression of this response.",
            },
            "what_worked": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "timestamp_s": {"type": "number", "description": "Approximate start time in seconds."},
                        "quote": {"type": "string", "description": "Short quote from the transcript."},
                        "why": {"type": "string", "description": "Why this moment landed well."},
                    },
                    "required": ["timestamp_s", "quote", "why"],
                    "additionalProperties": False,
                },
                "description": "2-3 concrete moments where the candidate was effective. Cite real quotes.",
            },
            "top_patterns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short name for the pattern (e.g. 'Hedging language')."},
                        "examples": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "timestamp_s": {"type": "number"},
                                    "quote": {"type": "string"},
                                },
                                "required": ["timestamp_s", "quote"],
                                "additionalProperties": False,
                            },
                            "description": "1-3 timestamped quotes showing the pattern.",
                        },
                        "fix": {"type": "string", "description": "Concrete exercise or rephrasing to practice."},
                    },
                    "required": ["name", "examples", "fix"],
                    "additionalProperties": False,
                },
                "description": "Up to 3 highest-impact patterns to fix. Concrete, not generic.",
            },
            "reasoning_style": {
                "type": "string",
                "description": "1-2 sentences on how the candidate structured his reasoning (e.g. 'STAR-format, strong on context but light on impact metrics').",
            },
            "next_practice": {
                "type": "string",
                "description": "The single highest-leverage thing for the candidate to practice next.",
            },
        },
        "required": ["overall_summary", "what_worked", "top_patterns", "reasoning_style", "next_practice"],
        "additionalProperties": False,
    },
}


def _system_prompt() -> list[dict]:
    bodies = profile_store.read_all()
    life_pattern = bodies.get("Life-Pattern Awareness", "").strip()
    text = (
        "You are an interview coach for the candidate, a senior frontend / full-stack software "
        "engineer with 10+ years of experience. You give specific, actionable feedback "
        "on his interview and practice transcripts. Your job is NOT to validate — it's "
        "to find the highest-leverage patterns he can actually fix.\n\n"
        f"CANDIDATE'S CRITERIA AND TARGET ROLES:\n{bodies.get('Criteria', '').strip()}\n\n"
        f"CANDIDATE'S CAREER BIO (so you know what good answers should reference):\n"
        f"{bodies.get('Career Bio', '').strip()}\n\n"
    )
    if life_pattern:
        text += (
            f"CANDIDATE'S LIFE-PATTERN AWARENESS (private — informs coaching only, never reference externally):\n"
            f"{life_pattern}\n\n"
        )
    text += (
        "Quality bar: every pattern you flag must include real timestamped quotes. "
        "Every 'what worked' moment must reference a specific thing he said. "
        "Skip generic advice like 'be more confident' or 'speak slower'. "
        "Call the write_coaching_report tool exactly once."
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _user_message(*, transcript_text: str, segments: list[dict], metrics: dict, context: dict) -> str:
    ctx_lines: list[str] = []
    if context.get("kind") == "interview":
        ctx_lines.append("Type: real interview")
        if context.get("company"):
            ctx_lines.append(f"Company: {context['company']}")
        if context.get("round"):
            ctx_lines.append(f"Round: {context['round']}")
    elif context.get("kind") == "practice":
        ctx_lines.append("Type: practice session")
        if context.get("question"):
            ctx_lines.append(f"Question: {context['question']}")
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(no context provided)"

    if segments:
        seg_lines = []
        for s in segments:
            ts = float(s.get("start") or 0)
            seg_lines.append(f"[{ts:6.1f}] {s.get('text', '').strip()}")
        seg_block = "\n".join(seg_lines)
    else:
        seg_block = "(no timestamps — pasted text input; quote segments by approximate position rather than timestamp_s, and pass 0 for timestamp_s in the tool call)"

    metric_lines = "\n".join(f"  {k}: {v}" for k, v in metrics.items())

    return (
        f"CONTEXT:\n{ctx_block}\n\n"
        f"MECHANICAL METRICS:\n{metric_lines}\n\n"
        f"TRANSCRIPT (timestamped segments):\n{seg_block}\n\n"
        f"FULL TRANSCRIPT TEXT:\n{transcript_text}"
    )


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.Anthropic(api_key=key)


def _cost_usd(usage: dict) -> float:
    return (
        usage["input_tokens"] * PRICE_INPUT_PER_M / 1_000_000
        + usage["output_tokens"] * PRICE_OUTPUT_PER_M / 1_000_000
        + usage["cache_read_input_tokens"] * PRICE_CACHE_READ_PER_M / 1_000_000
        + usage["cache_creation_input_tokens"] * PRICE_CACHE_WRITE_PER_M / 1_000_000
    )


def synthesize(
    *,
    transcript_text: str,
    segments: list[dict],
    metrics: dict,
    context: dict,
) -> tuple[dict[str, Any], float] | None:
    """Call Sonnet for a coaching report. Returns (report_dict, cost_usd)."""
    client = _client()
    system = _system_prompt()
    user_msg = _user_message(
        transcript_text=transcript_text, segments=segments,
        metrics=metrics, context=context,
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            tools=[COACHING_TOOL],
            tool_choice={"type": "tool", "name": "write_coaching_report"},
        )
    except anthropic.APIStatusError as e:
        print(f"  coaching API error {e.status_code}: {e.message}")
        return None

    if resp.stop_reason == "refusal":
        print("  coaching LLM refused")
        return None

    tool_block = next(
        (b for b in resp.content if b.type == "tool_use" and b.name == "write_coaching_report"),
        None,
    )
    if tool_block is None:
        print(f"  no write_coaching_report in response (stop_reason={resp.stop_reason})")
        return None

    usage = {
        "input_tokens": resp.usage.input_tokens or 0,
        "output_tokens": resp.usage.output_tokens or 0,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return dict(tool_block.input), _cost_usd(usage), usage


def _mmss(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return f"{seconds}s"
    return f"{s // 60}:{s % 60:02d}"


def render_markdown(report: dict, context: dict, metrics: dict) -> str:
    lines = ["# Coaching report", ""]
    if context.get("kind") == "interview":
        lines.append(f"_Real interview — {context.get('company', '?')}_  ")
    elif context.get("kind") == "practice":
        q = context.get("question") or ""
        lines.append(f"_Practice — {q[:80]}_  ")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_  ")
    lines.append("")
    lines.append("## Overall")
    lines.append(report.get("overall_summary", ""))
    lines.append("")
    # Mechanical metrics are surfaced in their own card on the page — not
    # duplicated here.
    lines.append("## What worked")
    for item in report.get("what_worked", []):
        lines.append(f"- **[{_mmss(item['timestamp_s'])}]** \"{item['quote']}\"  ")
        lines.append(f"  → {item['why']}")
    lines.append("")
    lines.append("## Top patterns to fix")
    for pat in report.get("top_patterns", []):
        lines.append(f"### {pat['name']}")
        for ex in pat.get("examples", []):
            lines.append(f"- **[{_mmss(ex['timestamp_s'])}]** \"{ex['quote']}\"")
        lines.append(f"**Fix:** {pat['fix']}")
        lines.append("")
    lines.append("## Reasoning style")
    lines.append(report.get("reasoning_style", ""))
    lines.append("")
    lines.append("## Practice this next")
    lines.append(report.get("next_practice", ""))
    return "\n".join(lines)


def save_report(*, audio_filename: str, markdown: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(audio_filename).stem
    path = REPORTS_DIR / f"{stem}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


_DIARIZE_SYSTEM = (
    "You are a transcription post-processor for interview audio. Given a raw, "
    "single-block transcript, you split it into speaker turns and label each.\n\n"
    "Rules — all hard requirements:\n"
    "1. Identify each speaker from context. Default schema: INTERVIEWER and "
    "CANDIDATE. If a panel is clearly present, use INTERVIEWER_1, "
    "INTERVIEWER_2, etc., but keep CANDIDATE singular.\n"
    "2. PRESERVE EVERY WORD. Do not drop or paraphrase anything — including "
    "filler words ('um', 'uh', 'yeah', 'like', 'you know'), false starts, "
    "stutters, repeated words, profanity, or noise transcriptions. Only add "
    "structure: split into turns, label each.\n"
    "3. Default judgment: short prompts/questions/clarifications → INTERVIEWER; "
    "substantive answers, stories, explanations → CANDIDATE.\n"
    "4. If a single turn contains a long stretch by one speaker, keep it as ONE "
    "turn — don't split mid-thought. Conversely, when speakers genuinely swap "
    "back-and-forth quickly, capture each separately.\n\n"
    "Output strictly valid JSON. No code fences. No commentary. Schema:\n"
    '{"turns": [{"speaker": "INTERVIEWER", "text": "..."}, '
    '{"speaker": "CANDIDATE", "text": "..."}]}'
)


def diarize_transcript(transcript_text: str, *, context_hint: str = "") -> tuple[list[dict], dict]:
    """Use Sonnet to split a raw interview transcript into speaker turns.
    Returns ([{speaker, text}, ...], usage_dict).
    Raises json.JSONDecodeError if the model returns invalid JSON."""
    if not transcript_text or not transcript_text.strip():
        return [], {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    user = (context_hint + "\n\nRaw transcript:\n\n" + transcript_text).strip()
    client = _client()
    # Stream because long inputs + large max_tokens can exceed the 10-min
    # non-streaming request cap, and the SDK refuses to send otherwise.
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=_DIARIZE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        resp = stream.get_final_message()
    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    # Lenient fence-stripping, in case Sonnet ignores the rule
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip().rsplit("```", 1)[0]
    data = json.loads(raw)
    turns = data.get("turns") or []
    # Coerce to expected shape; drop malformed entries silently
    cleaned: list[dict] = []
    for t in turns:
        sp = str(t.get("speaker") or "").strip()
        tx = str(t.get("text") or "").strip()
        if sp and tx:
            cleaned.append({"speaker": sp, "text": tx})
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }
    return cleaned, usage


QUESTION_MATCH_TOOL = {
    "name": "report_question_matches",
    "description": "Report which interviewer turns ask a question that matches one of the candidate's prepared practice questions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "turn_index": {
                            "type": "integer",
                            "description": "0-based index of the interviewer turn (from the numbered TURNS list) that asks the question.",
                        },
                        "practice_question_id": {
                            "type": "integer",
                            "description": "id of the matching practice-bank question. Use only ids from the PRACTICE QUESTIONS list.",
                        },
                        "intent": {
                            "type": "string",
                            "description": "1-2 sentences in plain language: what the interviewer is really probing for or trying to learn about the candidate with this question.",
                        },
                    },
                    "required": ["turn_index", "practice_question_id", "intent"],
                },
            }
        },
        "required": ["matches"],
    },
}

_QMATCH_SYSTEM = (
    "You analyze a job-interview transcript that has been split into speaker "
    "turns. You are also given the candidate's bank of PRACTICE QUESTIONS he prepared "
    "with. Find interviewer turns that ask a question semantically equivalent "
    "to one of the practice questions — the same underlying thing is being "
    "assessed, not merely shared keywords. For each strong match report the "
    "turn_index, the matching practice_question_id, and a short 'intent' "
    "explaining what the interviewer is really trying to learn about the "
    "candidate. Only report confident matches; if an interviewer question has "
    "no good match in the bank, omit it. Never invent practice_question_ids — "
    "use only ids from the provided list. Only the interviewer's questions "
    "count; ignore the candidate's turns."
)


def match_interview_questions(
    turns: list[dict], questions: list[dict]
) -> tuple[list[dict], dict]:
    """Match interviewer turns to the candidate's practice-question bank.
    Returns ([{turn_index, practice_question_id, intent}, ...], usage)."""
    empty_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    if not turns or not questions:
        return [], empty_usage
    bank = "\n".join(f"[{q['id']}] {q['question']}" for q in questions)
    lines = []
    for i, t in enumerate(turns):
        sp = str(t.get("speaker") or "").strip()
        tx = str(t.get("text") or "").strip()
        lines.append(f"{i}\t{sp}: {tx}")
    user = (
        "PRACTICE QUESTIONS (id in brackets):\n" + bank
        + "\n\nTURNS (index<TAB>speaker: text):\n" + "\n".join(lines)
    )
    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_QMATCH_SYSTEM,
        messages=[{"role": "user", "content": user}],
        tools=[QUESTION_MATCH_TOOL],
        tool_choice={"type": "tool", "name": "report_question_matches"},
    )
    block = next(
        (b for b in resp.content
         if b.type == "tool_use" and b.name == "report_question_matches"),
        None,
    )
    matches: list[dict] = []
    valid_ids = {q["id"] for q in questions}
    if block:
        for m in (block.input.get("matches") or []):
            ti = m.get("turn_index")
            qid = m.get("practice_question_id")
            if ti is None or qid not in valid_ids:
                continue
            if not (0 <= ti < len(turns)):
                continue
            matches.append({
                "turn_index": int(ti),
                "practice_question_id": int(qid),
                "intent": (m.get("intent") or "").strip(),
            })
    usage = {
        "input_tokens": resp.usage.input_tokens or 0,
        "output_tokens": resp.usage.output_tokens or 0,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }
    return matches, usage


COMMON_QUESTION_TOOL = {
    "name": "report_common_questions",
    "description": "Report interviewer turns that ask a recognizable, common, or important interview question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "turn_index": {
                            "type": "integer",
                            "description": "0-based index of the interviewer turn (from the numbered TURNS list) that asks the question.",
                        },
                        "canonical_question": {
                            "type": "string",
                            "description": "The standard, well-known phrasing of this common interview question (not the interviewer's exact words). E.g. 'Tell me about a time you led an initiative that impacted the whole team or company.'",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["behavioral", "technical", "system_design", "fe_specific", "personal"],
                            "description": "Best-fit category for the practice bank.",
                        },
                        "intent": {
                            "type": "string",
                            "description": "1-2 sentences: what the interviewer is really probing for with this question.",
                        },
                        "existing_question_id": {
                            "type": ["integer", "null"],
                            "description": "If this canonical question already matches one in the EXISTING PRACTICE BANK, its id; otherwise null.",
                        },
                    },
                    "required": ["turn_index", "canonical_question", "category", "intent"],
                },
            }
        },
        "required": ["matches"],
    },
}

_COMMON_Q_SYSTEM = (
    "You analyze a job-interview transcript split into speaker turns. Identify "
    "interviewer turns that ask a question which is a recognizable COMMON or "
    "IMPORTANT interview question — the kind covered in interview-prep guides "
    "(behavioral / leadership / 'tell me about a time', technical, system "
    "design, frontend-specific, and career/motivation questions). For each, "
    "return the turn_index, a canonical_question giving the standard well-known "
    "phrasing (not the interviewer's verbatim words), a best-fit category, and "
    "a short intent describing what is really being probed. You are also given "
    "the candidate's EXISTING PRACTICE BANK: if a question already matches one in the "
    "bank, set existing_question_id to that id; otherwise set it to null so a "
    "new practice question can be created. Ignore pure logistics, small talk, "
    "and follow-up clarifications that are not standalone interview questions. "
    "Only the interviewer's questions count."
)


def identify_common_questions(
    turns: list[dict], existing_questions: list[dict]
) -> tuple[list[dict], dict]:
    """Find interviewer turns asking recognizable common interview questions.
    Returns ([{turn_index, canonical_question, category, intent,
    existing_question_id}], usage)."""
    empty_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    if not turns:
        return [], empty_usage
    bank = "\n".join(f"[{q['id']}] {q['question']}" for q in existing_questions) or "(empty)"
    lines = []
    for i, t in enumerate(turns):
        sp = str(t.get("speaker") or "").strip()
        tx = str(t.get("text") or "").strip()
        lines.append(f"{i}\t{sp}: {tx}")
    user = (
        "EXISTING PRACTICE BANK (id in brackets):\n" + bank
        + "\n\nTURNS (index<TAB>speaker: text):\n" + "\n".join(lines)
    )
    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_COMMON_Q_SYSTEM,
        messages=[{"role": "user", "content": user}],
        tools=[COMMON_QUESTION_TOOL],
        tool_choice={"type": "tool", "name": "report_common_questions"},
    )
    block = next(
        (b for b in resp.content
         if b.type == "tool_use" and b.name == "report_common_questions"),
        None,
    )
    out: list[dict] = []
    valid_ids = {q["id"] for q in existing_questions}
    if block:
        for m in (block.input.get("matches") or []):
            ti = m.get("turn_index")
            if ti is None or not (0 <= ti < len(turns)):
                continue
            cq = (m.get("canonical_question") or "").strip()
            if not cq:
                continue
            eid = m.get("existing_question_id")
            eid = int(eid) if (eid in valid_ids) else None
            out.append({
                "turn_index": int(ti),
                "canonical_question": cq,
                "category": (m.get("category") or "behavioral").strip(),
                "intent": (m.get("intent") or "").strip(),
                "existing_question_id": eid,
            })
    usage = {
        "input_tokens": resp.usage.input_tokens or 0,
        "output_tokens": resp.usage.output_tokens or 0,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }
    return out, usage


def save_diarized(*, transcript_path: Path, turns: list[dict]) -> Path:
    """Write diarized turns next to the transcript JSON.
    For `transcripts/foo.json`, writes `transcripts/foo.diarized.json`."""
    out = transcript_path.parent / (transcript_path.stem + ".diarized.json")
    out.write_text(json.dumps({"turns": turns}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def log_cost(*, operation: str, interview_id: int | None, usage: dict, cost: float) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO api_costs (provider, operation, interview_id, "
            "input_tokens, output_tokens, cached_input_tokens, cost_usd) "
            "VALUES (?,?,?,?,?,?,?)",
            ("anthropic", operation, interview_id,
             usage["input_tokens"], usage["output_tokens"],
             usage["cache_read_input_tokens"], cost),
        )
        conn.commit()
