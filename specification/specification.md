# Nikon Imaging Cloud — Image Transfer Downloader

## 1. Purpose

An application that performs a **one-way sync** (download only) of images from
**Nikon Imaging Cloud** to local disk (or another configured destination). It never
uploads to, modifies, or deletes from the cloud.

When photos are taken with a Nikon camera they can be transferred over Wi-Fi to Nikon
Imaging Cloud. The cloud holds them **temporarily** (each item has a lifetime / storing
period and eventually expires). This tool grabs those images locally before they expire,
as a durable backup, without manually clicking through the web UI.

> **No images are removed from the imaging cloud by this tool** (download-only).

## 2. Status of this specification

There is **no official, documented public API** for Nikon Imaging Cloud. Sections 4–6
were reverse-engineered from the public web client (`https://imagingcloud.nikon.com`, a
Next.js SPA) on 2026-06-15 by inspecting its JavaScript bundles and probing endpoints.
**Endpoints, field names, the auth flow and operation codes are unofficial and may
change without notice.** Treat this as a working hypothesis to validate against live
traffic, not a contract.

The URLs the user originally noted map to the SPA, not the data API:
- `https://imagingcloud.nikon.com/transfer/list/` is the **web page**; the underlying
  data call is `POST https://api.user.cwp.imagingcloud.nikon.com/bff/transfer/list`.
- Auth is at `accounts.cld.nikon.com` (the `auth.` prefix may also resolve; the active
  login UI observed is `accounts.cld.nikon.com/login?service_id=nic_local`).

## 3. High-level architecture (observed)

| Layer | Host | Notes |
|-------|------|-------|
| Web frontend | `imagingcloud.nikon.com` | Next.js static SPA on S3 + CloudFront. China variant: `imagingcloud.nikon.com.cn` |
| Account / auth | `accounts.cld.nikon.com` | Nikon Centralized Account System (NCAS), Keycloak-based OIDC |
| Data API (BFF) | `api.user.cwp.imagingcloud.nikon.com` | Backend-for-frontend, `/bff/*` routes, JSON over POST |
| File storage | presigned URLs | `original_file_url` / `thumbnail_file_url` returned per item (S3/CloudFront-style) |

Access constraints to be aware of:

- **User-Agent gating:** requests without a browser-like `User-Agent` are served a
  `/sorry` page. The client must send a realistic browser UA.
- **Region gating:** the service is country-restricted; the SPA has `/area`,
  `/country`, `/inaccessible` flows. Requests may need a valid `country` value.
- **Terms acceptance:** accounts that have not accepted current terms are redirected to
  `accounts.cld.nikon.com/login/complete?service_id=nic_local`.

## 4. Authentication (observed)

OIDC (Keycloak) behind Nikon's NCAS gateway:

- Login UI: `https://accounts.cld.nikon.com/login?service_id=nic_local&lang=<lang>`
- Standard Keycloak endpoints exist under the realm: `openid-connect/auth`, `/token`,
  `/userinfo`, `/logout`.
- Flow: **authorization code + PKCE** (`code_challenge_method`, `code_challenge`),
  `grant_type=authorization_code`, refresh via `grant_type=refresh_token`,
  `scope=openid`. The web app uses the `keycloak-js` adapter; the realm and `client_id`
  are loaded at runtime (not baked into the bundle) and the realm is not directly
  discoverable (the gateway returns 302/403 for `.well-known` probes).
- The resulting **access token is passed to the data API in a custom header named
  `access_token`** (NOT `Authorization: Bearer`).

**Confirmed by live capture (2026-06-15):**
- Token endpoint: `https://auth.accounts.cld.nikon.com/auth/realms/user/protocol/openid-connect/token`
  (realm = `user`).
- `client_id` = `c0004`.
- Access token lifetime `expires_in` = **300 s** (5 minutes); a `refresh_token`
  is issued, so unattended refresh via the refresh-token grant works.
- **Refresh tokens rotate**: each refresh invalidates the previous refresh
  token and returns a new one. The persisted session **must** be overwritten
  with the rotated token after every refresh, or the next refresh gets HTTP
  400. (The engine does this; ad-hoc scripts that don't will burn the token.)

### Authentication strategy for this tool (to decide — see §9)

The realm/client_id are runtime-loaded and login sits behind Nikon's branded gateway, so
the most robust approach is likely:

1. **Browser-assisted login + token capture** (recommended starting point): drive a real
   browser (e.g. Playwright) through the NCAS login once using the configured
   credentials, capture the `access_token` (and refresh token), then call the BFF API
   directly. Refresh as needed via the Keycloak token endpoint. This survives the
   continuous-service requirement (§7) as long as the refresh token stays valid.
2. **Manual token paste:** user logs in via their own browser, copies the `access_token`
   from devtools, pastes into the tool. Simplest; poor UX; token is short-lived.
3. **Full headless PKCE replication:** only viable if a stable `client_id`/realm and
   redirect URI can be determined; most brittle.


FEEDBACK: Option 1 perfered as a start!

### Credentials & secrets

Credentials must come from **configuration / environment / OS keychain**, never be
committed to the repo. Suggested config keys:

```
NIC_USERNAME   # Nikon account email
NIC_PASSWORD   # Nikon account password   (store in env var or OS secret store)
NIC_COUNTRY    # country code required by the API
NIC_DEST_DIR   # local destination root
```

> ⚠️ **Security:** Do not store the password in this spec or any tracked file. Use an
> `.env` (git-ignored), environment variables, or a secret manager / OS keychain.

## 5. API conventions (observed)

- Base URL: `https://api.user.cwp.imagingcloud.nikon.com`
- Method: `POST`, `Content-Type: application/json`
- Auth header: `access_token: <token>`
- Each call carries an **operation code** identifier (e.g. `IF_FR100_H07`,
  `IF_FR000_H01`, `IF_DU0xx`). Confirm whether it is a real request header when
  validating against live traffic.

### Relevant endpoints

| Route | Op code (example) | Purpose |
|-------|-------------------|---------|
| `/bff/transfer/list` | `IF_FR100_H07` | List stored images (the core endpoint for this tool) |
| `/bff/transfer` | — | Transfer feature root |
| `/bff/transfer/set` | — | Configure transfer to third-party cloud storage |
| `/bff/home` | — | Home dashboard / connected products |
| `/bff/home/ConnectionStatus` | — | Camera connection status |
| `/bff/home/deleteProduct` | — | Remove a registered product (not used by this tool) |

## 6. Core endpoint: `POST /bff/transfer/list`

**Request body** (field names as observed):

```json
{
  "country": "SE",
  "offset": 1,
  "limit": 20,
  "thirdpcs_list": [],
  "device_id_list": [],
  "file_format_id_list": [],
  "lifetime": 30,
  "sort_condition": "0",
  "sort_desc": "1"
}
```

- Pagination: **`offset` is a 1-based *item* offset**, not a page number.
  `offset=1` returns items 1..`limit`; the next window is `offset + limit`
  (i.e. 1, 21, 41, …). `offset=0` is rejected with HTTP 500. Empty results
  mark the end. *(Confirmed live; an earlier "page number" reading was wrong.)*
- `limit` max ~20: the web app uses 20, and `limit=300` is rejected with 500.
- Types are strict: `offset`/`limit`/`lifetime` are **integers**,
  `sort_condition`/`sort_desc` are **strings**. Sending the wrong type, or an
  out-of-range `lifetime` (e.g. 365), returns HTTP 400/500 with `error_code`.
- Confirmed default body sent by the web app: `country=SE` (region-specific),
  `limit=20`, `lifetime=30`, `sort_condition="0"`, `sort_desc="1"`, with the
  three filter lists empty.
- `device_id_list` filters by camera; `file_format_id_list` filters by file
  type. `thirdpcs_list` targets third-party cloud storage (not needed here).

**Response** — the image array is **nested under `item_info`**, not top-level:

```json
{
  "item_info": { "total_items_count": <int>, "item_list": [ ... ] },
  "camera": [...], "service": [...], "transferStatus": {...},
  "getUploadlimitInfo": {...}, "excluded_transfer_files_list": [...]
}
```

`total_items_count` is the count *in this window* (== returned length), **not**
the grand total, so iterate until a short window. Each item in `item_list`
(field names as seen live):

| Field | Meaning |
|-------|---------|
| `id` | Image identifier (ULID) |
| `name` | Full file name incl. extension, e.g. `_NZF5080.NEF` |
| `file_extension` | **Category**, e.g. `RAW`, `JPEG` (not the literal ext) |
| `original_file_url` | **Direct download URL for the full-resolution file** |
| `thumbnail_file_url` | Thumbnail download URL |
| `thumbnail_file_status` | Thumbnail availability flag |
| `device_name` | Camera that captured it, e.g. `NIKON Z f` |
| `upload_date` | ISO-8601 w/ ms + offset, e.g. `...+02:00` |
| `shooting_date` | ISO-8601 capture time (drives the YYYY/MM/DD layout) |
| `lifetime` | Remaining storing period in days (e.g. `29`) |
| `picture_control_name` | Picture Control applied, e.g. `AUTO` |
| `thirdpcs_transfer_status_list` | Per third-party-cloud transfer status |
| `c2pa_manifest_existance` | C2PA manifest present (`"0"`/`"1"`) |

Note: `original_file_size` / `image_size` (seen in the JS bundle) are **not**
present in the live list response, so size-based checks must be optional.

## 7. Application behaviour

### Download / sync flow

1. Authenticate and obtain an `access_token` (§4).
2. Page through `POST /bff/transfer/list` (offset/limit) to enumerate items, optionally
   filtered by camera and/or file format.
3. For each selected item, `GET original_file_url` to download the **full-resolution**
   file (`thumbnail_file_url` only for previews).
4. Write into a **`YYYY/MM/DD`** directory structure under the destination, derived from
   `shooting_date`, preserving the original filename and capture timestamps.
5. **Idempotent / resumable:** skip files already downloaded (match on `id` and/or
   name + `original_file_size`). One-way only — never delete or modify cloud-side.
6. Optionally persist `id` + size in a local manifest to detect partial/failed
   downloads and avoid re-fetching.

### Image selection

- It must be **selectable which images to download**, typically `.NEF` (RAW) files.
- **First milestone:** mirror *all* images (no filtering UI needed yet).
- Later: filter by file format (RAW/JPEG), camera, and/or date range.

### Run modes

- **One-shot:** authenticate, sync new images, exit.
- **Service / daemon:** run continuously and **poll** Nikon Imaging Cloud on an interval
  for newly transferred images, downloading them before they expire.

### Platforms

- Must run on **Linux and macOS**, **preferably Windows** as well. Favour a
  cross-platform language/runtime and avoid OS-specific path/keychain assumptions
  (abstract the secret store).

### Functional requirements

- One-way (download-only) sync; never write to the cloud.
- Configurable destination root; `YYYY/MM/DD` layout from `shooting_date`.
- Resume safely after interruption; do not re-download existing files.
- Respect token expiry; refresh transparently for long-running service mode.
- Send a browser-like `User-Agent` and the required `country`.
- Optional filters: file format (e.g. `.NEF`), camera (`device_id_list`), date range.
- Sensible rate limiting / backoff for the API and the presigned download URLs.

### Non-functional / operational notes

- Images expire from the cloud (`lifetime`) — the service must poll often enough to
  catch them before expiry.
- Presigned download URLs are likely short-lived; download soon after listing.
- Surface auth / region / UA errors clearly.

## 8. User interface

### Framework: NiceGUI

The GUI uses **[NiceGUI](https://nicegui.io)** (Python, async, built on FastAPI).
Rationale:

- **Async-native** — matches Playwright's async API (login/token capture) and lets the
  poll loop run as an `asyncio` background task without blocking the UI.
- **Real event model** (not full-page reruns) — supports live progress bars, streaming
  logs, and a responsive thumbnail grid with selection.
- **Browser or native window** — the same code can run in the browser or as a desktop
  window (via `pywebview`); cross-platform (Linux/macOS/Windows).

> Streamlit is a viable simpler fallback, but its rerun-on-interaction model is awkward
> for live progress and long-running tasks, so NiceGUI is preferred.

### Architecture: engine vs. UI (keep separate)

- **Core engine** — headless, dependency-free of the GUI: auth/token store, API client,
  lister, downloader, sync state/manifest, and the poll scheduler. Must run standalone
  as the service/daemon (§7 run modes) with **no GUI required** (important for headless
  Linux).
- **NiceGUI front-end** — an *optional* control panel over the engine. It triggers engine
  actions and visualises state; it must not own core logic. Because NiceGUI is async,
  the poller can run in-process beside the UI when the GUI is used, or as a separate
  process/service otherwise.

### Login integration (Option 1 — preferred, per feedback)

NiceGUI **cannot** host Nikon's login in its own tab (cross-origin + Nikon's CSP
`frame-ancestors` forbid framing, and JS can't read a cross-origin token). Instead:

1. User clicks **"Log in to Nikon"** in the NiceGUI app.
2. The engine launches a **headed Playwright browser window** to the NCAS login URL
   (`accounts.cld.nikon.com/login?service_id=nic_local`).
3. User completes login there; Playwright intercepts network traffic and captures the
   `access_token` (+ refresh token).
4. Token is persisted to the secret store; NiceGUI shows **"Connected"** and refreshes
   transparently thereafter.
5. **Manual token paste** remains a fallback input field for when browser automation is
   unavailable.

### Screens / components (initial scope)

- **Connection / login:** login button + status (connected / token expiry), account email.
- **Settings:** destination root, `country`, poll interval, file-format filter
  (All / RAW `.NEF` / JPEG), optional camera filter. Backed by the §4 config keys.
- **Images:** paged thumbnail grid (`thumbnail_file_url`) with per-item metadata
  (name, camera, shooting date, size, expiry/`lifetime`); checkboxes to select items;
  "select all". First milestone may simply mirror all.
- **Sync:** Start / Stop, per-file and overall progress, a live log, and counts
  (queued / downloaded / skipped / failed).
- **Service status:** when running as a poller — last poll time, next poll, items found.

### UI requirements

- Never expose the password in the UI after entry; show only connection state.
- Long operations run as background tasks; the UI stays responsive (no blocking calls).
- Clear surfacing of auth / region / User-Agent / rate-limit errors.
- The app must be fully usable headless (engine + CLI/service) without launching NiceGUI.



### 8.1 Command-line interface

The tool exposes a first-class CLI for scripting, headless/server use, and manual
control without the GUI. The CLI drives the same core engine as the GUI and is available
on all supported platforms.

**Invocation:**

```
nikon_transfer [--config PATH] [--verbose] [--json] [--no-color] <command> [options]
```

**Global options:**

| Flag | Description |
|------|-------------|
| `--config PATH` | Path to config file (default: `~/.nikon_transfer/config.toml`) |
| `--verbose`, `-v` | Increase log verbosity (repeat for more: `-vv`) |
| `--json` | Emit all output as JSON — applies to every command |
| `--no-color` | Disable ANSI colour (also auto-disabled when stdout is not a TTY) |

---

#### `auth` — manage authentication

```
nikon_transfer auth login        # Open a browser window to authenticate via NCAS
nikon_transfer auth logout       # Revoke and delete all stored tokens
nikon_transfer auth status       # Show current auth state, account email, token expiry
```

`auth login` launches a headed Playwright browser to the NCAS login page
(`accounts.cld.nikon.com/login?service_id=nic_local`), waits for the user to complete
login, captures the access and refresh tokens, and persists them to the OS secret store.
Prints `Authenticated as <email>` on success.

Example output:

```
$ nikon_transfer auth status
Status:    Connected
Account:   user@example.com
Token:     expires in 4 min 32 s  (auto-refreshed)
```

---

#### `list` — list images in Nikon Imaging Cloud

```
nikon_transfer list [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--format FORMAT` | `table` | Output format: `table`, `json`, `csv` |
| `--filter-type RAW\|JPEG\|ALL` | `ALL` | Filter by file category |
| `--filter-camera DEVICE_ID` | — | Restrict to a specific camera |
| `--date-from YYYY-MM-DD` | — | Include only images shot on or after this date |
| `--date-to YYYY-MM-DD` | — | Include only images shot on or before this date |
| `--sort date\|name` | `date` | Sort field |
| `--asc` | — | Ascending sort (default: descending) |
| `--limit N` | unlimited | Stop after N items |

Default `table` output:

```
$ nikon_transfer list --filter-type RAW
  ID (short)   NAME              CAMERA       SHOT DATE    TYPE   LIFETIME
  01J3…4A      _NZF5082.NEF      NIKON Z f    2026-06-12   RAW    29 d
  01J2…9C      _NZF5081.NEF      NIKON Z f    2026-06-11   RAW     6 d   ← yellow
  01J1…2B      _NZF5080.NEF      NIKON Z f    2026-06-10   RAW     2 d   ← red

3 images  (211 total on cloud, 3 match filter)
```

Lifetime colour coding: ≤ 3 days → red, ≤ 7 days → yellow. Suppressed by `--no-color`
or when output is piped.

---

#### `download` — download images to local disk

```
nikon_transfer download [options] [ID ...]
```

Positional `ID ...` arguments: one or more image IDs to download specifically. If
omitted, downloads all images matching the active filter options (a full sync pass).

| Flag | Default | Description |
|------|---------|-------------|
| `--dest PATH` | `NIC_DEST_DIR` | Override the destination root for this run |
| `--filter-type RAW\|JPEG\|ALL` | `ALL` | File-category filter |
| `--filter-camera DEVICE_ID` | — | Camera filter |
| `--date-from YYYY-MM-DD` | — | Lower bound on `shooting_date` |
| `--date-to YYYY-MM-DD` | — | Upper bound on `shooting_date` |
| `--dry-run` | — | Print what would be downloaded; write nothing |
| `--concurrency N` | `3` | Parallel download workers |
| `--retries N` | `3` | Per-file retry attempts on transient errors |

Files are placed under `<dest>/YYYY/MM/DD/<filename>` derived from `shooting_date`,
preserving the original filename. Already-present files are skipped (idempotent).

Progress output:

```
$ nikon_transfer download --filter-type RAW --dest /photos
Downloading 42 RAW files to /photos …
[████████████░░░░░░░░]  12/42  _NZF5081.NEF  28.4 MB / 31.2 MB
Done: 42 downloaded, 7 skipped (already present), 0 failed.
```

With `--dry-run`:

```
$ nikon_transfer download --dry-run --filter-type RAW
Would download 42 files (1.3 GB).  Run without --dry-run to proceed.
```

---

#### `sync` — continuous poll loop

Run a background poller that checks for new images on a schedule, downloading them as
they appear and never re-downloading files already on disk.

```
nikon_transfer sync [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--interval SECONDS` | `300` | Poll interval |
| `--once` | — | Run exactly one poll cycle then exit (one-shot mode) |
| `--dest PATH` | `NIC_DEST_DIR` | Override destination root |
| `--filter-type RAW\|JPEG\|ALL` | `ALL` | File-category filter |
| `--filter-camera DEVICE_ID` | — | Camera filter |

In continuous mode the command logs one timestamped line per poll cycle and one line per
downloaded file. Responds to `SIGINT`/`SIGTERM` by completing any in-flight download
and exiting cleanly.

```
$ nikon_transfer sync --interval 300 --filter-type RAW
2026-06-28 09:00:00  Poll #1 — 211 images on cloud, 0 new.
2026-06-28 09:05:00  Poll #2 — 212 images on cloud, 1 new.
2026-06-28 09:05:01  Downloading _NZF5083.NEF … done (31.1 MB).
2026-06-28 09:10:00  Poll #3 — 212 images on cloud, 0 new.
^C
Interrupted — finishing current transfer … done. Exiting.
```

---

#### `config` — manage persistent configuration

```
nikon_transfer config show              # Print all config values (password masked)
nikon_transfer config set KEY VALUE     # Persist a config key
nikon_transfer config get KEY           # Print a single key
```

Supported keys correspond to the §4 environment variables:

| Key | Env var | Description |
|-----|---------|-------------|
| `country` | `NIC_COUNTRY` | Country code sent to the API (e.g. `SE`) |
| `dest_dir` | `NIC_DEST_DIR` | Local destination root |
| `poll_interval` | — | Default poll interval for `sync` (seconds) |
| `file_filter` | — | Default file-type filter (`ALL`, `RAW`, `JPEG`) |

**Precedence (highest wins):** CLI flag → environment variable → config file.

---

#### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Authentication error (missing or expired token — run `auth login`) |
| 2 | Network or API error |
| 3 | Configuration error (missing required value) |
| 4 | Partial failure (one or more files failed after all retries) |

---

## 9. Suggested implementation notes (non-binding)

- **Python** fits all constraints: NiceGUI (UI) + Playwright (login/token capture) +
  `httpx`/`requests` (API & downloads), cross-platform.
- Keep the auth/token-capture concern isolated behind an interface so it can be swapped
  (manual paste → headless login → full PKCE) without touching the sync logic.
- Keep the core engine importable and runnable independently of NiceGUI.

## 10. Open questions

**Resolved by live capture (2026-06-15):**
- ✅ OIDC realm = `user`, `client_id` = `c0004` (see §4).
- ✅ Access token lifetime = 300 s; refresh-token grant works for unattended
  refresh (see §4).
- ✅ Pagination: `offset` is a 1-based **item** offset (step by `limit`),
  `offset=0` → HTTP 500, empty result ends iteration (see §6).
- ✅ Confirmed default body values + strict types: `country=SE`, `limit=20`
  (max ~20), `lifetime=30` (int), `sort_condition="0"`/`sort_desc="1"` (str).
- ✅ The operation code (`IF_FR100_H07`) is **not** required as a request header
  — replaying the captured URL + body + `access_token` returns HTTP 200.
- ✅ Response nesting + item schema confirmed live: images are under
  `item_info.item_list`; `file_extension` is a category (`RAW`/`JPEG`);
  `original_file_url`/`thumbnail_file_url` present; `original_file_size` is
  **absent** from the list response (see §6).
- ✅ Refresh tokens **rotate** — persist the new one each refresh (see §4).

**Still open:**
1. `file_format_id_list` id values (RAW vs JPEG) and `device_id` format — to
   enable server-side filtering instead of client-side category matching.
2. **End-to-end download not yet exercised**: listing/paging/auth are verified
   against the live account (211 images), but no `original_file_url` has been
   downloaded yet, so file integrity / presigned-URL expiry are unconfirmed.
3. Rate limits / throttling thresholds for bulk download of all items.

## 11. Legal / ToS note

This uses undocumented, unsupported endpoints. Intended scope is a user downloading
**their own** images from **their own** account for personal backup. Automated access
may not be sanctioned by Nikon's terms of service; review before distributing.

## ALSO
make sure the README.md is continously updated with the user information, how to use the tool

--dry-run should behave just as if it was "sharp" but not download or change anythin in nikon cloud. For example if skipping due to already downloaded, it should skip in --dry-run as well.