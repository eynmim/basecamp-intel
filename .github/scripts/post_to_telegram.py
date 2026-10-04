"""Post a daily intelligence report to a Telegram channel.

Pipeline:
  1. Read $REPORT_FILE (markdown or HTML).
  2. Validate the report against the expected schema; abort if it drifts.
  3. Skip if state/posted.json already records this exact file (sha256).
  4. Split into one Telegram message per opportunity.
  5. If the first message is a "═ ACTIVE DEADLINES ═" (or, for events,
     "═ EVENT CALENDAR ═") section, edit the existing pinned message in
     place (or send + pin if no state yet).
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

from zoneinfo import ZoneInfo

import requests

CHUNK_SIZE = 3800  # Telegram hard limit is 4096; leave headroom.
API_BASE = "https://api.telegram.org"
STATE_DIR = Path("state")
POSTED_LEDGER = STATE_DIR / "posted.json"
PINNED_STATE = STATE_DIR / "pinned.json"
SEEN_LEDGER = STATE_DIR / "seen.json"
# Board lines tagged with their item's location, read by the Worker when a
# country button under a pinned board is tapped.
FILTER_INDEX = STATE_DIR / "filters.json"
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
# First external link inside an item — the opportunity's stable identity.
# Item prose is rewritten by the scout every run (measured similarity 0.65-0.97
# day-over-day), so text hashing cannot tell "changed" from "reworded". The
# destination URL does not churn, so that is what we key the seen-ledger on.
HREF_RE = re.compile(r'href="([^"]+)"')
# "31 Aug 2026", "04 Sep 2026" — a moved deadline is the one delta worth re-sending.
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DEADLINE_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTHS})[a-z]*\.?\s+(\d{{4}})\b", re.IGNORECASE)
# A date only counts as a deadline if its line says so.
# Ordered loosely by specificity: the earliest keyword in a clause wins, so
# "last day to apply" has to be matchable or "document deadline 1 Feb" hijacks
# a line whose real answer is "last day to apply 15 Jan".
DEADLINE_KEYWORD_RE = re.compile(
    r"last day to apply|apply by|applications? close|"
    r"deadline|closes?\b|closing|application window|submission|due\b|expires?\b",
    re.IGNORECASE,
)
# Board entry link, rewritten to point at the original Telegram message.
BOARD_LINK_RE = re.compile(r'<a href="([^"]+)">([^<]*)</a>')
# Largest the pinned board may get. validate_report() aborts the whole run at
# 3500, and the board has grown 1016 -> 2906 chars since May, so cap it here.
BOARD_LIMIT = 3000

# Mark the deadline-board section so we know which message to pin/edit.
# Events use the same mechanism under a calendar heading.
DEADLINE_BOARD_RE = re.compile(r"(?m)^<b>═[^<]*(?:ACTIVE DEADLINES|EVENT CALENDAR)[^<]*═</b>\s*$")

# "Location: Turin, Italy" — where the position or event is. The last
# comma-separated part is the country, the one before it the city.
LOCATION_RE = re.compile(r"(?m)^Location:\s*(.+?)\s*$")
FLAGS = {
    "Italy": "🇮🇹", "Germany": "🇩🇪", "Netherlands": "🇳🇱", "Sweden": "🇸🇪",
    "Finland": "🇫🇮", "France": "🇫🇷", "Spain": "🇪🇸", "Belgium": "🇧🇪",
    "Austria": "🇦🇹", "Switzerland": "🇨🇭", "Denmark": "🇩🇰", "Norway": "🇳🇴",
    "Czechia": "🇨🇿", "Poland": "🇵🇱", "Portugal": "🇵🇹", "Ireland": "🇮🇪",
    "Greece": "🇬🇷", "Estonia": "🇪🇪", "Luxembourg": "🇱🇺", "Slovenia": "🇸🇮",
    "United Kingdom": "🇬🇧", "United States": "🇺🇸", "Canada": "🇨🇦",
    "Japan": "🇯🇵", "South Korea": "🇰🇷", "Singapore": "🇸🇬",
    "United Arab Emirates": "🇦🇪", "Iran": "🇮🇷",
    "EU-wide": "🇪🇺", "Online": "💻", "Global": "🌍", "Unknown": "❔",
}
COUNTRY_ALIASES = {
    "uk": "United Kingdom", "england": "United Kingdom", "great britain": "United Kingdom",
    "us": "United States", "usa": "United States", "czech republic": "Czechia",
    "the netherlands": "Netherlands", "holland": "Netherlands", "korea": "South Korea",
    "uae": "United Arab Emirates", "eu": "EU-wide", "europe": "EU-wide",
    "europe-wide": "EU-wide", "virtual": "Online", "remote": "Global",
    "worldwide": "Global", "international": "Global",
    **{name.lower(): name for name in FLAGS},
}
# Tail of the button row, after the real countries, in this order.
FILTER_TAIL = ["EU-wide", "Online", "Global", "Unknown"]


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


def send_message(
    token: str, chat_id: str, text: str, thread_id: int | None = None, reply_markup: dict | None = None
) -> int:
    """Send a message to a chat (or a topic if thread_id is set); return new message_id."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = telegram_call(token, "sendMessage", payload)
    if not data.get("ok"):
        die(f"sendMessage failed: {data.get('description', '<no description>')}")
    return data["result"]["message_id"]


def edit_message(
    token: str, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None
) -> bool:
    """Edit an existing message. Return True on success, False if it can't be edited.

    An edit without reply_markup strips the message's buttons, so the board
    passes its keyboard on every edit.
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = telegram_call(token, "editMessageText", payload)
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


# --------------------------------------------------------------------------
# Duplicate suppression
#
# The scout re-researches every open opportunity every run, so ~87 % of a
# daily report is material that already went out. Suppressing it here rather
# than asking the scout to remember is deliberate: the URL is a stable key the
# LLM cannot drift on, whereas item prose is rewritten each run.
#
# Cadence: one full re-send on the weekly refresh day (default Monday), then
# new-only for the rest of the week. The pinned deadline board carries
# everything still open, so nothing is lost by staying quiet.
# --------------------------------------------------------------------------


def item_url(msg: str) -> str | None:
    """The opportunity's identity: its first non-Telegram link."""
    for match in HREF_RE.finditer(msg):
        url = match.group(1)
        if not url.startswith("https://t.me/"):
            return url.split("#")[0].rstrip("/")
    return None


def item_deadline(msg: str, today: dt.date | None = None) -> str | None:
    """The item's closing date as YYYY-MM-DD, or None if not stated clearly.

    Deliberately conservative. Taking the first date in the item mis-fires on
    prose like "catalogue unchanged since 20 Oct 2025" or "window opened
    22 Jul", which then reads as a moved deadline and re-sends a duplicate —
    the exact thing this pipeline exists to stop. So: only dates on a line
    that names a deadline, only future dates, and for a range ("18 Aug –
    01 Sep 2026") the closing end.

    Returning None is safe: a None on either side suppresses rather than
    re-sends.
    """
    today = today or dt.datetime.now(ZoneInfo("Europe/Rome")).date()
    for line in msg.splitlines():
        # "|" and ";" separate independent facts on one line, e.g.
        # "Deadline: 07 Aug 2026 | Award: ... Maker Faire 13 Dec 2026".
        # Taking the largest date on the whole line picked up the fair date;
        # when the scout later dropped that clause the value "moved" four
        # months and fired a false 'Deadline moved' banner.
        for clause in re.split(r"[|;]", line):
            keyword = DEADLINE_KEYWORD_RE.search(clause)
            if not keyword:
                continue
            # The date belonging to the keyword is the first one after it,
            # not the largest in the clause. "last day to apply 15 Jan 2027,
            # document deadline 1 Feb 2027" must resolve to 15 Jan, and must
            # keep resolving to 15 Jan after the scout rewords the tail.
            for match in DEADLINE_RE.finditer(clause, keyword.start()):
                day, mon, year = match.groups()
                month_num = MONTHS.split("|").index(mon[:3].title()) + 1
                try:
                    found = dt.date(int(year), month_num, int(day))
                except ValueError:
                    continue
                if found >= today:
                    return found.isoformat()
    return None


def evict_expired(seen: dict, *, grace_days: int = 90, today: dt.date | None = None) -> dict:
    """Forget opportunities whose deadline is long past.

    Annual programmes reuse their URL — DAAD STEM 2027 and 2028 are the same
    link — so a permanent ledger would silently swallow next year's call. It
    also keeps state/seen.json from growing in git forever.
    """
    today = today or dt.datetime.now(ZoneInfo("Europe/Rome")).date()
    cutoff = today - dt.timedelta(days=grace_days)
    stale = (today - dt.timedelta(days=180)).isoformat()
    kept = {}
    for url, record in seen.items():
        deadline = record.get("deadline")
        if deadline:
            try:
                if dt.date.fromisoformat(deadline) < cutoff:
                    continue
            except ValueError:
                pass
        elif record.get("last_sent", stale) < stale:
            # Nothing to age out on (rolling calls, free meetups). Forget it once
            # it hasn't gone out for half a year, so a yearly event that reuses
            # its URL comes back as new instead of staying muted forever.
            continue
        kept[url] = record
    dropped = len(seen) - len(kept)
    if dropped:
        print(f"  Seen-ledger: evicted {dropped} record(s) whose deadline "
              f"passed before {cutoff.isoformat()}.")
    return kept


def deadline_move(item: str, record: dict, today: dt.date | None) -> tuple[str | None, str | None]:
    """Decide whether this item's deadline genuinely moved.

    Returns (new_deadline_to_announce, reason_it_was_suppressed).

    The banner asserts a fact about a date, so a false one is worse than a
    plain duplicate. Two guards, both measured against the 20-day August
    window (5 raw re-sends -> 2):

      A. Only announce a deadline getting *sooner*. Slipping later is not
         urgent and rides along on the weekly full refresh anyway.
      B. Never announce a date already recorded for this URL. A value that
         returns to one it held before did not move — the scout's prose was
         parsed differently, e.g. chips-ju bounced 09-16 -> 09-17 -> 09-07
         -> 09-16 across four runs.
    """
    new = item_deadline(item, today)
    old = record.get("deadline")
    if not new or not old or new == old:
        return None, None
    if new in record.get("deadline_history", []):
        return None, f"{old} -> {new} (seen before; extraction bounce)"
    if new > old:
        return None, f"{old} -> {new} (later, not urgent; waits for the weekly refresh)"
    return new, None


def split_item(msg: str) -> tuple[str, str | None]:
    """Return (leading section header + preamble, numbered item) for a message.

    split_into_messages() prepends the section header to the first item under
    it. When that item is suppressed the header has to be transplanted onto
    the next surviving item, so the two halves are separated here.
    """
    match = ITEM_START_RE.search(msg)
    if not match:
        return msg, None
    return msg[: match.start()].strip(), msg[match.start():].strip()


def filter_seen(
    messages: list[str],
    seen: dict,
    *,
    full_refresh: bool,
    today: "dt.date | None" = None,
) -> tuple[list[str], list[tuple[int, str]], dict]:
    """Drop already-delivered items; keep sections, new items, moved deadlines.

    Returns (kept messages, [(index in kept, url)] for ledger updates, stats).
    """
    kept: list[str] = []
    urls: list[tuple[int, str]] = []
    stats = {"suppressed": 0, "new": 0, "changed": 0, "sections": 0, "no_url": 0}
    pending_header: str | None = None

    for msg in messages:
        header, item = split_item(msg)

        if item is None:
            # A section-only message (title block, PORTFOLIO SNAPSHOT, the
            # deadline board, THIS WEEK'S INTEL). Always fresh — always sent.
            kept.append(msg)
            pending_header = None
            stats["sections"] += 1
            continue

        if header:
            pending_header = header

        url = item_url(item)
        record = seen.get(url) if url else None

        if url is None:
            # No link to key on. Sending is the safe direction.
            stats["no_url"] += 1
        elif record and not full_refresh:
            moved, why = deadline_move(item, record, today)
            if moved:
                stats["changed"] += 1
                item = f"<i>🔄 Deadline moved: {record['deadline']} → {moved}</i>\n{item}"
            else:
                if why:
                    # Log, don't deliver. If these turn out to be real moves the
                    # guards are too tight and the log is the evidence.
                    stats.setdefault("guarded", [])
                    stats["guarded"].append(why)
                stats["suppressed"] += 1
                continue
        elif record:
            pass  # full refresh — re-send everything
        else:
            stats["new"] += 1

        if pending_header:
            item = f"{pending_header}\n\n{item}"
            pending_header = None
        if url:
            urls.append((len(kept), url))
        kept.append(item)

    return kept, urls, stats


def renumber(messages: list[str]) -> list[str]:
    """Renumber surviving items 1..N so the feed doesn't read '2., 7., 19.'."""
    counter = 0
    out: list[str] = []
    for msg in messages:
        match = ITEM_START_RE.search(msg)
        if not match:
            out.append(msg)
            continue
        counter += 1
        start = match.start()
        out.append(msg[:start] + re.sub(r"^<b>\d+\.", f"<b>{counter}.", msg[start:]))
    return out


def telegram_permalink(chat_id: str, thread_id: int | None, message_id: int) -> str | None:
    """Deep link to a message inside a private supergroup topic."""
    if not chat_id.startswith("-100"):
        return None
    internal = chat_id[4:]
    if thread_id is not None:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


def link_board_to_messages(
    board: str, seen: dict, chat_id: str, thread_id: int | None
) -> tuple[str, int]:
    """Repoint each board entry's link at the original Telegram message.

    The board is a reminder index, so its arrow should jump to the full item
    already in the topic (which carries the external link) rather than leave
    Telegram. Entries with no recorded message_id keep their external link.
    """
    linked = 0

    def _swap(match: re.Match[str]) -> str:
        nonlocal linked
        url, label = match.group(1), match.group(2)
        record = seen.get(url.split("#")[0].rstrip("/"))
        message_id = record.get("message_id") if record else None
        if not message_id:
            return match.group(0)
        permalink = telegram_permalink(chat_id, thread_id, message_id)
        if not permalink:
            return match.group(0)
        linked += 1
        return f'<a href="{permalink}">{label}</a>'

    return BOARD_LINK_RE.sub(_swap, board), linked


def cap_board(board: str, limit: int = BOARD_LIMIT) -> str:
    """Trim the board to a line count that fits, newest-dropped-last.

    validate_report() aborts the whole run past 3500 chars and the board has
    grown steadily, so this is a hard backstop rather than a nicety.
    """
    if len(board) <= limit:
        return board
    lines = board.split("\n")
    while len("\n".join(lines)) > limit - 60 and len(lines) > 2:
        lines.pop()
    dropped = len(board.split("\n")) - len(lines)
    lines.append(f"\n<i>… +{dropped} more, trimmed to fit Telegram's limit</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Location filter
#
# Each pinned board carries one button per country. Tapping one makes the
# Worker swap the board for that country's lines, grouped by city; the next
# run puts the full board back. The Worker has no storage of its own, so this
# run publishes what it needs in state/filters.json.
# --------------------------------------------------------------------------


def item_location(msg: str) -> tuple[str, str]:
    """(city, country) from the item's Location: line, as plain text."""
    match = LOCATION_RE.search(msg)
    if not match:
        return "", "Unknown"
    text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
    # "Milan, Italy (hybrid)" is still Italy; a stray remark must not make
    # its own country button.
    text = re.sub(r"\([^)]*\)", "", text)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return "", "Unknown"
    country = COUNTRY_ALIASES.get(parts[-1].lower(), parts[-1])
    # Telegram caps callback_data at 64 bytes, not characters:
    # "loc:opportunities:" + country + ":" + 6-char stamp.
    country = country.encode()[:36].decode(errors="ignore").strip()
    city = parts[-2] if len(parts) > 1 else ""
    return city, country


def build_location_filter(
    board: str, linked: str, locations: dict, category: str, stamp: str
) -> tuple[list[dict], list[list[dict]]]:
    """Tag each board line with its item's location; build the country keyboard.

    `board` and `linked` are the same board before and after its arrows were
    repointed at Telegram messages. Lines correspond one to one, so the raw
    line gives the item URL and the linked line is what gets displayed.

    Every button carries `stamp`, a hash of this run's board. The Worker only
    acts on a button it finds in filters.json, so a copy of filters.json that
    is older than the board (the commit lands after the send, and GitHub's raw
    CDN caches for minutes) cannot overwrite the board with stale content.
    """
    lines = []
    for raw_line, shown in zip(board.split("\n"), linked.split("\n")):
        match = BOARD_LINK_RE.search(raw_line)
        if not match:
            continue  # heading, blank line or note
        city, country = locations.get(match.group(1).split("#")[0].rstrip("/"), ("", "Unknown"))
        lines.append({"html": shown, "city": city, "country": country})
    if not lines:
        return [], []

    counts: dict[str, int] = {}
    for line in lines:
        counts[line["country"]] = counts.get(line["country"], 0) + 1

    def order(country: str) -> tuple:
        if country == "Italy":
            return (0, 0, "")
        if country in FILTER_TAIL:
            return (2, FILTER_TAIL.index(country), "")
        return (1, -counts[country], country)

    buttons = [
        {"text": f"{FLAGS.get(c, '🌐')} {c} ({counts[c]})", "callback_data": f"loc:{category}:{c}:{stamp}"}
        for c in sorted(counts, key=order)
    ]
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    keyboard.append([{"text": "📋 All", "callback_data": f"loc:{category}:ALL:{stamp}"}])
    return lines, keyboard


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

    # Locations of every item in the report, taken before dedup: suppressed
    # items are still on the board, and the filter has to cover them too.
    locations = {}
    for msg in messages:
        _, item = split_item(msg)
        url = item_url(item) if item else None
        if url:
            locations[url] = item_location(item)

    # --- Duplicate suppression -------------------------------------------
    # Weekly rhythm: one full re-send on refresh day, new-only the rest of
    # the week. Rome time, because that's the clock the scouts date against.
    seen_all = load_json(SEEN_LEDGER, {})
    if not isinstance(seen_all, dict):
        seen_all = {}
    seen = seen_all.setdefault(category, {})
    seen = evict_expired(seen)
    seen_all[category] = seen

    cats_cfg = load_json(TOPICS_CONFIG, {}).get("categories", {}) or {}
    dedup_enabled = bool(cats_cfg.get(category, {}).get("dedup", False))

    today_rome = dt.datetime.now(ZoneInfo("Europe/Rome")).date()
    refresh_day = os.environ.get("WEEKLY_REFRESH_DAY", "mon").strip().lower()[:3]
    weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    is_refresh_day = refresh_day in weekdays and today_rome.weekday() == weekdays.index(refresh_day)
    full_refresh = (
        is_refresh_day
        or force
        or os.environ.get("FULL_REFRESH", "").strip().lower() in ("1", "true", "yes")
    )

    item_urls: list[tuple[int, str]] = []
    if dedup_enabled:
        messages, item_urls, stats = filter_seen(
            messages, seen, full_refresh=full_refresh, today=today_rome
        )
        messages = renumber(messages)
        mode = "FULL REFRESH (weekly)" if full_refresh else "new-only"
        print(f"  Dedup [{mode}]: {stats['new']} new, {stats['changed']} deadline-moved, "
              f"{stats['suppressed']} suppressed, {stats['sections']} sections, "
              f"{stats['no_url']} unkeyed.")
        for why in stats.get("guarded", []):
            print(f"    guarded 'Deadline moved': {why}")
        if not full_refresh and stats["new"] == 0 and stats["changed"] == 0:
            print("  Nothing new today — only the sections and the refreshed board go out.")

    # Point the pinned board's arrows at the original messages, and cap its
    # size so a busy week can't push it past the validator's 3500-char abort.
    board_markup: dict | None = None
    if has_board:
        rebuilt = []
        for msg in messages:
            if DEADLINE_BOARD_RE.search(msg):
                raw_board = msg
                msg, linked = link_board_to_messages(msg, seen, chat_id, thread_id)
                full_board = msg
                before = len(msg)
                msg = cap_board(msg)
                stamp = hashlib.sha256(msg.encode()).hexdigest()[:6]
                lines, keyboard = build_location_filter(raw_board, full_board, locations, category, stamp)
                print(f"  Deadline board: {linked} entr(ies) deep-linked to their "
                      f"original message; {before} chars"
                      + (f" -> {len(msg)} (trimmed)" if len(msg) != before else ""))
                if keyboard:
                    board_markup = {"inline_keyboard": keyboard}
                    filters = load_json(FILTER_INDEX, {})
                    filters[category] = {
                        "updated": today_rome.isoformat(),
                        "label": label,
                        "board": msg,
                        "keyboard": keyboard,
                        "lines": lines,
                    }
                    save_json(FILTER_INDEX, filters)
                    print(f"  Location filter: {len(lines)} board line(s) across "
                          f"{sum(len(row) for row in keyboard) - 1} location button(s).")
            rebuilt.append(msg)
        messages = rebuilt

    if not messages:
        # Every item was already delivered and the report had no standalone
        # sections. A silent day is a correct outcome, not a failure — say so
        # rather than exiting nonzero and firing the failure alert.
        messages = [
            f"<b>📡 {label} — {today_rome.isoformat()}</b>\n\n"
            "Nothing new since the last run. The pinned deadline board is "
            "current."
        ]

    # Pinned-state is keyed by category so each topic has its own deadline
    # board state. Schema: {"<category>": {"message_id": ..., "last_updated": ...}}.
    # Older single-category runs wrote a flat {"message_id": ...} dict; if we
    # see that, migrate it under default_category so the existing pinned
    # message keeps getting edited in place.
    pin_state_all = load_json(PINNED_STATE, {})
    if not isinstance(pin_state_all, dict):
        pin_state_all = {}
    if "message_id" in pin_state_all:
        default_cat = load_json(TOPICS_CONFIG, {}).get("default_category", "opportunities")
        print(f"  Migrating flat pinned.json schema -> per-category (under '{default_cat}').")
        pin_state_all = {default_cat: pin_state_all}
    pin_state = pin_state_all.get(category, {})

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

    url_by_index = dict(item_urls)

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
                if edit_message(token, chat_id, pin_state["message_id"], chunk, reply_markup=board_markup):
                    print(f"    edited existing pinned message {pin_state['message_id']}.")
                    pin_message(token, chat_id, pin_state["message_id"])  # re-pin if user unpinned
                    # Stamp the edit too, not just send+pin — otherwise
                    # last_updated freezes at the day the board was created
                    # and reads as a dead pipeline.
                    pin_state["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    pin_state_all[category] = pin_state
                    save_json(PINNED_STATE, pin_state_all)
                    sent += 1
                    time.sleep(1)
                    continue
                print("    pinned message gone; sending fresh.")

            mid = send_message(
                token, chat_id, chunk, thread_id=thread_id,
                reply_markup=board_markup if is_deadline_board and j == 0 else None,
            )
            sent += 1

            # Remember where this opportunity landed, so the deadline board
            # can link back to it and future runs can suppress it.
            if j == 0 and i in url_by_index:
                url = url_by_index[i]
                record = seen.setdefault(url, {"first_seen": today_rome.isoformat()})
                title_match = re.search(r"<b>\d+\.\s*(.+?)</b>", msg)
                deadline = item_deadline(msg, today_rome) or record.get("deadline")
                history = record.setdefault("deadline_history", [])
                if deadline and deadline not in history:
                    history.append(deadline)
                record.update({
                    "title": title_match.group(1)[:120] if title_match else record.get("title", ""),
                    "deadline": deadline,
                    "last_sent": today_rome.isoformat(),
                    "message_id": mid,
                })
                # Persist per message, not at the end. A die() at message 20
                # of Monday's 23 would otherwise re-send all 20 next run —
                # and Monday's run is both the largest and the likeliest to
                # trip a rate limit.
                save_json(SEEN_LEDGER, seen_all)

            if is_deadline_board and j == 0:
                pin_message(token, chat_id, mid)
                pin_state["message_id"] = mid
                pin_state["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
                if not isinstance(pin_state_all, dict):
                    pin_state_all = {}
                pin_state_all[category] = pin_state
                save_json(PINNED_STATE, pin_state_all)

            time.sleep(1)  # Stay under per-chat rate limits.

    # Persist the seen-ledger so tomorrow's run can suppress what just went out.
    if dedup_enabled:
        seen_all[category] = seen
        save_json(SEEN_LEDGER, seen_all)
        print(f"  Seen-ledger: {len(seen)} known opportunit(ies) in '{category}'.")

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
