"""Telegram bot — register a chat once, then send batch-ready pings to it.

Setup (one-time, done from the dashboard):
  1. Find your bot in Telegram (the username @BotFather gave you at setup).
  2. Send /start to it.
  3. Open /telegram/register on the dashboard — we poll getUpdates and store
     the first chat_id we see in app_meta.
"""
from __future__ import annotations

import os

import httpx

from . import db

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")
    return t


def get_chat_id() -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'telegram_chat_id'"
        ).fetchone()
        return row["value"] if row else None


def _set_chat_id(chat_id: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES ('telegram_chat_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = CURRENT_TIMESTAMP",
            (chat_id,),
        )
        conn.commit()


def register_from_updates() -> tuple[str | None, str]:
    """Poll getUpdates, take the first chat_id we find, store it. Returns
    (chat_id, status_message)."""
    try:
        resp = httpx.get(
            TELEGRAM_API.format(token=_token(), method="getUpdates"),
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return None, f"Telegram API error: {e}"
    data = resp.json()
    if not data.get("ok"):
        return None, f"Telegram returned not-ok: {data}"
    updates = data.get("result") or []
    if not updates:
        return None, (
            "No updates yet. Open Telegram, find your bot, and send /start, "
            "then click Register again."
        )
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id") or "")
        if cid:
            _set_chat_id(cid)
            who = chat.get("username") or chat.get("first_name") or "(unknown)"
            return cid, f"Registered chat with {who} (id {cid})."
    return None, "Got updates but no chat_id in any of them. Try /start in Telegram first."


def send_message(text: str, *, parse_mode: str = "Markdown") -> tuple[bool, str]:
    chat_id = get_chat_id()
    if not chat_id:
        return False, "no chat registered; visit /telegram/register first"
    try:
        resp = httpx.post(
            TELEGRAM_API.format(token=_token(), method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                  "disable_web_page_preview": True},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return False, f"Telegram send error: {e}"
    return True, "sent"


def _log(channel: str, event: str, payload: str, success: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO notifications (channel, event, payload, success) VALUES (?,?,?,?)",
            (channel, event, payload, 1 if success else 0),
        )
        conn.commit()


def notify_filter_pass(passed_count: int, dashboard_url: str) -> tuple[bool, str]:
    """Cheap nightly nudge: N jobs cleared the filters and await review.
    No LLM involved. Skips sending on empty nights."""
    if passed_count < 1:
        return False, "nothing passed filters; skipping nudge"
    plural = "s" if passed_count != 1 else ""
    verb = "are" if passed_count != 1 else "is"
    text = (
        f"*{passed_count} new job{plural} passed filters* and {verb} waiting for review.\n\n"
        f"Review: {dashboard_url}"
    )
    ok, status = send_message(text)
    _log("telegram", "filter_pass", str(passed_count), ok)
    return ok, status


def notify_serpapi(count: int, dashboard_url: str) -> tuple[bool, str]:
    """Separate nightly ping for the broad (SerpAPI) discovery run."""
    if count < 1:
        return False, "no new SerpAPI jobs; skipping"
    plural = "s" if count != 1 else ""
    text = (
        f"🔎 *{count} new job{plural} from broad search (SerpAPI)* in the Passed tab.\n\n"
        f"Review: {dashboard_url}"
    )
    ok, status = send_message(text)
    _log("telegram", "serpapi", str(count), ok)
    return ok, status


def notify_processing_done(title: str, dashboard_url: str, *, ok: bool = True) -> tuple[bool, str]:
    """Ping when an interview/practice recording finishes (or fails) processing."""
    if ok:
        text = f"✅ Done processing: *{title}*\n\nView the report: {dashboard_url}"
    else:
        text = (
            f"⚠️ Processing *failed* for: *{title}*\n\n"
            f"It won't finish on its own — re-upload or reprocess. {dashboard_url}"
        )
    sent, status = send_message(text)
    _log("telegram", "processing_done" if ok else "processing_failed", title, sent)
    return sent, status


def notify_batch_ready(batch_id: int, jobs: list[dict], dashboard_url: str) -> tuple[bool, str]:
    """Send a Markdown summary of the ready batch."""
    if not jobs:
        return False, "no jobs in batch"
    lines = [f"*Batch #{batch_id} ready* — {len(jobs)} jobs drafted.\n"]
    for j in jobs[:5]:
        fit = j.get("fit_score") or 0
        lines.append(f"  • {fit:>3} — {j['company']}: {j['title'][:60]}")
    if len(jobs) > 5:
        lines.append(f"  • ...+{len(jobs) - 5} more")
    lines.append(f"\nReview: {dashboard_url}")
    ok, status = send_message("\n".join(lines))
    if ok:
        from . import batches as _batches  # local import to avoid cycle
        _batches.mark_notified(batch_id)
    return ok, status
