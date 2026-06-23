# Elevator — backend

Personal job-search automation portal. A FastAPI + SQLite app that runs the whole
pipeline: discover jobs, filter and LLM-screen them against your criteria, batch the
best ones, generate tailored application content, and coach your interview and
practice recordings. Single-user, self-hosted, and usable from a phone over a
private network (Tailscale).

> Privacy: all personal data — profile, jobs DB, audio, transcripts, reports — stays
> local and is gitignored. Audio never leaves the machine; only transcript text and
> JD text are sent to the LLM.

## What it does

**Job pipeline**
- **Discovery** — nightly scrape of direct ATS boards (Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday) for a curated company list, plus an Adzuna fallback, plus
  a broad SerpAPI (Google Jobs) keyword sweep.
- **Filter** — rules pass for role / level / location / recency, with
  recruiting-agency and aggregator blocklists and URL de-duplication.
- **Screen** — Haiku parses the JD and scores fit + ATS match across 8 perspectives,
  grounded in your profile.
- **Batch** — bundle scored jobs, move/filter them by batch, and generate cover
  letters + "why this company" content.
- **Manual intake** — paste a JD, or send a JD from the iOS share sheet; it's parsed
  and scored automatically.

**Interview prep**
- **Coaching** — upload or record interview audio; local Whisper transcription +
  diarization, then a Sonnet coaching report.
- **Practice** — question bank (core + variants), standard answers with keyword flash
  cards, and in-browser recording with on-the-go practice.
- **iOS share sheet** — send a JD or an interview recording straight from your phone;
  an audio recording's filename auto-fills company / date / round.

**Bridges & ops**
- **jmc bridge** — push batched jobs into the companion "Phone Screen" app (stored in
  a private GitHub Gist), matched to a batch by name.
- **Telegram** — pings on processing done, discovery results, and batch ready.
- **Scheduled** — discovery (2 AM) and SerpAPI (2:30 AM) on weekday mornings, via
  launchd.

## Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # then fill in the values (see Configuration)
python -m uvicorn src.main:app --host 0.0.0.0 --port 8742
```

Open `http://localhost:8742` and log in with `DASHBOARD_PASSWORD`.

## Configuration

All config is via `.env` (copy from `.env.example`).

- **Required to boot:** `ENCRYPTION_KEY`, `DASHBOARD_PASSWORD`.
- **Required for the pipeline:** `ANTHROPIC_API_KEY`.
- **Optional, per feature:** `ADZUNA_*`, `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`,
  `INGEST_TOKEN` (iOS shortcuts), `JMC_GIST_*` (jmc bridge),
  `ELEVATOR_ROOT_PATH` + `ELEVATOR_DASHBOARD_URL` (deployment behind a path proxy).

Your six profile sections live in `config/profile.md`, and the curated company list
in `config/companies.yaml` — both gitignored and edited locally (see the `*.example`
templates in `config/`).

## Layout

```
src/                FastAPI app, pipeline, and discovery scrapers
src/discovery/      ATS scrapers (greenhouse, lever, ashby, smartrecruiters, workday),
                    adzuna, serpapi, and the discovery runner
src/templates/      Jinja2 templates
src/static/         CSS + in-browser recorder
config/             profile.md + companies.yaml (gitignored) and *.example templates
data/               jobs.db, audio, transcripts, coaching reports, backups (gitignored)
scripts/            One-off maintenance / migration scripts
logs/, models/      Local runtime + Whisper model (gitignored)
```

## Deployment notes

- Served behind **Tailscale Serve** in production; set `ELEVATOR_ROOT_PATH` to the
  path prefix (e.g. `/elevator-portal`) so generated URLs and redirects resolve.
- Nightly discovery + SerpAPI run via **launchd** (macOS), weekday mornings only.
- The `/jobs/ingest` and `/coaching/ingest` endpoints are token-protected
  (`INGEST_TOKEN`) for the iOS Shortcut share-sheet flows.
