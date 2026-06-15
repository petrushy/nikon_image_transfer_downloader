# Nikon Imaging Cloud — Image Transfer Downloader

One-way downloader / sync for images stored in **Nikon Imaging Cloud**. It logs
in via your account, lists the images the cloud is holding, and downloads them
to local disk in a `YYYY/MM/DD` folder layout. It never uploads, modifies, or
deletes anything in the cloud.

> Nikon Imaging Cloud has **no public API**. This tool drives the real web app
> with a browser and replays the requests it observes. Endpoints are
> unofficial and may change. See [`specification/specification.md`](specification/specification.md).
> Intended for downloading **your own** images from **your own** account.

## How it works

1. **Login (Option 1 — browser-assisted):** a real browser opens; you log in to
   your Nikon account. The tool intercepts the `access_token` the web app uses
   and a sample `/bff/transfer/list` request, then talks to the API directly.
2. **List & filter:** pages through the transfer list, optionally filtered by
   file format (e.g. RAW `.NEF`).
3. **Download:** streams each image to `DEST/YYYY/MM/DD/<name>`, skipping files
   already downloaded (idempotent / resumable via a small manifest).
4. **Service mode:** optionally polls on an interval so new images are grabbed
   before they expire from the cloud.

## Install

Requires Python 3.11+. [uv](https://docs.astral.sh/uv/) is the recommended way
to run the project.

### With uv (recommended)

```bash
uv sync                          # create the env from uv.lock and install
uv run playwright install chromium   # one-time: download the browser
```

`uv sync` reads `pyproject.toml` / `uv.lock` and provisions a matching Python
(pinned in `.python-version`) plus all dependencies into `.venv`. Then prefix
commands with `uv run` (see Usage) — no manual activation needed.

### With pip (alternative)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium      # one-time: download the browser
```

## Configure

Provide your Nikon credentials via environment variables or a YAML file. See
[`.env.example`](.env.example). For local testing the default credentials file
is `.env/login.txt`:

```yaml
login:
    username: you@example.com
    password: your-password
```

Other useful settings: `NIC_DEST_DIR`, `NIC_FILE_FORMAT` (`all`|`raw`|`jpeg`),
`NIC_POLL_INTERVAL`, `NIC_HEADLESS`.

> **Never commit credentials.** `.env/`, tokens, and downloads are git-ignored.

## Usage

```bash
uv run nikon-downloader login      # open a browser, log in, store the session
uv run nikon-downloader sync       # one-shot: download anything new, then exit
uv run nikon-downloader service    # poll the cloud on an interval (daemon)
uv run nikon-downloader ui         # NiceGUI panel at http://localhost:8080
```

(With an activated venv, drop the `uv run` prefix. Equivalently:
`uv run python -m nikon_downloader <command>`.)

## Architecture

The **core engine** (`config`, `models`, `auth`, `api`, `downloader`, `sync`)
is headless and has no UI dependency, so it runs fine as a service. The
**NiceGUI UI** (`ui`) is an optional front-end over the same engine.

| Module | Responsibility |
|--------|----------------|
| `config` | Settings from env + credentials file |
| `models` | `ImageItem` and response parsing |
| `auth` | Browser login, token/request capture, token store, refresh |
| `api` | `/bff/transfer/list` client with pagination |
| `downloader` | Download files, `YYYY/MM/DD` layout, resume manifest |
| `sync` | Orchestration, filtering, poll service |
| `cli` / `ui` | Headless CLI / optional NiceGUI control panel |

## Status / caveats

- The login form auto-fill is best-effort (selectors are unknown); you may need
  to complete the login manually in the browser window the first time.
- Token refresh uses the captured OIDC refresh token when available; otherwise
  it falls back to a fresh browser login (so fully-unattended service mode needs
  a refresh token to have been captured).
- Exact values for `country`, pagination limits, and token lifetimes are still
  being confirmed against live traffic (see the spec's open questions).
