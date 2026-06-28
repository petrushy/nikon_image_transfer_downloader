"""Data model for an image stored in Nikon Imaging Cloud.

Field names mirror the (reverse-engineered, unofficial) ``/bff/transfer/list``
response described in ``specification/specification.md``. Parsing is defensive:
the API is undocumented, so missing or differently-typed fields must not crash
the whole sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Formats the cloud might use for date fields. Tried in order after ISO/epoch.
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def parse_timestamp(value: object) -> datetime | None:
    """Best-effort parse of a cloud date field into a datetime.

    Accepts ISO-8601 strings, common ``strptime`` layouts, and epoch numbers
    (seconds or milliseconds). Returns ``None`` if nothing matches.
    """
    if value is None or value == "":
        return None

    # Numeric epoch (seconds or milliseconds).
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit()
    ):
        number = float(value)
        if number > 1e12:  # almost certainly milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class ImageItem:
    """One image in the cloud transfer list."""

    id: str
    name: str
    file_extension: str  # category string: "RAW" or "JPEG" (not the literal ext)
    original_file_url: str | None
    original_file_size: int | None
    thumbnail_file_url: str | None
    device_name: str
    shooting_date: datetime | None
    upload_date: datetime | None
    lifetime: int | None  # remaining days before the image expires from the cloud
    raw: dict  # the untouched source object, for debugging / future fields

    @classmethod
    def from_api(cls, data: dict) -> "ImageItem":
        size = data.get("original_file_size")
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None

        lt = data.get("lifetime")
        try:
            lt = int(lt) if lt is not None else None
        except (TypeError, ValueError):
            lt = None

        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            file_extension=str(data.get("file_extension", "")),
            original_file_url=data.get("original_file_url") or None,
            original_file_size=size,
            thumbnail_file_url=data.get("thumbnail_file_url") or None,
            device_name=str(data.get("device_name", "")),
            shooting_date=parse_timestamp(data.get("shooting_date")),
            upload_date=parse_timestamp(data.get("upload_date")),
            lifetime=lt,
            raw=data,
        )

    @property
    def effective_date(self) -> datetime | None:
        """Date used to build the YYYY/MM/DD folder layout."""
        return self.shooting_date or self.upload_date
