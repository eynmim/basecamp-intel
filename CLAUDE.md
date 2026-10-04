# CLAUDE.md — basecamp-intel

Context for any Claude Code session, scheduled routine, or agent working in
this repository. Read this before writing a report, changing the pipeline,
or searching for opportunities.

Operational detail (secrets, workflow triggers, migration steps, drop-in
routine prompts) lives in `README.md`. This file covers **who the intel is
for** and **how to decide what belongs in it**.

---

## 1. What this repo is

A serverless pipeline that turns scheduled research runs into a categorised
Telegram feed:

```
routine → reports/<category>/YYYY-MM-DD.md → git push main
        → .github/workflows/post-to-telegram.yml
        → .github/scripts/post_to_telegram.py  (validate → split → route → send/pin)
        → topic in the "BaseCamp Intel" supergroup   (bot: @Chavosh2_Bot)
```

A separate Cloudflare Worker (`worker/index.js`) handles the interactive
side of the same bot: `/ask`, `/fa`, `/translate`, `/linkedin`, `/keep`,
`/schedule`.

Categories and their topic IDs are in `.github/topics.json`.

**Category boundary — Opportunities vs Events.** Anything with a date Ali
attends or competes on goes to **Events** (`reports/events/`): conferences,
expos, hackathons, hardware/firmware contests, startup challenges, pitch
competitions, accelerator calls, meetups, workshops, webinars. **Opportunities**
keeps what he applies to for a position or money: degrees, scholarships,
fellowships, internships, jobs, research grants. Never list the same thing
in both.

**Routines never call `api.telegram.org` themselves.** A routine's job ends
at `git push`. The Action owns delivery.

---

## 2. Who this is for — capability profile

Everything the scouts search for is matched against this profile. Keep it
accurate; a stale profile is the single biggest cause of irrelevant results.

### Identity and legal status

| | |
|---|---|
| Name | Ali Mansouri |
| Based in | Turin, Italy |
| Citizenship | Iran (Iranian passport) |
| Residence | Italian *Permesso di Soggiorno* (study) → Schengen mobility |
| GitHub | [eynmim](https://github.com/eynmim) |
| Portfolio | eynmim.github.io |

### Education

- **MSc Computer Engineering — Embedded & Smart System Design**,
  Politecnico di Torino, 2025–2027 *(in progress)*.
  Coursework: computer architectures, embedded OS & architectures,
  electronics for embedded systems, SoC architecture, microelectronic
  systems, synthesis/optimisation of digital systems, cybersecurity for
  embedded systems, specification & simulation of digital systems.
- **BSc Electronics and Communication Engineering**, Politecnico di
  Torino, 2022–2025.

### Professional experience

| Role | Org | Period | Substance |
|---|---|---|---|
| Embedded IoT Engineer (part-time) | **P2CAM** | Aug 2024 – Mar 2026 | Owned end-to-end firmware architecture of a battery-powered ESP32-S3 IoT camera: 15+ subsystems, BLE-to-mobile protocol (<100 ms), multi-tier power management (6+ month battery, 99 % idle-power reduction), OTA, layered watchdogs, dual-core FreeRTOS, sustained 30 FPS LVGL UI |
| On-Board Electronics Engineer | **Stratobotic** | Sep – Dec 2024 | UAV power-distribution PCB schematic + layout in KiCad, MPPT circuits, Arduino GNSS tracking. Recommendation letter received |
| Embedded Software Developer | **PoliTO Robotic Team** | Nov 2023 – Sep 2024 | RTOS on STM32 under competition constraints; USART / CAN / SPI bring-up; C and Python |

### Technical stack, by depth

Use these tiers when judging "does this opportunity actually match".

- **Deep (can be hired on / can defend in interview):**
  ESP32-S3 + ESP-IDF, FreeRTOS (dual-core), embedded C, BLE protocol
  design, low-power / power-management architecture, OTA update systems,
  LVGL UI, STM32 (HAL + CubeIDE/CubeMX), KiCad PCB design.
- **Working (has shipped something real):**
  C++, Python, audio DSP — FFT / spectral subtraction / dual-mic
  beamforming, MEMS I2S microphones (INMP441), USART / CAN / SPI / I2C,
  PlatformIO + CMake, Git, Docker, computer vision with YOLO,
  Raspberry Pi + Picamera2, Altium, Keil.
- **Familiar (coursework or exploratory, do not oversell):**
  SoC architecture, HDL synthesis/optimisation, embedded cybersecurity,
  RISC-V, Zephyr, edge ML.

### Portfolio artefacts worth citing in an application

- **Life_logger** — ESP32-S3 dual-mic FFT beamforming with spectral
  subtraction and overlap-add reconstruction; 512-point hardware-accelerated
  FFT/IFFT via `esp-dsp`; FreeRTOS; INMP441 I2S mics. *The strongest single
  proof-of-work for anything DSP, audio, or sensor-array related.*
- **camera_project_repo** — YOLO object detection driving servo tracking;
  Raspberry Pi + Picamera2 + Flask MJPEG + pigpio.
- **STM32F411_DistanceSensor** — STM32 sensor firmware.
- **ROBOT** — PoliTO robotic team work.
- 5 public repos, 9 total (4 private).

### Certificates

Microcontroller Embedded C Programming (Udemy, 2024) · Mastering RTOS:
FreeRTOS and STM32Fx (Udemy, 2024) · Algorithms and Data Structures in C
(PoliTO, 2023).

### Languages

Persian (native) · English (fluent) · Italian (intermediate, improving).

Persian replies should use natural everyday Persian, not heavily
Arabic-loaded formal register. Keep dates and technical identifiers
(ESP32-S3, BLE 5.4, MSCA) in Latin script.

---

## 3. Eligibility rules — check these before including anything

These are hard filters. An opportunity that fails one is noise, not intel.

1. **Iranian passport.** Exclude anything ITAR-restricted, US-defence
   adjacent, or explicitly closed to Iranian nationals. Where nationality
   restrictions are plausible but unstated, say so — do not assume open.
2. **Italian PdS gives Schengen mobility, not EU citizenship.** Many EU
   funding lines (EIT Digital scholarships, some national schemes) are
   EU/EEA-only. State clearly which bucket an award falls in.
3. **DAAD Tehran and several Iranian embassy channels are suspended.**
   Route via Italy / direct portals and say so explicitly.
4. **Enrolled MSc student until 2027.** Full-time permanent roles that
   require immediate start are a mismatch; thesis placements, internships,
   working-student roles, and post-2027 pipelines are not.
5. **Third-Country National status** is an *advantage* for MSCA and some
   mobility schemes — flag it when it applies.

---

## 4. Relevance rubric

Score every candidate before it earns a numbered slot. Anything scoring
below "worth his afternoon" gets dropped, not padded into the report.

| Dimension | Strong match | Weak match — drop or demote |
|---|---|---|
| Technical fit | Names embedded / firmware / RTOS / DSP / BLE / IoT / PCB / edge-AI work he has shipped | Generic "software engineer", web, pure data science, pure ML research with no hardware |
| Eligibility | Confirmed open to Iranian nationals applying from Italy | Silent or restricted on nationality — include only with an explicit caveat |
| Timing | Deadline reachable, or a pipeline he must prepare for now | Already closed, or so far out it is not actionable |
| Leverage | He has a concrete artefact to point at (Life_logger, P2CAM firmware, Stratobotic PCB) | Nothing in his portfolio speaks to it |
| Value | Funded, credentialed, or a real career step | Unpaid, unknown organiser, credential-farming |

**Prefer 5 opportunities he will act on over 17 he will scroll past.**

---

## 5. Duplicate discipline — the Action owns this, not you

This was the pipeline's biggest failure mode. On 2026-05-21, 16 of 17 items
had already been posted the day before; on 2026-08-20 it was still 21 of 24.

**Since 2026-08-20 the Action suppresses duplicates itself.** Scouts do not
have to, and should not try to:

- **Keep writing the complete list every run**, exactly as before. The report
  file in git stays a full daily snapshot — that is the archive, and the
  deadline board is built from it.
- `post_to_telegram.py` keys every numbered item on its **first external
  link** and records it in `state/seen.json`. An item whose URL is already in
  the ledger is not delivered again.
- **Cadence:** Monday re-sends everything (a weekly full refresh, repetition
  intended). Tuesday–Sunday deliver only items never sent before, plus any
  item whose **deadline date moved** — those go out with a
  `🔄 Deadline moved: old → new` line on top.
- **The URL is the identity.** Always give an item a stable primary link and
  keep it stable across runs. A link that changes shape day to day
  (tracking parameters, a different mirror) re-posts as a new opportunity.
  Never link an aggregator when a canonical page exists.
- **State the deadline on a line that says so** — `Deadline: 31 Aug 2026`,
  `Application window: 18 Aug–01 Sep 2026`. Dates on lines without a
  deadline keyword are ignored on purpose, because prose like "catalogue
  unchanged since 20 Oct 2025" used to register as a moved deadline and
  re-send the item.
- The **ACTIVE DEADLINES board** is the carry-over surface. It is edited in
  place, and the Action rewrites each entry's arrow to deep-link to the
  original Telegram message for that opportunity. Keep one line per entry
  and keep the entry's link identical to the item's link — that is how the
  two are matched.
- A quiet day is a valid result. The Action will simply deliver the sections
  and the refreshed board.

---

## 6. Report contract

`post_to_telegram.py` runs `validate_report()` and **aborts the workflow on
drift**, so these are mechanical requirements, not style preferences:

- **Title line** — a `<b>...</b>` line before the first section divider.
- **Section divider** — `<b>═ SECTION NAME ═</b>` alone on its line.
- **Numbered item** — `<b>N. Title</b>` at the start of a line. A bare
  `N. Title` without the `<b>` wrapper fails validation.
- Telegram-flavoured HTML only: `<b>`, `<i>`, `<a href="">`, `<code>`,
  `<pre>`. No markdown, no `<p>`, no `<br>`.
- Escape `&` as `&amp;` and `<` as `&lt;` in plain text.
- Every numbered item stays under 3500 characters.
- Balanced `<b>` / `<i>` / `<a>` tags.

Enforced by convention, **not** by the validator — nothing will fail the run,
but break these and the feed degrades:

- Each numbered item ends with 3–5 hashtags from the taxonomy in
  README §Hashtag taxonomy. Do not invent tags without updating that table.
- Every numbered item carries a primary link; it is the item's identity for
  duplicate suppression (§5). An item with no link can never be deduplicated.
- Keep the deadline board under ~2 KB. Past 3000 chars the Action trims it
  from the bottom and appends a "+N more" line; past 3500 the validator
  aborts the entire run. It reached 2906 in June 2026.

Splitting is structural: title block → one message; each numbered item →
one message; the section header rides on the first item beneath it.

`opportunities` and `events` have `has_deadline_board: true`. The events
board is headed `<b>═ 📅 EVENT CALENDAR ═</b>` instead of ACTIVE DEADLINES.

---

## 7. Hard rules

- **Never invent a deadline, stipend, or eligibility fact.** If it is not
  on the official page, write "unconfirmed" and link where to verify.
  A wrong deadline is worse than a missing one.
- **Always link the primary source**, not an aggregator blog.
- **Do not post to Telegram from a routine.** The Action owns delivery.
- **Do not hand-edit `state/posted.json`, `state/pinned.json`, or
  `state/seen.json`.** They are machine-managed and auto-committed with
  `[skip ci]`. Deleting `seen.json` makes the next run re-deliver everything.
- **Do not write auxiliary files into `reports/`** (no `error.log`, no
  scratch). The workflow triggers on any push under `reports/`.
- Push straight to `main` — no branch, no PR.
- Secrets (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_CHANNEL_ID`)
  live in GitHub Actions secrets and the Cloudflare dashboard. Never commit
  a value, never echo one into a log.

---

## 8. Preferences to keep current

Ali should edit this block directly; scouts treat it as authoritative.

- **Primary goal:** funded PhD / MSCA doctoral position in embedded systems,
  DSP, or edge AI in the EU, starting after the MSc completes in 2027.
- **Secondary:** paid MSc thesis placements and R&D internships at EU
  embedded houses — imec, Fraunhofer IIS, Nordic, ST, NXP, Espressif, Bosch.
- **Also wanted:** scholarships that stack with the PoliTO MSc, and
  hardware competitions with real prize money and a portfolio payoff
  (competitions are reported under Events, not Opportunities).
- **Events:** conferences, expos, hackathons, contests, startup challenges,
  meetups, workshops and webinars in embedded / IoT / edge AI / hardware
  startups. In person in Italy (Turin and Milan first), elsewhere in the EU
  only when worth the trip, plus online.
- **Region priority:** Italy > EU (DE / NL / SE / BE) > global remote.
  Non-EU relocations (Japan, Korea, Switzerland) only when fully funded.
- **Not interested in:** unpaid work, pure web/frontend roles, generic
  data-science positions, anything requiring an immediate full-time start
  before 2027.
