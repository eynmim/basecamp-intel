"""Replay past reports through the dedup filter without touching Telegram.

Answers the only question that matters before this goes live: how many
messages would Ali actually have received? Sends nothing, writes nothing.

    python .github/scripts/replay_dedup.py                # last 14 days
    python .github/scripts/replay_dedup.py 2026-08-01     # from a date
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "poster", Path(__file__).with_name("post_to_telegram.py")
)
poster = importlib.util.module_from_spec(spec)
sys.modules["poster"] = poster
spec.loader.exec_module(poster)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def main() -> None:
    since = sys.argv[1] if len(sys.argv) > 1 else (
        (dt.date.today() - dt.timedelta(days=14)).isoformat()
    )

    reports = sorted(
        p for p in Path("reports").glob("*.md")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem >= since
    )
    if not reports:
        print(f"No opportunities reports on or after {since}.")
        return

    seen: dict = {}
    before_total = after_total = 0
    fake_message_id = 1000

    print(f"Replaying {len(reports)} report(s) from {reports[0].stem} to {reports[-1].stem}")
    print(f"{'date':<12} {'day':<4} {'mode':<14} {'was':>4} {'now':>4}   breakdown")
    print("-" * 84)

    for path in reports:
        day = dt.date.fromisoformat(path.stem)
        body = path.read_text(encoding="utf-8")
        messages = poster.split_into_messages(body)
        before = len(messages)

        full = day.weekday() == 0  # Monday
        kept, urls, stats = poster.filter_seen(messages, seen, full_refresh=full, today=day)
        after = len(kept)

        # Simulate the ledger writes the real run would perform.
        for idx, url in urls:
            fake_message_id += 1
            record = seen.setdefault(url, {"first_seen": day.isoformat()})
            record.update({
                "deadline": poster.item_deadline(kept[idx], day) or record.get("deadline"),
                "last_sent": day.isoformat(),
                "message_id": fake_message_id,
            })

        before_total += before
        after_total += after
        mode = "FULL REFRESH" if full else "new-only"
        print(f"{path.stem:<12} {WEEKDAYS[day.weekday()]:<4} {mode:<14} "
              f"{before:>4} {after:>4}   "
              f"new={stats['new']} moved={stats['changed']} "
              f"suppressed={stats['suppressed']} sections={stats['sections']}")

    print("-" * 84)
    saved = before_total - after_total
    pct = round(100 * saved / before_total) if before_total else 0
    print(f"{'TOTAL':<12} {'':<4} {'':<14} {before_total:>4} {after_total:>4}   "
          f"{saved} messages suppressed ({pct}%)")
    print(f"\nAverage per day: {before_total / len(reports):.1f} -> "
          f"{after_total / len(reports):.1f} messages")
    print(f"Seen-ledger would hold {len(seen)} opportunities.")


if __name__ == "__main__":
    main()
