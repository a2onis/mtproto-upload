# Telegram MTProto proxy collector

A GitHub Actions job runs **once a day at 00:00 Asia/Tehran time** (20:30 UTC), retrieves recent messages from configured Telegram channels, extracts only valid Telegram MTProto proxy links, and writes the unique links to a `.txt` file in the **`results/`** folder of this repository, named with the date and time the results were received.

Nothing is uploaded anywhere: the file is committed straight back to the branch the workflow ran on, so the repository itself is the archive.

The workflow can also be started manually from **Actions → Collect MTProto proxies → Run workflow**.

## GitHub repository configuration

In **Settings → Secrets and variables → Actions**, add:

### Variables

- `CHANNEL_IDS` — required; comma-separated Telegram channel usernames or numeric IDs, e.g. `@public_channel,@another_public_channel`. You do not need to own or administer public channels; use their `@username` (recommended) and the authenticated Telegram account can read their public posts. For private channels, the account must be a member. A bare numeric channel ID may not be resolvable unless that Telegram session already has the channel's entity/access hash cached, so public usernames are the most reliable format.
- `LOOKBACK_HOURS` — optional; how far back each run reads, defaults to `24` so a daily run leaves no gap between runs.
- `RESULTS_DIR` — optional; folder the `.txt` files are written to and committed from, defaults to `results`.

### Secrets

- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` — credentials from [my.telegram.org](https://my.telegram.org).
- `TELEGRAM_SESSION` — a Telethon `StringSession` for an account that can read the channels. It is only needed for private channels and numeric IDs: without it the job falls back to scraping the public `https://t.me/s/<channel>` preview, which works for public usernames and needs no Telegram credentials. Generate it interactively on a trusted local machine (never in Actions logs and never commit it):

  ```python
  from telethon.sync import TelegramClient
  from telethon.sessions import StringSession

  with TelegramClient(StringSession(), API_ID, API_HASH) as client:
      print(client.session.save())
  ```

  Replace `API_ID` and `API_HASH` with local values, complete Telegram's login/2FA prompts, then save the printed string as the GitHub secret. Treat it like a password; it grants access to the Telegram account.

The workflow needs `permissions: contents: write` (already set in `.github/workflows/collect-proxies.yml`) so it can push the results commit.

## Schedule

`cron: "30 20 * * *"` — 20:30 UTC, which is **00:00 in Tehran**. Iran is UTC+03:30 year-round because DST was abolished in 2022, so this stays correct in summer and winter alike. GitHub Actions scheduled runs can start late under load, so the actual commit time may drift a few minutes.

## Output behavior

Files are named `mtproto-proxies-YYYY-MM-DD_HH-MM-SS.txt`, with the timestamp in `Asia/Tehran` taken once at the start of the run, so the name records when the results were received. For each channel the job reads newest-first and processes messages from the previous 24 hours, stopping at the cutoff; it does not cap the number of messages within that window. Links are deduplicated across all channels within a run.

It recognizes `tg://proxy?...` and `t.me/proxy?...` / `telegram.me/proxy?...` links containing `server`, `port`, and `secret`.

If a run finds no valid configs it writes **no file at all** and reports that in the job log — the results folder only ever contains real lists. The commit step checks for changes first, so an empty run produces no commit.

Because the lookback matches the schedule, a scheduled run delayed by more than 24 hours can leave a gap. Re-running the workflow manually does not backfill it; raise `LOOKBACK_HOURS` for that run if you need to.

The commit step rebases before pushing, so a manual run overlapping the scheduled one does not fail the job.

## Local checks

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```
