"""Authentication: browser-assisted login and token capture.

Nikon Imaging Cloud has no public API and the OIDC realm/client are loaded at
runtime, so we don't try to replicate the login by hand. Instead we drive the
*real* web app with Playwright and intercept the requests it makes:

  * the ``access_token`` header it attaches to every data-API call, and
  * a sample ``/bff/transfer/list`` request, which reveals the exact URL,
    headers and body (``country`` etc.) we then replay ourselves.

We also capture the OIDC token endpoint + ``client_id`` + ``refresh_token`` so
unattended refresh is possible; if any of that is missing we fall back to a
fresh browser login.

This is the "Option 1" strategy from the specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

APP_URL = "https://imagingcloud.nikon.com/"
TRANSFER_LIST_PAGE = "https://imagingcloud.nikon.com/transfer/list/"
API_HOST = "api.user.cwp.imagingcloud.nikon.com"
LIST_PATH = "/bff/transfer/list"
TOKEN_PATH = "openid-connect/token"

# A normal desktop Chrome UA. Headless Chromium otherwise advertises
# "HeadlessChrome", which Nikon's user-agent gate rejects with a /sorry page.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# How long to wait for the user to finish logging in (seconds).
LOGIN_TIMEOUT = 300


class AuthError(Exception):
    """Raised when login or token capture fails."""


class RefreshUnavailable(Exception):
    """Raised when we lack the data needed to refresh without a browser."""


@dataclass
class CapturedSession:
    """Everything needed to talk to the data API, captured from the browser."""

    access_token: str
    list_url: str
    list_headers: dict[str, str] = field(default_factory=dict)
    list_body: dict = field(default_factory=dict)
    refresh_token: str | None = None
    token_endpoint: str | None = None
    client_id: str | None = None
    expires_in: int | None = None
    captured_at: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> float | None:
        if self.expires_in is None:
            return None
        return self.captured_at + self.expires_in

    def looks_expired(self, leeway: int = 60) -> bool:
        """Whether the access token is at/near expiry.

        With no ``expires_in`` we can't know, so we report ``False`` and
        rely on a 401 to trigger refresh instead.
        """
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - leeway)


class TokenStore:
    """Persists a :class:`CapturedSession` as JSON on disk."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> CapturedSession | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return CapturedSession(**data)
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("Ignoring unreadable session file at %s", self.path)
            return None

    def save(self, session: CapturedSession) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(session), indent=2), encoding="utf-8"
        )
        # Token is sensitive; keep it owner-readable only (best effort).
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class BrowserAuthenticator:
    """Logs in via a real browser and captures the session."""

    def __init__(
        self,
        headless: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.headless = headless
        self.user_agent = user_agent

    async def authenticate(
        self, username: str | None = None, password: str | None = None
    ) -> CapturedSession:
        """Open a browser, optionally pre-fill credentials, capture a session.

        If credentials are provided we attempt to fill the login form, but the
        user can always complete or correct it manually in the window.
        """
        captured: dict = {}
        token_ready = asyncio.Event()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(user_agent=self.user_agent)
            page = await context.new_page()

            self._attach_capture(context, captured, token_ready)

            log.info("Opening %s for login", APP_URL)
            await page.goto(APP_URL, wait_until="domcontentloaded")
            await self._try_autofill(page, username, password)

            try:
                await asyncio.wait_for(
                    token_ready.wait(), timeout=LOGIN_TIMEOUT
                )
            except asyncio.TimeoutError:
                await browser.close()
                raise AuthError(
                    "Timed out waiting for login. Complete the login in the "
                    "browser window within the allotted time."
                )

            # We have a token; now make sure we also captured a real
            # /bff/transfer/list request to learn its headers + body.
            if "list_url" not in captured:
                log.info("Navigating to transfer list to capture template")
                try:
                    await page.goto(
                        TRANSFER_LIST_PAGE, wait_until="domcontentloaded"
                    )
                    await self._wait_for(
                        lambda: "list_url" in captured, timeout=30
                    )
                except Exception:  # noqa: BLE001 - best effort
                    log.warning("Could not capture a transfer/list template")

            await browser.close()

        if "access_token" not in captured:
            raise AuthError("Login completed but no access token captured.")

        session = CapturedSession(
            access_token=captured["access_token"],
            list_url=captured.get(
                "list_url", f"https://{API_HOST}{LIST_PATH}"
            ),
            list_headers=captured.get("list_headers", {}),
            list_body=captured.get("list_body", {}),
            refresh_token=captured.get("refresh_token"),
            token_endpoint=captured.get("token_endpoint"),
            client_id=captured.get("client_id"),
            expires_in=captured.get("expires_in"),
        )
        log.info(
            "Captured session (refreshable=%s)",
            session.refresh_token is not None,
        )
        return session

    async def refresh(self, session: CapturedSession) -> CapturedSession:
        """Refresh the access token using the OIDC refresh-token grant.

        Raises :class:`RefreshUnavailable` if we don't have the pieces needed,
        so the caller can fall back to :meth:`authenticate`.
        """
        if not (
            session.refresh_token
            and session.token_endpoint
            and session.client_id
        ):
            raise RefreshUnavailable(
                "Missing refresh token, endpoint or client_id"
            )

        data = {
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": session.client_id,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                session.token_endpoint,
                data=data,
                headers={"User-Agent": self.user_agent},
            )
        if resp.status_code != 200:
            raise RefreshUnavailable(
                f"Refresh failed: HTTP {resp.status_code}"
            )

        payload = resp.json()
        return CapturedSession(
            access_token=payload["access_token"],
            list_url=session.list_url,
            list_headers=session.list_headers,
            list_body=session.list_body,
            refresh_token=payload.get("refresh_token", session.refresh_token),
            token_endpoint=session.token_endpoint,
            client_id=session.client_id,
            expires_in=payload.get("expires_in"),
        )

    # -- capture wiring -----------------------------------------------------

    def _attach_capture(
        self, context, captured: dict, token_ready: asyncio.Event
    ):
        """Install request/response listeners that fill ``captured``."""

        def on_request(request):
            try:
                # Any data-API call carries the access_token header we want.
                if API_HOST in request.url:
                    token = request.headers.get("access_token")
                    if token and "access_token" not in captured:
                        captured["access_token"] = token
                        token_ready.set()

                # The transfer/list call teaches us the exact request shape.
                if LIST_PATH in request.url and "list_url" not in captured:
                    headers = dict(request.headers)
                    # Drop volatile/auto headers we don't want to replay.
                    for key in (
                        "access_token",
                        "content-length",
                        "host",
                        "cookie",
                    ):
                        headers.pop(key, None)
                    captured["list_url"] = request.url
                    captured["list_headers"] = headers
                    body = (
                        request.post_data_json if request.post_data else None
                    )
                    if isinstance(body, dict):
                        captured["list_body"] = body

                # The OIDC token request reveals endpoint + client_id.
                if TOKEN_PATH in request.url:
                    captured["token_endpoint"] = request.url.split("?")[0]
                    form = parse_qs(request.post_data or "")
                    if "client_id" in form:
                        captured["client_id"] = form["client_id"][0]
            except Exception:  # noqa: BLE001 - listeners must never throw
                log.debug("request listener error", exc_info=True)

        async def on_response(response):
            try:
                if TOKEN_PATH in response.url and response.ok:
                    payload = await response.json()
                    if "refresh_token" in payload:
                        captured["refresh_token"] = payload["refresh_token"]
                    if "expires_in" in payload:
                        captured["expires_in"] = payload["expires_in"]
            except Exception:  # noqa: BLE001
                log.debug("response listener error", exc_info=True)

        context.on("request", on_request)
        context.on("response", on_response)

    async def _try_autofill(
        self, page, username: str | None, password: str | None
    ):
        """Best-effort credential entry. Selectors are unknown, so try a few.

        Any failure is non-fatal — the user can complete the form manually.
        """
        if not (username and password):
            return
        # Give the gateway a moment to redirect to the login form.
        await page.wait_for_timeout(2000)
        user_selectors = [
            "input[type=email]",
            "input[name*=mail i]",
            "input[name*=user i]",
            "input[id*=user i]",
        ]
        try:
            for selector in user_selectors:
                field_ = page.locator(selector).first
                if await field_.count() and await field_.is_visible():
                    await field_.fill(username)
                    log.info("Pre-filled username via %s", selector)
                    break
            pw_field = page.locator("input[type=password]").first
            if await pw_field.count() and await pw_field.is_visible():
                await pw_field.fill(password)
                log.info("Pre-filled password")
        except Exception:  # noqa: BLE001
            log.debug("Autofill skipped", exc_info=True)

    @staticmethod
    async def _wait_for(predicate, timeout: float, interval: float = 0.25):
        """Poll ``predicate`` until true or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(interval)
