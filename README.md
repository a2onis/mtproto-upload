# Telegram MTProto proxy collector and UploadKon uploader

A GitHub Actions job runs at **06:00 and 18:00 Asia/Tehran time** (02:30 and 14:30 UTC), retrieves recent messages from configured Telegram channels, extracts only valid Telegram MTProto proxy links, writes unique links to a Tehran-timestamped `.txt` file, and uploads it to UploadKon.

The workflow can also be started manually from **Actions → Collect and upload MTProto proxies → Run workflow**.

## GitHub repository configuration

In **Settings → Secrets and variables → Actions**, add:

### Variables

- `CHANNEL_IDS` — required; comma-separated Telegram channel usernames or numeric IDs, e.g. `@public_channel,@another_public_channel`. You do not need to own or administer public channels; use their `@username` (recommended) and the authenticated Telegram account can read their public posts. For private channels, the account must be a member. A bare numeric channel ID may not be resolvable unless that Telegram session already has the channel's entity/access hash cached, so public usernames are the most reliable format.
- `UPLOADKON_URL` — optional; defaults to `https://my.uploadkon.ir/api/v1/file-entries`.
- `UPLOADKON_BACKEND_ID` — optional; defaults to `cdc_W8Ekm8` from the supplied upload form.
- `UPLOADKON_PARENT_ID` — optional; defaults to the drive root (empty).
- `UPLOADKON_WORKSPACE_ID` — optional; defaults to `0`.
- `UPLOADKON_UPLOAD_TYPE` — optional; defaults to `bedrive`.

### Secrets

- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` — credentials from [my.telegram.org](https://my.telegram.org).
- `TELEGRAM_SESSION` — a Telethon `StringSession` for an account that can read the channels. Generate it interactively on a trusted local machine (never in Actions logs and never commit it):

  ```python
  from telethon.sync import TelegramClient
  from telethon.sessions import StringSession

  with TelegramClient(StringSession(), API_ID, API_HASH) as client:
      print(client.session.save())
  ```

  Replace `API_ID` and `API_HASH` with local values, complete Telegram's login/2FA prompts, then save the printed string as the GitHub secret. Treat it like a password; it grants access to the Telegram account.
- `UPLOADKON_COOKIE` — the current full `Cookie` header value from an authenticated UploadKon session.
- `UPLOADKON_XSRF_TOKEN` — the XSRF token expected in the `X-XSRF-TOKEN` request header (URL-decode the `XSRF-TOKEN` cookie value if necessary).

UploadKon's browser session credentials expire and may need to be refreshed. Rotate/revoke any session cookies or XSRF values that have been shared outside GitHub Secrets; do not paste credentials into source code, issues, or logs. The request example provided for this project contained session-cookie/XSRF credentials, so invalidate that session and create fresh values before running this workflow.

## Output behavior

Files are named `mtproto-proxies-YYYY-MM-DD_HH-MM-SS.txt`, with the timestamp in `Asia/Tehran`. For each channel, the job reads newest-first and processes messages from the previous 12 hours, stopping at the cutoff; it does not cap the number of messages within that window. It deduplicates links within each output file. GitHub Actions scheduled runs can be delayed, so a delay longer than the 12-hour lookback may leave a gap. It recognizes `tg://proxy?...` and `t.me/proxy?...` / `telegram.me/proxy?...` links containing `server`, `port`, and `secret`. If no valid configs are found, an empty local file is created and upload is skipped. Files are created on the ephemeral Actions runner and are not committed to the repository.

## Local checks

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```
