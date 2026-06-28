# Nikon Imaging Cloud — Image Transfer Downloader

One-way downloader / sync for images stored in **Nikon Imaging Cloud**.
Logs in via your Nikon account, lists the images the cloud is holding, and
downloads them to local disk in a `YYYY/MM/DD` folder layout. It never
uploads, modifies, or deletes anything in the cloud.

> **Unofficial API.** Nikon Imaging Cloud has no public API. This tool drives
> the real web app with a browser and replays the requests it observes.
> Endpoints are unofficial and may change without notice.
> See [`specification/specification.md`](specification/specification.md).
> Intended for downloading **your own** images from **your own** account.

---

## How it works

1. **Login (browser-assisted):** a real Chromium window opens; you log in to
   your Nikon account normally. The tool intercepts the `access_token` the web
   app uses and a sample API request, then calls the data API directly
   from that point on.
2. **Refresh:** the captured OIDC refresh token lets subsequent runs refresh
   the access token without opening a browser again.
3. **List & filter:** pages through the transfer endpoint, optionally filtered
   by file type (RAW / JPEG), camera, or date range.
4. **Download:** streams each image to `DEST/YYYY/MM/DD/<name>`, skipping
   files already on disk (idempotent / resumable via a small manifest).
5. **Sync mode:** polls the cloud on a configurable interval so new images are
   grabbed automatically before they expire.

---

## Install

Requires **Python 3.11+**.
[uv](https://docs.astral.sh/uv/) is the recommended installer.

### With uv (recommended)

```bash
uv sync                              # create .venv and install all deps
uv run playwright install chromium   # one-time: download the browser binary
```

`uv sync` reads `pyproject.toml` / `uv.lock`, provisions the pinned Python
version (from `.python-version`), and installs all dependencies into `.venv`.
Prefix every command with `uv run` — no manual activation needed.

### With pip

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium          # one-time: download the browser binary
```

---

## Quick start

```bash
# 1. Log in (opens a Chromium window — complete login there)
uv run nikon-downloader auth login

# 2. See what's on the cloud
uv run nikon-downloader list

# 3. Download everything new
uv run nikon-downloader download

# 4. Keep syncing automatically (Ctrl-C to stop)
uv run nikon-downloader sync
```

---

## Configuration

### Credentials (username / password)

Store credentials in a YAML file (default: `.env/login.txt`, git-ignored) or
as environment variables. The YAML format:

```yaml
login:
    username: you@example.com
    password: your-password
```

Environment variables (always take precedence over the file):

| Variable | Description |
|----------|-------------|
| `NIC_USERNAME` | Nikon account email |
| `NIC_PASSWORD` | Nikon account password |
| `NIC_CREDENTIALS_FILE` | Path to an alternative credentials YAML file |

> **Never commit credentials.** `.env/`, token files, and downloaded images
> are git-ignored.

### Persistent settings

Non-secret settings live in `~/.nikon_transfer/config.json` and can be
managed with `nikon-downloader config`:

```bash
nikon-downloader config set dest_dir    /Volumes/Photos/Nikon
nikon-downloader config set country     SE
nikon-downloader config set file_filter RAW
nikon-downloader config set poll_interval 300

nikon-downloader config show   # print all values
nikon-downloader config get dest_dir
```

Supported keys:

| Key | Default | Description |
|-----|---------|-------------|
| `dest_dir` | `downloads` | Root folder for downloaded images |
| `country` | `SE` | Country code sent to the Nikon API |
| `file_filter` | `all` | Default file-type filter for `download` and `sync` (`all`, `raw`, `jpeg`) |
| `poll_interval` | `300` | Seconds between polls in `sync` mode |

### Environment variable reference

All environment variables override the config file:

| Variable | Description |
|----------|-------------|
| `NIC_DEST_DIR` | Download destination root |
| `NIC_COUNTRY` | Country code |
| `NIC_FILE_FORMAT` | File type filter (`all` / `raw` / `jpeg`) |
| `NIC_POLL_INTERVAL` | Poll interval in seconds |
| `NIC_STATE_DIR` | Where to store the session token + manifest (default: `.state`) |
| `NIC_HEADLESS` | Set to `1` to run the login browser headlessly |

### Precedence (highest wins)

```
CLI flag  >  environment variable  >  config file  >  built-in default
```

---

## CLI reference

The tool is invoked as **`nikon-downloader`**.

```
nikon-downloader [--config PATH] [-v] [--json] [--no-color] <command>
```

**Global options**

| Flag | Description |
|------|-------------|
| `--config PATH` | Use a different config file (default: `~/.nikon_transfer/config.json`) |
| `-v` / `--verbose` | Enable info logging; repeat (`-vv`) for debug |
| `--json` | Emit all output as machine-readable JSON |
| `--no-color` | Disable ANSI colour (also auto-disabled when stdout is not a TTY) |

---

### `auth` — manage authentication

```bash
nikon-downloader auth login     # open browser, capture session, store tokens
nikon-downloader auth logout    # delete stored tokens
nikon-downloader auth status    # show connection state and token expiry
```

`auth login` opens a Chromium window to the Nikon login page. Complete the
login there; the tool captures the session automatically and stores it in
`.state/session.json`.

Example output of `auth status`:

```
Status:  Connected
Token:   expires in 4 min 32 s  (auto-refreshed)
```

---

### `list` — list images in Nikon Imaging Cloud

```bash
nikon-downloader list [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--format table\|json\|csv` | `table` | Output format |
| `--filter-type ALL\|RAW\|JPEG` | `ALL` | File category filter |
| `--filter-camera DEVICE` | | Substring match on camera name |
| `--date-from YYYY-MM-DD` | | Earliest shooting date to include |
| `--date-to YYYY-MM-DD` | | Latest shooting date to include |
| `--sort date\|name` | `date` | Sort field |
| `--asc` | | Ascending order (default: descending) |
| `--limit N` | | Stop after N images |

**Example — list RAW files from the last week, sorted by name:**

```bash
nikon-downloader list --filter-type RAW --date-from 2026-06-21 --sort name
```

**Example table output:**

```
ID            Name              Camera       Shot Date    Type   Lifetime
01J3…4A…      _NZF5082.NEF      NIKON Z f    2026-06-12   RAW    29 d
01J2…9C…      _NZF5081.NEF      NIKON Z f    2026-06-11   RAW     6 d
01J1…2B…      _NZF5080.NEF      NIKON Z f    2026-06-10   RAW     2 d

183 images
```

Lifetime colour coding: ≤ 3 days → red, ≤ 7 days → yellow.

**Machine-readable output** (`--json` implies JSON format for `list`):

```bash
nikon-downloader --json list --filter-type RAW | jq '.[].name'
```

---

### `download` — download images to local disk

```bash
nikon-downloader download [OPTIONS] [ID...]
```

Pass one or more image IDs to download specific images; omit them to
download everything that matches the filter options.

| Option | Default | Description |
|--------|---------|-------------|
| `--dest PATH` | `dest_dir` config | Override destination root for this run |
| `--filter-type ALL\|RAW\|JPEG` | `ALL` | File category filter |
| `--filter-camera DEVICE` | | Camera name filter |
| `--date-from YYYY-MM-DD` | | Lower bound on shooting date |
| `--date-to YYYY-MM-DD` | | Upper bound on shooting date |
| `--dry-run` | | Print what would be downloaded; write nothing |
| `--concurrency N` | `3` | Parallel download workers |
| `--retries N` | `3` | Per-file retry attempts on transient errors |

Files are written to `<dest>/YYYY/MM/DD/<filename>` from `shooting_date`.
Already-present files are skipped (idempotent — safe to re-run after
interruption).

**Examples:**

```bash
# Download all RAW files
nikon-downloader download --filter-type RAW

# Preview what would be downloaded
nikon-downloader download --filter-type RAW --dry-run

# Download specific images by ID
nikon-downloader download 01J3ABC123 01J3DEF456

# Download to a specific folder
nikon-downloader download --dest /Volumes/Photos/Nikon --filter-type RAW

# JSON summary (useful in scripts)
nikon-downloader --json download --filter-type RAW
```

**Example output:**

```
Listing images from Nikon Imaging Cloud...
Found 183 images, 183 match filter 'all'.
  ↓ downloads/2026/06/13/_NZF5082.NEF
  ↓ downloads/2026/06/13/_NZF5081.NEF
Done: 2 downloaded, 181 skipped, 0 failed.
```

---

### `sync` — continuous poll loop

```bash
nikon-downloader sync [OPTIONS]
```

Polls the cloud repeatedly and downloads new images as they appear. Already-
present files are always skipped. Responds to `Ctrl-C` / `SIGTERM` by
finishing the current download and exiting cleanly.

| Option | Default | Description |
|--------|---------|-------------|
| `--interval SECONDS` | `300` | How often to poll |
| `--once` | | Run exactly one poll cycle then exit (one-shot mode) |
| `--dest PATH` | `dest_dir` config | Override destination root |
| `--filter-type ALL\|RAW\|JPEG` | `ALL` | File category filter |
| `--filter-camera DEVICE` | | Camera name filter |

**Examples:**

```bash
# Run as a background service, poll every 5 minutes
nikon-downloader sync --interval 300 --filter-type RAW

# One-shot (equivalent to the old `nikon-downloader sync`)
nikon-downloader sync --once

# Run as a launchd / systemd service (pipe output to a log)
nikon-downloader sync --interval 300 >> ~/logs/nikon-sync.log 2>&1
```

**Example output:**

```
2026-06-28 09:00:00  Listing images from Nikon Imaging Cloud...
2026-06-28 09:00:02  Found 183 images, 183 match filter 'all'.
2026-06-28 09:00:02  Done: 0 downloaded, 183 skipped, 0 failed.
2026-06-28 09:00:02  Sleeping 300s until next poll.
2026-06-28 09:05:02  Listing images from Nikon Imaging Cloud...
2026-06-28 09:05:04  Found 184 images, 184 match filter 'all'.
2026-06-28 09:05:04    ↓ downloads/2026/06/28/_NZF5083.NEF
2026-06-28 09:05:07  Done: 1 downloaded, 183 skipped, 0 failed.
```

---

### `config` — manage persistent settings

```bash
nikon-downloader config show              # print all values (JSON: --json)
nikon-downloader config set KEY VALUE     # write a value to the config file
nikon-downloader config get KEY           # print a single value
```

---

### `ui` — NiceGUI control panel

```bash
nikon-downloader ui                  # open at http://localhost:8080
nikon-downloader ui --port 9090      # custom port
```

Launches an optional browser-based GUI with the same engine underneath.
The GUI and the CLI are fully interchangeable — the engine, token store,
and manifest are shared.

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Authentication error — run `auth login` |
| 2 | Network or API error |
| 3 | Configuration error (unknown key, missing required value) |
| 4 | Partial failure — some files failed after all retries |
| 130 | Interrupted by Ctrl-C |

---

## Running as a service

### macOS launchd

Create `~/Library/LaunchAgents/com.nikon-downloader.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nikon-downloader</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/.venv/bin/nikon-downloader</string>
    <string>sync</string>
    <string>--interval</string><string>300</string>
    <string>--filter-type</string><string>RAW</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/nikon-downloader.log</string>
  <key>StandardErrorPath</key><string>/tmp/nikon-downloader.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.nikon-downloader.plist
```

### Linux systemd

```ini
[Unit]
Description=Nikon Image Transfer Downloader
After=network-online.target

[Service]
ExecStart=/path/to/.venv/bin/nikon-downloader sync --interval 300
Restart=on-failure
WorkingDirectory=/path/to/project

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now nikon-downloader
```

---

## Architecture

The project follows a strict **engine / interface** split:

- **Core engine** (`config`, `models`, `auth`, `api`, `downloader`, `sync`)
  — headless, no GUI or CLI dependency. Runs fine as a daemon or imported
  as a library.
- **CLI** (`cli`) — `click`-based front-end with `rich` table output.
- **GUI** (`ui`) — optional NiceGUI control panel; lazy-imported by `cli ui`
  so the CLI works without NiceGUI installed.

| Module | Responsibility |
|--------|----------------|
| `config` | Settings from env vars, config file, and CLI overrides |
| `models` | `ImageItem` dataclass, `parse_timestamp`, date helpers |
| `auth` | Browser login, token capture, `TokenStore`, token refresh |
| `api` | `NikonCloudClient` — paginated `/bff/transfer/list` calls |
| `downloader` | Stream files to disk, `YYYY/MM/DD` layout, `Manifest`, retries |
| `sync` | `SyncFilter`, `SyncEngine` orchestration, poll service |
| `cli` | `click` command groups, `rich` output, exit codes |
| `ui` | Optional NiceGUI control panel |

### Download directory layout

```
downloads/
  2026/
    05/
      23/
        _NZF4904.NEF
        _NZF4905.NEF
    06/
      13/
        _NZF5025.NEF
        _NZF5025.NEF.xmp
```

### State files

| File | Contents |
|------|----------|
| `~/.nikon_transfer/state/session.json` | Captured access + refresh tokens (chmod 600) |
| `~/.nikon_transfer/state/manifest.json` | Record of downloaded item IDs / sizes / paths |

Override the location with `NIC_STATE_DIR` if needed (e.g. to pin state to
the project directory: `NIC_STATE_DIR=.state`).

---

## Caveats and known limitations

- **Unofficial API.** Nikon has no public API; endpoints, field names and
  auth flow were reverse-engineered and may change without notice.
- **Login autofill is best-effort.** Selector names for the login form are
  unknown; you may need to type credentials manually in the browser window.
- **Token refresh.** The OIDC refresh token rotates on every use — the tool
  persists the new token automatically, but ad-hoc scripts that don't may
  invalidate the session.
- **Presigned URLs are short-lived.** Download URLs expire soon after
  listing; the tool downloads immediately after listing to avoid this.
- **No cloud deletion.** This tool is download-only and will never remove
  anything from Nikon Imaging Cloud.
