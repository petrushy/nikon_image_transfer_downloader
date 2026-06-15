"""Thin async client for the Nikon Imaging Cloud data API.

It replays the ``/bff/transfer/list`` request captured during login (so we
inherit the exact URL, headers and body the web app uses) and only overrides
``offset``/``limit`` for pagination.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from .auth import DEFAULT_USER_AGENT, CapturedSession
from .models import ImageItem

log = logging.getLogger(__name__)

# The web app uses 20; larger values (e.g. 300) are rejected with HTTP 500.
DEFAULT_PAGE_SIZE = 20


class Unauthorized(Exception):
    """Raised when the API rejects the access token (HTTP 401/403)."""


class NikonCloudClient:
    """Lists images from the transfer endpoint."""

    def __init__(self, session: CapturedSession, http: httpx.AsyncClient):
        self._session = session
        self._http = http

    @property
    def session(self) -> CapturedSession:
        return self._session

    def update_session(self, session: CapturedSession) -> None:
        """Swap in a refreshed session (e.g. after a token refresh)."""
        self._session = session

    async def list_images(self, offset: int, limit: int) -> list[ImageItem]:
        """Fetch one window of the transfer list.

        ``offset`` is a **1-based item offset** (the first item is at
        ``offset=1``; ``offset=0`` gives HTTP 500). ``limit`` is the window
        size, so the next window starts at ``offset + limit``.
        """
        body = {**self._session.list_body, "offset": offset, "limit": limit}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            **self._session.list_headers,
            "access_token": self._session.access_token,
        }
        resp = await self._http.post(
            self._session.list_url, json=body, headers=headers
        )
        if resp.status_code in (401, 403):
            raise Unauthorized(f"API returned HTTP {resp.status_code}")
        resp.raise_for_status()

        # The image array is nested: response -> item_info -> item_list.
        payload = resp.json()
        item_info = payload.get("item_info") or {}
        items = item_info.get("item_list") or []
        return [ImageItem.from_api(item) for item in items]

    async def iter_all_images(
        self, page_size: int = DEFAULT_PAGE_SIZE
    ) -> AsyncIterator[ImageItem]:
        """Yield every image, paging until a short/empty window is returned."""
        offset = 1  # the API uses a 1-based item offset
        while True:
            items = await self.list_images(offset=offset, limit=page_size)
            for item in items:
                yield item
            if len(items) < page_size:
                break  # last window
            offset += page_size
