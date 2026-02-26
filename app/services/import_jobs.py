from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from flask import Flask

from app.extensions import db
from app.models import Bookmark, BookmarkContent, Folder, ImportJob, utcnow
from app.services.bookmark_import import ImportedBookmark, parse_bookmark_html
from app.services.common import normalize_url
from app.services.content import (
    ExtractedContent,
    LINK_STATUS_UNREACHABLE,
    fetch_and_extract,
)
from app.services.internal_links import bookmark_is_internal, set_internal_link_status
from app.services.sync import (
    log_sync_event,
    serialize_bookmark_for_sync,
    serialize_folder_for_sync,
)

_RUNTIME_LOCK = threading.Lock()
_RUNTIME_STATE: dict[int, dict] = {}


@dataclass
class _WorkItem:
    entry: ImportedBookmark
    normalized_url: str
    action: str
    bookmark_id: int | None = None


def start_import_job(app: Flask, user_id: int, job_id: int, html: str) -> None:
    thread = threading.Thread(
        target=_run_import_job,
        args=(app, user_id, job_id, html),
        daemon=True,
        name=f"import-job-{job_id}",
    )
    thread.start()


def get_import_job_details(job: ImportJob) -> dict:
    payload = job.as_dict()
    runtime = _runtime_snapshot(job.id)
    payload.update(
        {
            "processed_items": runtime.get("processed_items", 0),
            "total_items": runtime.get("total_items", 0),
            "total_failed": runtime.get("total_failed", 0),
            "current_title": runtime.get("current_title"),
            "current_url": runtime.get("current_url"),
            "items_per_second": runtime.get("items_per_second"),
            "eta_seconds": runtime.get("eta_seconds"),
            "elapsed_seconds": runtime.get("elapsed_seconds"),
            "status_message": runtime.get("status_message"),
        }
    )
    if job.status in {"done", "failed"} and payload["processed_items"] == 0:
        payload["processed_items"] = payload["total_created"] + payload["total_skipped"]
    return payload


def _run_import_job(app: Flask, user_id: int, job_id: int, html: str) -> None:
    with app.app_context():
        db.session.remove()
        started_at = utcnow()
        _runtime_update(
            job_id,
            {
                "status": "running",
                "started_at": started_at,
                "processed_items": 0,
                "total_items": 0,
                "total_failed": 0,
                "current_title": None,
                "current_url": None,
                "status_message": "Parsing bookmarks HTML...",
            },
        )
        try:
            job = ImportJob.query.filter_by(id=job_id, user_id=user_id).first()
            if not job:
                return
            job.status = "running"
            job.progress = 0
            job.total_created = 0
            job.total_skipped = 0
            job.error_message = None
            db.session.commit()

            entries = parse_bookmark_html(html)
            total_items = len(entries)
            _runtime_update(job_id, {"total_items": total_items})

            if total_items == 0:
                _finish_job(job_id, "done", started_at, "No bookmarks found in upload.")
                return

            timeout = float(app.config["CONTENT_FETCH_TIMEOUT"])
            max_bytes = int(app.config["CONTENT_MAX_BYTES"])
            max_workers = int(app.config.get("IMPORT_WORKERS", 12))
            max_workers = max(4, min(max_workers, 24))

            work_items, skipped = _plan_work(user_id, entries)
            processed = 0
            created = 0
            failed = 0

            if skipped:
                processed = skipped
                _persist_progress(
                    job_id,
                    processed=processed,
                    total=total_items,
                    created=created,
                    skipped=skipped,
                    failed=failed,
                    started_at=started_at,
                    current_title="Skipping duplicate bookmarks",
                    current_url=None,
                    status_message=f"Skipped {skipped} existing or duplicate bookmarks.",
                )

            folder_cache: dict[tuple[int | None, str], int] = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        fetch_and_extract,
                        item.entry.url,
                        timeout=timeout,
                        max_bytes=max_bytes,
                    ): item
                    for item in work_items
                }

                for future in as_completed(futures):
                    item = futures[future]
                    extracted = _resolve_future(future)
                    status_message = "Imported bookmark"
                    try:
                        folder_id = _ensure_folder_path(
                            user_id=user_id,
                            path_parts=item.entry.folder_path,
                            cache=folder_cache,
                        )
                        if item.action == "restore" and item.bookmark_id:
                            restored = _restore_bookmark(
                                user_id=user_id,
                                bookmark_id=item.bookmark_id,
                                entry=item.entry,
                                normalized_url=item.normalized_url,
                                folder_id=folder_id,
                                extracted=extracted,
                            )
                            if restored:
                                created += 1
                                status_message = "Restored bookmark"
                            else:
                                skipped += 1
                                status_message = "Skipped already active bookmark"
                        else:
                            _create_bookmark(
                                user_id=user_id,
                                entry=item.entry,
                                normalized_url=item.normalized_url,
                                folder_id=folder_id,
                                extracted=extracted,
                            )
                            created += 1

                        db.session.commit()
                    except Exception as exc:
                        db.session.rollback()
                        failed += 1
                        status_message = f"Failed: {str(exc)[:160]}"

                    processed += 1
                    _persist_progress(
                        job_id,
                        processed=processed,
                        total=total_items,
                        created=created,
                        skipped=skipped,
                        failed=failed,
                        started_at=started_at,
                        current_title=item.entry.title,
                        current_url=item.entry.url,
                        status_message=status_message,
                    )

            final_status = "done"
            final_message = None
            if failed:
                final_message = f"{failed} bookmarks failed to import."
            _finish_job(job_id, final_status, started_at, final_message)
        except Exception as exc:
            db.session.rollback()
            _finish_job(job_id, "failed", started_at, str(exc))
        finally:
            db.session.remove()


def _finish_job(
    job_id: int,
    status: str,
    started_at: datetime,
    error_message: str | None,
) -> None:
    snapshot = _runtime_snapshot(job_id)
    processed = int(snapshot.get("processed_items", 0))
    total = int(snapshot.get("total_items", 0))
    created = int(snapshot.get("total_created", 0))
    skipped = int(snapshot.get("total_skipped", 0))

    job = ImportJob.query.filter_by(id=job_id).first()
    if not job:
        return

    job.status = status
    job.progress = int((processed / total) * 100) if total else 100
    job.total_created = created
    job.total_skipped = skipped
    job.error_message = error_message
    db.session.commit()

    _runtime_update(
        job_id,
        {
            "status": status,
            "finished_at": utcnow(),
            "status_message": error_message
            if error_message
            else ("Import completed." if status == "done" else "Import failed."),
            "started_at": started_at,
        },
    )


def _plan_work(
    user_id: int, entries: list[ImportedBookmark]
) -> tuple[list[_WorkItem], int]:
    normalized_values = [normalize_url(entry.url) for entry in entries]
    existing_map = _existing_bookmarks(user_id, normalized_values)

    work_items: list[_WorkItem] = []
    skipped = 0

    for entry, normalized in zip(entries, normalized_values, strict=False):
        if not normalized:
            skipped += 1
            continue
        existing = existing_map.get(normalized)
        if existing and existing.deleted_at is None:
            skipped += 1
            continue

        action = "restore" if existing else "create"
        work_items.append(
            _WorkItem(
                entry=entry,
                normalized_url=normalized,
                action=action,
                bookmark_id=existing.id if existing else None,
            )
        )

    return work_items, skipped


def _existing_bookmarks(
    user_id: int, normalized_urls: list[str]
) -> dict[str, Bookmark]:
    unique_urls = [url for url in dict.fromkeys(normalized_urls) if url]
    if not unique_urls:
        return {}

    rows: list[Bookmark] = []
    chunk_size = 400
    for i in range(0, len(unique_urls), chunk_size):
        chunk = unique_urls[i : i + chunk_size]
        rows.extend(
            Bookmark.query.filter_by(user_id=user_id)
            .filter(Bookmark.normalized_url.in_(chunk))
            .all()
        )

    return {row.normalized_url: row for row in rows}


def _ensure_folder_path(
    user_id: int,
    path_parts: list[str],
    cache: dict[tuple[int | None, str], int],
) -> int | None:
    parent_id = None
    for part in path_parts:
        name = part.strip()
        if not name:
            continue
        key = (parent_id, name)
        cached = cache.get(key)
        if cached is not None:
            parent_id = cached
            continue

        folder = Folder.query.filter_by(
            user_id=user_id,
            name=name,
            parent_id=parent_id,
        ).first()
        if not folder:
            folder = Folder(user_id=user_id, name=name, parent_id=parent_id)
            db.session.add(folder)
            db.session.flush()
            log_sync_event(
                user_id,
                "folder",
                folder.id,
                "create",
                serialize_folder_for_sync(folder),
            )
        parent_id = folder.id
        cache[key] = parent_id
    return parent_id


def _create_bookmark(
    user_id: int,
    entry: ImportedBookmark,
    normalized_url: str,
    folder_id: int | None,
    extracted: ExtractedContent,
) -> None:
    notes_text = (extracted.text or "").strip() or None
    bookmark = Bookmark(
        user_id=user_id,
        folder_id=folder_id,
        url=entry.url,
        normalized_url=normalized_url,
        title=entry.title or None,
        notes=notes_text,
        link_status=extracted.status,
    )
    db.session.add(bookmark)
    db.session.flush()
    _apply_extracted_content(bookmark, extracted)
    log_sync_event(
        user_id,
        "bookmark",
        bookmark.id,
        "create",
        serialize_bookmark_for_sync(bookmark),
    )


def _restore_bookmark(
    user_id: int,
    bookmark_id: int,
    entry: ImportedBookmark,
    normalized_url: str,
    folder_id: int | None,
    extracted: ExtractedContent,
) -> bool:
    bookmark = Bookmark.query.filter_by(id=bookmark_id, user_id=user_id).first()
    if not bookmark:
        _create_bookmark(
            user_id=user_id,
            entry=entry,
            normalized_url=normalized_url,
            folder_id=folder_id,
            extracted=extracted,
        )
        return True

    if bookmark.deleted_at is None:
        return False

    bookmark.deleted_at = None
    bookmark.deleted_by = None
    bookmark.title = bookmark.title or entry.title
    bookmark.folder_id = bookmark.folder_id or folder_id
    notes_value = (bookmark.notes or "").strip().lower()
    if notes_value in {"", "none"}:
        bookmark.notes = (extracted.text or "").strip() or None
    _apply_extracted_content(bookmark, extracted)
    log_sync_event(
        user_id,
        "bookmark",
        bookmark.id,
        "restore",
        serialize_bookmark_for_sync(bookmark),
    )
    return True


def _apply_extracted_content(bookmark: Bookmark, extracted: ExtractedContent) -> None:
    if bookmark_is_internal(bookmark):
        set_internal_link_status(bookmark)
        return

    content = bookmark.content or BookmarkContent(bookmark_id=bookmark.id)
    bookmark.link_status = extracted.status
    bookmark.last_checked_at = utcnow()
    content.extracted_text = extracted.text
    content.extracted_at = utcnow()
    content.fetch_status = extracted.status
    content.fetch_error = extracted.error
    content.content_hash = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
    db.session.add(content)


def _resolve_future(future) -> ExtractedContent:
    try:
        return future.result()
    except Exception as exc:
        return ExtractedContent(
            title=None,
            text="",
            status=LINK_STATUS_UNREACHABLE,
            error=str(exc),
            status_code=None,
            final_url=None,
        )


def _persist_progress(
    job_id: int,
    processed: int,
    total: int,
    created: int,
    skipped: int,
    failed: int,
    started_at: datetime,
    current_title: str | None,
    current_url: str | None,
    status_message: str,
) -> None:
    job = ImportJob.query.filter_by(id=job_id).first()
    if job:
        job.progress = int((processed / total) * 100) if total else 100
        job.total_created = created
        job.total_skipped = skipped
        db.session.commit()

    _runtime_update(
        job_id,
        {
            "processed_items": processed,
            "total_items": total,
            "total_created": created,
            "total_skipped": skipped,
            "total_failed": failed,
            "started_at": started_at,
            "current_title": current_title,
            "current_url": current_url,
            "status_message": status_message,
        },
    )


def _runtime_update(job_id: int, updates: dict) -> None:
    with _RUNTIME_LOCK:
        state = _RUNTIME_STATE.get(job_id, {}).copy()
        state.update(updates)
        _RUNTIME_STATE[job_id] = state


def _runtime_snapshot(job_id: int) -> dict:
    with _RUNTIME_LOCK:
        state = _RUNTIME_STATE.get(job_id, {}).copy()

    started_at = state.get("started_at")
    finished_at = state.get("finished_at")
    now = finished_at or utcnow()
    elapsed_seconds = None
    if isinstance(started_at, datetime):
        elapsed_seconds = max(0, int((now - started_at).total_seconds()))

    processed = int(state.get("processed_items", 0) or 0)
    total = int(state.get("total_items", 0) or 0)

    items_per_second = None
    eta_seconds = None
    if elapsed_seconds and processed > 0:
        items_per_second = round(processed / elapsed_seconds, 2)
        if total > processed and items_per_second > 0:
            remaining = total - processed
            eta_seconds = int(remaining / items_per_second)

    state["elapsed_seconds"] = elapsed_seconds
    state["items_per_second"] = items_per_second
    state["eta_seconds"] = eta_seconds
    state.setdefault("total_created", 0)
    state.setdefault("total_skipped", 0)
    state.setdefault("total_failed", 0)
    return state
