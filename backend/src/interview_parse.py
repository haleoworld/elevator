"""Haiku-based extraction of interview metadata from a free-form description.

The user describes their interview ("this was with Veeva yesterday for the
hiring-manager round") either by typing or by recording a short clip; we
extract {company, interview_date, round}. Relative dates ("yesterday",
"last Tuesday") are resolved against today's date, which is passed in.
"""
from __future__ import annotations

import datetime as _dt
import os

import anthropic

MODEL = "claude-haiku-4-5"

_TOOL = {
    "name": "extract_interview_meta",
    "description": "Extract structured interview metadata from a description.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {
                "type": "string",
                "description": (
                    "The company the interview was with. Empty string if not "
                    "mentioned."
                ),
            },
            "interview_date": {
                "type": "string",
                "description": (
                    "The date the interview happened, formatted strictly as "
                    "'YYYY-MM-DD'. Resolve relative references ('yesterday', "
                    "'last Monday', 'this morning') against TODAY'S DATE given "
                    "in the user message. Empty string if no date is "
                    "mentioned or inferable."
                ),
            },
            "round": {
                "type": "string",
                "description": (
                    "The interview round/stage. Normalize to a short label "
                    "like 'recruiter screen', 'phone screen', "
                    "'hiring-manager', 'technical', 'system design', 'panel', "
                    "'onsite', 'final'. Empty string if not mentioned."
                ),
            },
        },
        "required": ["company", "interview_date", "round"],
        "additionalProperties": False,
    },
}


def _clean(s) -> str:
    s = str(s or "").strip()
    if any(m in s for m in ("</antml", "<parameter ", "<antml-parameter")):
        return ""
    return s


def parse(description: str, *, today: str | None = None) -> dict:
    """Returns {company, interview_date, round}. Empty strings for missing."""
    out = {"company": "", "interview_date": "", "round": ""}
    if not description or not description.strip():
        return out

    today = today or _dt.date.today().isoformat()
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=(
            "You extract interview metadata from a candidate's free-form "
            "description. Return ONLY what is stated or clearly inferable. "
            "Never invent a company or date."
        ),
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "extract_interview_meta"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"TODAY'S DATE is {today}.\n\n"
                    f"Interview description:\n{description[:4000]}"
                ),
            }
        ],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            data = block.input or {}
            return {
                "company":        _clean(data.get("company")),
                "interview_date": _clean(data.get("interview_date")),
                "round":          _clean(data.get("round")),
            }
    return out
