# Telegram MTProto proxy collector and UploadKon uploader

A GitHub Actions job runs at **06:00 and 18:00 Asia/Tehran time** (02:30 and 14:30 UTC), retrieves recent messages from configured Telegram channels, extracts only valid Telegram MTProto proxy links, writes unique links to a Tehran-timestamped `.txt` file, and uploads it to UploadKon.

The workflow can also be started manually from **Actions → Collect and upload MTProto proxies → Run workflow**.

## GitHub repository configuration

In **Settings → Secrets and variables → Actions**, add:

### Variables

- `CHANNEL_IDS` — required; comma-separated Telegram channel usernames or numeric IDs, e.g. `@public_channel,@another_public_channel`. You do not need to own or administer public channels; use their `@username` (recommended) and the authenticated Telegram account can read their public posts. For private channels, the account must be a member. A bare numeric channel ID may not be resolvable unless that Telegram session already has the channel's entity/access hash cached, so public usernames are the most reliable format.
- `UPLOADKON_MODE` — optional; `auto` (default) sends the file to the public `uploadkon.ir` uploader first and falls back to the `my.uploadkon.ir` drive API when a cookie is configured. Use `public` to only use the anonymous uploader (no secrets needed) or `drive` to only use the account drive API.
- `UPLOADKON_PUBLIC_URL` — optional; public upload endpoint, defaults to `https://uploadkon.ir/`.
- `UPLOADKON_URL` — optional; authenticated drive API used when the mode includes `drive`; defaults to `https://my.uploadkon.ir/api/v1/file-entries`.
- `UPLOADKON_BACKEND_ID` — optional; defaults to `cdc_W8Ekm8` from the supplied upload form.
- `UPLOADKON_PARENT_ID` — optional; defaults to the drive root (empty).
- `UPLOADKON_WORKSPACE_ID` — optional; defaults to `0`.
- `UPLOADKON_UPLOAD_TYPE` — optional; defaults to `bedrive`.
- `UPLOADKON_MAX_ATTEMPTS` — optional; how many times a failed upload is retried (default `4`).
- `UPLOADKON_TIMEOUT` — optional; read timeout in seconds per upload attempt (default `120`).
- `UPLOADKON_TOTAL_TIMEOUT` — optional; total seconds allowed for all upload attempts and endpoints (default `600`).

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
- `UPLOADKON_COOKIE` — optional when `UPLOADKON_MODE=public`; the current full `Cookie` header value from an authenticated UploadKon session (required for `drive`).
- `UPLOADKON_XSRF_TOKEN` — the XSRF token expected in the `X-XSRF-TOKEN` request header (URL-decode the `XSRF-TOKEN` cookie value if necessary).

UploadKon's browser session credentials expire and may need to be refreshed. Rotate/revoke any session cookies or XSRF values that have been shared outside GitHub Secrets; do not paste credentials into source code, issues, or logs. The request example provided for this project contained session-cookie/XSRF credentials, so invalidate that session and create fresh values before running this workflow.

## Output behavior

Files are named `mtproto-proxies-YYYY-MM-DD_HH-MM-SS.txt`, with the timestamp in `Asia/Tehran`. For each channel, the job reads newest-first and processes messages from the previous 12 hours, stopping at the cutoff; it does not cap the number of messages within that window. It deduplicates links within each output file. GitHub Actions scheduled runs can be delayed, so a delay longer than the 12-hour lookback may leave a gap. It recognizes `tg://proxy?...` and `t.me/proxy?...` / `telegram.me/proxy?...` links containing `server`, `port`, and `secret`. If no valid configs are found, an empty local file is created and upload is skipped. Files are created on the ephemeral Actions runner and are not committed to the repository.

## Upload reliability

The generated `.txt` is uploaded straight to UploadKon from the same job, with retries and two interchangeable endpoints:

- **`public`** (default first choice) — `POST https://uploadkon.ir/` with `submitr=1`, `ajax=1` and the `file` field, exactly like the site's browser AJAX flow. No login is needed; the response returns a direct file link and a delete code, both printed as notices. The free tier accepts up to 100 MB and removes files after 15 days.
- **`drive`** — the authenticated `my.uploadkon.ir` drive API using `UPLOADKON_COOKIE`/`UPLOADKON_XSRF_TOKEN`, used as the fallback in `auto` mode (it has been observed to hang for minutes on GitHub runners).

Per endpoint, the uploader:

- refreshes the UploadKon session/`XSRF-TOKEN` before each drive attempt;
- uses a separate connect timeout (30 s) and a configurable read timeout (`UPLOADKON_TIMEOUT`, default 120 s);
- retries timeouts, connection resets, HTTP `408`/`425`/`429`/`5xx`, non-JSON replies, and expired session/CSRF responses (`401`/`403`/`419`) up to `UPLOADKON_MAX_ATTEMPTS` times (default 4) with exponential backoff (5 s, 10 s, 20 s, capped at 30 s);
- stays inside an overall `UPLOADKON_TOTAL_TIMEOUT` budget (default 600 s) so retries never outlast the job;
- fails fast for permanent errors such as `400` (bad request) or a rejection with no file link, then moves on to the next endpoint;
- reopens the file for every attempt so a retry always sends the full body.

If every attempt at every endpoint fails, the job fails with the collected error details, but the proxy list is always kept as the `mtproto-proxies` workflow artifact. A retry after a read timeout can occasionally create a duplicate file on UploadKon, since the server may have received the first request.

## Local checks

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```
