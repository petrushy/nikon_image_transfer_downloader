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
  "country": "<country code>",
  "offset": 0,
  "limit": 50,
  "thirdpcs_list": [],
  "device_id_list": [],
  "file_format_id_list": [],
  "lifetime": "<filter>",
  "sort_condition": "0",
  "sort_desc": "true"
}
```

- Pagination is **offset / limit** based.
- `device_id_list` filters by camera; `file_format_id_list` filters by file type
  (the UI exposes `format_jpeg` / `format_raw` — relevant for selecting `.NEF` RAW files).
- `thirdpcs_list` relates to third-party cloud storage targets (not needed for local
  download).

**Response** (shape `{"item_list": [ ... ]}`), each item observed to include:

| Field | Meaning |
|-------|---------|
| `id` | Image identifier |
| `name` | File name |
| `device_name` | Camera that captured it |
| `file_extension` | e.g. `jpg`, `nef`, `wav` |
| `original_file_url` | **Direct download URL for the full-resolution file** |
| `original_file_size` | Size of the original file |
| `thumbnail_file_url` | Thumbnail download URL |
| `image_size` | Image dimensions/size info |
| `upload_date` | When uploaded to the cloud |
| `shooting_date` | Capture timestamp (drives the YYYY/MM/DD layout) |
| `lifetime` | Storing period (item expires after this) |
| `picture_control_name` | Picture Control applied |
| `thirdpcs_transfer_status_list` | Per third-party-cloud transfer status `[{id, transfer_status}]` |
| `c2pa_manifest_existance` | Whether a C2PA content-authenticity manifest exists (`"0"`/`"1"`) |

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

## 9. Suggested implementation notes (non-binding)

- **Python** fits all constraints: NiceGUI (UI) + Playwright (login/token capture) +
  `httpx`/`requests` (API & downloads), cross-platform.
- Keep the auth/token-capture concern isolated behind an interface so it can be swapped
  (manual paste → headless login → full PKCE) without touching the sync logic.
- Keep the core engine importable and runnable independently of NiceGUI.

## 10. Open questions to resolve via live traffic capture

1. Exact OIDC realm and `client_id` (runtime-loaded; capture from a logged-in session).
2. Whether the operation code (e.g. `IF_FR100_H07`) is an actual request header.
3. Exact allowed/required values for `country`, `sort_condition`, `lifetime`,
   `file_format_id_list`, and the `device_id` format.
4. Pagination limits (max `limit`; whether a total-count field exists to know when to stop).
5. Expiry/lifetime of `original_file_url` presigned links and of the access token.
6. Precise token refresh mechanics for unattended service mode.
7. Rate limits / throttling thresholds.

## 11. Legal / ToS note

This uses undocumented, unsupported endpoints. Intended scope is a user downloading
**their own** images from **their own** account for personal backup. Automated access
may not be sanctioned by Nikon's terms of service; review before distributing.
