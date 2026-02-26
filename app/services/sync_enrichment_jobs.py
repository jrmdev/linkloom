from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask

from app.extensions import db
from app.models import Bookmark, BookmarkContent, utcnow
from app.services.content import (
    ExtractedContent,
    LINK_STATUS_UNREACHABLE,
    fetch_and_extract,
)
from app.services.internal_links import bookmark_is_internal, set_internal_link_status


def start_sync_first_replace_server_enrichment(
    app: Flask,
    user_id: int,
    bookmark_ids: list[int],
) -> None:
    ordered_ids = [int(value) for value in dict.fromkeys(bookmark_ids) if value]
    if not ordered_ids:
        return

    worker = threading.Thread(
        target=_run_sync_first_replace_server_enrichment,
        args=(app, user_id, tuple(ordered_ids)),
        daemon=True,
        name=f"sync-first-enrichment-{user_id}",
    )
    worker.start()


def _run_sync_first_replace_server_enrichment(
    app: Flask,
    user_id: int,
    bookmark_ids: tuple[int, ...],
) -> None:
    with app.app_context():
        db.session.remove()
        timeout = float(app.config["CONTENT_FETCH_TIMEOUT"])
        max_bytes = int(app.config["CONTENT_MAX_BYTES"])
        worker_count = int(app.config.get("SYNC_ENRICHMENT_WORKERS", 8))
        worker_count = max(1, min(worker_count, 32))
        targets: list[tuple[int, str]] = []

        for bookmark_id in bookmark_ids:
            try:
                bookmark = Bookmark.query.filter_by(
                    id=bookmark_id, user_id=user_id
                ).first()
                if not bookmark or bookmark.deleted_at is not None:
                    continue

                if bookmark_is_internal(bookmark):
                    set_internal_link_status(bookmark)
                    db.session.commit()
                    continue

                targets.append((bookmark.id, bookmark.url))
            except Exception as exc:
                db.session.rollback()
                app.logger.warning(
                    "Failed sync enrichment for bookmark %s (user %s): %s",
                    bookmark_id,
                    user_id,
                    exc,
                )

        if targets:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(
                        fetch_and_extract,
                        url,
                        timeout=timeout,
                        max_bytes=max_bytes,
                    ): bookmark_id
                    for bookmark_id, url in targets
                }

                for future in as_completed(futures):
                    bookmark_id = futures[future]
                    try:
                        extracted = future.result()
                    except Exception as exc:
                        extracted = ExtractedContent(
                            title=None,
                            text="",
                            status=LINK_STATUS_UNREACHABLE,
                            error=str(exc),
                            status_code=None,
                            final_url=None,
                        )

                    try:
                        bookmark = Bookmark.query.filter_by(
                            id=bookmark_id,
                            user_id=user_id,
                        ).first()
                        if not bookmark or bookmark.deleted_at is not None:
                            continue
                        if bookmark_is_internal(bookmark):
                            set_internal_link_status(bookmark)
                            db.session.commit()
                            continue

                        content = bookmark.content or BookmarkContent(
                            bookmark_id=bookmark.id
                        )
                        bookmark.link_status = extracted.status
                        bookmark.last_checked_at = utcnow()
                        content.extracted_text = extracted.text
                        content.extracted_at = utcnow()
                        content.fetch_status = extracted.status
                        content.fetch_error = extracted.error
                        content.content_hash = hashlib.sha256(
                            extracted.text.encode("utf-8")
                        ).hexdigest()

                        extracted_notes = _normalize_notes_value(extracted.text)
                        if extracted_notes:
                            bookmark.notes = extracted_notes
                        elif _normalize_notes_value(bookmark.notes) is None:
                            bookmark.notes = None

                        db.session.add(content)
                        db.session.commit()
                    except Exception as exc:
                        db.session.rollback()
                        app.logger.warning(
                            "Failed sync enrichment for bookmark %s (user %s): %s",
                            bookmark_id,
                            user_id,
                            exc,
                        )

        db.session.remove()


def _normalize_notes_value(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.lower() == "none":
        return None
    return text
