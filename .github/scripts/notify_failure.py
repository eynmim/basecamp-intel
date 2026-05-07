"""Post a brief failure alert to the same Telegram channel.

Invoked from the workflow's `if: failure()` step. Keeps the message
short on purpose — the link goes back to the GitHub Actions run for
the actual error log.
"""

from __future__ import annotations

import os
import sys

import requests


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    run_url = os.environ.get("RUN_URL", "").strip() or "<no run URL>"
    report_file = os.environ.get("REPORT_FILE", "").strip() or "<unknown>"

    if not token or not chat_id:
        print("notify_failure: missing token or chat_id; nothing to post.", file=sys.stderr)
        return

    text = (
        "⚠️ <b>BASECAMP INTEL pipeline FAILED</b>\n"
        f"Report: <code>{report_file}</code>\n"
        f'<a href="{run_url}">View run logs →</a>'
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        print(f"notify_failure: HTTP {resp.status_code} — {resp.text[:200]}")
    except requests.RequestException as exc:
        print(f"notify_failure: send failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
