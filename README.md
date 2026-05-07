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
- The Python script (`.github/scripts/post_to_telegram.py`) reads the file,
  converts markdown -> Telegram-flavoured HTML if needed, splits to
  3800-char chunks (Telegram's hard limit is 4096), and posts each chunk via
  `sendMessage` with `parse_mode=HTML`.
- If Telegram returns `ok: false` for any chunk, the workflow fails with the
  API's error description so the failure is visible in the Actions log.

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

## Layout

```
.github/
  workflows/post-to-telegram.yml   # trigger + orchestration
  scripts/post_to_telegram.py      # markdown -> HTML, chunking, sendMessage
reports/
  .gitkeep                         # keeps the directory in git
  YYYY-MM-DD.md                    # written by the Claude routine
  YYYY-MM-DD-telegram.html         # optional pre-rendered HTML variant
```
