"""Column types shared by every model."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetime stored as a naive UTC value.

    SQLite has no native tz-aware type, so the conversion is made explicit in one place:
    values are converted to UTC and stripped on the way in, and tagged ``UTC`` on the way
    out. Naive input is rejected — an ambiguous timestamp is a bug, not a default.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime rejected; use timezone-aware UTC values.")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)
