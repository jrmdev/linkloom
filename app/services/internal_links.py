from __future__ import annotations

from app.models import Bookmark, utcnow

INTERNAL_LINK_TAG = "internal"
INTERNAL_LINK_STATUS = "N/A"


def bookmark_is_internal(bookmark: Bookmark) -> bool:
    return any(
        (tag.name or "").strip().lower() == INTERNAL_LINK_TAG for tag in bookmark.tags
    )


def set_internal_link_status(bookmark: Bookmark) -> None:
    bookmark.link_status = INTERNAL_LINK_STATUS
    bookmark.last_checked_at = utcnow()
