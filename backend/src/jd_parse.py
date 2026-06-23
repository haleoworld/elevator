"""Cheap Haiku-based extraction of structured fields from a raw JD blob.

Used by the /jobs/paste page's auto-parse button — given the JD text, returns
{company, title, location} so the user doesn't have to type them in by hand.
Cost is ~$0.001 per call (Haiku 4.5, ~1k input tokens, <100 output tokens).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import anthropic

MODEL = "claude-haiku-4-5"

# LinkedIn/Indeed/etc. show relative post dates ("5 hours ago", "1 day ago",
# "2 weeks ago"). The LLM can't resolve those (no notion of "today"), so we
# convert them deterministically here.
_REL_DATE_RE = re.compile(
    r"\b(?:(just\s+now|today|yesterday)"
    r"|(?:posted\s+)?(\d+)\s+(minute|hour|day|week|month)s?\s+ago)\b",
    re.IGNORECASE,
)


def _relative_date(text: str) -> str | None:
    """Resolve a relative post-date phrase in `text` to 'YYYY-MM-DD', or None."""
    if not text:
        return None
    m = _REL_DATE_RE.search(text)
    if not m:
        return None
    now = datetime.now()
    word, num, unit = m.group(1), m.group(2), m.group(3)
    if word:
        d = now - timedelta(days=1) if word.lower() == "yesterday" else now
    else:
        n = int(num)
        unit = unit.lower()
        if unit in ("minute", "hour"):
            d = now                       # same day
        elif unit == "day":
            d = now - timedelta(days=n)
        elif unit == "week":
            d = now - timedelta(weeks=n)
        else:                             # month
            d = now - timedelta(days=30 * n)
    return d.strftime("%Y-%m-%d")

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


def _clean_field(s) -> str:
    """Strip Haiku's occasional tool-call XML leakage in string fields."""
    s = str(s or "").strip()
    if not s:
        return ""
    bad_markers = ("</antml", "<parameter ", "<antml-parameter")
    if any(m in s for m in bad_markers):
        return ""
    return s


# Page tool = the field extractor PLUS a cleaned description body, so a single
# Haiku call turns a noisy scraped page into JD text + structured fields.
import copy as _copy  # noqa: E402
_PAGE_TOOL = _copy.deepcopy(_TOOL)
_PAGE_TOOL["name"] = "extract_job_posting"
_PAGE_TOOL["description"] = (
    "Extract the job description body and structured fields from the raw text of "
    "a job-posting web page."
)
_PAGE_TOOL["input_schema"]["properties"]["description"] = {
    "type": "string",
    "description": (
        "The actual job posting body, cleaned of site navigation, menus, headers, "
        "footers, cookie banners, 'similar jobs', login prompts, and ads. Keep the "
        "real posting only: role summary, responsibilities, requirements, "
        "qualifications, and benefits. Plain text; preserve paragraph and bullet "
        "structure with line breaks. Empty string if the page has no real job "
        "description."
    ),
}
_PAGE_TOOL["input_schema"]["required"] = _TOOL["input_schema"]["required"] + ["description"]


def clean_page(page_text: str, url: str = "", page_title: str = "") -> dict:
    """Turn raw scraped page text (innerText) into a cleaned JD body + fields.

    Returns the same dict shape as parse() plus a 'jd_text' key holding the
    cleaned description. All missing fields come back as '' (jd_text as '')."""
    empty = {
        "company": "", "title": "", "location": "", "work_arrangement": "",
        "salary_min": 0, "salary_max": 0, "salary_currency": "",
        "posted_date": "", "job_type": "", "reference_id": "", "job_board": "",
        "jd_text": "",
    }
    if not page_text or not page_text.strip():
        return empty

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=(
            "You are given raw text scraped from a job-posting web page. It "
            "includes navigation, menus, ads, and boilerplate around the real "
            "posting. Isolate the actual job posting: return its description body "
            "plus structured fields. Return ONLY what the page states; never "
            "invent. Use an empty string for any field not present."
        ),
        tools=[_PAGE_TOOL],
        tool_choice={"type": "tool", "name": "extract_job_posting"},
        messages=[{
            "role": "user",
            "content": (
                f"URL: {url or '(none)'}\n"
                f"Page title: {page_title or '(none)'}\n\n"
                f"Raw page text:\n\n{page_text[:24000]}"
            ),
        }],
    )

    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            data = block.input or {}
            smin = data.get("salary_min") or 0
            smax = data.get("salary_max") or 0
            posted = _clean_field(data.get("posted_date"))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", posted):
                posted = _relative_date(page_text) or ""
            return {
                "company":          _clean_field(data.get("company")),
                "title":            _clean_field(data.get("title")),
                "location":         _clean_field(data.get("location")),
                "work_arrangement": _clean_field(data.get("work_arrangement")),
                "salary_min":       int(smin) if isinstance(smin, (int, float)) and smin > 0 else 0,
                "salary_max":       int(smax) if isinstance(smax, (int, float)) and smax > 0 else 0,
                "salary_currency":  _clean_field(data.get("salary_currency")).upper(),
                "posted_date":      posted,
                "job_type":         _clean_field(data.get("job_type")),
                "reference_id":     _clean_field(data.get("reference_id")),
                "job_board":        _clean_field(data.get("job_board")),
                "jd_text":          _clean_field(data.get("description")),
            }
    return empty


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
            # Prefer an explicit YYYY-MM-DD from the LLM; otherwise resolve a
            # relative phrase ("5 hours ago", "1 day ago") from the raw text.
            posted = _clean(data.get("posted_date"))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", posted):
                posted = _relative_date(jd_text) or ""
            return {
                "company":          _clean(data.get("company")),
                "title":            _clean(data.get("title")),
                "location":         _clean(data.get("location")),
                "work_arrangement": _clean(data.get("work_arrangement")),
                "salary_min":       int(smin) if isinstance(smin, (int, float)) and smin > 0 else 0,
                "salary_max":       int(smax) if isinstance(smax, (int, float)) and smax > 0 else 0,
                "salary_currency":  _clean(data.get("salary_currency")).upper(),
                "posted_date":      posted,
                "job_type":         _clean(data.get("job_type")),
                "reference_id":     _clean(data.get("reference_id")),
                "job_board":        _clean(data.get("job_board")),
            }
    return {
        "company": "", "title": "", "location": "", "work_arrangement": "",
        "salary_min": 0, "salary_max": 0, "salary_currency": "",
        "posted_date": "", "job_type": "", "reference_id": "", "job_board": "",
    }
