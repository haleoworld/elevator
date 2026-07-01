"""FastAPI app — Phase 1 shell.

Routes:
  GET  /            redirect to /profile (or /login if not authed)
  GET  /login       login form
  POST /login       verify password, set session
  POST /logout      clear session
  GET  /profile     editor for all 6 sections
  POST /profile/{key}  save a single section
  GET  /health      system health page
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

from datetime import datetime  # noqa: E402

from . import (  # noqa: E402 — must come after load_dotenv
    answer_gen, audio, auth, batches, companies_store, content, db, health,
    jmc_bridge, notifier, practice, process_audio, profile_store, screen,
)
import threading  # noqa: E402
import uuid  # noqa: E402
from fastapi import UploadFile, File  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

SRC_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(SRC_DIR / "templates"))


# Inline flat SVG icons (Lucide stroke style). Replaces emoji UI icons across
# the app so the look is monochrome and consistent regardless of OS.
_ICONS: dict[str, str] = {
    "trash":    '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    "search":   '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "sparkles": '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3z"/>',
    "pencil":   '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
    "undo":     '<path d="M9 14L4 9l5-5"/><path d="M4 9h7a8 8 0 1 1 0 16h-1"/>',
    "warning":  '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "refresh":  '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/>',
    "check":    '<polyline points="20 6 9 17 4 12"/>',
    "mic":      '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>',
    "record":   '<circle cx="12" cy="12" r="5" fill="currentColor"/>',
    "stop":     '<rect x="6" y="6" width="12" height="12" fill="currentColor"/>',
    "copy":     '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "play":     '<polygon points="6 3 20 12 6 21 6 3"/>',
    "help":     '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
}


def _icon(name: str, size: int = 16) -> str:
    inner = _ICONS.get(name, "")
    return (
        f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{inner}</svg>'
    )


from markupsafe import Markup as _Markup  # noqa: E402
templates.env.globals["icon"] = lambda name, size=16: _Markup(_icon(name, size))

import html as _html_mod  # noqa: E402


def _unescape_html_safe(s):
    """Decode HTML entities (`&#39;` → `'`), then re-escape only the
    structural chars (<, >, &) and wrap in Markup so Jinja's autoescape
    doesn't undo our work. Result is safe inside <pre> blocks."""
    if not s:
        return s
    decoded = _html_mod.unescape(str(s))
    # Re-escape only what's structurally needed in HTML; leave quotes alone.
    safe = decoded.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _Markup(safe)


templates.env.filters["unescape_html"] = _unescape_html_safe


def _fmt_mmss(seconds):
    """Seconds → 'M:SS' (e.g. 517 → '8:37'). Passes through non-numerics."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return seconds
    return f"{s // 60}:{s % 60:02d}"


templates.env.filters["mmss"] = _fmt_mmss

# When served behind a path-prefix proxy (Tailscale Serve, nginx, etc.), the
# proxy strips the prefix before forwarding — so routes match clean paths —
# but we still need to *generate* outbound URLs (links, redirects, form actions)
# with the prefix. ROOT_PATH is read from env and injected as a Jinja global
# and used to construct redirects.
ROOT_PATH = os.environ.get("ELEVATOR_ROOT_PATH", "").rstrip("/")
templates.env.globals["root_path"] = ROOT_PATH


def _static_version() -> str:
    """Cache-bust the CSS link with the mtime of style.css. Means any edit
    you make automatically invalidates the browser cache without a hard refresh."""
    try:
        return str(int((SRC_DIR / "static" / "style.css").stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["static_version"] = _static_version()


def _rp(path: str) -> str:
    """Prefix a path with ROOT_PATH (no-op when ROOT_PATH is empty)."""
    return f"{ROOT_PATH}{path}"


def _session_secret() -> str:
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY not set. Generate with: "
            'python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return key


app = FastAPI(title="Elevator", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    https_only=False,           # Tailscale is the trust boundary, not TLS
    same_site="lax",
    max_age=60 * 60 * 24 * 30,  # 30 days
)
app.mount("/static", StaticFiles(directory=str(SRC_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init()
    auth.init_password()
    # Recover any recording left unprocessed by a prior crash/restart.
    process_audio.resume_unprocessed()


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if auth.is_logged_in(request):
        return RedirectResponse(url=_rp("/profile"), status_code=303)
    return RedirectResponse(url=_rp("/login"), status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None):
    if auth.is_logged_in(request):
        return RedirectResponse(url=_rp("/profile"), status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    if auth.verify(password):
        request.session["authed"] = True
        return RedirectResponse(url=_rp("/profile"), status_code=303)
    return RedirectResponse(url=_rp("/login?error=1"), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=_rp("/login"), status_code=303)


@app.get("/profile", response_class=HTMLResponse)
def profile_view(request: Request, saved: str | None = None):
    if (r := auth.require_login(request)) is not None:
        return r
    bodies = profile_store.read_all()
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "sections": profile_store.SECTIONS,
            "bodies": bodies,
            "saved_key": saved,
        },
    )


@app.post("/profile/{key}")
def profile_save(request: Request, key: str, body: str = Form("")):
    if (r := auth.require_login(request)) is not None:
        return r
    if profile_store.get_section(key) is None:
        raise HTTPException(status_code=404, detail="unknown section")
    profile_store.write_section(key, body)
    return RedirectResponse(url=_rp(f"/profile?saved={key}#{key}"), status_code=303)


@app.get("/companies", response_class=HTMLResponse)
def companies_view(
    request: Request,
    saved: str | None = None,
    errors: str | None = None,
    q_pattern: str = "",
    q_segment: str = "",
    q_size: str = "",
    q_canada: str = "",
    q_ats: str = "",
    q_on: str = "",
):
    if (r := auth.require_login(request)) is not None:
        return r
    raw = companies_store.load_all()
    rows = [{"c": c, "pattern": companies_store.classify_pattern(c.segment, c.slug)} for c in raw]

    def has_ats(c, kind: str) -> bool:
        return {
            "gh":   bool(c.greenhouse_slug),
            "lv":   bool(c.lever_slug),
            "wd":   bool(c.workday_url),
            "ab":   bool(c.ashby_slug),
            "sr":   bool(c.smartrecruiters_slug),
            "adzuna": not any([c.greenhouse_slug, c.lever_slug, c.workday_url,
                               c.ashby_slug, c.smartrecruiters_slug]),
        }.get(kind, True)

    if q_pattern:
        rows = [r for r in rows if r["pattern"] == q_pattern]
    if q_segment:
        s = q_segment.lower()
        rows = [r for r in rows if s in (r["c"].segment or "").lower()]
    if q_size:
        rows = [r for r in rows if r["c"].headcount_band == q_size]
    if q_canada:
        target = {"yes": True, "no": False, "unknown": None}.get(q_canada, "skip")
        if target != "skip":
            rows = [r for r in rows if r["c"].has_canada_team is target]
    if q_ats:
        rows = [r for r in rows if has_ats(r["c"], q_ats)]
    if q_on == "on":
        rows = [r for r in rows if r["c"].enabled]
    elif q_on == "off":
        rows = [r for r in rows if not r["c"].enabled]

    return templates.TemplateResponse(
        "companies.html",
        {
            "request": request,
            "company_rows": rows,
            "total_rows": len(raw),
            "headcount_bands": companies_store.HEADCOUNT_BANDS,
            "company": None,
            "saved": saved,
            "errors": [errors] if errors else [],
            "form_action": _rp("/companies"),
            "submit_label": "Add company",
            "q_pattern": q_pattern, "q_segment": q_segment, "q_size": q_size,
            "q_canada": q_canada, "q_ats": q_ats, "q_on": q_on,
        },
    )


@app.get("/companies/add", response_class=HTMLResponse)
def companies_add_form(request: Request, errors: str | None = None):
    if (r := auth.require_login(request)) is not None:
        return r
    return templates.TemplateResponse(
        "company_add.html",
        {
            "request": request,
            "headcount_bands": companies_store.HEADCOUNT_BANDS,
            "company": None,
            "errors": [errors] if errors else [],
            "form_action": _rp("/companies"),
            "submit_label": "Add company",
        },
    )


def _form_to_company(form: dict) -> companies_store.Company:
    def s(k: str) -> str:
        return (form.get(k) or "").strip()

    canada_raw = (form.get("has_canada_team") or "").strip()
    canada = True if canada_raw == "yes" else (False if canada_raw == "no" else None)
    return companies_store.Company(
        name=s("name"),
        slug=s("slug").lower(),
        segment=s("segment"),
        headcount_band=s("headcount_band") or "500-5000",
        greenhouse_slug=s("greenhouse_slug") or None,
        lever_slug=s("lever_slug") or None,
        workday_url=s("workday_url") or None,
        ashby_slug=s("ashby_slug") or None,
        smartrecruiters_slug=s("smartrecruiters_slug") or None,
        has_canada_team=canada,
        enabled=bool(form.get("enabled")),
        notes=s("notes"),
    )


@app.post("/companies")
async def companies_add(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    form = dict(await request.form())
    c = _form_to_company(form)
    errs = companies_store.upsert(c)
    if errs:
        return templates.TemplateResponse(
            "company_add.html",
            {
                "request": request,
                "headcount_bands": companies_store.HEADCOUNT_BANDS,
                "company": c,
                "errors": errs,
                "form_action": _rp("/companies"),
                "submit_label": "Add company",
            },
            status_code=400,
        )
    return RedirectResponse(url=_rp(f"/companies?saved={c.slug}"), status_code=303)


@app.post("/companies/bulk-delete")
async def companies_bulk_delete(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    slugs = form.getlist("slugs")
    n = 0
    for s in slugs:
        if s and companies_store.delete(s):
            n += 1
    return RedirectResponse(url=_rp(f"/companies?saved=deleted-{n}"), status_code=303)


@app.get("/companies/{slug}/edit", response_class=HTMLResponse)
def company_edit_form(request: Request, slug: str):
    if (r := auth.require_login(request)) is not None:
        return r
    c = companies_store.get(slug)
    if c is None:
        raise HTTPException(status_code=404, detail="company not found")
    return templates.TemplateResponse(
        "company_edit.html",
        {
            "request": request,
            "company": c,
            "headcount_bands": companies_store.HEADCOUNT_BANDS,
            "errors": [],
            "submit_label": "Save changes",
        },
    )


@app.post("/companies/{slug}/edit")
async def company_edit_save(request: Request, slug: str):
    if (r := auth.require_login(request)) is not None:
        return r
    if companies_store.get(slug) is None:
        raise HTTPException(status_code=404, detail="company not found")
    form = dict(await request.form())
    c = _form_to_company(form)
    errs = companies_store.upsert(c, updating_slug=slug)
    if errs:
        return templates.TemplateResponse(
            "company_edit.html",
            {
                "request": request,
                "company": c,
                "headcount_bands": companies_store.HEADCOUNT_BANDS,
                "errors": errs,
                "submit_label": "Save changes",
            },
            status_code=400,
        )
    return RedirectResponse(url=_rp(f"/companies?saved={c.slug}"), status_code=303)


@app.post("/companies/{slug}/delete")
def company_delete(request: Request, slug: str):
    if (r := auth.require_login(request)) is not None:
        return r
    companies_store.delete(slug)
    return RedirectResponse(url=_rp("/companies"), status_code=303)


DISCOVERY_PIDFILE = REPO_ROOT / "data" / "discovery.pid"
DISCOVERY_LOG = REPO_ROOT / "logs" / "discovery.log"
SCREEN_PIDFILE = REPO_ROOT / "data" / "screen.pid"
SCREEN_LOG = REPO_ROOT / "logs" / "screen.log"
EXPIRE_LOG = REPO_ROOT / "logs" / "expire.log"


@app.post("/jobs/run-discovery")
def jobs_run_discovery(request: Request, include_screen: str = Form("")):
    if (r := auth.require_login(request)) is not None:
        return r
    import subprocess
    cmd = [
        str(REPO_ROOT / ".venv" / "bin" / "python"),
        "-m", "src.discovery.run",
    ]
    if include_screen != "yes":
        cmd.append("--no-screen")
    DISCOVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DISCOVERY_LOG, "ab") as logf:
        logf.write(f"\n\n==== {datetime.now().isoformat()} discovery started ====\n".encode())
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    DISCOVERY_PIDFILE.write_text(str(proc.pid))
    flash = "discovery-started-with-screen" if include_screen == "yes" else "discovery-started"
    return RedirectResponse(url=_rp(f"/jobs?started={flash}"), status_code=303)


@app.post("/jobs/run-screen")
def jobs_run_screen(request: Request):
    """Run Phase 3 LLM screening only — on jobs currently in Passed."""
    if (r := auth.require_login(request)) is not None:
        return r
    import subprocess
    cmd = [
        str(REPO_ROOT / ".venv" / "bin" / "python"),
        "-m", "src.screen",
    ]
    SCREEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    SCREEN_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCREEN_LOG, "ab") as logf:
        logf.write(f"\n\n==== {datetime.now().isoformat()} screen started ====\n".encode())
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    SCREEN_PIDFILE.write_text(str(proc.pid))
    return RedirectResponse(url=_rp("/jobs?started=screen-started"), status_code=303)


@app.post("/jobs/prune-expired")
def jobs_prune_expired(request: Request):
    """Check active jobs' postings and drop any whose listing was removed."""
    if (r := auth.require_login(request)) is not None:
        return r
    import subprocess
    EXPIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EXPIRE_LOG, "ab") as logf:
        logf.write(f"\n\n==== {datetime.now().isoformat()} prune started ====\n".encode())
        subprocess.Popen(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-m", "src.expire"],
            cwd=str(REPO_ROOT), stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return RedirectResponse(url=_rp("/jobs?started=prune-started"), status_code=303)


@app.post("/jobs/run-serpapi")
def jobs_run_serpapi(request: Request):
    """TEST: broad keyword discovery via SerpAPI Google Jobs. Results land in
    the Passed tab (bypassing the curated-company gate) for quality review."""
    if (r := auth.require_login(request)) is not None:
        return r
    from .discovery import serpapi as _serp
    if not _serp.key_configured():
        raise HTTPException(status_code=400,
                            detail="SERPAPI_KEY not set in backend/.env — add it and retry.")
    try:
        res = _serp.run_test()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"SerpAPI error: {e}")
    return RedirectResponse(
        url=_rp(f"/jobs?tab=passed&saved=serpapi-{res['inserted']}"), status_code=303)


def _job_run_status(pidfile, logfile, *, label: str) -> dict | None:
    """Generic 'is this background job running?' helper. Used for both
       discovery and screen. Returns None if there's no recent activity."""
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None
    started_at = datetime.fromtimestamp(pidfile.stat().st_mtime)
    try:
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, PermissionError, OSError):
        alive = False
    last_lines: list[str] = []
    last_activity_at = started_at
    if logfile.exists():
        try:
            sz = logfile.stat().st_size
            with open(logfile, "rb") as f:
                f.seek(max(0, sz - 4096))
                tail = f.read().decode("utf-8", errors="replace")
            for ln in tail.splitlines():
                s = ln.strip()
                if s and any(s.startswith(p) for p in
                    ("[discover]", "[filter]", "[screen]", "[batch]", "[content]", "====")):
                    last_lines.append(s)
            last_activity_at = datetime.fromtimestamp(logfile.stat().st_mtime)
        except OSError:
            pass
    now = datetime.now()
    if not alive and (now - last_activity_at).total_seconds() > 180:
        return None
    return {
        "running": alive,
        "label": label,
        "started_at": started_at,
        "elapsed_s": int((now - started_at).total_seconds()),
        "last_activity_at": last_activity_at,
        "since_last_activity_s": int((now - last_activity_at).total_seconds()),
        "last_line": last_lines[-1] if last_lines else "",
        "tail": last_lines[-6:],
    }


def _discovery_status() -> dict | None:
    return _job_run_status(DISCOVERY_PIDFILE, DISCOVERY_LOG, label="Discovery")


def _screen_status() -> dict | None:
    return _job_run_status(SCREEN_PIDFILE, SCREEN_LOG, label="LLM screen")


def _score_class(score: int) -> str:
    if score >= 80: return "score-excellent"
    if score >= 60: return "score-good"
    if score >= 40: return "score-meh"
    return "score-poor"


# How a job entered the system, derived from its `source`. Discovery scrapers
# (workday/greenhouse/lever/ashby/adzuna/smartrecruiters) all read as "Discovery".
_INTAKE_LABELS = {"serpapi": "SerpAPI", "manual": "Paste", "shortcut": "Shortcut"}


def _intake_label(source: str | None) -> str:
    if not source:
        return "—"
    return _INTAKE_LABELS.get(source, "Discovery")


@app.post("/jobs/bulk-delete")
async def jobs_bulk_delete(request: Request):
    """Soft-delete selected jobs. Auto-purged after 5 days."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    if not ids:
        return RedirectResponse(url=_rp("/jobs?saved=deleted-0"), status_code=303)
    ph = ",".join("?" * len(ids))
    with db.connect() as conn:
        # Which of these are in a batch? Capture before unlinking so we can
        # fix those batches' job_count afterward.
        affected_batches = [r[0] for r in conn.execute(
            f"SELECT DISTINCT batch_id FROM batch_jobs WHERE job_id IN ({ph})", ids
        )]
        cur = conn.execute(
            f"UPDATE jobs SET deleted_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({ph}) AND deleted_at IS NULL", ids,
        )
        # A deleted job leaves any batch it was in. Reset its stage back to
        # 'scored' so that if it's later restored it lands in the Scored tab
        # rather than being stuck in 'queued' with no batch.
        conn.execute(f"DELETE FROM batch_jobs WHERE job_id IN ({ph})", ids)
        conn.execute(
            f"UPDATE jobs SET stage = 'scored' "
            f"WHERE id IN ({ph}) AND stage IN ('queued', 'generating', 'ready')",
            ids,
        )
        for bid in affected_batches:
            conn.execute(
                "UPDATE batches SET job_count = "
                "  (SELECT COUNT(*) FROM batch_jobs WHERE batch_id = ?) WHERE id = ?",
                (bid, bid),
            )
        conn.commit()
    return RedirectResponse(url=_rp(f"/jobs?saved=deleted-{cur.rowcount}"), status_code=303)


@app.post("/jobs/bulk-move-batch")
async def jobs_bulk_move_batch(request: Request):
    """Move selected batched jobs into another batch."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    target = (form.get("target_batch_id") or "").strip()
    tab = form.get("tab") or "batched"
    if not ids or not target.isdigit():
        return RedirectResponse(url=_rp(f"/jobs?tab={tab}&saved=moved-0"), status_code=303)
    n = batches.move_jobs(ids, int(target))
    return RedirectResponse(url=_rp(f"/jobs?tab={tab}&saved=moved-{n}"), status_code=303)


@app.post("/jobs/send-to-jmc")
async def jobs_send_to_jmc(request: Request):
    """Push selected batched jobs into the jmc app, into a batch matched by the
    job's elevator batch name (created in jmc if missing)."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    tab = form.get("tab") or "batched"
    if not ids:
        return RedirectResponse(url=_rp(f"/jobs?tab={tab}&jmc=none"), status_code=303)
    ph = ",".join("?" * len(ids))
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT j.id, j.company, j.title, j.url, j.location, j.jd_text, "
            "       j.salary_min, j.salary_max, j.salary_currency, j.remote_type, "
            "       j.job_type, j.reference_id, j.posted_at, b.name AS batch_name "
            "FROM jobs j JOIN batch_jobs bj ON bj.job_id = j.id "
            "JOIN batches b ON b.id = bj.batch_id "
            f"WHERE j.id IN ({ph}) AND j.deleted_at IS NULL", ids,
        )]
    jobs_by_batch: dict[str, list[dict]] = {}
    for row in rows:
        jobs_by_batch.setdefault(row["batch_name"], []).append(row)
    if not jobs_by_batch:
        return RedirectResponse(url=_rp(f"/jobs?tab={tab}&jmc=none"), status_code=303)
    try:
        s = jmc_bridge.send_jobs(jobs_by_batch)
    except Exception as e:
        print(f"  jmc send failed: {e}")
        return RedirectResponse(url=_rp(f"/jobs?tab={tab}&jmc=error"), status_code=303)
    msg = f"added-{s['added']}-skipped-{s['skipped']}"
    if s["created_batches"]:
        msg += "-new-" + str(len(s["created_batches"]))
    return RedirectResponse(url=_rp(f"/jobs?tab={tab}&jmc={msg}"), status_code=303)


@app.post("/jobs/bulk-restore")
async def jobs_bulk_restore(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    if not ids:
        return RedirectResponse(url=_rp("/jobs?tab=deleted&saved=restored-0"), status_code=303)
    ph = ",".join("?" * len(ids))
    with db.connect() as conn:
        cur = conn.execute(f"UPDATE jobs SET deleted_at = NULL WHERE id IN ({ph})", ids)
        conn.commit()
    return RedirectResponse(url=_rp(f"/jobs?tab=deleted&saved=restored-{cur.rowcount}"), status_code=303)


@app.post("/jobs/bulk-delete-permanent")
async def jobs_bulk_delete_permanent(request: Request, tab: str = Form("deleted")):
    """Hard-delete the selected jobs. Works from any tab — `tab` controls
       where we redirect afterward."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    if not ids:
        return RedirectResponse(url=_rp(f"/jobs?tab={tab}&saved=purged-0"), status_code=303)
    ph = ",".join("?" * len(ids))
    with db.connect() as conn:
        # api_costs.job_id has no ON DELETE CASCADE — detach so the row survives.
        conn.execute(f"UPDATE api_costs   SET job_id = NULL WHERE job_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM applications WHERE job_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM batch_jobs   WHERE job_id IN ({ph})", ids)
        cur = conn.execute(f"DELETE FROM jobs   WHERE id     IN ({ph})", ids)
        conn.commit()
    return RedirectResponse(url=_rp(f"/jobs?tab={tab}&saved=purged-{cur.rowcount}"), status_code=303)


@app.post("/jobs/add-to-batch")
async def jobs_add_to_batch(request: Request):
    """Add selected scored jobs to a new or existing batch.
       Form: job_ids[], batch_target ('new' or a batch id), name (for new)."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    target = (form.get("batch_target") or "new").strip()
    name = (form.get("batch_name") or "").strip() or None
    if not ids:
        return RedirectResponse(url=_rp("/jobs?tab=scored&saved=batch-0"), status_code=303)

    if target == "new":
        batch_id = batches.form_batch(name=name, job_ids=ids)
        if batch_id is None:
            return RedirectResponse(url=_rp("/jobs?tab=scored&err=no_eligible"), status_code=303)
        return RedirectResponse(url=_rp(f"/batches/{batch_id}?created=1"), status_code=303)

    if target.isdigit():
        n = batches.add_jobs_to_batch(int(target), ids)
        return RedirectResponse(url=_rp(f"/batches/{int(target)}?added={n}"), status_code=303)

    return RedirectResponse(url=_rp("/jobs?tab=scored&err=bad_target"), status_code=303)


@app.post("/jobs/bulk-rescreen")
async def jobs_bulk_rescreen(request: Request, tab: str = Form("scored")):
    """Reset selected jobs back to Passed and trigger Phase 3 LLM screen.
       Useful after tightening criteria or changing the scoring tool."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
    if not ids:
        return RedirectResponse(url=_rp(f"/jobs?tab={tab}&saved=rescreen-0"), status_code=303)
    ph = ",".join("?" * len(ids))
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE jobs SET stage='filtered', filter_status='passed', "
            f"fit_score=NULL, fit_reason=NULL, ats_score=NULL, "
            f"ats_missing_keywords=NULL WHERE id IN ({ph})", ids,
        )
        conn.commit()
        n = cur.rowcount or 0

    def _screen():
        screen.run(job_ids=ids)
    import threading as _th
    _th.Thread(target=_screen, daemon=True).start()
    return RedirectResponse(
        url=_rp(f"/jobs?tab=passed&saved=rescreening-{n}"),
        status_code=303,
    )


@app.post("/jobs/delete-all")
async def jobs_delete_all(request: Request):
    """Soft-delete every non-deleted job. Auto-purged after 5 days."""
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL"
        )
        conn.commit()
    return RedirectResponse(url=_rp(f"/jobs?saved=delete-all-{cur.rowcount}"), status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_view(request: Request, tab: str = "scored",
              q_company: str = "", q_reason: str = "",
              q_fit: list[str] = Query(default=[]),
              q_role: list[str] = Query(default=[]),
              q_co: list[str] = Query(default=[]),
              q_fe_pct: list[str] = Query(default=[]),
              q_min_ats: str = "", q_fe: str = "", q_location: str = "",
              q_batch: str = "", q_rehost: str = ""):
    if (r := auth.require_login(request)) is not None:
        return r
    if tab not in ("scored", "passed", "dropped", "batched", "deleted"):
        tab = "scored"

    # Re-host tagging list (jobs page display only; NOT a discovery drop-list).
    from .filter import is_rehost as _is_rehost, REHOST_DOMAINS

    purged_n = db.purge_old_deleted_jobs(days=5)

    with db.connect() as conn:
        totals_row = conn.execute(
            "SELECT "
            "  COUNT(*) AS all_, "
            "  SUM(CASE WHEN filter_status='passed' AND stage='filtered' AND deleted_at IS NULL THEN 1 ELSE 0 END) AS passed_unscored, "
            "  SUM(CASE WHEN filter_status='dropped' AND deleted_at IS NULL THEN 1 ELSE 0 END) AS dropped, "
            "  SUM(CASE WHEN stage='scored' AND deleted_at IS NULL THEN 1 ELSE 0 END) AS scored, "
            "  SUM(CASE WHEN deleted_at IS NULL AND id IN (SELECT job_id FROM batch_jobs) THEN 1 ELSE 0 END) AS batched, "
            "  SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted "
            "FROM jobs"
        ).fetchone()
        cost_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM api_costs WHERE operation='fit_screen'"
        ).fetchone()

        def append_filters(q: str, params: list) -> str:
            if q_company:
                q += "AND LOWER(company) LIKE ? "
                params.append(f"%{q_company.lower()}%")
            if q_reason:
                if q_reason == "(no reason)":
                    q += "AND (drop_reason IS NULL OR drop_reason = '') "
                else:
                    q += "AND drop_reason = ? "
                    params.append(q_reason)
            # Fit / role-fit / co-fit tier filters run in Python after scoring
            # (role_fit & co_fit are computed there), so nothing here for them.
            if q_min_ats == "no_score":
                q += "AND ats_score IS NULL "
            elif q_min_ats and q_min_ats.isdigit():
                q += "AND ats_score >= ? "
                params.append(int(q_min_ats))
            if q_fe == "yes":
                q += "AND json_extract(ats_missing_keywords, '$.is_frontend') = 1 "
            elif q_fe == "no":
                q += "AND json_extract(ats_missing_keywords, '$.is_frontend') = 0 "
            if q_location:
                q += "AND LOWER(COALESCE(location, '')) LIKE ? "
                params.append(f"%{q_location.lower()}%")
            if q_batch.isdigit():
                q += "AND id IN (SELECT job_id FROM batch_jobs WHERE batch_id = ?) "
                params.append(int(q_batch))
            if q_rehost in ("only", "hide"):
                likes = " OR ".join("url LIKE ?" for _ in REHOST_DOMAINS)
                params.extend(f"%{d}%" for d in REHOST_DOMAINS)
                q += (f"AND ({likes}) " if q_rehost == "only"
                      else f"AND NOT ({likes}) ")
            return q

        SELECT_COLS = (
            "id, company, title, location, fit_score, ats_score, "
            "filter_status, drop_reason, deleted_at, jd_text, "
            "posted_at, discovered_at, job_board, stage, source, url, "
            "(SELECT bj.batch_id FROM batch_jobs bj WHERE bj.job_id = jobs.id LIMIT 1) AS batch_id, "
            "(SELECT b.name FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id "
            " WHERE bj.job_id = jobs.id LIMIT 1) AS batch_name, "
            "json_extract(ats_missing_keywords, '$.is_frontend') AS is_frontend, "
            "json_extract(ats_missing_keywords, '$.perspectives.tech_stack.score') AS p_tech, "
            "json_extract(ats_missing_keywords, '$.perspectives.fe_be_breakdown.score') AS p_febr, "
            "json_extract(ats_missing_keywords, '$.perspectives.requirements.score') AS p_req, "
            "json_extract(ats_missing_keywords, '$.perspectives.role_expectations.score') AS p_role, "
            "json_extract(ats_missing_keywords, '$.perspectives.team_culture.score') AS p_tc, "
            "json_extract(ats_missing_keywords, '$.perspectives.company_culture.score') AS p_cc, "
            "json_extract(ats_missing_keywords, '$.perspectives.company_value.score') AS p_cv, "
            "json_extract(ats_missing_keywords, '$.perspectives.company_business_model.score') AS p_cbm"
        )

        TAB_QUERIES = {
            "scored":  (f"SELECT {SELECT_COLS} FROM jobs "
                        "WHERE stage='scored' AND deleted_at IS NULL ",
                        "ORDER BY discovered_at DESC, fit_score DESC LIMIT 500"),
            "passed":  (f"SELECT {SELECT_COLS} FROM jobs "
                        "WHERE filter_status='passed' AND stage='filtered' AND deleted_at IS NULL ",
                        "ORDER BY discovered_at DESC LIMIT 500"),
            "dropped": (f"SELECT {SELECT_COLS} FROM jobs "
                        "WHERE filter_status='dropped' AND deleted_at IS NULL ",
                        "ORDER BY discovered_at DESC LIMIT 1000"),
            "batched": (f"SELECT {SELECT_COLS} FROM jobs "
                        "WHERE deleted_at IS NULL "
                        "AND id IN (SELECT job_id FROM batch_jobs) ",
                        "ORDER BY discovered_at DESC LIMIT 1000"),
            "deleted": (f"SELECT {SELECT_COLS} FROM jobs "
                        "WHERE deleted_at IS NOT NULL ",
                        "ORDER BY deleted_at DESC LIMIT 1000"),
        }
        REASON_WHERE = {
            "scored":  "stage='scored' AND deleted_at IS NULL",
            "passed":  "filter_status='passed' AND stage='filtered' AND deleted_at IS NULL",
            "dropped": "filter_status='dropped' AND deleted_at IS NULL",
            "batched": "deleted_at IS NULL AND id IN (SELECT job_id FROM batch_jobs)",
            "deleted": "deleted_at IS NOT NULL",
        }
        drop_reasons = [r[0] or "(no reason)" for r in conn.execute(
            f"SELECT DISTINCT drop_reason FROM jobs "
            f"WHERE {REASON_WHERE[tab]} AND drop_reason IS NOT NULL "
            "ORDER BY drop_reason"
        )]
        base_q, order_q = TAB_QUERIES[tab]
        params: list = []
        q = append_filters(base_q, params) + order_q
        rows = [dict(r) for r in conn.execute(q, params)]

        from . import jd_analyzer  # noqa: E402
        def _avg(*vals):
            xs = [v for v in vals if isinstance(v, int)]
            return round(sum(xs) / len(xs)) if xs else None
        for d in rows:
            d["score_class"] = _score_class(d.get("fit_score") or 0)
            d["intake"] = _intake_label(d.get("source"))
            d["is_rehost"] = _is_rehost(d.get("url"))
            # Role fit = avg(tech_stack, fe_be_breakdown, requirements, role_expectations)
            d["role_fit"] = _avg(d.get("p_tech"), d.get("p_febr"),
                                  d.get("p_req"),  d.get("p_role"))
            d["role_fit_class"] = _score_class(d["role_fit"]) if d["role_fit"] is not None else None
            # Company fit = avg(team_culture, company_culture, company_value, company_business_model)
            d["company_fit"] = _avg(d.get("p_tc"), d.get("p_cc"),
                                     d.get("p_cv"), d.get("p_cbm"))
            d["company_fit_class"] = _score_class(d["company_fit"]) if d["company_fit"] is not None else None
            # FE% from keyword-based jd_analyzer (different from LLM is_frontend bool)
            jd = d.get("jd_text") or ""
            d["fe_share"] = jd_analyzer.analyze(jd).get("frontend_split") if jd else None
            # Drop the heavy jd_text field before template rendering — we got what we needed
            d.pop("jd_text", None)

        # Tier post-filters (multi-select). fit/role/co share the same buckets.
        def _tier_of(s):
            if s is None: return "none"
            if s >= 70: return "apply"
            if s >= 66: return "transition"
            if s >= 40: return "practice"
            return "drop"
        if q_fit:
            rows = [d for d in rows if _tier_of(d.get("fit_score")) in q_fit]
        if q_role:
            rows = [d for d in rows if _tier_of(d.get("role_fit")) in q_role]
        if q_co:
            rows = [d for d in rows if _tier_of(d.get("company_fit")) in q_co]
        if q_fe_pct:
            def _fe_ok(fs):
                if fs is None: return "none" in q_fe_pct
                if fs >= 50:   return "high" in q_fe_pct
                return "low" in q_fe_pct
            rows = [d for d in rows if _fe_ok(d.get("fe_share"))]

        scored = rows if tab == "scored" else []
        passed_not_scored = rows if tab == "passed" else []
        dropped_jobs = rows if tab == "dropped" else []
        batched_jobs = rows if tab == "batched" else []
        deleted_jobs = rows if tab == "deleted" else []

    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "tab": tab,
            "scored": scored,
            "passed_not_scored": passed_not_scored,
            "dropped_jobs": dropped_jobs,
            "batched_jobs": batched_jobs,
            "deleted_jobs": deleted_jobs,
            "drop_reasons": drop_reasons,
            "q_company": q_company,
            "q_reason": q_reason,
            "q_fit": q_fit,
            "q_role": q_role,
            "q_co": q_co,
            "q_fe_pct": q_fe_pct,
            "q_min_ats": q_min_ats,
            "q_fe": q_fe,
            "q_location": q_location,
            "q_batch": q_batch,
            "q_rehost": q_rehost,
            "purged_n": purged_n,
            "discovery_status": _discovery_status(),
            "screen_status": _screen_status(),
            "serpapi_configured": bool(os.environ.get("SERPAPI_KEY", "").strip()),
            "batch_options": batches.list_batch_options() if tab in ("scored", "batched") else [],
            "totals": {
                "all": totals_row["all_"] or 0,
                "passed_unscored": totals_row["passed_unscored"] or 0,
                "dropped": totals_row["dropped"] or 0,
                "batched": totals_row["batched"] or 0,
                "deleted": totals_row["deleted"] or 0,
                "scored": totals_row["scored"] or 0,
            },
            "screen_calls": cost_row[0],
            "screen_cost_usd": cost_row[1],
        },
    )


@app.get("/jobs/{job_id:int}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    import json as _json
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, source, external_id, company, title, location, remote_type, "
            "job_type, salary_min, salary_max, salary_currency, "
            "reference_id, job_board, "
            "url, jd_text, posted_at, "
            "discovered_at, filter_status, drop_reason, fit_score, fit_reason, "
            "ats_score, ats_missing_keywords, stage "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    j = dict(row)
    try:
        meta = _json.loads(j.get("ats_missing_keywords") or "{}")
    except _json.JSONDecodeError:
        meta = {}
    j["is_frontend"] = bool(meta.get("is_frontend"))
    j["missing_requirements"] = meta.get("missing_requirements") or []
    score = j.get("fit_score") or 0
    j["score_class"] = _score_class(score)
    # Per-perspective scores from the 8-axis breakdown. Each entry:
    # {key: {"label": "...", "score": int, "score_class": "...", "analysis": "..."}}
    _PERSPECTIVE_LABELS = {
        "tech_stack":             "Tech stack",
        "fe_be_breakdown":        "Frontend/backend breakdown",
        "requirements":           "Requirements",
        "role_expectations":      "Role expectations",
        "team_culture":           "Team culture",
        "company_culture":        "Company culture",
        "company_value":          "Company value",
        "company_business_model": "Company business model",
    }
    persp_raw = meta.get("perspectives") or {}
    j["perspectives"] = []
    for key, label in _PERSPECTIVE_LABELS.items():
        blk = persp_raw.get(key) or {}
        s = blk.get("score") if isinstance(blk.get("score"), int) else None
        j["perspectives"].append({
            "key": key,
            "label": label,
            "score": s,
            "score_class": _score_class(s) if s is not None else None,
            "analysis": (blk.get("analysis") or "").strip(),
        })
    from . import jd_analyzer  # noqa: E402
    from markupsafe import Markup as _Markup  # noqa: E402
    j["analysis"] = jd_analyzer.analyze(j.get("jd_text"))
    j["jd_text_html"] = _Markup(jd_analyzer.format_for_display(j.get("jd_text")))
    return templates.TemplateResponse(
        "job_detail.html",
        {"request": request, "j": j},
    )


@app.get("/batches", response_class=HTMLResponse)
def batches_view(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    return templates.TemplateResponse(
        "batches.html",
        {
            "request": request,
            "batches": batches.list_batches(),
            "pickable_jobs": batches.scored_unbatched(),
            "chat_registered": bool(notifier.get_chat_id()),
        },
    )


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    bundle = batches.get_batch(batch_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return templates.TemplateResponse(
        "batch_detail.html",
        {"request": request, "batch": bundle["batch"], "jobs": bundle["jobs"]},
    )


@app.post("/batches/bulk-delete")
async def batches_bulk_delete(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    ids = [int(x) for x in form.getlist("batch_ids") if x.isdigit()]
    n = batches.delete_batches(ids)
    return RedirectResponse(url=_rp(f"/batches?saved=deleted-{n}"), status_code=303)


@app.post("/batches/form")
async def batches_form(request: Request):
    """Create a batch (top-N OR hand-picked), optionally named. Does NOT
    generate content — that's a separate, explicit step on the batch page."""
    if (r := auth.require_login(request)) is not None:
        return r
    form = await request.form()
    name = (form.get("name") or "").strip() or None
    mode = form.get("mode") or "top_n"
    if mode == "handpick":
        job_ids = [int(x) for x in form.getlist("job_ids") if x.isdigit()]
        if not job_ids:
            return RedirectResponse(url=_rp("/batches?err=no_jobs_picked"), status_code=303)
        batch_id = batches.form_batch(name=name, job_ids=job_ids)
    else:
        try:
            top_n = int(form.get("top_n") or 5)
        except ValueError:
            top_n = 5
        batch_id = batches.form_batch(name=name, top_n=max(1, min(20, top_n)))
    if batch_id is None:
        return RedirectResponse(url=_rp("/batches?err=no_scored_jobs"), status_code=303)
    return RedirectResponse(url=_rp(f"/batches/{batch_id}?created=1"), status_code=303)


@app.post("/batches/{batch_id:int}/rename")
def batches_rename(request: Request, batch_id: int, name: str = Form("")):
    if (r := auth.require_login(request)) is not None:
        return r
    if batches.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail="batch not found")
    batches.rename_batch(batch_id, name.strip() or None)
    return RedirectResponse(url=_rp(f"/batches/{batch_id}?renamed=1"), status_code=303)


@app.post("/batches/{batch_id:int}/generate")
def batches_generate(request: Request, batch_id: int):
    """On-demand content generation for an existing batch. Runs in a background
    thread so the request returns immediately; status flips to 'generating'."""
    if (r := auth.require_login(request)) is not None:
        return r
    bundle = batches.get_batch(batch_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="batch not found")

    def _gen():
        content.generate(batch_id)
        b = batches.get_batch(batch_id)
        dashboard_url = os.environ.get(
            "ELEVATOR_DASHBOARD_URL", "http://localhost:8742"
        ).rstrip("/") + f"/batches/{batch_id}"
        notifier.notify_batch_ready(batch_id, b["jobs"] if b else [], dashboard_url)

    import threading as _th
    _th.Thread(target=_gen, daemon=True).start()
    return RedirectResponse(url=_rp(f"/batches/{batch_id}?generating=1"), status_code=303)


@app.get("/telegram/register", response_class=HTMLResponse)
def telegram_register_get(request: Request, status: str | None = None,
                          chat_id: str | None = None):
    if (r := auth.require_login(request)) is not None:
        return r
    return templates.TemplateResponse(
        "telegram_register.html",
        {
            "request": request,
            "current_chat_id": notifier.get_chat_id(),
            "status": status,
            "registered_chat_id": chat_id,
        },
    )


@app.post("/telegram/register")
def telegram_register_post(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    cid, status = notifier.register_from_updates()
    return RedirectResponse(
        url=_rp(f"/telegram/register?status={status}&chat_id={cid or ''}"),
        status_code=303,
    )


@app.post("/telegram/test")
def telegram_test(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    ok, status = notifier.send_message("Elevator test ping ✅", parse_mode="")
    return RedirectResponse(
        url=_rp(f"/telegram/register?status={status}"), status_code=303
    )


@app.get("/jobs/paste", response_class=HTMLResponse)
def jobs_paste_form(request: Request, status: str | None = None):
    if (r := auth.require_login(request)) is not None:
        return r
    return templates.TemplateResponse(
        "jobs_paste.html", {"request": request, "status": status},
    )


@app.get("/jobs/{job_id:int}/edit", response_class=HTMLResponse)
def jobs_edit_form(request: Request, job_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, source, company, title, location, url, jd_text, "
            "remote_type, job_type, salary_min, salary_max, salary_currency, "
            "posted_at, reference_id, job_board "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "job_edit.html",
        {"request": request, "j": dict(row)},
    )


@app.post("/jobs/{job_id:int}/edit")
def jobs_edit_submit(
    request: Request,
    job_id: int,
    url: str = Form(...),
    company: str = Form(""),
    title: str = Form(""),
    location: str = Form(""),
    work_arrangement: str = Form(""),
    job_type: str = Form(""),
    salary_range: str = Form(""),
    posted_date: str = Form(""),
    reference_id: str = Form(""),
    job_board: str = Form(""),
    jd_text: str = Form(""),
):
    if (r := auth.require_login(request)) is not None:
        return r
    url_clean = url.strip()
    if not url_clean:
        raise HTTPException(status_code=400, detail="URL is required")
    company_clean = company.strip() or "(unknown)"
    title_clean = title.strip() or "(no title yet)"
    location_clean = location.strip() or None
    remote_type_clean = work_arrangement.strip() or None
    job_type_clean = job_type.strip() or None
    reference_id_clean = reference_id.strip() or None
    job_board_clean = job_board.strip() or None
    posted_at_clean = _parse_iso_date(posted_date)
    jd_text_clean = jd_text.strip() or None
    sal_min, sal_max, sal_cur = _parse_salary_range(salary_range)

    with db.connect() as conn:
        before = conn.execute(
            "SELECT jd_text FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if before is None:
            raise HTTPException(status_code=404)
        jd_changed = (before["jd_text"] or "") != (jd_text_clean or "")
        conn.execute(
            "UPDATE jobs SET company=?, title=?, url=?, location=?, "
            "remote_type=?, job_type=?, "
            "salary_min=?, salary_max=?, salary_currency=?, "
            "posted_at=?, reference_id=?, job_board=?, "
            "jd_text=? WHERE id = ?",
            (company_clean, title_clean, url_clean, location_clean,
             remote_type_clean, job_type_clean,
             sal_min, sal_max, sal_cur,
             posted_at_clean, reference_id_clean, job_board_clean,
             jd_text_clean, job_id),
        )
        # If the JD changed and is now non-empty, put it back through screening.
        if jd_changed and jd_text_clean:
            conn.execute(
                "UPDATE jobs SET stage='filtered', filter_status='passed', "
                "fit_score=NULL, fit_reason=NULL, ats_score=NULL, "
                "ats_missing_keywords=NULL WHERE id = ?",
                (job_id,),
            )
        conn.commit()

    if jd_changed and jd_text_clean:
        def _screen():
            screen.run(job_ids=[job_id])
        import threading as _th
        _th.Thread(target=_screen, daemon=True).start()
        return RedirectResponse(url=_rp(f"/jobs/{job_id}?edited=1&screening=1"), status_code=303)
    return RedirectResponse(url=_rp(f"/jobs/{job_id}?edited=1"), status_code=303)


@app.post("/jobs/paste/auto-parse")
async def jobs_paste_auto_parse(request: Request):
    """Haiku-based field extraction. Body: JSON `{jd_text: "..."}`.
       Returns JSON `{company, title, location}` — empty strings if not present."""
    if (r := auth.require_login(request)) is not None:
        return r
    import json as _json
    try:
        body = await request.json()
    except _json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    jd_text = (body.get("jd_text") or "").strip()
    if not jd_text:
        return JSONResponse({"error": "jd_text required"}, status_code=400)
    from . import jd_parse  # noqa: E402
    try:
        result = jd_parse.parse(jd_text)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(result)


def _parse_salary_range(s: str) -> tuple[int | None, int | None, str | None]:
    """Parse human-typed salary like '150000-200000 USD' or '$100K - $150K'
    into (min, max, currency). Returns (None, None, None) if unparseable."""
    if not s or not s.strip():
        return None, None, None
    import re as _re
    t = s.upper().replace("$", "").replace(",", "")
    cur_match = _re.search(r"\b(USD|CAD|EUR|GBP|AUD|JPY)\b", t)
    currency = cur_match.group(1) if cur_match else None
    if currency:
        t = t.replace(currency, "")
    nums = _re.findall(r"(\d+(?:\.\d+)?)\s*([KM]?)", t)

    def _to_int(num_str, suffix):
        x = float(num_str)
        if suffix == "K":
            x *= 1_000
        elif suffix == "M":
            x *= 1_000_000
        return int(x)

    if not nums:
        return None, None, currency
    if len(nums) == 1:
        v = _to_int(*nums[0])
        return v, v, currency
    lo = _to_int(*nums[0])
    hi = _to_int(*nums[1])
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi, currency


def _parse_iso_date(s: str) -> str | None:
    """Coerce user-entered date to a SQLite-friendly 'YYYY-MM-DD HH:MM:SS' if
    parseable, else None. Accepts 'YYYY-MM-DD' (most common from Haiku)."""
    import re as _re
    s = (s or "").strip()
    if not s:
        return None
    m = _re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return f"{m.group(1)} 00:00:00" if m else None


@app.post("/jobs/paste")
def jobs_paste_submit(
    request: Request,
    url: str = Form(...),
    company: str = Form(""),
    title: str = Form(""),
    location: str = Form(""),
    work_arrangement: str = Form(""),
    job_type: str = Form(""),
    salary_range: str = Form(""),
    posted_date: str = Form(""),
    reference_id: str = Form(""),
    job_board: str = Form(""),
    jd_text: str = Form(""),
):
    if (r := auth.require_login(request)) is not None:
        return r
    import hashlib
    url_clean = url.strip()
    if not url_clean:
        raise HTTPException(status_code=400, detail="URL is required")
    eid = "manual-" + hashlib.sha256(url_clean.encode("utf-8")).hexdigest()[:32]

    company_clean = company.strip() or "(unknown)"
    title_clean = title.strip() or "(no title yet)"
    location_clean = location.strip() or None
    remote_type_clean = work_arrangement.strip() or None
    job_type_clean = job_type.strip() or None
    reference_id_clean = reference_id.strip() or None
    job_board_clean = job_board.strip() or None
    posted_at_clean = _parse_iso_date(posted_date)
    jd_text_clean = jd_text.strip() or None
    sal_min, sal_max, sal_cur = _parse_salary_range(salary_range)

    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id, jd_text FROM jobs WHERE source='manual' AND external_id=?",
            (eid,),
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO jobs (source, external_id, company, title, location, "
                "remote_type, job_type, salary_min, salary_max, salary_currency, "
                "posted_at, reference_id, job_board, "
                "url, jd_text, stage, filter_status) "
                "VALUES ('manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'filtered', 'passed')",
                (eid, company_clean, title_clean, location_clean,
                 remote_type_clean, job_type_clean,
                 sal_min, sal_max, sal_cur,
                 posted_at_clean, reference_id_clean, job_board_clean,
                 url_clean, jd_text_clean),
            )
            job_id = cur.lastrowid
            had_jd_before = False
        else:
            job_id = existing["id"]
            had_jd_before = bool((existing["jd_text"] or "").strip())
            conn.execute(
                "UPDATE jobs SET "
                "  company         = CASE WHEN ? != '(unknown)'      THEN ? ELSE company END, "
                "  title           = CASE WHEN ? != '(no title yet)' THEN ? ELSE title   END, "
                "  location        = COALESCE(?, location), "
                "  remote_type     = COALESCE(?, remote_type), "
                "  job_type        = COALESCE(?, job_type), "
                "  salary_min      = COALESCE(?, salary_min), "
                "  salary_max      = COALESCE(?, salary_max), "
                "  salary_currency = COALESCE(?, salary_currency), "
                "  posted_at       = COALESCE(?, posted_at), "
                "  reference_id    = COALESCE(?, reference_id), "
                "  job_board       = COALESCE(?, job_board), "
                "  jd_text         = COALESCE(?, jd_text), "
                "  filter_status='passed', stage='filtered', deleted_at=NULL "
                "WHERE id = ?",
                (company_clean, company_clean,
                 title_clean,   title_clean,
                 location_clean, remote_type_clean, job_type_clean,
                 sal_min, sal_max, sal_cur,
                 posted_at_clean, reference_id_clean, job_board_clean,
                 jd_text_clean,
                 job_id),
            )
        conn.commit()

    # Only run Phase 3 screening when a JD is now present and (this is a new
    # row OR a JD was just added). Skipping otherwise saves $ on placeholder
    # URLs that have no JD yet.
    if jd_text_clean and (existing is None or not had_jd_before):
        def _screen():
            screen.run(job_ids=[job_id])
        import threading as _th
        _th.Thread(target=_screen, daemon=True).start()
        return RedirectResponse(url=_rp(f"/jobs?manual_added=1&screening={job_id}"), status_code=303)

    return RedirectResponse(url=_rp(f"/jobs/{job_id}?manual_saved=1"), status_code=303)


@app.post("/jobs/ingest")
async def jobs_ingest(request: Request):
    """Token-protected ingest for the iOS Shortcut / bookmarklet. The client
    sends the already-rendered page text from the user's logged-in browser, so
    bot-blocked sites (LinkedIn, Indeed, etc.) are captured client-side. We clean
    it with Haiku, upsert the job, and screen it. Auth is by INGEST_TOKEN, not
    session (the request comes from a Shortcut, not the browser session)."""
    expected = (os.environ.get("INGEST_TOKEN") or "").strip()
    form = await request.form()
    token = (form.get("token") or request.query_params.get("token") or "").strip()
    if not expected or token != expected:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    url_clean = (form.get("url") or "").strip()
    page_title = (form.get("page_title") or "").strip()
    page_text = (form.get("page_text") or "").strip()
    if not url_clean:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    if len(page_text) < 80:
        return JSONResponse(
            {"ok": False, "error": "no_jd",
             "message": "Couldn't read a job description from this page. Open the JD in Safari (not the app), or paste the text manually."},
            status_code=422)

    from . import jd_parse  # noqa: E402
    try:
        f = jd_parse.clean_page(page_text, url=url_clean, page_title=page_title)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"parse_failed: {e}"}, status_code=500)

    jd_text_clean = (f.get("jd_text") or "").strip() or None
    if not jd_text_clean:
        return JSONResponse(
            {"ok": False, "error": "no_jd",
             "message": "No job description found on that page. Make sure the full JD is visible, then try again."},
            status_code=422)

    import hashlib
    eid = "manual-" + hashlib.sha256(url_clean.encode("utf-8")).hexdigest()[:32]
    company_clean = (f.get("company") or "").strip() or "(unknown)"
    title_clean = (f.get("title") or "").strip() or "(no title yet)"
    posted_at_clean = _parse_iso_date(f.get("posted_date") or "")
    sal_min = f.get("salary_min") or None
    sal_max = f.get("salary_max") or None
    sal_cur = (f.get("salary_currency") or "").strip() or None

    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id, jd_text FROM jobs WHERE source='shortcut' AND external_id=?",
            (eid,),
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO jobs (source, external_id, company, title, location, "
                "remote_type, job_type, salary_min, salary_max, salary_currency, "
                "posted_at, reference_id, job_board, url, jd_text, stage, filter_status) "
                "VALUES ('shortcut', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filtered', 'passed')",
                (eid, company_clean, title_clean, (f.get("location") or "").strip() or None,
                 (f.get("work_arrangement") or "").strip() or None,
                 (f.get("job_type") or "").strip() or None,
                 sal_min, sal_max, sal_cur, posted_at_clean,
                 (f.get("reference_id") or "").strip() or None,
                 (f.get("job_board") or "").strip() or None,
                 url_clean, jd_text_clean),
            )
            job_id = cur.lastrowid
            had_jd_before = False
        else:
            job_id = existing["id"]
            had_jd_before = bool((existing["jd_text"] or "").strip())
            conn.execute(
                "UPDATE jobs SET "
                "  company  = CASE WHEN ? != '(unknown)'      THEN ? ELSE company END, "
                "  title    = CASE WHEN ? != '(no title yet)' THEN ? ELSE title   END, "
                "  location = COALESCE(?, location), jd_text = ?, "
                "  filter_status='passed', stage='filtered', deleted_at=NULL "
                "WHERE id = ?",
                (company_clean, company_clean, title_clean, title_clean,
                 (f.get("location") or "").strip() or None, jd_text_clean, job_id),
            )
        conn.commit()

    # Ingest is a deliberate "score this for me" action, so always (re)screen —
    # whether the row is new or an existing one being refreshed.
    def _screen():
        screen.run(job_ids=[job_id])
    import threading as _th
    _th.Thread(target=_screen, daemon=True).start()

    prefix = request.scope.get("root_path", "")
    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "company": company_clean,
        "title": title_clean,
        "message": f"Added {company_clean} — {title_clean}. Scoring in the background.",
        "view_url": f"{request.base_url.scheme}://{request.url.netloc}{prefix}/jobs/{job_id}",
    })


ALLOWED_AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".aiff", ".flac", ".webm", ".mp4"}


def _save_uploaded_audio(file: UploadFile) -> str:
    """Persist an uploaded audio file to data/audio/ with a uuid name. Returns
    the bare filename (relative to AUDIO_DIR)."""
    audio.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    orig = Path(file.filename or "audio.m4a")
    ext = orig.suffix.lower() or ".m4a"
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(status_code=400,
                            detail=f"unsupported audio extension: {ext}")
    name = f"{uuid.uuid4().hex}{ext}"
    dest = audio.AUDIO_DIR / name
    with open(dest, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            out.write(chunk)
    return name


@app.get("/coaching", response_class=HTMLResponse)
def coaching_list(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        interviews = [dict(r) for r in conn.execute(
            "SELECT i.id, i.audio_path, i.round, i.company, i.interview_date, i.occurred_at, "
            "  (SELECT path FROM coaching_reports cr WHERE cr.interview_id = i.id LIMIT 1) AS report_path, "
            "  (SELECT word_count FROM transcripts t WHERE t.interview_id = i.id LIMIT 1) AS word_count "
            "FROM interviews i ORDER BY i.occurred_at DESC LIMIT 50"
        )]
        practice_rows = [dict(r) for r in conn.execute(
            "SELECT s.id, s.audio_path, s.occurred_at, q.question, "
            "  (SELECT path FROM coaching_reports cr WHERE cr.practice_session_id = s.id LIMIT 1) AS report_path, "
            "  (SELECT word_count FROM transcripts t WHERE t.practice_session_id = s.id LIMIT 1) AS word_count "
            "FROM practice_sessions s JOIN practice_questions q ON q.id = s.question_id "
            "ORDER BY s.occurred_at DESC LIMIT 50"
        )]
    return templates.TemplateResponse(
        "coaching_list.html",
        {"request": request, "interviews": interviews, "practice_rows": practice_rows},
    )


@app.post("/coaching/parse-description")
async def coaching_parse_description(request: Request):
    """Extract {company, interview_date, round} from a free-form description.
       Body: JSON {text} OR multipart {file} (a short audio clip we transcribe).
       Returns JSON of the three fields (empty strings when not stated)."""
    if (r := auth.require_login(request)) is not None:
        return r
    from . import interview_parse  # noqa: E402

    ctype = request.headers.get("content-type", "")
    description = ""
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "filename"):
            # Save to a temp file, transcribe, then remove — we don't keep
            # the description audio.
            tmp_name = _save_uploaded_audio(upload)
            tmp_path = audio.AUDIO_DIR / tmp_name
            try:
                result = audio.transcribe(str(tmp_path))
                description = (result.get("text") or "").strip()
            except Exception as e:
                return JSONResponse({"error": f"transcription failed: {e}"}, status_code=500)
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
        try:
            body = await request.json()
            description = (body.get("text") or "").strip()
        except Exception:
            description = ""

    if not description:
        return JSONResponse({"error": "no description provided"}, status_code=400)
    try:
        meta = interview_parse.parse(description)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    meta["description"] = description  # echo back the transcript for the UI
    return JSONResponse(meta)


def _normalize_interview_date(s: str) -> str | None:
    import re as _re
    s = (s or "").strip()
    m = _re.match(r"^\d{4}-\d{2}-\d{2}$", s)
    return s if m else None


@app.post("/coaching/upload")
async def coaching_upload(
    request: Request,
    file: UploadFile = File(...),
    round_: str = Form("", alias="round"),
    company: str = Form(""),
    interview_date: str = Form(""),
):
    if (r := auth.require_login(request)) is not None:
        return r
    name = _save_uploaded_audio(file)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO interviews (round, company, interview_date, occurred_at, audio_path) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (round_ or None, company.strip() or None,
             _normalize_interview_date(interview_date), name),
        )
        interview_id = cur.lastrowid
        conn.commit()
    process_audio.enqueue("interview", interview_id)
    return RedirectResponse(url=_rp(f"/coaching/interview/{interview_id}"), status_code=303)


@app.post("/coaching/ingest")
async def coaching_ingest(request: Request):
    """Token-protected interview-audio ingest for the iOS Shortcut share sheet.
    Multipart: token + file (audio) + optional description. The audio's filename
    is used as the description (company/date/round auto-filled from it), then we
    transcribe + analyze in the background. Auth by INGEST_TOKEN, not session."""
    expected = (os.environ.get("INGEST_TOKEN") or "").strip()
    form = await request.form()
    token = (form.get("token") or request.query_params.get("token") or "").strip()
    if not expected or token != expected:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"ok": False, "error": "no audio file"}, status_code=400)

    # Description = explicit field if sent, else the audio's filename (no ext).
    description = (form.get("description") or "").strip()
    if not description and upload.filename:
        from pathlib import Path as _Path
        description = _Path(upload.filename).stem.replace("_", " ").strip()

    try:
        name = _save_uploaded_audio(upload)
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": str(e.detail)}, status_code=400)

    company = interview_date = round_ = ""
    if description:
        from . import interview_parse  # noqa: E402
        try:
            meta = interview_parse.parse(description)
            company = (meta.get("company") or "").strip()
            interview_date = (meta.get("interview_date") or "").strip()
            round_ = (meta.get("round") or "").strip()
        except Exception as e:
            print(f"  coaching ingest parse failed: {e}")

    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO interviews (round, company, interview_date, occurred_at, audio_path) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (round_ or None, company or None,
             _normalize_interview_date(interview_date), name),
        )
        interview_id = cur.lastrowid
        conn.commit()
    process_audio.enqueue("interview", interview_id)

    label = " - ".join(x for x in (company, round_, interview_date) if x) or "Interview"
    prefix = request.scope.get("root_path", "")
    return JSONResponse({
        "ok": True,
        "interview_id": interview_id,
        "company": company, "date": interview_date, "round": round_,
        "description_used": description,
        "message": f"Got it: {label}. Transcribing and analyzing in the background. You'll get a Telegram ping when it's done.",
        "view_url": f"{request.base_url.scheme}://{request.url.netloc}{prefix}/coaching/interview/{interview_id}",
    })


@app.post("/coaching/paste")
def coaching_paste(
    request: Request,
    text: str = Form(...),
    round_: str = Form("", alias="round"),
    company: str = Form(""),
    interview_date: str = Form(""),
):
    if (r := auth.require_login(request)) is not None:
        return r
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO interviews (round, company, interview_date, occurred_at, notes) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (round_ or None, company.strip() or None,
             _normalize_interview_date(interview_date), text),
        )
        interview_id = cur.lastrowid
        conn.commit()
    process_audio.enqueue("interview", interview_id)
    return RedirectResponse(url=_rp(f"/coaching/interview/{interview_id}"), status_code=303)


@app.post("/practice/{question_id}/paste")
def practice_paste(
    request: Request,
    question_id: int,
    text: str = Form(...),
):
    if (r := auth.require_login(request)) is not None:
        return r
    if practice.get_question(question_id) is None:
        raise HTTPException(status_code=404)
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO practice_sessions (question_id, notes) VALUES (?, ?)",
            (question_id, text),
        )
        session_id = cur.lastrowid
        conn.commit()
    process_audio.enqueue("practice", session_id)
    return RedirectResponse(url=_rp(f"/coaching/practice/{session_id}"), status_code=303)


@app.get("/coaching/interview/{interview_id}", response_class=HTMLResponse)
def coaching_interview_detail(request: Request, interview_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        interview = conn.execute(
            "SELECT id, audio_path, round, company, interview_date, name, occurred_at "
            "FROM interviews WHERE id = ?",
            (interview_id,),
        ).fetchone()
        if interview is None:
            raise HTTPException(status_code=404)
        transcript = conn.execute(
            "SELECT path, word_count, wpm, filler_count, filler_rate, "
            "talk_ratio, pause_p50, pause_p90, duration_s FROM transcripts "
            "WHERE interview_id = ?",
            (interview_id,),
        ).fetchone()
        report = conn.execute(
            "SELECT path, summary, next_practice, top_patterns_json, "
            "question_matches_json FROM coaching_reports "
            "WHERE interview_id = ?",
            (interview_id,),
        ).fetchone()
    return _render_coaching_detail(
        request,
        kind="interview",
        row=dict(interview),
        transcript=dict(transcript) if transcript else None,
        report=dict(report) if report else None,
        title=_interview_title(dict(interview)),
    )


def _interview_title(row: dict) -> str:
    """Custom name if set, else a label built from company/round/date,
    else 'Interview #N'."""
    if (row.get("name") or "").strip():
        return row["name"].strip()
    parts = [p for p in (row.get("company"), row.get("round"), row.get("interview_date")) if p]
    return " · ".join(parts) if parts else f"Interview #{row['id']}"


@app.post("/coaching/interview/{interview_id}/rename")
def coaching_interview_rename(request: Request, interview_id: int,
                              name: str = Form(""), company: str = Form(""),
                              interview_date: str = Form(""), round: str = Form("")):
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        exists = conn.execute("SELECT 1 FROM interviews WHERE id = ?", (interview_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404)
        conn.execute(
            "UPDATE interviews SET name = ?, company = ?, interview_date = ?, round = ? WHERE id = ?",
            (name.strip() or None, company.strip() or None,
             interview_date.strip() or None, round.strip() or None, interview_id))
        conn.commit()
    return RedirectResponse(url=_rp(f"/coaching/interview/{interview_id}?renamed=1"), status_code=303)


@app.post("/coaching/interview/{interview_id}/delete")
def coaching_interview_delete(request: Request, interview_id: int):
    """Delete an interview and its on-disk artifacts (audio, transcript JSON,
    diarized JSON, coaching report MD)."""
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        iv = conn.execute(
            "SELECT audio_path FROM interviews WHERE id = ?", (interview_id,)
        ).fetchone()
        if iv is None:
            raise HTTPException(status_code=404)
        transcripts = conn.execute(
            "SELECT path FROM transcripts WHERE interview_id = ?", (interview_id,)
        ).fetchall()
        reports = conn.execute(
            "SELECT path FROM coaching_reports WHERE interview_id = ?", (interview_id,)
        ).fetchall()
        # Delete DB rows (children first).
        conn.execute("DELETE FROM coaching_reports WHERE interview_id = ?", (interview_id,))
        conn.execute("DELETE FROM transcripts WHERE interview_id = ?", (interview_id,))
        conn.execute("DELETE FROM api_costs WHERE interview_id = ?", (interview_id,))
        conn.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))
        conn.commit()

    # Remove on-disk files (best-effort; missing files are fine).
    def _rm(p):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    if iv["audio_path"]:
        _rm(audio.AUDIO_DIR / iv["audio_path"])
    for t in transcripts:
        _rm(audio.TRANSCRIPTS_DIR / t["path"])
        # also the sibling diarized file, if any
        stem = Path(t["path"]).stem
        _rm(audio.TRANSCRIPTS_DIR / f"{stem}.diarized.json")
    reports_dir = Path(__file__).resolve().parent.parent / "data" / "coaching-reports"
    for rep in reports:
        _rm(reports_dir / rep["path"])

    return RedirectResponse(url=_rp("/coaching?deleted=1"), status_code=303)


@app.get("/coaching/practice/{session_id}", response_class=HTMLResponse)
def coaching_practice_detail(request: Request, session_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    with db.connect() as conn:
        sess = conn.execute(
            "SELECT s.id, s.audio_path, s.occurred_at, q.question "
            "FROM practice_sessions s JOIN practice_questions q ON q.id = s.question_id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if sess is None:
            raise HTTPException(status_code=404)
        transcript = conn.execute(
            "SELECT path, word_count, wpm, filler_count, filler_rate, "
            "talk_ratio, pause_p50, pause_p90, duration_s FROM transcripts "
            "WHERE practice_session_id = ?",
            (session_id,),
        ).fetchone()
        report = conn.execute(
            "SELECT path, summary, next_practice, top_patterns_json FROM coaching_reports "
            "WHERE practice_session_id = ?",
            (session_id,),
        ).fetchone()
    return _render_coaching_detail(
        request,
        kind="practice",
        row=dict(sess),
        transcript=dict(transcript) if transcript else None,
        report=dict(report) if report else None,
        title=f"Practice session #{session_id}",
    )


def _render_coaching_detail(request, *, kind, row, transcript, report, title):
    import json as _json
    transcript_json = None
    diarized = None
    if transcript:
        path = audio.TRANSCRIPTS_DIR / transcript["path"]
        if path.exists():
            transcript_json = _json.loads(path.read_text(encoding="utf-8"))
        dz_path = path.parent / (path.stem + ".diarized.json")
        if dz_path.exists():
            try:
                diarized = _json.loads(dz_path.read_text(encoding="utf-8"))
            except _json.JSONDecodeError:
                diarized = None
    overall_text = None
    reasoning_text = None
    practice_text = None
    what_worked: list[dict] = []
    top_patterns: list[dict] = []
    q_matches: list[dict] = []
    if report:
        path = (Path(__file__).resolve().parent.parent / "data" /
                "coaching-reports" / report["path"])
        report_md = path.read_text(encoding="utf-8") if path.exists() else ""
        import re as _re

        def _section(heading):
            m = _re.search(
                rf"\n##\s*{_re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)",
                report_md, _re.DOTALL | _re.IGNORECASE,
            )
            return m.group(1).strip() if m else None

        overall_text   = _section("Overall") or (report.get("summary") or "").strip() or None
        reasoning_text = _section("Reasoning style")
        practice_text  = _section("Practice this next") or (report.get("next_practice") or "").strip() or None

        # Parse "What worked" items from the raw markdown (seconds intact).
        ww_block = _section("What worked") or ""
        for m in _re.finditer(
            r'-\s*\*\*\[(\d+(?:\.\d+)?)s\]\*\*\s*"([^"]*)"\s*\n\s*→\s*(.+?)(?=\n-\s*\*\*\[|\Z)',
            ww_block, _re.DOTALL,
        ):
            ts = float(m.group(1))
            what_worked.append({
                "ts_s": ts, "ts_label": _fmt_mmss(ts),
                "quote": m.group(2).strip(),
                "why": " ".join(m.group(3).split()),
            })

        # Top patterns come from structured JSON (reliable) when available.
        import json as _json2
        try:
            patterns_raw = _json2.loads(report.get("top_patterns_json") or "[]")
        except (_json2.JSONDecodeError, TypeError):
            patterns_raw = []
        for p in patterns_raw:
            examples = []
            for ex in (p.get("examples") or []):
                ts = float(ex.get("timestamp_s") or 0)
                examples.append({"ts_s": ts, "ts_label": _fmt_mmss(ts),
                                 "quote": (ex.get("quote") or "").strip()})
            top_patterns.append({
                "name": (p.get("name") or "").strip(),
                "examples": examples,
                "fix": (p.get("fix") or "").strip(),
            })

        # Interviewer questions matched to the candidate's practice bank.
        try:
            q_matches = _json.loads(report.get("question_matches_json") or "[]") or []
        except (_json.JSONDecodeError, TypeError):
            q_matches = []
    # Attach a start time to each diarized turn so we can show + scrub to it.
    if diarized and diarized.get("turns"):
        turns = diarized["turns"]
        segments = (transcript_json or {}).get("segments") or []
        if segments:
            # ACCURATE: the raw transcript has real per-segment timestamps.
            # Map each turn to the real time by cumulative word position —
            # the diarized text is the same words, just re-split by speaker.
            seg_marks = []  # (cumulative_word_index_at_seg_start, seg_start_time)
            cum = 0
            for s in segments:
                seg_marks.append((cum, float(s.get("start") or 0)))
                cum += len((s.get("text") or "").split())
            turn_cum = 0
            for t in turns:
                start_time = 0.0
                for wc_start, st in seg_marks:
                    if wc_start <= turn_cum:
                        start_time = st
                    else:
                        break
                t["start_s"] = round(start_time, 1)
                turn_cum += len((t.get("text") or "").split())
            diarized["ts_accurate"] = True
        elif transcript and transcript.get("duration_s"):
            # FALLBACK: no real segments — estimate from word position.
            dur = float(transcript["duration_s"])
            wcs = [len((t.get("text") or "").split()) for t in turns]
            total = sum(wcs) or 1
            cum = 0
            for t, wc in zip(turns, wcs):
                t["start_s"] = round(cum / total * dur, 1)
                cum += wc
            diarized["ts_accurate"] = False

        # Attach each coaching note to the turn it happened in, so the
        # transcript can show a collapsible "coach note" at that spot.
        annotations = []
        for w in what_worked:
            annotations.append({"ts_s": w["ts_s"], "kind": "worked",
                                "title": "What worked", "quote": w["quote"],
                                "note": w["why"]})
        for p in top_patterns:
            for ex in p["examples"]:
                annotations.append({"ts_s": ex["ts_s"], "kind": "pattern",
                                    "title": p["name"], "quote": ex["quote"],
                                    "note": p["fix"]})
        if annotations:
            starts = [t.get("start_s") for t in turns]
            for ann in annotations:
                idx = 0
                for i, st in enumerate(starts):
                    if st is not None and st <= ann["ts_s"] + 0.5:
                        idx = i
                    else:
                        break
                turns[idx].setdefault("notes", []).append(ann)

        # Link interviewer turns to the practice question they match.
        for m in q_matches:
            ti = m.get("turn_index")
            if ti is None or not (0 <= ti < len(turns)):
                continue
            q = practice.get_question(m.get("practice_question_id"))
            if not q:
                continue
            turns[ti]["q_match"] = {
                "intent": m.get("intent") or "",
                "question": q["question"],
                "qid": q["id"],
                "core_qid": q.get("core_id") or q["id"],
            }

    status = "ready" if report else ("coaching" if transcript else "transcribing")
    return templates.TemplateResponse(
        "coaching_detail.html",
        {
            "request": request,
            "title": title,
            "kind": kind,
            "row": row,
            "status": status,
            "transcript": transcript,
            "transcript_json": transcript_json,
            "diarized": diarized,
            "report": report,
            "overall_text": overall_text,
            "reasoning_text": reasoning_text,
            "practice_text": practice_text,
            "what_worked": what_worked,
            "top_patterns": top_patterns,
            "audio_url": _rp(f"/audio/{row['audio_path']}"),
        },
    )


@app.get("/audio/{filename}")
def serve_audio(request: Request, filename: str):
    if (r := auth.require_login(request)) is not None:
        return r
    # Disallow path traversal
    safe = Path(filename).name
    path = audio.AUDIO_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/practice", response_class=HTMLResponse)
def practice_list(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    return templates.TemplateResponse(
        "practice_list.html",
        {"request": request, "cores": practice.list_cores()},
    )


@app.get("/practice/{question_id}", response_class=HTMLResponse)
def practice_question_detail(request: Request, question_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    q = practice.get_question(question_id)
    if q is None:
        raise HTTPException(status_code=404)
    # Variants aren't practiced directly — send to the core they belong to.
    if q.get("core_id"):
        return RedirectResponse(url=_rp(f"/practice/{q['core_id']}"), status_code=303)
    sessions = practice.list_sessions(question_id=question_id)
    asked_in = practice.interview_asks_for_core(question_id)
    return templates.TemplateResponse(
        "practice_question.html",
        {"request": request, "question": q, "sessions": sessions, "asked_in": asked_in},
    )


@app.post("/practice/{question_id}/record")
async def practice_record(
    request: Request,
    question_id: int,
    file: UploadFile = File(...),
):
    if (r := auth.require_login(request)) is not None:
        return r
    if practice.get_question(question_id) is None:
        raise HTTPException(status_code=404)
    name = _save_uploaded_audio(file)
    session_id = practice.create_session(question_id, name)
    process_audio.enqueue("practice", session_id)
    return RedirectResponse(
        url=_rp(f"/coaching/practice/{session_id}"), status_code=303
    )


@app.post("/practice/{question_id}/standard-answer")
def practice_save_standard_answer(
    request: Request,
    question_id: int,
    standard_answer: str = Form(""),
):
    if (r := auth.require_login(request)) is not None:
        return r
    if practice.get_question(question_id) is None:
        raise HTTPException(status_code=404)
    practice.set_standard_answer(question_id, standard_answer)
    return RedirectResponse(url=_rp(f"/practice/{question_id}"), status_code=303)


@app.post("/practice/{question_id}/standard-answer/generate")
def practice_generate_standard_answer(request: Request, question_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    q = practice.get_question(question_id)
    if q is None:
        raise HTTPException(status_code=404)
    try:
        answer = answer_gen.generate_answer(q["question"])
    except Exception as e:
        print(f"  answer generation failed: {e}")
        return RedirectResponse(url=_rp(f"/practice/{question_id}?gen=error"), status_code=303)
    if answer:
        practice.set_standard_answer(question_id, answer)
    return RedirectResponse(url=_rp(f"/practice/{question_id}"), status_code=303)


@app.post("/practice/{question_id}/keywords")
def practice_save_keywords(
    request: Request,
    question_id: int,
    keywords: str = Form(""),
):
    if (r := auth.require_login(request)) is not None:
        return r
    if practice.get_question(question_id) is None:
        raise HTTPException(status_code=404)
    # Normalise: one keyword per line, drop blanks, preserve order.
    cleaned = "\n".join(
        line.strip() for line in keywords.splitlines() if line.strip()
    )
    practice.set_keywords(question_id, cleaned)
    return RedirectResponse(url=_rp(f"/practice/{question_id}"), status_code=303)


@app.post("/practice/{question_id}/keywords/generate")
def practice_generate_keywords(request: Request, question_id: int):
    if (r := auth.require_login(request)) is not None:
        return r
    q = practice.get_question(question_id)
    if q is None:
        raise HTTPException(status_code=404)
    answer = (q.get("standard_answer") or "").strip()
    if not answer:
        return RedirectResponse(url=_rp(f"/practice/{question_id}?kw=noanswer"), status_code=303)
    try:
        kws = answer_gen.generate_keywords(answer)
    except Exception as e:
        print(f"  keyword generation failed: {e}")
        return RedirectResponse(url=_rp(f"/practice/{question_id}?kw=error"), status_code=303)
    practice.set_keywords(question_id, "\n".join(kws))
    return RedirectResponse(url=_rp(f"/practice/{question_id}"), status_code=303)


@app.get("/health", response_class=HTMLResponse)
def health_view(request: Request):
    if (r := auth.require_login(request)) is not None:
        return r
    checks = health.run_all()
    overall = "ok" if all(c.status == "ok" for c in checks) else (
        "fail" if any(c.status == "fail" for c in checks) else "warn"
    )
    with db.connect() as conn:
        daily = [dict(r) for r in conn.execute(
            "SELECT date(ran_at) AS d, "
            "SUM(CASE WHEN source='discovery' THEN new_jobs ELSE 0 END) AS disc_new, "
            "SUM(CASE WHEN source='discovery' THEN new_passed ELSE 0 END) AS disc_passed, "
            "SUM(CASE WHEN source='serpapi' THEN new_jobs ELSE 0 END) AS serp_new, "
            "SUM(CASE WHEN source='serpapi' THEN new_passed ELSE 0 END) AS serp_passed "
            "FROM discovery_runs GROUP BY d ORDER BY d DESC LIMIT 21"
        ).fetchall()]
        for _r in daily:
            try:
                _r["dow"] = datetime.strptime(_r["d"], "%Y-%m-%d").strftime("%a")
            except (ValueError, TypeError):
                _r["dow"] = ""
        wd_rows = conn.execute(
            "SELECT strftime('%w', ran_at) AS wd, SUM(new_passed) AS tp, "
            "COUNT(DISTINCT date(ran_at)) AS days FROM discovery_runs GROUP BY wd"
        ).fetchall()
    _names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    _order = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    weekday = []
    for r in wd_rows:
        days = r["days"] or 1
        weekday.append({"name": _names[int(r["wd"])],
                        "avg_passed": round((r["tp"] or 0) / days, 1),
                        "total_passed": r["tp"] or 0})
    weekday.sort(key=lambda x: _order[x["name"]])
    return templates.TemplateResponse(
        "health.html",
        {"request": request, "checks": checks, "overall": overall,
         "daily": daily, "weekday": weekday},
    )
