"""Read/write the 6-section profile.md.

Format: each section starts with `## <Name>` on its own line. Everything
between that header and the next `## ` (or EOF) is the section's body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.md"


@dataclass(frozen=True)
class Section:
    key: str           # url-safe slug
    name: str          # header text in profile.md
    label: str         # display label
    blurb: str         # one-line UI hint
    private: bool = False


SECTIONS: tuple[Section, ...] = (
    Section(
        key="criteria",
        name="Criteria",
        label="1. Criteria",
        blurb="What the system shows you. Roles, seniority, salary floor, industries, exclude-list.",
    ),
    Section(
        key="resume",
        name="Resume",
        label="2. Resume",
        blurb="Paste your raw resume text. Don't re-edit it here — use the version that's been getting interviews.",
    ),
    Section(
        key="career-bio",
        name="Career Bio",
        label="3. Career Bio",
        blurb="400-700 words, first person, your voice. Feeds every cover letter.",
    ),
    Section(
        key="work-history-detail",
        name="Work History Detail",
        label="4. Work History Detail",
        blurb="5-10 honest bullets per role, including things that didn't make the resume. Honesty pass required (see profile-inputs-status.md).",
    ),
    Section(
        key="tone-samples",
        name="Tone Samples",
        label="5. Tone Samples",
        blurb="2-3 pieces of your writing (200-500 words each) so the system learns your voice. Not AI-generated.",
    ),
    Section(
        key="life-pattern-awareness",
        name="Life-Pattern Awareness",
        label="6. Life-Pattern Awareness",
        blurb="Private — coaching only. Never appears in cover letters or outward content.",
        private=True,
    ),
)

_SECTION_BY_KEY: dict[str, Section] = {s.key: s for s in SECTIONS}
# Match ONLY the canonical section names so a `## ` heading inside a user-written
# body (e.g. `## 1. Early Life…` in the Life-Pattern paragraph) isn't mistaken for
# a section break. Previously this regex matched any `## ` line, which silently
# dropped content with internal `## ` headings on read-back.
_HEADER_RE = re.compile(
    r"^##\s+(" + "|".join(re.escape(s.name) for s in SECTIONS) + r")\s*$",
    re.MULTILINE,
)


def get_section(key: str) -> Section | None:
    return _SECTION_BY_KEY.get(key)


def _ensure_file() -> None:
    if PROFILE_PATH.exists():
        return
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        "\n\n".join(f"## {s.name}\n" for s in SECTIONS),
        encoding="utf-8",
    )


def read_all() -> dict[str, str]:
    """Return {section.name: body} for every section, body stripped of leading/trailing whitespace."""
    _ensure_file()
    raw = PROFILE_PATH.read_text(encoding="utf-8")
    out: dict[str, str] = {s.name: "" for s in SECTIONS}

    matches = list(_HEADER_RE.finditer(raw))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip("\n")
        if name in out:
            out[name] = body
    return out


def write_section(key: str, body: str) -> None:
    section = get_section(key)
    if section is None:
        raise KeyError(f"unknown section: {key}")
    bodies = read_all()
    bodies[section.name] = body.replace("\r\n", "\n").rstrip() + ("\n" if body.strip() else "")
    _write_all(bodies)


def _write_all(bodies: dict[str, str]) -> None:
    parts: list[str] = []
    for s in SECTIONS:
        body = bodies.get(s.name, "").strip("\n")
        parts.append(f"## {s.name}\n" + (body + "\n" if body else ""))
    PROFILE_PATH.write_text("\n".join(parts), encoding="utf-8")
