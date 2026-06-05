"""Cheap Haiku-based extraction of structured fields from a raw JD blob.

Used by the /jobs/paste page's auto-parse button — given the JD text, returns
{company, title, location} so the user doesn't have to type them in by hand.
Cost is ~$0.001 per call (Haiku 4.5, ~1k input tokens, <100 output tokens).
"""
from __future__ import annotations

import os

import anthropic

MODEL = "claude-haiku-4-5"

_TOOL = {
    "name": "extract_job_fields",
    "description": "Extract structured fields from a job description.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {
                "type": "string",
                "description": (
                    "The hiring company's name as stated in the JD. Just the "
                    "name (no 'Inc'/'LLC' suffix). Empty string if the JD "
                    "doesn't make the company name clear."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "The job title as written in the JD. Empty string if not "
                    "stated."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "Location as stated — city + state/province + country, or "
                    "'Remote (Canada)' / 'Hybrid - Toronto', etc. Empty "
                    "string if not stated."
                ),
            },
            "work_arrangement": {
                "type": "string",
                "description": (
                    "Work arrangement, one of: 'Remote', 'Hybrid', 'On-site', "
                    "or empty string if not clearly stated. Pick the one the "
                    "JD most explicitly indicates."
                ),
            },
            "salary_min": {
                "type": "integer",
                "description": (
                    "Lower bound of the annual salary range as an integer in "
                    "the JD's stated currency (e.g., 100000 for $100K). 0 if "
                    "no salary disclosed."
                ),
            },
            "salary_max": {
                "type": "integer",
                "description": (
                    "Upper bound of the annual salary range as an integer in "
                    "the JD's stated currency. 0 if no salary disclosed."
                ),
            },
            "salary_currency": {
                "type": "string",
                "description": (
                    "ISO currency code for the salary range (e.g., 'USD', "
                    "'CAD', 'EUR', 'GBP'). Empty string if no salary."
                ),
            },
            "posted_date": {
                "type": "string",
                "description": (
                    "Date the job was posted, formatted strictly as "
                    "'YYYY-MM-DD'. Look for explicit lines like 'Posted on "
                    "March 5, 2026' or 'Date Posted: 2026-03-05'. Empty "
                    "string if not stated — do NOT guess."
                ),
            },
            "job_type": {
                "type": "string",
                "description": (
                    "Employment type. Pick one of: 'Full-time', 'Part-time', "
                    "'Contract', 'Internship', 'Temporary'. Empty string if "
                    "not clearly stated."
                ),
            },
            "reference_id": {
                "type": "string",
                "description": (
                    "Posting reference / requisition ID as written in the JD "
                    "(examples: 'JR-0102005', 'R-1234', '2026-FE-007'). NOT "
                    "the URL slug. Empty string if not stated."
                ),
            },
            "job_board": {
                "type": "string",
                "description": (
                    "Where the JD says it's posted. Examples: 'LinkedIn', "
                    "'Indeed', 'company careers page', 'Greenhouse', "
                    "'Workday', 'Lever'. Empty string if the JD doesn't say."
                ),
            },
        },
        "required": ["company", "title", "location",
                     "work_arrangement", "salary_min", "salary_max",
                     "salary_currency",
                     "posted_date", "job_type", "reference_id", "job_board"],
        "additionalProperties": False,
    },
}


def parse(jd_text: str) -> dict:
    """Returns {company, title, location}. Any missing field comes back as ''."""
    if not jd_text or not jd_text.strip():
        return {"company": "", "title": "", "location": ""}

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=(
            "You extract structured fields from raw job descriptions. Return "
            "ONLY what the JD actually states — never invent. If a field is "
            "not present, return an empty string for it."
        ),
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "extract_job_fields"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Extract company name, job title, and location from this "
                    "job description:\n\n" + jd_text[:12000]
                ),
            }
        ],
    )
    def _clean(s):
        """Strip Haiku's occasional tool-call XML leakage in string fields."""
        s = str(s or "").strip()
        if not s:
            return ""
        # Discard the value entirely if it contains tool-call XML markers.
        # Haiku 4.5 sometimes produces strings like:
        #   "</antml-parameter>\n<parameter name=\"salary_min\">128400"
        bad_markers = ("</antml", "<parameter ", "<antml-parameter")
        if any(m in s for m in bad_markers):
            return ""
        return s

    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            data = block.input or {}
            smin = data.get("salary_min") or 0
            smax = data.get("salary_max") or 0
            return {
                "company":          _clean(data.get("company")),
                "title":            _clean(data.get("title")),
                "location":         _clean(data.get("location")),
                "work_arrangement": _clean(data.get("work_arrangement")),
                "salary_min":       int(smin) if isinstance(smin, (int, float)) and smin > 0 else 0,
                "salary_max":       int(smax) if isinstance(smax, (int, float)) and smax > 0 else 0,
                "salary_currency":  _clean(data.get("salary_currency")).upper(),
                "posted_date":      _clean(data.get("posted_date")),
                "job_type":         _clean(data.get("job_type")),
                "reference_id":     _clean(data.get("reference_id")),
                "job_board":        _clean(data.get("job_board")),
            }
    return {
        "company": "", "title": "", "location": "", "work_arrangement": "",
        "salary_min": 0, "salary_max": 0, "salary_currency": "",
        "posted_date": "", "job_type": "", "reference_id": "", "job_board": "",
    }
