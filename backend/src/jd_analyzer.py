"""Static keyword analysis of a JD's plain text. No LLM, no network.

Used by the job detail page to surface tech-stack pills, FE/BE breakdown,
frontend share %, and a best-effort requirements list — purely from the
already-stored jd_text. Cheap to recompute on every page load.
"""
from __future__ import annotations

import re

# Each key is the canonical label shown in the UI; values are substrings
# searched (case-insensitive) in the JD text. Keep terms specific enough not
# to false-match (e.g. "go" is omitted because it's a noisy English word —
# "golang" / "go programming" are the safer signals).
_FE_KEYWORDS: dict[str, list[str]] = {
    "frontend":            ["frontend", "front-end", "front end"],
    "react":               ["react"],
    "angular":             ["angular"],
    "vue":                 ["vue.js", "vuejs", "vue "],
    "svelte":              ["svelte"],
    "next.js":             ["next.js", "nextjs"],
    "typescript":          ["typescript"],
    "javascript":          ["javascript"],
    "html":                [" html", "html5"],
    "css":                 [" css", "css3"],
    "tailwind":            ["tailwind"],
    "redux":               ["redux"],
    "ngrx":                ["ngrx"],
    "rxjs":                ["rxjs"],
    "webpack":             ["webpack"],
    "vite":                ["vite"],
    "jest":                ["jest"],
    "cypress":             ["cypress"],
    "playwright":          ["playwright"],
    "webdriverio":         ["webdriverio", "webdriver.io"],
    "testing library":     ["testing library"],
    "vitest":              ["vitest"],
    "storybook":           ["storybook"],
    "accessibility":       ["accessibility", "a11y", "aria", "wcag", "aoda"],
    "microfrontends":      ["micro-frontends", "microfrontends", "micro frontends"],
    "module federation":   ["module federation"],
    "ui":                  ["ui engineer", "user interface"],
    "design system":       ["design system"],
    "web performance":     ["web performance", "core web vitals"],
}

_BE_KEYWORDS: dict[str, list[str]] = {
    "node.js":              ["node.js", "nodejs"],
    "python":               ["python"],
    "java":                 [" java ", "java,", "java."],
    "golang":               ["golang", " go programming"],
    "rust":                 [" rust"],
    "ruby":                 [" ruby"],
    "scala":                ["scala"],
    "django":               ["django"],
    "flask":                [" flask"],
    "rails":                [" rails"],
    "spring":               ["spring boot", "spring framework"],
    "express":              ["express.js", "expressjs"],
    "fastapi":              ["fastapi"],
    "nestjs":               ["nest.js", "nestjs"],
    "sql":                  [" sql", "postgresql", "postgres", "mysql"],
    "mongodb":              ["mongodb"],
    "redis":                ["redis"],
    "elasticsearch":        ["elasticsearch", "opensearch"],
    "dynamodb":             ["dynamodb"],
    "cassandra":            ["cassandra"],
    "kubernetes":           ["kubernetes", "k8s"],
    "docker":               ["docker"],
    "terraform":            ["terraform"],
    "aws":                  ["aws", "amazon web services"],
    "gcp":                  ["gcp", "google cloud"],
    "azure":                ["azure"],
    "kafka":                ["kafka"],
    "microservices":        ["microservices"],
    "grpc":                 ["grpc"],
    "graphql":              ["graphql"],
    "backend":              ["backend", "back-end", "back end"],
    "infrastructure":       ["infrastructure"],
    "devops":               ["devops"],
    "ci/cd":                ["ci/cd", "ci-cd", "continuous integration"],
    "monolith":             ["monolith"],
    "distributed systems":  ["distributed system"],
}


def _count_matches(low: str, terms: list[str]) -> int:
    return sum(low.count(t.lower()) for t in terms)


def _scan(jd_low: str, vocab: dict[str, list[str]]) -> dict[str, int]:
    """Return {label: count} for every label whose terms appear at least once."""
    found: dict[str, int] = {}
    for label, terms in vocab.items():
        n = _count_matches(jd_low, terms)
        if n:
            found[label] = n
    return found


# Common JD "requirements" section headers. Match on its own line.
_REQ_HEADERS = re.compile(
    r"(^|\n)\s*(?:#+\s*)?"
    r"(requirements?|qualifications?|what you('ll| will)? need|"
    r"what we'?re looking for|you'?ll bring|must[- ]haves?|"
    r"you have|we want|skills (required|we want)|"
    r"required (skills|qualifications)|about you|your (background|experience))"
    r"\s*:?\s*\n",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*[-•*●▪◦▫•‣◦]\s+(.+)$", re.MULTILINE)


def _extract_requirements(text: str) -> list[str]:
    if not text:
        return []
    m = _REQ_HEADERS.search(text)
    if not m:
        return []
    rest = text[m.end():]
    # Cut at the next major heading-like line (so we don't pull "Benefits" bullets).
    cut = re.search(r"\n\s*\n[A-Z][A-Za-z ]{2,50}\s*:?\s*\n", rest)
    if cut:
        rest = rest[: cut.start()]
    bullets = _BULLET.findall(rest)
    cleaned: list[str] = []
    seen: set[str] = set()
    for b in bullets:
        s = " ".join(b.split())  # collapse whitespace
        if 10 <= len(s) <= 320 and s.lower() not in seen:
            cleaned.append(s)
            seen.add(s.lower())
    return cleaned[:15]


_JOB_TYPE_RE = re.compile(
    r"\b(full[- ]?time|part[- ]?time|contract|contractor|"
    r"freelance|temporary|internship)\b",
    re.IGNORECASE,
)


def _infer_job_type(text: str) -> str | None:
    m = _JOB_TYPE_RE.search(text or "")
    return m.group(1).lower().replace(" ", "-") if m else None


_HTML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def _esc(s: str) -> str:
    return "".join(_HTML_ESCAPE.get(c, c) for c in s)


_BULLET_LINE = re.compile(r"^\s*[-•*●▪◦▫‣○⊙·]\s+(.+)$")
_NUMBERED_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
# Short, all-caps heading-like line: "REQUIREMENTS", "WHAT YOU'LL DO"
_CAPS_HEADING = re.compile(r"^[A-Z][A-Z0-9\s\-:&/(),'.]+$")
# "Title-Cased Heading:" — short, ends with a colon, mostly title-cased
_COLON_HEADING = re.compile(r"^[A-Z][\w\s,&\-/'.]{2,80}:$")


# Common JD section markers — used to inject paragraph breaks when the source
# arrived as a single un-broken blob (common from Greenhouse / Workday after
# HTML-stripping). Order matters slightly — longer phrases first.
_SECTION_MARKERS = [
    "About Us", "About us", "About the team", "About the role", "About the position",
    "About the Company", "About the company",
    "The Role", "The role",
    "What you'll do", "What you will do", "What you’ll do", "What You'll Do",
    "What you'll bring", "What You'll Bring", "What you bring", "What You Bring",
    "What you need", "What we're looking for", "What we are looking for",
    "Responsibilities", "Key Responsibilities", "Your responsibilities",
    "Requirements", "Required Qualifications", "Minimum Qualifications",
    "Basic Qualifications", "Qualifications", "Must Have", "Must-have", "Must have",
    "Nice to have", "Nice to Have", "Nice-to-have", "Preferred Qualifications",
    "Bonus Points", "Plus Points",
    "Why join us", "Why Join Us", "Why work here",
    "What we offer", "What We Offer", "Benefits", "Perks", "Compensation",
    "Salary Range", "Pay Range", "Pay Transparency",
    "How we work", "Our Values", "Our Culture",
    "Equal Opportunity", "Equal Employment Opportunity", "EEO",
    "Diversity and Inclusion", "Diversity, Equity",
    "Cool things you'll do", "Skills",
]
_SECTION_MARKER_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(m) for m in _SECTION_MARKERS) + r")(?=[\s:.])"
)

_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
    "&ndash;": "–", "&mdash;": "—", "&hellip;": "…",
    "&rsquo;": "’", "&lsquo;": "‘", "&rdquo;": "”", "&ldquo;": "“",
    "&bull;": "•", "&middot;": "·",
}


def _decode_entities(s: str) -> str:
    for ent, repl in _HTML_ENTITIES.items():
        s = s.replace(ent, repl)
    return s


def _preprocess(text: str) -> str:
    """Decode entities + inject paragraph breaks at section markers.
    When the source arrived as one blob, isolate marker phrases onto their own
    line with a trailing colon so the line-pass treats them as headings."""
    t = _decode_entities(text)
    if t.count("\n") < 5:
        t = _SECTION_MARKER_RE.sub(lambda m: f"\n\n{m.group(1)}:\n\n", t)
        t = re.sub(r":+", ":", t)         # collapse "About Us::" → "About Us:"
        t = re.sub(r"\n{3,}", "\n\n", t)  # collapse extra blank lines
    return t


def format_for_display(text: str | None) -> str:
    """Convert raw JD plain-text into mildly-structured HTML.
    Heuristics: bullet rows → <ul>, numbered rows → <ol>, ALL-CAPS short lines
    or 'Title:' lines → <h4>, blank line gaps → paragraph break."""
    if not text:
        return ""
    text = _preprocess(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    list_kind: str | None = None  # 'ul' | 'ol' | None

    def close_list():
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    for raw in lines:
        s = raw.strip()
        if not s:
            close_list()
            continue

        b = _BULLET_LINE.match(s)
        if b:
            if list_kind != "ul":
                close_list()
                out.append('<ul class="jd-list">')
                list_kind = "ul"
            out.append(f"<li>{_esc(b.group(1).strip())}</li>")
            continue

        n = _NUMBERED_LINE.match(s)
        if n:
            if list_kind != "ol":
                close_list()
                out.append('<ol class="jd-list">')
                list_kind = "ol"
            out.append(f"<li>{_esc(n.group(2).strip())}</li>")
            continue

        close_list()

        if 4 <= len(s) <= 90 and _CAPS_HEADING.match(s) and any(c.isalpha() for c in s):
            out.append(f'<h4 class="jd-heading">{_esc(s)}</h4>')
            continue
        if _COLON_HEADING.match(s) and len(s) <= 90:
            out.append(f'<h4 class="jd-heading">{_esc(s)}</h4>')
            continue

        out.append(f"<p>{_esc(s)}</p>")

    close_list()
    return "\n".join(out)


def analyze(jd_text: str | None) -> dict:
    """Returns:
      tech_stack:       sorted list of all distinct labels found (FE + BE)
      fe_counts:        {label: count} for FE-flavored terms found
      be_counts:        {label: count} for BE-flavored terms found
      fe_total:         sum of fe_counts values
      be_total:         sum of be_counts values
      frontend_split:   round(fe_total / (fe_total + be_total) * 100) or None
      requirements:     up to 15 bullet strings from a "Requirements" section
      job_type:         inferred job type keyword (or None)
    """
    if not jd_text:
        return {
            "tech_stack": [], "fe_counts": {}, "be_counts": {},
            "fe_total": 0, "be_total": 0, "frontend_split": None,
            "requirements": [], "job_type": None,
        }
    low = jd_text.lower()
    fe = _scan(low, _FE_KEYWORDS)
    be = _scan(low, _BE_KEYWORDS)
    fe_total = sum(fe.values())
    be_total = sum(be.values())
    split = round(fe_total / (fe_total + be_total) * 100) if (fe_total + be_total) else None
    tech_stack = sorted(set(fe.keys()) | set(be.keys()))
    return {
        "tech_stack": tech_stack,
        "fe_counts": fe,
        "be_counts": be,
        "fe_total": fe_total,
        "be_total": be_total,
        "frontend_split": split,
        "requirements": _extract_requirements(jd_text),
        "job_type": _infer_job_type(jd_text),
    }
