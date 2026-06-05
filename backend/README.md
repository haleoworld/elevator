# Elevator — backend

Personal job-search automation. See `../proposal.md` for the spec, `../build-plan.md` for the build phases.

This repo is the **Phase 1 shell**: profile editor only, no pipeline yet.

## Quick start

```bash
cd elevator/backend  # or wherever you put it

# 1. Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Fill in .env (two required values — copy from .env.example if missing)
python3 -c "import secrets; print(secrets.token_hex(32))"  # paste into ENCRYPTION_KEY=
# Edit .env, set DASHBOARD_PASSWORD too

# 3. Run
python -m uvicorn src.main:app --host 0.0.0.0 --port 8742
```

Open `http://localhost:8742`, log in with `DASHBOARD_PASSWORD`.

## Layout

```
config/profile.md          Your six profile sections (markdown, edited via the web UI)
data/jobs.db               SQLite — schema initialized on first run
data/audio/                Interview + practice recordings (Phase 5)
data/transcripts/          Whisper output (Phase 5)
data/coaching-reports/     Generated coaching markdown (Phase 5)
data/backups/              Daily encrypted backups (Phase 5)
models/                    whisper.cpp model files (Phase 5)
logs/                      Rotated logs
src/                       Python source
```

## What works right now (Phase 1)

- Password login
- Edit and save the 6 profile sections (criteria, resume, career bio, work history, tone samples, life-pattern)
- System health page

## What doesn't (intentionally)

Everything else. No Adzuna pulls, no LLM calls, no audio, no notifications, no batches. Those land in Window 2 (Phases 2-5).
