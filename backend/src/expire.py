"""Prune job postings that have been removed from their board (expired).

A posting whose source reports it gone — Workday's cxs detail API returning
403/404/410, or a hard 404/410 on other boards — is dropped with the reason
'posting expired/removed' so dead listings stop showing up in the pipeline.

Conservative by design: network hiccups never prune a live posting, and SPA
boards that 200 their shell for removed jobs simply won't be caught here.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import db  # noqa: E402
from .discovery import workday as _workday  # noqa: E402

_HEADERS = {"User-Agent": "elevator/1.0 (personal use)", "Accept": "*/*"}


def is_expired(url: str, *, client: httpx.Client | None = None) -> bool:
    """True only when we're confident the posting is gone."""
    if not url:
        return False
    if "myworkdayjobs.com" in url:
        return _workday.posting_gone(url, client=client)
    owns = client is None
    if owns:
        client = httpx.Client(timeout=10.0, follow_redirects=True)
    try:
        r = client.get(url, headers=_HEADERS)
        return r.status_code in (404, 410)
    except httpx.HTTPError:
        return False
    finally:
        if owns:
            client.close()


def prune(statuses: tuple[str, ...] = ("passed", "scored"),
          limit: int | None = None) -> dict:
    """Check active jobs and drop the ones whose posting is gone."""
    ph = ",".join("?" * len(statuses))
    sql = (
        f"SELECT id, company, url FROM jobs WHERE deleted_at IS NULL "
        f"AND filter_status IN ({ph}) AND url IS NOT NULL AND url != ''"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, statuses).fetchall()]

    stats = {"checked": 0, "expired": 0}
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        for r in rows:
            stats["checked"] += 1
            if is_expired(r["url"], client=client):
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE jobs SET filter_status = 'dropped', "
                        "drop_reason = 'posting expired/removed', stage = 'filtered' "
                        "WHERE id = ?",
                        (r["id"],),
                    )
                    conn.commit()
                stats["expired"] += 1
                print(f"[expire] dropped {r['id']} {r['company']} — {r['url'][:70]}")
    print(f"[expire] checked={stats['checked']} expired={stats['expired']}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Drop jobs whose posting was removed.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-scored", action="store_true",
                    help="also check 'scored' jobs (default: passed + scored)")
    args = ap.parse_args()
    prune(limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
