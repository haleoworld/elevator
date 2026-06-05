"""Local Whisper transcription + mechanical metrics.

Uses `mlx-whisper` (medium model) running on Apple Silicon's Neural Engine.
Audio never leaves the Mac mini — Anthropic only ever sees the transcript text.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

WHISPER_MODEL = "mlx-community/whisper-medium-mlx"

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = REPO_ROOT / "data" / "audio"
TRANSCRIPTS_DIR = REPO_ROOT / "data" / "transcripts"

FILLER_RE = re.compile(
    r"\b(um+|uh+|er+|ah+|like|you know|kind of|sort of|i mean|basically|"
    r"actually|literally|right\?|so\.\.\.)\b",
    re.IGNORECASE,
)


def transcribe(audio_path: str) -> dict[str, Any]:
    """Transcribe a single audio file. Returns dict with 'text' and 'segments'."""
    import mlx_whisper  # lazy: ~3s import cost paid only when needed
    return mlx_whisper.transcribe(audio_path, path_or_hf_repo=WHISPER_MODEL)


def mechanical_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Compute free metrics from a transcription result.

    All values are derived from the transcript + segment timings.
    """
    text = (result.get("text") or "").strip()
    segments = result.get("segments") or []
    words = text.split()
    word_count = len(words)

    if segments:
        duration_s = float(segments[-1].get("end") or 0)
    else:
        duration_s = 0.0  # text-paste path: no timing data

    wpm = (word_count / (duration_s / 60.0)) if duration_s > 0 else 0.0

    filler_matches = FILLER_RE.findall(text)
    filler_count = len(filler_matches)
    filler_rate = (filler_count / word_count) if word_count else 0.0

    pauses: list[float] = []
    for prev, curr in zip(segments, segments[1:]):
        gap = float(curr.get("start") or 0) - float(prev.get("end") or 0)
        if gap > 0.3:
            pauses.append(gap)
    pause_p50 = float(statistics.median(pauses)) if pauses else 0.0
    pause_p90 = (
        float(statistics.quantiles(pauses, n=10)[-1])
        if len(pauses) >= 10
        else (max(pauses) if pauses else 0.0)
    )

    speech_time = sum(
        float(s.get("end") or 0) - float(s.get("start") or 0) for s in segments
    )
    talk_ratio = (speech_time / duration_s) if duration_s > 0 else 1.0

    return {
        "word_count": word_count,
        "duration_s": round(duration_s, 2),
        "wpm": round(wpm, 1),
        "filler_count": filler_count,
        "filler_rate": round(filler_rate, 4),
        "talk_ratio": round(talk_ratio, 3),
        "pause_p50": round(pause_p50, 2),
        "pause_p90": round(pause_p90, 2),
    }


def save_transcript(
    *,
    audio_filename: str,
    result: dict[str, Any],
) -> Path:
    """Write the transcript (text + segments JSON) to data/transcripts/.
    Returns the path (relative to TRANSCRIPTS_DIR root) of the JSON file."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(audio_filename).stem
    path = TRANSCRIPTS_DIR / f"{stem}.json"
    payload = {
        "text": result.get("text", ""),
        "segments": [
            {
                "start": float(s.get("start") or 0),
                "end": float(s.get("end") or 0),
                "text": (s.get("text") or "").strip(),
            }
            for s in (result.get("segments") or [])
        ],
        "language": result.get("language"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_transcript(transcript_path: str) -> dict[str, Any]:
    p = Path(transcript_path)
    if not p.is_absolute():
        p = TRANSCRIPTS_DIR / p
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)
