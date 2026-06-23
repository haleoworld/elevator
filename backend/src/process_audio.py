"""Background pipeline: transcribe → metrics → coaching report.

Called from the dashboard upload handler in a daemon thread, or from the CLI
for re-processing (`python -m src.process_audio --interview-id N`).
"""
from __future__ import annotations

import argparse
import os
import queue as _queue
import sys
import threading as _threading
import traceback
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

from . import audio, coaching, db  # noqa: E402


# ── Serialized processing queue ──────────────────────────────────────────────
# Upload handlers call enqueue() and return immediately. A single daemon worker
# drains the queue one job at a time, so concurrent uploads queue up instead of
# colliding on the SQLite write lock or thrashing CPU with parallel Whisper runs.
_job_queue: "_queue.Queue[tuple[str, int]]" = _queue.Queue()
_worker_lock = _threading.Lock()
_worker_started = False


def _worker_loop() -> None:
    while True:
        kind, obj_id = _job_queue.get()
        try:
            if kind == "interview":
                process_interview(obj_id)
            elif kind == "practice":
                process_practice(obj_id)
        except Exception as e:  # noqa: BLE001 — keep the worker alive
            print(f"[worker] {kind}:{obj_id} crashed: {e}", file=sys.stderr)
        finally:
            _job_queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            _threading.Thread(target=_worker_loop, daemon=True,
                              name="audio-worker").start()
            _worker_started = True


def enqueue(kind: str, obj_id: int) -> None:
    """Queue an interview/practice job for the single background worker."""
    _ensure_worker()
    _job_queue.put((kind, obj_id))
    print(f"[queue] enqueued {kind}:{obj_id} (depth={_job_queue.qsize()})")


def resume_unprocessed() -> int:
    """Re-queue any recording that has input (audio or text) but no transcript.
    Called at startup so jobs interrupted by a restart finish on their own
    instead of hanging in 'processing' forever."""
    n = 0
    with db.connect() as conn:
        for r in conn.execute(
            "SELECT id FROM interviews i "
            "WHERE (i.audio_path IS NOT NULL OR i.notes IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.interview_id = i.id)"
        ).fetchall():
            enqueue("interview", r["id"]); n += 1
        for r in conn.execute(
            "SELECT id FROM practice_sessions s "
            "WHERE (s.audio_path IS NOT NULL OR s.notes IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.practice_session_id = s.id)"
        ).fetchall():
            enqueue("practice", r["id"]); n += 1
    if n:
        print(f"[resume] re-queued {n} unprocessed recording(s) at startup")
    return n


def _notify_done(context: dict, interview_id, practice_session_id, *, ok: bool) -> None:
    base = os.environ.get("ELEVATOR_DASHBOARD_URL", "http://localhost:8742").rstrip("/")
    if interview_id is not None:
        bits = [b for b in (context.get("company"), context.get("round")) if b]
        title = " — ".join(bits) if bits else f"Interview #{interview_id}"
        url = f"{base}/coaching/interview/{interview_id}"
    else:
        q = (context.get("question") or "").strip()
        title = (q[:57] + "…") if len(q) > 58 else (q or f"Practice #{practice_session_id}")
        url = f"{base}/coaching/practice/{practice_session_id}"
    try:
        from . import notifier
        notifier.notify_processing_done(title, url, ok=ok)
    except Exception as e:  # noqa: BLE001
        print(f"[notify] failed (non-fatal): {e}")


def _dedup_target(interview_id, practice_session_id) -> None:
    """Keep only the newest transcript + report for this interview/session.
    Runs after a successful pipeline so re-processing replaces a prior run
    instead of accumulating duplicate rows."""
    with db.connect() as conn:
        if interview_id is not None:
            col, val = "interview_id", interview_id
        elif practice_session_id is not None:
            col, val = "practice_session_id", practice_session_id
        else:
            return
        for table in ("coaching_reports", "transcripts"):
            conn.execute(
                f"DELETE FROM {table} WHERE {col} = ? AND id < "
                f"(SELECT MAX(id) FROM {table} WHERE {col} = ?)",
                (val, val),
            )
        conn.commit()


def process_interview(interview_id: int) -> None:
    """Full pipeline for an interview row. Audio if audio_path set, else text."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT i.id, i.audio_path, i.round, i.notes, j.company "
            "FROM interviews i LEFT JOIN applications a ON a.id = i.application_id "
            "LEFT JOIN jobs j ON j.id = a.job_id WHERE i.id = ?",
            (interview_id,),
        ).fetchone()
    if row is None:
        print(f"interview {interview_id} not found", file=sys.stderr)
        return
    context = {
        "kind": "interview",
        "company": row["company"] or "",
        "round": row["round"] or "",
    }
    if row["audio_path"]:
        _run_pipeline(
            source_name=row["audio_path"],
            audio_path=audio.AUDIO_DIR / row["audio_path"],
            text=None,
            context=context,
            interview_id=interview_id, practice_session_id=None,
        )
    elif row["notes"]:
        _run_pipeline(
            source_name=f"interview-{interview_id}-text",
            audio_path=None,
            text=row["notes"],
            context=context,
            interview_id=interview_id, practice_session_id=None,
        )
    else:
        print(f"interview {interview_id} has neither audio nor text", file=sys.stderr)


def process_practice(session_id: int) -> None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.id, s.audio_path, s.notes, q.question "
            "FROM practice_sessions s JOIN practice_questions q ON q.id = s.question_id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        print(f"practice session {session_id} not found", file=sys.stderr)
        return
    context = {"kind": "practice", "question": row["question"]}
    if row["audio_path"]:
        _run_pipeline(
            source_name=row["audio_path"],
            audio_path=audio.AUDIO_DIR / row["audio_path"],
            text=None,
            context=context,
            interview_id=None, practice_session_id=session_id,
        )
    elif row["notes"]:
        _run_pipeline(
            source_name=f"practice-{session_id}-text",
            audio_path=None,
            text=row["notes"],
            context=context,
            interview_id=None, practice_session_id=session_id,
        )
    else:
        print(f"practice session {session_id} has neither audio nor text", file=sys.stderr)


def _run_pipeline(
    *,
    source_name: str,
    audio_path: Path | None,
    text: str | None,
    context: dict,
    interview_id: int | None,
    practice_session_id: int | None,
) -> None:
    try:
        if audio_path is not None:
            print(f"[transcribe] {source_name}")
            result = audio.transcribe(str(audio_path))
            print(f"[transcribe] {len(result.get('text', ''))} chars, "
                  f"{len(result.get('segments', []))} segments")
        else:
            print(f"[text] using pasted text ({len(text or '')} chars)")
            result = {"text": text or "", "segments": [], "language": "en"}

        transcript_path = audio.save_transcript(audio_filename=source_name, result=result)
        metrics = audio.mechanical_metrics(result)
        print(f"[metrics] {metrics}")

        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO transcripts (interview_id, practice_session_id, path, "
                "word_count, wpm, filler_count, filler_rate, talk_ratio, "
                "pause_p50, pause_p90, duration_s) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (interview_id, practice_session_id, transcript_path.name,
                 metrics["word_count"], metrics["wpm"],
                 metrics["filler_count"], metrics["filler_rate"],
                 metrics["talk_ratio"], metrics["pause_p50"], metrics["pause_p90"],
                 metrics.get("duration_s")),
            )
            transcript_id = cur.lastrowid
            conn.commit()

        # Speaker diarization (interview only). Always run it — the diarized
        # speaker turns drive the transcript's speaker badges, inline coach
        # notes, and question-match cards. Real per-segment timestamps (when
        # present) are aligned to the turns at render time for accurate scrub.
        if interview_id is not None and result.get("text"):
            print("[diarize] calling Sonnet to split into speaker turns")
            try:
                turns, dz_usage = coaching.diarize_transcript(
                    result["text"],
                    context_hint=(
                        f"This is an interview recording. Round: "
                        f"{context.get('round', 'unknown')}."
                    ),
                )
                if turns:
                    out = coaching.save_diarized(transcript_path=transcript_path, turns=turns)
                    print(f"[diarize] {len(turns)} turns saved to {out.name}")
                    dz_cost = (dz_usage["input_tokens"] * 3 + dz_usage["output_tokens"] * 15) / 1_000_000
                    coaching.log_cost(operation="diarize", interview_id=interview_id,
                                       usage=dz_usage, cost=dz_cost)
            except Exception as e:
                print(f"[diarize] failed (non-fatal): {e}")

        print("[coaching] calling Sonnet")
        synth = coaching.synthesize(
            transcript_text=result.get("text", ""),
            segments=result.get("segments", []),
            metrics=metrics,
            context=context,
        )
        if synth is None:
            print("[coaching] FAILED — no report written")
            return
        report, cost, usage = synth
        markdown = coaching.render_markdown(report, context, metrics)
        report_path = coaching.save_report(audio_filename=source_name, markdown=markdown)

        with db.connect() as conn:
            conn.execute(
                "INSERT INTO coaching_reports (interview_id, practice_session_id, "
                "transcript_id, path, summary, top_patterns_json, next_practice) "
                "VALUES (?,?,?,?,?,?,?)",
                (interview_id, practice_session_id, transcript_id,
                 report_path.name, report.get("overall_summary", ""),
                 _json_dumps(report.get("top_patterns", [])),
                 report.get("next_practice", "")),
            )
            conn.commit()
        coaching.log_cost(operation="coaching", interview_id=interview_id,
                          usage=usage, cost=cost)

        # Match the interviewer's questions to the candidate's practice bank so the
        # transcript can link each probe to a prepared question.
        if interview_id is not None:
            try:
                import json as _json
                from . import practice
                dz_file = transcript_path.parent / (transcript_path.stem + ".diarized.json")
                turns = []
                if dz_file.exists():
                    turns = _json.loads(dz_file.read_text(encoding="utf-8")).get("turns") or []
                if turns:
                    qs = practice.list_questions()
                    qmatches, qm_usage = coaching.match_interview_questions(turns, qs)
                    with db.connect() as conn:
                        conn.execute(
                            "UPDATE coaching_reports SET question_matches_json = ? "
                            "WHERE interview_id = ?",
                            (_json_dumps(qmatches), interview_id),
                        )
                        conn.commit()
                    qm_cost = (qm_usage["input_tokens"] * 3 + qm_usage["output_tokens"] * 15) / 1_000_000
                    coaching.log_cost(operation="question_match", interview_id=interview_id,
                                      usage=qm_usage, cost=qm_cost)
                    print(f"[qmatch] {len(qmatches)} interviewer questions matched to practice bank")
            except Exception as e:
                print(f"[qmatch] failed (non-fatal): {e}")

        _dedup_target(interview_id, practice_session_id)
        print(f"[done] report saved to {report_path.name} (cost ${cost:.4f})")
        _notify_done(context, interview_id, practice_session_id, ok=True)
    except Exception as e:
        print(f"[ERROR] {e}\n{traceback.format_exc()}", file=sys.stderr)
        _notify_done(context, interview_id, practice_session_id, ok=False)


def _json_dumps(x) -> str:
    import json
    return json.dumps(x)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--interview-id", type=int)
    g.add_argument("--practice-session-id", type=int)
    args = ap.parse_args()
    if args.interview_id:
        process_interview(args.interview_id)
    else:
        process_practice(args.practice_session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
