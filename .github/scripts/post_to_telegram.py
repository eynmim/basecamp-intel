"""Post a daily intelligence report to a Telegram channel.

Reads the file at $REPORT_FILE, converts markdown -> Telegram HTML if needed,
splits into <=3800-char chunks, and posts via the Bot API. Fails the workflow
on any non-ok response.
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from pathlib import Path

import requests

CHUNK_SIZE = 3800  # Telegram hard limit is 4096; leave headroom for tags.
API_BASE = "https://api.telegram.org"


def die(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def md_to_telegram_html(md: str) -> str:
    """Convert a small subset of markdown to Telegram-supported HTML.

    Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a href>,
    <blockquote>, <tg-spoiler>. Headers are rendered as bold lines.
    """
    text = md

    # Fenced code blocks ```lang\n...\n``` -> <pre><code class="language-...">
    def _fence(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        body = html.escape(match.group(2))
        if lang:
            return f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>'
        return f"<pre>{body}</pre>"

    text = re.sub(r"```([^\n`]*)\n(.*?)```", _fence, text, flags=re.DOTALL)

    # Protect <pre>...</pre> blocks while we transform the rest, so we don't
    # double-escape their contents.
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"\x00PRE{len(placeholders) - 1}\x00"

    text = re.sub(r"<pre>.*?</pre>", _stash, text, flags=re.DOTALL)

    # Escape HTML in the remaining (non-pre) text.
    text = html.escape(text)

    # Inline code `x` -> <code>x</code>  (works on already-escaped text)
    text = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", text)

    # Links [text](url) -> <a href="url">text</a>
    def _link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        # URL was HTML-escaped above; unescape only the &amp; -> & for the href
        # so Telegram receives the real URL.
        href = url.replace("&amp;", "&")
        return f'<a href="{href}">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, text)

    # Bold **x** or __x__
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", text)

    # Italic *x* or _x_  (run after bold so ** isn't mis-parsed)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?!_)", r"<i>\1</i>", text)

    # Headers -> bold lines (drop the leading #'s)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Bullet markers: keep the indent, render with a real bullet.
    text = re.sub(r"^([ \t]*)[-*+][ \t]+", r"\1• ", text, flags=re.MULTILINE)

    # Restore <pre> blocks.
    def _unstash(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return placeholders[idx]

    text = re.sub(r"\x00PRE(\d+)\x00", _unstash, text)

    return text


def split_for_telegram(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """Split text into chunks <= limit chars, preferring paragraph/line breaks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Try paragraph break, then line break, then space.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_chunk(token: str, chat_id: str, text: str, *, attempt: int = 1) -> None:
    url = f"{API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        die(f"Telegram returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")

    if resp.status_code == 429 and attempt <= 3:
        retry_after = int(data.get("parameters", {}).get("retry_after", 2))
        print(f"Rate-limited; sleeping {retry_after}s then retrying (attempt {attempt}).")
        time.sleep(retry_after + 1)
        send_chunk(token, chat_id, text, attempt=attempt + 1)
        return

    if not data.get("ok"):
        desc = data.get("description", "<no description>")
        die(f"Telegram API error (HTTP {resp.status_code}): {desc}")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    report_file = os.environ.get("REPORT_FILE", "").strip()

    if not token:
        die("TELEGRAM_BOT_TOKEN secret is empty or missing.")
    if not chat_id:
        die("TELEGRAM_CHANNEL_ID secret is empty or missing.")
    if not report_file:
        die("REPORT_FILE env var is empty (workflow did not pick a file).")

    path = Path(report_file)
    if not path.is_file():
        die(f"Report file does not exist: {report_file}")

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        die(f"Report file is empty: {report_file}")

    if path.suffix.lower() == ".md":
        body = md_to_telegram_html(raw)
        fmt = "markdown -> HTML"
    else:
        body = raw
        fmt = "HTML (passthrough)"

    chunks = split_for_telegram(body)
    print(f"Posting {path} ({fmt}); {len(raw)} chars -> {len(chunks)} chunk(s).")

    for i, chunk in enumerate(chunks, start=1):
        print(f"  Sending chunk {i}/{len(chunks)} ({len(chunk)} chars)...")
        send_chunk(token, chat_id, chunk)
        if i < len(chunks):
            time.sleep(1)  # Stay under Telegram's per-chat rate limit.

    print(f"OK: posted {path} to channel {chat_id} in {len(chunks)} message(s).")


if __name__ == "__main__":
    main()
