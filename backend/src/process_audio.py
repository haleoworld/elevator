"""Background pipeline: transcribe → metrics → coaching report.

Called from the dashboard upload handler in a daemon thread, or from the CLI
for re-processing (`python -m src.process_audio --interview-id N`).
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

from . import audio, coaching, db  # noqa: E402


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

        # Speaker diarization (interview only, when no segment-level timestamps
        # exist — for segment-aware transcripts the per-segment text is enough).
        if interview_id is not None and result.get("text") and not result.get("segments"):
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

        print(f"[done] report saved to {report_path.name} (cost ${cost:.4f})")
    except Exception as e:
        print(f"[ERROR] {e}\n{traceback.format_exc()}", file=sys.stderr)


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
