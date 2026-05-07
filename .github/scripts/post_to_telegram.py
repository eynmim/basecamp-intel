"""Post a daily intelligence report to a Telegram channel.

Reads $REPORT_FILE, splits it into one Telegram message per opportunity
(based on section dividers and numbered items), and posts each message
separately. Falls back to length-based chunking if any single message
exceeds Telegram's 4096-char limit. Fails the workflow if the Bot API
returns ok:false.
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from pathlib import Path

import requests

CHUNK_SIZE = 3800  # Telegram hard limit is 4096; leave headroom.
API_BASE = "https://api.telegram.org"

# Section header line, e.g. <b>═ PORTFOLIO SNAPSHOT ═</b>
SECTION_RE = re.compile(r"(?m)^<b>═.*?═</b>\s*$")
# Numbered item start, e.g. <b>1. Study a Master's...</b>
ITEM_START_RE = re.compile(r"(?m)^<b>\d+\.\s")
# Detect raw Telegram-HTML in the source so we don't double-escape it.
HTML_TAG_RE = re.compile(r"</?(b|i|u|s|a|code|pre|blockquote)\b", re.IGNORECASE)


def die(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def md_to_telegram_html(md: str) -> str:
    """Convert a small subset of markdown to Telegram-supported HTML."""
    text = md

    def _fence(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        body = html.escape(match.group(2))
        if lang:
            return f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>'
        return f"<pre>{body}</pre>"

    text = re.sub(r"```([^\n`]*)\n(.*?)```", _fence, text, flags=re.DOTALL)

    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"\x00PRE{len(placeholders) - 1}\x00"

    text = re.sub(r"<pre>.*?</pre>", _stash, text, flags=re.DOTALL)
    text = html.escape(text)
    text = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", text)

    def _link(match: re.Match[str]) -> str:
        return f'<a href="{match.group(2).replace("&amp;", "&")}">{match.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?!_)", r"<i>\1</i>", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^([ \t]*)[-*+][ \t]+", r"\1• ", text, flags=re.MULTILINE)
    text = re.sub(r"\x00PRE(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
    return text


def split_into_messages(text: str) -> list[str]:
    """Split a Basecamp Intel report into one message per opportunity.

    Layout assumed:
      <b>📡 BASECAMP INTEL — DATE</b>      ← title (becomes message 1)
      <b>name | role | school</b>
      <b>═ PORTFOLIO SNAPSHOT ═</b>        ← section without items
      ...snapshot lines...
      <b>═ ⚡ URGENT — ... ═</b>            ← section with numbered items
      <b>1. ...</b>
      ...
      <b>2. ...</b>
      ...
      <b>═ ... ═</b>
      <b>3. ...</b>
      ...

    Output:
      - Title block (everything before the first ═ section) → 1 message.
      - Section without numbered items → 1 message (header + body).
      - Section with numbered items → 1 message per item; the section
        header is prepended to the first item only.
    """
    section_headers = SECTION_RE.findall(text)
    if not section_headers:
        return [text.strip()] if text.strip() else []

    parts = SECTION_RE.split(text)
    messages: list[str] = []

    title_block = parts[0].strip()
    if title_block:
        messages.append(title_block)

    for header, body in zip(section_headers, parts[1:]):
        body = body.strip()
        item_starts = [m.start() for m in ITEM_START_RE.finditer(body)]

        if not item_starts:
            messages.append(f"{header}\n\n{body}".strip() if body else header)
            continue

        preamble = body[: item_starts[0]].strip()
        for i, start in enumerate(item_starts):
            end = item_starts[i + 1] if i + 1 < len(item_starts) else len(body)
            item = body[start:end].strip()
            if i == 0:
                pieces = [header]
                if preamble:
                    pieces.append(preamble)
                pieces.append(item)
                messages.append("\n\n".join(pieces))
            else:
                messages.append(item)

    return [m for m in messages if m]


def split_for_telegram(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """Hard fallback: if any single message exceeds the limit, split it."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
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

    # The routine writes Telegram-flavoured HTML directly into .md files,
    # so detect raw HTML and skip the markdown converter when present.
    if HTML_TAG_RE.search(raw):
        body = raw
        fmt = "HTML (passthrough)"
    elif path.suffix.lower() == ".md":
        body = md_to_telegram_html(raw)
        fmt = "markdown -> HTML"
    else:
        body = raw
        fmt = "raw"

    messages = split_into_messages(body)
    if not messages:
        die(f"After splitting, no messages to send from {report_file}.")

    print(f"Posting {path} ({fmt}); {len(raw)} chars -> {len(messages)} message(s).")

    sent = 0
    for i, msg in enumerate(messages, start=1):
        chunks = split_for_telegram(msg)
        for j, chunk in enumerate(chunks, start=1):
            label = f"{i}/{len(messages)}" if len(chunks) == 1 else f"{i}.{j}/{len(messages)}"
            print(f"  Sending {label} ({len(chunk)} chars)...")
            send_chunk(token, chat_id, chunk)
            sent += 1
            time.sleep(1)  # Stay under per-chat rate limits.

    print(f"OK: posted {path} to {chat_id} as {sent} message(s).")


if __name__ == "__main__":
    main()
