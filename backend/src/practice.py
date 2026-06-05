"""Practice question bank + session lifecycle.

Seeds practice_questions on first call. The flow is identical to interview
analysis — record audio, transcribe, generate coaching — but tagged with a
question_id so reports stay grouped.
"""
from __future__ import annotations

from . import db

SEED_QUESTIONS: list[tuple[str, str]] = [
    # behavioral
    ("behavioral", "Tell me about a complex frontend system you architected — context, decisions, outcome."),
    ("behavioral", "Describe a time you disagreed with a senior technical decision. How did you handle it?"),
    ("behavioral", "Walk me through a performance optimization you led. What made it hard?"),
    ("behavioral", "Tell me about a time you mentored a junior engineer. What changed?"),
    ("behavioral", "Describe a time you had to ship faster than you wanted to. How did you compromise?"),
    # technical
    ("technical", "How would you architect a real-time observability dashboard for thousands of users?"),
    ("technical", "Walk me through how you'd approach state management in a complex Angular or React app."),
    ("technical", "How would you optimize rendering for a table with 100,000+ rows of streaming data?"),
    ("technical", "Describe your testing strategy for a critical UI flow in a regulated industry."),
    ("technical", "Walk me through how you'd design a reusable design system used by 5+ teams."),
    # system_design
    ("system_design", "Design a multi-tenant admin dashboard for a B2B SaaS product."),
    ("system_design", "How would you architect frontend infrastructure for a micro-frontend setup?"),
    ("system_design", "Design a real-time collaborative editor like Figma multiplayer."),
    # fe_specific
    ("fe_specific", "What makes B2B SaaS frontend different from consumer products?"),
    ("fe_specific", "How do you handle UI for workflows with complex approval gates in regulated industries?"),
    ("fe_specific", "Walk me through how you instrument a frontend for production observability."),
    # personal / motivation
    ("personal", "Walk me through your career so far. What's the through-line?"),
    ("personal", "Why are you looking to leave your current role?"),
    ("personal", "What's your ideal engineering culture?"),
    ("personal", "Why this company specifically?"),
]


def seed_if_empty() -> int:
    """Seed the question bank on first run. Returns count inserted."""
    with db.connect() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM practice_questions").fetchone()[0]
        if existing:
            return 0
        for category, question in SEED_QUESTIONS:
            conn.execute(
                "INSERT INTO practice_questions (category, question, source) "
                "VALUES (?, ?, 'seed')",
                (category, question),
            )
        conn.commit()
        return len(SEED_QUESTIONS)


def list_questions() -> list[dict]:
    seed_if_empty()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT q.id, q.category, q.question, "
            "  (SELECT COUNT(*) FROM practice_sessions s WHERE s.question_id = q.id) AS attempt_count, "
            "  (SELECT MAX(occurred_at) FROM practice_sessions s WHERE s.question_id = q.id) AS last_attempt "
            "FROM practice_questions q "
            "ORDER BY q.category, q.id"
        ).fetchall()
        return [dict(r) for r in rows]


def list_cores() -> list[dict]:
    """Core (non-variant) questions, each with its nested variant phrasings.
    Practice attaches to the core, so attempt counts are the core's own."""
    seed_if_empty()
    with db.connect() as conn:
        cores = conn.execute(
            "SELECT q.id, q.category, q.question, "
            "  (SELECT COUNT(*) FROM practice_sessions s WHERE s.question_id = q.id) AS attempt_count, "
            "  (SELECT MAX(occurred_at) FROM practice_sessions s WHERE s.question_id = q.id) AS last_attempt "
            "FROM practice_questions q WHERE q.core_id IS NULL "
            "ORDER BY q.category, q.id"
        ).fetchall()
        out = []
        for c in cores:
            d = dict(c)
            d["variants"] = [
                dict(v) for v in conn.execute(
                    "SELECT id, question, category FROM practice_questions "
                    "WHERE core_id = ? ORDER BY id",
                    (c["id"],),
                ).fetchall()
            ]
            out.append(d)
        return out


def core_of(question_id: int) -> int | None:
    """Resolve a question id to its core id (itself if it is already a core)."""
    with db.connect() as conn:
        r = conn.execute(
            "SELECT id, core_id FROM practice_questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if r is None:
            return None
        return r["core_id"] or r["id"]


def variants_of(core_id: int) -> list[dict]:
    with db.connect() as conn:
        return [
            dict(v) for v in conn.execute(
                "SELECT id, question, category FROM practice_questions "
                "WHERE core_id = ? ORDER BY id",
                (core_id,),
            ).fetchall()
        ]


def nest_variant(variant_id: int, core_id: int) -> None:
    """Make `variant_id` a variant of `core_id`; roll its attempts up to the core."""
    if variant_id == core_id:
        return
    with db.connect() as conn:
        conn.execute("UPDATE practice_sessions SET question_id = ? WHERE question_id = ?",
                     (core_id, variant_id))
        conn.execute("UPDATE practice_questions SET core_id = ? WHERE id = ?",
                     (core_id, variant_id))
        conn.commit()


def set_question_text(question_id: int, text: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE practice_questions SET question = ? WHERE id = ?",
                     ((text or "").strip(), question_id))
        conn.commit()


_VALID_CATEGORIES = {"behavioral", "technical", "system_design", "fe_specific", "personal"}


def create_question(question: str, category: str = "behavioral",
                    source: str = "from_interview") -> int:
    """Insert a practice question, reusing an existing row on exact text match.
    Returns the question id."""
    q = (question or "").strip()
    if not q:
        raise ValueError("question text required")
    cat = category if category in _VALID_CATEGORIES else "behavioral"
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM practice_questions WHERE LOWER(question) = LOWER(?)",
            (q,),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO practice_questions (category, question, source) VALUES (?, ?, ?)",
            (cat, q, source),
        )
        conn.commit()
        return cur.lastrowid


def get_question(question_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, category, question, core_id FROM practice_questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["variants"] = [
            dict(v) for v in conn.execute(
                "SELECT id, question, category FROM practice_questions "
                "WHERE core_id = ? ORDER BY id",
                (question_id,),
            ).fetchall()
        ]
        return d


def create_session(question_id: int, audio_path: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO practice_sessions (question_id, audio_path) VALUES (?, ?)",
            (question_id, audio_path),
        )
        conn.commit()
        return cur.lastrowid


def list_sessions(question_id: int | None = None) -> list[dict]:
    with db.connect() as conn:
        if question_id is None:
            rows = conn.execute(
                "SELECT s.id, s.question_id, s.occurred_at, s.audio_path, q.question "
                "FROM practice_sessions s JOIN practice_questions q ON q.id = s.question_id "
                "ORDER BY s.occurred_at DESC LIMIT 50"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT s.id, s.question_id, s.occurred_at, s.audio_path, q.question "
                "FROM practice_sessions s JOIN practice_questions q ON q.id = s.question_id "
                "WHERE s.question_id = ? ORDER BY s.occurred_at DESC",
                (question_id,),
            ).fetchall()
        return [dict(r) for r in rows]
