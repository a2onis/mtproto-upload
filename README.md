# Telegram MTProto proxy collector and UploadKon uploader

A GitHub Actions job runs at **06:00 and 18:00 Asia/Tehran time** (02:30 and 14:30 UTC), retrieves recent messages from configured Telegram channels, extracts only valid Telegram MTProto proxy links, writes unique links to a Tehran-timestamped `.txt` file, and uploads it to UploadKon.

The workflow can also be started manually from **Actions → Collect and upload MTProto proxies → Run workflow**.

## GitHub repository configuration

In **Settings → Secrets and variables → Actions**, add:

### Variables

- `CHANNEL_IDS` — required; comma-separated Telegram channel usernames or numeric IDs, e.g. `@public_channel,@another_public_channel`. You do not need to own or administer public channels; use their `@username` (recommended) and the authenticated Telegram account can read their public posts. For private channels, the account must be a member. A bare numeric channel ID may not be resolvable unless that Telegram session already has the channel's entity/access hash cached, so public usernames are the most reliable format.
- `UPLOADKON_MODE` — optional; `auto` (default) sends the file to the public `uploadkon.ir` uploader first and falls back to the `my.uploadkon.ir` drive API when a cookie is configured. Use `public` to only use the anonymous uploader (no secrets needed) or `drive` to only use the account drive API.
- `UPLOADKON_PUBLIC_URL` — optional; public upload endpoint, defaults to `https://uploadkon.ir/`.
- `UPLOADKON_PUBLIC_ARCHIVE` — optional; `1` (default) uploads a `.zip` containing the `.txt`, because the anonymous endpoint rejects `.txt` with *«پسوند "txt" پشتیبانی نمی‌شود»*. Set to `0` to send the raw `.txt` (only works if the host starts accepting text files).
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
- `UPLOADKON_PUBLIC_COOKIE` — optional; a browser cookie for `uploadkon.ir` if the anonymous endpoint asks for one.
- `UPLOADKON_PROXY` — optional; HTTP(S) proxy URL used for the upload requests.
- `UPLOADKON_TRANSPORT_RETRIES` — optional; defaults to `3`. How often a request is re-sent on a fresh socket when the host drops the connection before answering (`('Connection aborted.', RemoteDisconnected(...))`). Only failures where no response byte arrived are re-sent, so an upload is never duplicated. Set to `0` to disable.
- `UPLOADKON_CONNECTION_CLOSE` — optional; defaults to off. Set to `1` to send `Connection: close` and force a new TCP+TLS handshake per request (the previous behaviour). Keep-alive is the default because it matches what a browser does and needs fewer handshakes.
- `UPLOADKON_TCP_MSS` — optional; defaults to `1000`, clamping the outgoing TCP MSS to dodge PMTU blackholes on routes to `.ir` hosts. Set to `0` to keep the kernel default if the clamp itself looks like the problem.
- `UPLOADKON_DIAGNOSE_ON_FAILURE` — optional; defaults to on. When every endpoint fails, run the upload host probe and print which request the host stopped answering. Set to `0` to skip it.

`UPLOADKON_TOTAL_TIMEOUT` is now split between endpoints: the first one may use at most 60 % of the remaining budget, so a host that ignores every request cannot leave the fallback endpoint with nothing (`only -21s of the configured upload budget remain`).

### Diagnosing a host that drops requests

`python fetch_and_upload.py --diagnose-upload [file]` sends progressively closer approximations of the real upload and prints the first one the host does not answer: a plain page `GET`, the same `GET` with the uploader's headers, a form-only `POST`, then three multipart uploads that form a matrix over **size** and **contents** (small/benign, real size/benign, the real file), then the drive listing `GET` and the drive upload `POST`. From which step fails it reports whether the host drops larger bodies, rejects the file's contents, or refuses the multipart upload at all - a distinction a failed upload cannot make on its own. The benign upload's *request body* is padded with random bytes to match the real one, so the two are comparable after compression. It exits non-zero when a probe goes unanswered.

### Matching the browser request

The drive upload is built to match a captured working browser request byte for byte apart from the boundary:

- the `file` part comes **first**, ahead of `clientMime`/`clientExtension`/`uploadType`/`backendId`/`parentId`/`workspaceId`/`relativePath` (`requests` appends the file part last);
- plain form fields carry only `Content-Disposition` - no `Content-Type: application/octet-stream`, which urllib3's encoder adds;
- no `X-Requested-With` header, and the `Sec-Fetch-Dest`/`-Mode`/`-Site` triple is sent;
- every cookie is sent, including `remember_web_...`, and `X-XSRF-TOKEN` is the URL-decoded `XSRF-TOKEN` from the latest response;
- the session is warmed with the same listing query the web UI issues (`section=home&folderId=0&workspaceId=0&orderBy=created_at&orderDir=desc&page=1`);
- the `201` reply carries a relative `fileEntry.url` (`api/v1/file-entries/76344`), which is resolved against the API origin so a usable link is reported.

UploadKon's browser session credentials expire and may need to be refreshed. Rotate/revoke any session cookies or XSRF values that have been shared outside GitHub Secrets; do not paste credentials into source code, issues, or logs. The request example provided for this project contained session-cookie/XSRF credentials, so invalidate that session and create fresh values before running this workflow.

## Output behavior

Files are named `mtproto-proxies-YYYY-MM-DD_HH-MM-SS.txt`, with the timestamp in `Asia/Tehran`. For each channel, the job reads newest-first and processes messages from the previous 12 hours, stopping at the cutoff; it does not cap the number of messages within that window. It deduplicates links within each output file. GitHub Actions scheduled runs can be delayed, so a delay longer than the 12-hour lookback may leave a gap. It recognizes `tg://proxy?...` and `t.me/proxy?...` / `telegram.me/proxy?...` links containing `server`, `port`, and `secret`. If no valid configs are found, an empty local file is created and upload is skipped. Files are created on the ephemeral Actions runner and are not committed to the repository.

## Upload reliability

The generated `.txt` is uploaded straight to UploadKon from the same job, with retries and two interchangeable endpoints:

- **`public`** (default first choice) — `POST https://uploadkon.ir/` with `submitr=1`, `ajax=1` and the `file` field, exactly like the site's browser AJAX flow. No login is needed. The site rejects text extensions (`txt`, `log`, `csv`, `json`, …), so the list is wrapped in `mtproto-proxies-YYYY-MM-DD_HH-MM-SS.txt.zip` (disable with `UPLOADKON_PUBLIC_ARCHIVE=0`). A successful reply returns the direct link and a delete code, both printed as notices. The free tier allows 100 MB and removes files after 15 days, so the Actions artifact remains the long-term copy.
- **`drive`** — the authenticated `my.uploadkon.ir` drive API using `UPLOADKON_COOKIE`/`UPLOADKON_XSRF_TOKEN`; this one accepts the raw `.txt`. It has been observed to hang for minutes on GitHub runners, so it is only used as the `auto` fallback.

Per endpoint, the uploader:

- loads `https://uploadkon.ir/` first so any session/anti-bot cookie is present, then reuses it for the public upload;
- refreshes the UploadKon session/`XSRF-TOKEN` before each drive attempt;
- **carries the cookies from every response over to the next request**: UploadKon rotates both `XSRF-TOKEN` and `aplod_kn_session` in the `set-cookie` headers of each reply (including error replies), so the uploader stores all of them in the session cookie jar and sends the newest values back, plus the URL-decoded `XSRF-TOKEN` in the `X-XSRF-TOKEN` header. `UPLOADKON_COOKIE`/`UPLOADKON_XSRF_TOKEN` are only the seed for the first request;
- reuses one HTTP session (and therefore one cookie jar) for all attempts at an endpoint, so a retry after a `419` sends the token the server just issued instead of the stale one;
- uses a separate connect timeout (30 s) and a configurable read timeout (`UPLOADKON_TIMEOUT`, default 120 s);
- retries timeouts, connection resets, HTTP `408`/`425`/`429`/`5xx`, non-JSON replies, and expired session/CSRF responses (`401`/`403`/`419`) up to `UPLOADKON_MAX_ATTEMPTS` times (default 4) with exponential backoff (5 s, 10 s, 20 s, capped at 30 s);
- stays inside an overall `UPLOADKON_TOTAL_TIMEOUT` budget (default 600 s) so retries never outlast the job;
- fails fast for permanent errors such as `400`/`419` or a rejection with no file link, then moves on to the next endpoint;
- rebuilds the upload payload for every attempt so a retry always sends the full body;
- re-sends a request on a fresh socket when the host drops the connection without answering, instead of spending a whole attempt (and its 5-30 s backoff) on it;
- reports the runner's public egress IP when a host refuses the connection, so the IP can be allowlisted on the host or routed through `UPLOADKON_PROXY`;
- fetches the returned public link once to confirm it really downloads (a broken link becomes a warning, not a false success);
- optionally routes through a proxy (`UPLOADKON_PROXY`, e.g. `http://user:pass@host:8080`) if the runner IP is blocked.

If every attempt at every endpoint fails, the job fails with the collected error details, but the proxy list is always kept as the `mtproto-proxies` workflow artifact. A retry after a read timeout can occasionally create a duplicate file on UploadKon, since the server may have received the first request.

## Local checks

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```
