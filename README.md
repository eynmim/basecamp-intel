# basecamp-intel

Bridge between a Claude Code routine and the **Embedded system** Telegram channel
(bot: `@Chavosh2_Bot`).

## What this repo does

A scheduled Claude Code routine writes a daily intelligence report (news +
opportunities) into this repo. A GitHub Action picks up the new file and posts
it to Telegram. There is no server to run — GitHub Actions is the entire
delivery pipeline.

## Architecture

```
Claude routine (08:10 CEST)
        │  writes file
        ▼
reports/YYYY-MM-DD.md
reports/YYYY-MM-DD-telegram.html
        │  git push to main
        ▼
.github/workflows/post-to-telegram.yml
        │  runs post_to_telegram.py
        ▼
Telegram Bot API → channel "Embedded system"
```

Pipeline details:

- The routine commits to `main` under `reports/`. It can write either the
  `.md` source or a pre-rendered `.html`. If both land in the same push, the
  `.html` is preferred (assumed to be the cleaner version).
- The workflow triggers on `push` when paths under `reports/` change, or
  manually via `workflow_dispatch`.
- The Python script (`.github/scripts/post_to_telegram.py`) reads the file
  and **splits it into one Telegram message per opportunity**, so the channel
  reads like a daily news feed:
  - Title block (everything before the first `<b>═ ... ═</b>` divider) =
    message 1.
  - Each `<b>═ SECTION ═</b>` section without numbered items = one message.
  - Each `<b>N. Title</b>` numbered item = one message; the section header
    is prepended to the first item under it only.
- If a single message somehow exceeds 3800 chars, it falls back to length-
  based chunking. (Telegram's hard limit is 4096.)
- If Telegram returns `ok: false` for any chunk, the workflow fails with
  the API's error description so the failure is visible in the Actions log.

## Report format the routine must produce

The script auto-splits on these structural markers, so the routine must
emit them exactly:

- **Title line:**       `<b>📡 BASECAMP INTEL — YYYY-MM-DD</b>` (required)
- **Section divider:**  `<b>═ SECTION NAME ═</b>` (line on its own)
- **Numbered item:**    `<b>N. Item title</b>` at the start of a line
  (followed by the body lines)

Inside each item / section, use Telegram-flavoured HTML directly — `<b>`,
`<i>`, `<a href="...">`, `<code>`, `<pre>`. No markdown, no `<p>`, no `<br>`.
Escape `&` as `&amp;` and `<` as `&lt;` in plain-text content. Keep each
numbered item under 3500 chars.

The script runs `validate_report()` before sending and **aborts the workflow
on schema drift** (missing title, unwrapped numbered items, unbalanced tags,
oversized items). The `if: failure()` step then posts an alert to the same
channel so you see the problem immediately, not via GitHub email.

### Pinned deadline board (special section)

If the report's first section is `<b>═ 📋 ACTIVE DEADLINES ═</b>`, the
script treats it as a **single editable pinned message** rather than a
fresh post each day:

- First run: send + pin + record `message_id` in `state/pinned.json`.
- Subsequent runs: `editMessageText` updates the same pinned message
  in place; falls back to send+pin if the user manually unpinned/deleted
  it.

The board is the channel's "always current" snapshot of upcoming
deadlines. Keep it short (under 1 KB) and one line per opportunity.

### Hashtag taxonomy

Every numbered item should end with a tag line so Telegram's built-in
search becomes a free filter UI. Use only these tags so search stays
consistent:

| Category | Allowed tags |
|---|---|
| Type     | `#scholarship` `#internship` `#job` `#competition` `#fellowship` `#grant` |
| Urgency  | `#urgent` `#open` `#planahead` |
| Topic    | `#DSP` `#embedded` `#firmware` `#IoT` `#BLE` `#audio` `#robotics` `#ML` `#ComputerVision` |
| Region   | `#Iran` `#Italy` `#EU` `#Germany` `#Netherlands` `#Sweden` `#Global` |

Pick 3–5 tags per item. New tags are not added without updating this
table first.

## State files (machine-managed, don't hand-edit)

| File | Purpose |
|---|---|
| `state/posted.json` | sha256 ledger of report files already posted; prevents double-posts on retries / `workflow_dispatch` replays. Use the `force` input to override. |
| `state/pinned.json` | `message_id` of the pinned deadline board so the script can edit-in-place. |

The workflow auto-commits these files at the end of a successful run with
`[skip ci]` so it doesn't re-trigger itself.

## Secrets

The workflow needs two repository secrets:

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather for `@Chavosh2_Bot` |
| `TELEGRAM_CHANNEL_ID` | Channel ID (e.g. `-1001234567890`) or `@channelusername` |

Both are already set on this repo.

## Manual testing

There are two ways to verify the pipeline end-to-end.

### Option A — drop a test file in `reports/`

```bash
cat > reports/2026-05-07.md <<'EOF'
# Test report

This is a **manual** test of the basecamp-intel pipeline.

- Item one
- Item two with a [link](https://example.com)
EOF

git add reports/2026-05-07.md
git commit -m "test: smoke-test telegram pipeline"
git push origin main
```

Watch the run on the **Actions** tab. The message should appear in the
channel within ~10 seconds of the workflow finishing.

### Option B — `workflow_dispatch`

Go to **Actions → Post report to Telegram → Run workflow**. Either leave the
`file` input blank (it will pick the newest file in `reports/`) or pass an
explicit path like `reports/2026-05-07.md`.

CLI equivalent (requires `gh`):

```bash
gh workflow run post-to-telegram.yml -f file=reports/2026-05-07.md
gh run watch
```

## Updating the channel ID or token

1. Get a new token from BotFather (`/token`) or copy the new channel ID.
   - For private channels, channel IDs look like `-100xxxxxxxxxx`. The
     easiest way to grab one is to forward a message from the channel to
     `@RawDataBot` and read `forward_from_chat.id`.
2. **Settings → Secrets and variables → Actions** in this repo.
3. Edit `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHANNEL_ID` and save.
4. Re-trigger the workflow (Option A or B above) to confirm.

The bot must be added to the channel as an **administrator** with permission
to post messages, otherwise `sendMessage` returns
`ok: false, description: "Bad Request: chat not found"` or similar.

## Routine prompt (drop-in)

Paste this as the body of the **Oppurtunities** routine in claude.ai so it
pushes the report directly to `main` and skips its own Telegram attempt
(the GitHub Action handles delivery):

```
You are the daily intelligence scout for Ali Mansouri (GitHub: eynmim,
MSc Embedded & Smart Systems @ PoliTO, Iranian passport, Italian PdS).

Compute today's date in Europe/Rome time and use it for everything:
  TODAY=$(TZ=Europe/Rome date +%Y-%m-%d)

Write a single report file at reports/$TODAY.md covering, in this order:

  0. ACTIVE DEADLINES — a short pinned board with every opportunity that
     still has an open deadline (whether new today or carried over from
     prior runs). One line each, sorted by urgency. Format:
       <b>═ 📋 ACTIVE DEADLINES ═</b>
       ⚡ <b>15 May (8d)</b> — Educations.com Master's <a href="...">→</a>
       ⚡ <b>30 Jun (54d)</b> — IEEE SPS Scholarship <a href="...">→</a>
       🆕 <b>30 Aug</b> — Arduino+Qualcomm Hackathon <a href="...">→</a>
       🔄 <b>~Mar 2027</b> — Erasmus Mundus EMINENT <a href="...">→</a>
     Keep this section under ~1 KB. Drop expired entries. The Action
     edits the existing pinned message in place — no fresh post each day.

  1. PORTFOLIO SNAPSHOT — what changed in his GitHub repos this run,
     new skills detected, new repos.
  2. URGENT — opportunities with deadlines under 30 days. One numbered
     item per opportunity.
  3. OPEN OPPORTUNITIES — currently open, no rush. One numbered item each.
  4. PLAN AHEAD — deadlines > 60 days; flag now so he can prepare.
     One numbered item each.
  5. THIS WEEK'S INTEL — short bullets of relevant industry/regional
     news (Iran/Italy/EU/embedded). No numbered items.

Match against: ESP32-S3, FreeRTOS, STM32, ARM Cortex-M, audio DSP/FFT,
beamforming, MEMS mics, computer vision (YOLO), IoT/BLE, Python, React,
embedded firmware. Eligibility: Iranian + Italian PdS.

Output format (Telegram-flavoured HTML in a .md file):
- Title:        <b>📡 BASECAMP INTEL — $TODAY</b>          (REQUIRED)
- Section divs: <b>═ SECTION NAME ═</b>                    (line on its own)
- Numbered:     <b>N. Item title</b>                       (start of a line)
- Use <b>, <i>, <a href="...">, plain text. No markdown, no <p>, no <br>.
- Escape & as &amp; and < as &lt; inside plain-text content.
- Each numbered item must stay under 3500 chars.
- End each numbered item with a tag line, e.g.:
    #scholarship #urgent #DSP #EU
  Allowed tags only — see README §Hashtag taxonomy. Pick 3–5 per item.

Delivery (do NOT post to Telegram yourself; do NOT write any error.log
or auxiliary file in reports/):
After writing the file, push directly to main — no branch, no PR:
  git checkout main
  git pull --ff-only origin main
  git add reports/
  git commit -m "Daily intel report: $TODAY"
  git push origin main

The GitHub Action in this repo validates the file, splits it into one
Telegram message per opportunity, edits the pinned deadline board, and
delivers everything. Your job ends at "git push".
```

## Layout

```
.github/
  workflows/post-to-telegram.yml   # trigger + orchestration
  scripts/post_to_telegram.py      # validate, split, send, pin/edit, ledger
  scripts/notify_failure.py        # if: failure() — alert the channel
reports/
  .gitkeep                         # keeps the directory in git
  YYYY-MM-DD.md                    # written by the Claude routine
state/
  posted.json                      # sha256 ledger; auto-committed by workflow
  pinned.json                      # message_id of the pinned deadline board
```
