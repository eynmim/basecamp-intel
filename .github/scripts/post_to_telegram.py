"""Post a daily intelligence report to a Telegram channel.

Pipeline:
  1. Read $REPORT_FILE (markdown or HTML).
  2. Validate the report against the expected schema; abort if it drifts.
  3. Skip if state/posted.json already records this exact file (sha256).
  4. Split into one Telegram message per opportunity.
  5. If the first message is a "═ ACTIVE DEADLINES ═" section, edit the
     existing pinned message in place (or send + pin if no state yet).
  6. Send the rest as new messages.
  7. Update state/ files; the workflow commits them back to the repo.
Failures (Telegram ok:false, schema drift, etc.) exit nonzero so the
workflow's `if: failure()` step posts an alert to the same channel.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

CHUNK_SIZE = 3800  # Telegram hard limit is 4096; leave headroom.
API_BASE = "https://api.telegram.org"
STATE_DIR = Path("state")
POSTED_LEDGER = STATE_DIR / "posted.json"
PINNED_STATE = STATE_DIR / "pinned.json"
TOPICS_CONFIG = Path(".github/topics.json")

# Section header line, e.g. <b>═ PORTFOLIO SNAPSHOT ═</b>
SECTION_RE = re.compile(r"(?m)^<b>═.*?═</b>\s*$")
# Numbered item start, e.g. <b>1. Study a Master's...</b>
ITEM_START_RE = re.compile(r"(?m)^<b>\d+\.\s")
# Detect raw Telegram-HTML in the source so we don't double-escape it.
HTML_TAG_RE = re.compile(r"</?(b|i|u|s|a|code|pre|blockquote)\b", re.IGNORECASE)
# A title line is any <b>...</b> line at the top of the report (before
# the first section divider). Any category-specific title text is fine.
TITLE_RE = re.compile(r"(?m)^<b>[^<\n]+</b>\s*$")
# Mark the deadline-board section so we know which message to pin/edit.
DEADLINE_BOARD_RE = re.compile(r"(?m)^<b>═[^<]*ACTIVE DEADLINES[^<]*═</b>\s*$")


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


def validate_report(text: str) -> list[str]:
    """Return a list of problems with the report, or an empty list if valid."""
    problems: list[str] = []

    sections = SECTION_RE.findall(text)
    if not sections:
        problems.append("no section dividers found (expected <b>═ ... ═</b> lines)")

    # A title block must exist before the first section divider, and contain
    # at least one <b>...</b> line. We don't care which keywords it uses —
    # different categories have different titles.
    first_section = SECTION_RE.search(text)
    preamble = text[: first_section.start()].strip() if first_section else text.strip()
    if not preamble:
        problems.append("missing title block before first section divider")
    elif not TITLE_RE.search(preamble):
        problems.append("title block has no <b>...</b> line; first non-blank line should be a bolded title")

    # Every line that looks like a numbered item must be wrapped in <b>.
    for line in text.splitlines():
        if re.match(r"^\d+\.\s", line):
            problems.append(f"numbered item missing <b>...</b> wrapper: {line[:80]!r}")

    # Tag balance for the tags we use.
    for tag in ("b", "i", "a"):
        opens = len(re.findall(rf"<{tag}\b[^>]*>", text, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}\b\s*>", text, re.IGNORECASE))
        if opens != closes:
            problems.append(f"unbalanced <{tag}> tags: {opens} open, {closes} close")

    # Check item lengths after splitting.
    if not problems:  # Only run if structure is otherwise sane.
        for msg in split_into_messages(text):
            if len(msg) > 3500:
                first = msg.split("\n", 1)[0][:80]
                problems.append(
                    f"message exceeds 3500 chars ({len(msg)}): {first!r}"
                )

    return problems


def split_into_messages(text: str) -> list[str]:
    """Split a Basecamp Intel report into one message per opportunity.

    - Title block (lines before first ═ section) → 1 message.
    - Section without numbered items → 1 message (header + body).
    - Section with numbered items → 1 message per item; the section
      header is prepended to the first item under it only.
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


def telegram_call(token: str, method: str, payload: dict, *, attempt: int = 1) -> dict:
    url = f"{API_BASE}/bot{token}/{method}"
    resp = requests.post(url, json=payload, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        die(f"Telegram {method} returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")

    if resp.status_code == 429 and attempt <= 3:
        retry_after = int(data.get("parameters", {}).get("retry_after", 2))
        print(f"Rate-limited on {method}; sleeping {retry_after}s (attempt {attempt}).")
        time.sleep(retry_after + 1)
        return telegram_call(token, method, payload, attempt=attempt + 1)

    return data


def send_message(token: str, chat_id: str, text: str, thread_id: int | None = None) -> int:
    """Send a message to a chat (or a topic if thread_id is set); return new message_id."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    data = telegram_call(token, "sendMessage", payload)
    if not data.get("ok"):
        die(f"sendMessage failed: {data.get('description', '<no description>')}")
    return data["result"]["message_id"]


def edit_message(token: str, chat_id: str, message_id: int, text: str) -> bool:
    """Edit an existing message. Return True on success, False if it can't be edited."""
    data = telegram_call(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if data.get("ok"):
        return True
    desc = (data.get("description") or "").lower()
    # "message to edit not found" / "message can't be edited" → caller falls through to send+pin.
    # "message is not modified" → body is identical to last time, treat as success.
    if "not modified" in desc:
        return True
    if "not found" in desc or "can't be edited" in desc:
        return False
    die(f"editMessageText failed: {data.get('description', '<no description>')}")
    return False  # unreachable


def pin_message(token: str, chat_id: str, message_id: int) -> None:
    data = telegram_call(token, "pinChatMessage", {
        "chat_id": chat_id,
        "message_id": message_id,
        "disable_notification": True,
    })
    if not data.get("ok"):
        # Pinning is best-effort; log but don't abort the run.
        print(f"WARN: pinChatMessage failed: {data.get('description', '<no description>')}")


def load_topic_config(category_hint: str) -> tuple[str, int | None, bool, str]:
    """Resolve (category, topic_id, has_deadline_board, label) for the run.

    The workflow passes the category extracted from the file path (or empty
    string for legacy root-level reports). We fall back to default_category
    if the hint is empty or unknown.
    """
    config = load_json(TOPICS_CONFIG, {})
    cats = config.get("categories", {}) or {}
    default_cat = config.get("default_category", "opportunities")

    category = category_hint or default_cat
    if category not in cats:
        if category_hint:
            print(f"WARN: category '{category_hint}' not in topics.json; "
                  f"falling back to default '{default_cat}'.")
        category = default_cat

    cat_cfg = cats.get(category, {}) or {}
    topic_id = cat_cfg.get("topic_id")
    has_board = bool(cat_cfg.get("has_deadline_board", False))
    label = cat_cfg.get("label", category)
    return category, topic_id, has_board, label


def load_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARN: {path} is invalid JSON, treating as empty ({exc}).")
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    report_file = os.environ.get("REPORT_FILE", "").strip()
    category_hint = os.environ.get("REPORT_CATEGORY", "").strip()
    force = os.environ.get("FORCE_REPOST", "").strip().lower() in ("1", "true", "yes")

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

    # Resolve category → topic_id from .github/topics.json. If topic_id is
    # null (or topics.json missing), thread_id stays None and the script
    # posts without a thread (works for plain channels).
    category, thread_id, has_board, label = load_topic_config(category_hint)

    # Idempotency: skip if this exact file has already been posted.
    sha = file_sha256(path)
    posted_ledger = load_json(POSTED_LEDGER, {"reports": {}})
    posted_reports = posted_ledger.setdefault("reports", {})
    prior = posted_reports.get(report_file)
    if prior and prior.get("sha256") == sha and not force:
        print(f"Report {report_file} already posted (sha256 match) — skipping.")
        print(f"  Posted at: {prior.get('posted_at')}, messages: {prior.get('message_count')}")
        print("  Set FORCE_REPOST=1 in workflow_dispatch to override.")
        return

    # Detect HTML so we don't re-escape what's already valid Telegram HTML.
    if HTML_TAG_RE.search(raw):
        body = raw
        fmt = "HTML (passthrough)"
    elif path.suffix.lower() == ".md":
        body = md_to_telegram_html(raw)
        fmt = "markdown -> HTML"
    else:
        body = raw
        fmt = "raw"

    # Schema validation — abort BEFORE sending if the report drifts.
    problems = validate_report(body)
    if problems:
        print("::error::Report failed schema validation:")
        for p in problems:
            print(f"  - {p}")
        die(f"Schema validation failed ({len(problems)} problem(s)). Aborting before send.")

    messages = split_into_messages(body)
    if not messages:
        die(f"After splitting, no messages to send from {report_file}.")

    # Pinned-state is keyed by category so each topic has its own deadline
    # board state. Schema: {"<category>": {"message_id": ..., "last_updated": ...}}.
    pin_state_all = load_json(PINNED_STATE, {})
    pin_state = pin_state_all.get(category, {}) if isinstance(pin_state_all, dict) else {}

    deadline_idx: int | None = None
    if has_board:
        deadline_idx = next(
            (i for i, m in enumerate(messages) if DEADLINE_BOARD_RE.search(m)),
            None,
        )

    print(f"Posting {path} ({fmt}); {len(raw)} chars -> {len(messages)} message(s).")
    print(f"  Category: {category} ({label}); "
          f"thread_id={thread_id if thread_id is not None else '(none, posting to chat root)'}")
    if deadline_idx is not None:
        print(f"  Deadline board at position {deadline_idx + 1}; will edit-or-send+pin.")
    elif has_board:
        print("  Category supports a deadline board, but the report doesn't include one.")

    sent = 0
    for i, msg in enumerate(messages):
        is_deadline_board = i == deadline_idx
        chunks = split_for_telegram(msg)

        for j, chunk in enumerate(chunks):
            tag = f"{i + 1}/{len(messages)}"
            if len(chunks) > 1:
                tag += f".{j + 1}"
            print(f"  Sending {tag} ({len(chunk)} chars)...")

            if is_deadline_board and j == 0 and pin_state.get("message_id"):
                # Try to update the existing pinned deadline message in this topic.
                if edit_message(token, chat_id, pin_state["message_id"], chunk):
                    print(f"    edited existing pinned message {pin_state['message_id']}.")
                    pin_message(token, chat_id, pin_state["message_id"])  # re-pin if user unpinned
                    sent += 1
                    time.sleep(1)
                    continue
                print("    pinned message gone; sending fresh.")

            mid = send_message(token, chat_id, chunk, thread_id=thread_id)
            sent += 1

            if is_deadline_board and j == 0:
                pin_message(token, chat_id, mid)
                pin_state["message_id"] = mid
                pin_state["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
                if not isinstance(pin_state_all, dict):
                    pin_state_all = {}
                pin_state_all[category] = pin_state
                save_json(PINNED_STATE, pin_state_all)

            time.sleep(1)  # Stay under per-chat rate limits.

    # Update the posted-ledger after a fully successful run.
    posted_reports[report_file] = {
        "sha256": sha,
        "category": category,
        "posted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "message_count": sent,
    }
    save_json(POSTED_LEDGER, posted_ledger)

    print(f"OK: posted {path} to {chat_id} (category={category}) as {sent} message(s).")


if __name__ == "__main__":
    main()
