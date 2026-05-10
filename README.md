# basecamp-intel

Bridge between scheduled Claude Code routines and a categorised Telegram
**Basecamp Intel** supergroup (bot: `@Chavosh2_Bot`). One routine per
category, all delivered into the right Topic of the same supergroup.

## What this repo does

Multiple scheduled Claude Code routines (Opportunities, Education, News, …)
write daily intelligence reports into this repo, each under its own
`reports/<category>/` folder. A GitHub Action picks up the new file,
validates it, and posts to the matching **Topic** in the supergroup
(routed via `.github/topics.json`). One pipeline, one bot, multiple
topics — no server.

## Architecture

```
Routine A (Opportunities, daily)        Routine B (Education, weekly)        Routine C (News, daily)
       │  writes                                │  writes                            │  writes
       ▼                                        ▼                                    ▼
reports/opportunities/YYYY-MM-DD.md     reports/education/YYYY-MM-DD.md      reports/news/YYYY-MM-DD.md
       │ git push to main                       │ git push to main                   │ git push to main
       ▼                                        ▼                                    ▼
                  .github/workflows/post-to-telegram.yml  ←  triggered on push to reports/**
                                  │  reads .github/topics.json to map <category> → topic_id
                                  ▼
                  .github/scripts/post_to_telegram.py
                                  │  validates → splits → sendMessage(message_thread_id=topic_id)
                                  ▼
                  Telegram supergroup "Basecamp Intel"
                          ┌──────────┬─────────────┬─────────┐
                          │ 💼 Opp.  │ 📚 Education │ 📰 News │
                          └──────────┴─────────────┴─────────┘
```

Pipeline details:

- Each routine commits to `main` under `reports/<category>/`. Category is
  derived from the file path:
  `reports/opportunities/YYYY-MM-DD.md` → category `opportunities` →
  posted to the Opportunities topic.
  Files at `reports/YYYY-MM-DD.md` (no subfolder) fall back to
  `default_category` from `.github/topics.json` for backwards compat.
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

## Migration: from a single channel to a categorised supergroup

If you've already done the migration, skip to **Routine prompts**. Otherwise,
do this once:

1. **Create a Telegram supergroup** named *Basecamp Intel*. In group
   settings, toggle **Topics ON** so it becomes a forum.
2. **Create three topics** inside it: *Opportunities* (💼), *Education* (📚),
   *News* (📰).
3. **Add `@Chavosh2_Bot` as an admin** with rights to *send messages*,
   *pin messages*, and *manage topics*.
4. **Get the supergroup chat_id and per-topic message_thread_id**. Easiest
   way: send any message inside each topic, forward each one to
   `@RawDataBot`, and read `chat.id` (same for all forwards) and
   `message_thread_id` (different per topic). Supergroup chat_ids are of
   the form `-1002xxxxxxxxxx`.
5. **Update `TELEGRAM_CHANNEL_ID`** in repo Settings → Secrets to the new
   supergroup's chat_id. The bot token stays the same.
6. **Fill in `.github/topics.json`** — replace each `"topic_id": null`
   with the actual integer for that topic. Commit and push.

Until step 6, `topic_id` stays `null` and the script posts without a
thread (so existing channel delivery keeps working). The migration is
fully reversible: blank out `topic_id`s and point `TELEGRAM_CHANNEL_ID`
at the old channel.

## Adding a new category later

1. Add it to `.github/topics.json` (`label`, `topic_id`, `has_deadline_board`).
2. Create the matching topic in the supergroup; grab its `message_thread_id`
   via @RawDataBot; paste it into the JSON.
3. Set up a routine in claude.ai that writes `reports/<new-cat>/$TODAY.md`.
   Use the relevant prompt below as a template.

## Routine prompts (drop-in)

There's one routine per category. All of them push to `main` directly,
let the Action handle Telegram, and use the same HTML output format —
they differ only in *what* they research and *where* they save the file.

### Opportunities routine — `reports/opportunities/$TODAY.md`

Paste this into the **Oppurtunities** routine in claude.ai:

```
You are the daily intelligence scout for Ali Mansouri (GitHub: eynmim,
MSc Embedded & Smart Systems @ PoliTO, Iranian passport, Italian PdS).

Compute today's date in Europe/Rome time and use it for everything:
  TODAY=$(TZ=Europe/Rome date +%Y-%m-%d)

Write a single report file at reports/opportunities/$TODAY.md covering,
in this order:

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
  git add reports/opportunities/
  git commit -m "Daily opportunities: $TODAY"
  git push origin main

The GitHub Action validates the file, splits it into one Telegram
message per opportunity, edits the pinned deadline board, and delivers
everything to the Opportunities topic. Your job ends at "git push".
```

### Education routine — `reports/education/$TODAY.md`

Set this up as a separate routine in claude.ai (e.g. weekly). Use the
prompt below:

```
You are the weekly Embedded Engineering education scout for Ali Mansouri
(MSc Embedded & Smart Systems @ PoliTO, eynmim on GitHub). Surface
high-quality educational content that pushes his current skill stack
forward — depth over breadth, no fluff, no clickbait.

Compute today's date in Europe/Rome:
  TODAY=$(TZ=Europe/Rome date +%Y-%m-%d)

Write a single report file at reports/education/$TODAY.md.

Curate (last 14 days unless evergreen):
  - New free courses, university lecture series, MOOCs (Coursera/edX/MIT
    OCW) on RTOS, embedded C, BLE, DSP/audio, Edge AI, formal verification,
    PCB/KiCad, Linux for embedded, RISC-V, signal integrity.
  - Open-source projects worth studying as code: representative repos,
    not toy demos. Include why the code is instructive (architecture,
    HAL design, RTOS pattern, DSP technique).
  - Datasheets / app notes worth deep-reading (Nordic, ST, Espressif,
    NXP, ARM whitepapers).
  - Books / book chapters with free legal sample chapters (e.g. Yiu's
    Cortex-M definitive guide releases, MISRA samples).
  - Conference talks (Embedded World, FOSDEM Embedded, ELC, ESC) with
    YouTube links.

DO NOT include: paid bootcamps, scholarship listings, jobs, news.
Those belong in the Opportunities and News routines.

Output format (Telegram-flavoured HTML in a .md file). Do NOT include
an ACTIVE DEADLINES section — Education has no deadline board.

  <b>📚 EMBEDDED EDUCATION — $TODAY</b>
  <b>Curated for Ali Mansouri | PoliTO MSc</b>

  <b>═ 🎯 THIS WEEK'S DEEP DIVE ═</b>
  One feature item: longer write-up on the most valuable resource of
  the week. Why it matters, what to read first, ~10 lines.

  <b>═ 📖 COURSES & SERIES ═</b>

  <b>1. Course/series title</b>
  Source: provider | Format: video / text / interactive
  Length: ~Nh | Cost: free / $X
  Why it matters: 1–2 lines tying to Ali's stack
  <a href="https://...">Open →</a>
  #education #FreeRTOS #embedded

  <b>2. ...</b>

  <b>═ 💻 CODE WORTH READING ═</b>

  <b>3. Repo or project name</b>
  Stars: N | Lang: C | License: MIT
  What's instructive: 1–2 lines (architecture, RTOS pattern, etc.)
  <a href="https://github.com/...">Repo →</a>
  #education #embedded #firmware

  <b>═ 📑 APP NOTES & WHITEPAPERS ═</b>

  <b>4. Document title</b>
  Vendor | doc number / version | Pages: N
  Key idea: 1–2 lines
  <a href="https://...">PDF →</a>
  #education #BLE #embedded

  <b>═ 🎤 TALKS WORTH 40 MIN ═</b>

  <b>5. Talk title</b>
  Speaker | venue | year | Length: Nm
  Tag-line: one sentence
  <a href="https://youtube.com/...">Watch →</a>
  #education #embedded

  <b>═ 📌 TL;DR ═</b>
  • One-line per item, in priority order.

FORMATTING RULES (validator aborts on any of these):
- Title line REQUIRED; must contain "EMBEDDED EDUCATION".
- Section dividers: <b>═ NAME ═</b> (own line)
- Numbered items:   <b>N. Title</b> (start of line)
- Each numbered item ends with a tag line. Allowed tags only —
  see README §Hashtag taxonomy. Pick 3–5 per item.
- Each item under 3500 chars.

Delivery (do NOT call api.telegram.org):
  git checkout main
  git pull --ff-only origin main
  git add reports/education/
  git commit -m "Weekly education: $TODAY"
  git push origin main
```

### News routine — `reports/news/$TODAY.md`

Set this up as a separate routine in claude.ai (e.g. daily, evening
slot to avoid colliding with the morning Opportunities run):

```
You are the daily news scout for Ali Mansouri's embedded engineering
career and Iran/Italy/EU regulatory environment. Surface what *changed*
in the last 24h — be specific, link primary sources, no editorialising.

Compute today's date in Europe/Rome:
  TODAY=$(TZ=Europe/Rome date +%Y-%m-%d)

Write a single report file at reports/news/$TODAY.md.

Cover (last 24–48 hours):
  - Embedded silicon: chip releases, dev-kit launches, EOL notices
    (Espressif / Nordic / ST / NXP / Renesas / TI / Microchip).
  - Open-source toolchain news: PlatformIO, ESP-IDF, Zephyr RTOS,
    FreeRTOS, KiCad, Renode, QEMU.
  - EU/Iran/Italy policy: visa changes affecting Iranian nationals;
    Italian permesso/lavoro reforms; EU Blue Card thresholds; Iran
    sanctions changes that affect academic / industrial mobility.
  - Standards: BLE / Wi-Fi / Matter / USB / IEC 61508 / cybersecurity
    (CRA, RED) — when something is *finalised* or *enforced*, not
    rumoured.

DO NOT include: scholarship listings, course recommendations, generic
industry editorials, hype articles.

Output format (Telegram-flavoured HTML in a .md file). Do NOT include
an ACTIVE DEADLINES section — News has no deadline board.

  <b>📰 EMBEDDED & MOBILITY NEWS — $TODAY</b>
  <b>For Ali Mansouri | Iran→Italy→EU embedded track</b>

  <b>═ 🔥 TOP STORY ═</b>
  Most consequential item of the day. 4–6 sentences max. Direct
  source link. Why it matters to Ali in one final sentence.

  <b>═ 🛠️ EMBEDDED & TOOLCHAIN ═</b>

  <b>1. Headline</b>
  Source: vendor / publication | Date: YYYY-MM-DD
  What changed: 2–3 sentences.
  <a href="https://...">Source →</a>
  #news #embedded #firmware

  <b>2. ...</b>

  <b>═ 🌍 IRAN / ITALY / EU MOBILITY ═</b>

  <b>3. Headline</b>
  Source: government portal / reputable outlet | Date: YYYY-MM-DD
  What changed: 2–3 sentences.
  Affects Ali if: 1 sentence.
  <a href="https://...">Source →</a>
  #news #Iran #Italy #EU

  <b>═ 📐 STANDARDS & REGULATION ═</b>

  <b>4. ...</b>

  <b>═ 📌 ALSO TODAY ═</b>
  • One-liner + link.
  • One-liner + link.

FORMATTING RULES (validator aborts on any of these):
- Title line REQUIRED; must contain "EMBEDDED" and "NEWS".
- Section dividers: <b>═ NAME ═</b> (own line)
- Numbered items:   <b>N. Title</b> (start of line)
- Each numbered item ends with a tag line. Allowed tags only.
- Each item under 3500 chars.

Delivery (do NOT call api.telegram.org):
  git checkout main
  git pull --ff-only origin main
  git add reports/news/
  git commit -m "Daily news: $TODAY"
  git push origin main
```

### LinkedIn routine — `reports/linkedin/$TODAY.md`

Reads the day's news report and rewrites the 3–5 most LinkedIn-worthy
items as ready-to-publish post drafts. Each draft becomes its own
Telegram message in the 💼 LinkedIn topic — Ali picks the one he likes
and pastes it into LinkedIn.

Schedule **30 minutes after the News routine** (e.g. News 19:00 → LinkedIn 19:30) so today's news file exists when this routine reads it.

```
You are the LinkedIn copywriter for Ali Mansouri:
- Iranian embedded-systems engineer, MSc Embedded & Smart Systems @ PoliTO
- Stack: ESP32-S3, STM32, FreeRTOS, BLE, embedded C/C++, LVGL, KiCad
- GitHub: eynmim — repos include Life_logger (ESP32-S3 dual-mic audio
  beamforming), STM32F411_DistanceSensor, camera_project_repo, ROBOT
- LinkedIn audience: embedded engineers in EU/Iran/global, hiring managers
  at EU embedded firms (NXP, ST, Espressif, Nordic, IMEC, Bosch),
  PoliTO MSc peers, scholarship reviewers, recruiters.

Each day you transform the news scout's report into ready-to-publish
LinkedIn posts in Ali's voice. Authentic > generic. Pick stories he
can speak to from his stack/experience, not generic AI hype.

Compute today's date in Europe/Rome:
  TODAY=$(TZ=Europe/Rome date +%Y-%m-%d)

STEP 1 — READ TODAY'S NEWS
Read the file reports/news/$TODAY.md.
If it doesn't exist (News routine hasn't run yet today), exit gracefully
without committing anything. Do NOT fabricate news.

STEP 2 — PICK 3 to 5 LINKEDIN-WORTHY ITEMS
Criteria:
  ✓ Concrete technical change — new spec, chip, library release, CVE,
    standards update — with a real "why this matters" angle.
  ✓ Something Ali can speak to from his stack (ESP32, STM32, FreeRTOS,
    BLE, embedded audio/DSP) or his journey (Iranian engineer in EU).
  ✓ Sparks discussion among other embedded engineers.
  ✗ Skip corporate acquisitions, marketing fluff, generic AI hype.
  ✗ Skip items Ali has no real opinion on — better fewer authentic
    posts than 5 generic ones.
  ✗ Skip already-saturated trending news (don't pile on).

STEP 3 — WRITE EACH POST
Style guide:
  - First 2 lines = strong hook. LinkedIn truncates the rest behind
    "see more" until the reader clicks.
  - 80–200 words total. Punchy paragraphs, blank lines between them.
  - First-person, conversational, technical but readable.
  - Concrete numbers, version IDs, repo names when relevant.
  - Ali is a learner-builder — not a thought leader. No grandstanding.
  - End with ONE question or CTA to invite comments.
  - 3–4 inline hashtags at the very end.

Tone guardrails (the validator doesn't enforce these but Ali will read
the draft — keep him from cringing):
  - No buzzwords: "revolutionary", "game-changer", "leverage", "synergy".
  - No openers like "As an engineer…", "In today's world…".
  - DO use specific repo names, IC names, version numbers, dates.
  - If Ali has no hands-on experience with the topic, write it as a
    "this caught my eye" post — not "I've been using…".

STEP 4 — WRITE THE REPORT
Create reports/linkedin/$TODAY.md with this EXACT structure (the
GitHub Action validates and aborts on drift):

<b>💼 LINKEDIN POST IDEAS — $TODAY</b>
<b>For Ali Mansouri | Embedded engineering audience</b>

<b>═ 📌 TODAY'S PICKS ═</b>
3–5 one-liners — what stories from today's news translate well to
LinkedIn, and the angle for each.
• Pick 1 — short angle
• Pick 2 — short angle
• ...
#linkedin #embedded

<b>═ 🔥 TOP PICK ═</b>

<b>1. About: ESP-IDF v5.5.4 release</b>
Angle: [one-liner explaining why this resonates]
Length: ~150 words | Tone: "TIL" / opinion / story / educational

POST TEXT (copy-paste this into LinkedIn):
─────────────────────────────────────────
Just spent the morning porting Life_logger to ESP-IDF v5.5.4.

[…rest of the post, with real line breaks where they should appear in
the published LinkedIn post…]

#embeddedsystems #ESP32 #firmware #IoT
─────────────────────────────────────────

#linkedin #embedded #firmware

<b>2. About: [next topic]</b>
[same structure: Angle line, Length/Tone line, POST TEXT block, tags]
#linkedin #embedded #BLE

<b>3. About: [next topic]</b>
[same structure]
#linkedin #embedded #Iran

[items 4–5 same if there's enough strong material — better 3 great posts
than 5 mediocre ones]

FORMATTING RULES (validator aborts on any of these):
- Title line REQUIRED.
- Section dividers exactly: <b>═ NAME ═</b>
- Numbered items exactly: <b>N. About: title</b>
- Use <b>, <i>, <a href=>, plain text. NO markdown like **, no <p>, no <br>.
- Escape & as &amp; and < as &lt; in plain-text content.
- Each numbered item under 3500 chars.
- End each numbered item with a hashtag line (3–5 tags).
  Allowed tags — same set as News, see README §Hashtag taxonomy.
  Plus #linkedin (always include).

STEP 5 — DELIVERY (do NOT post to Telegram or LinkedIn yourself):
After writing the file, push directly to main — no branch, no PR:
  git checkout main
  git pull --ff-only origin main
  git add reports/linkedin/
  git commit -m "Daily LinkedIn drafts: $TODAY"
  git push origin main

The GitHub Action validates the file, splits each post into its own
Telegram message, and posts everything to the 💼 LinkedIn topic of the
BaseCamp supergroup. Your job ends at "git push".
```

## Layout

```
.github/
  topics.json                            # category → topic_id mapping
  workflows/post-to-telegram.yml         # trigger + orchestration
  scripts/post_to_telegram.py            # validate, split, route, send, pin
  scripts/notify_failure.py              # if: failure() — alert the channel
reports/
  .gitkeep
  opportunities/YYYY-MM-DD.md            # Opportunities routine output
  education/YYYY-MM-DD.md                # Education routine output
  news/YYYY-MM-DD.md                     # News routine output
  linkedin/YYYY-MM-DD.md                 # LinkedIn routine output (reads from news/)
state/
  posted.json                            # sha256 ledger (per file path)
  pinned.json                            # per-category map of pinned message_ids
```
