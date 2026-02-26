from __future__ import annotations

import hashlib
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from flask import Flask
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Bookmark, BookmarkContent, DeadLinkJob, LinkCheck, utcnow
from app.services.content import (
    ExtractedContent,
    LINK_STATUS_DNS_ERROR,
    LINK_STATUS_NOT_FOUND,
    LINK_STATUS_SERVER_ERROR,
    LINK_STATUS_TIMEOUT,
    LINK_STATUS_UNREACHABLE,
    fetch_and_extract,
)
from app.services.internal_links import bookmark_is_internal, set_internal_link_status

PROBLEMATIC_RESULTS = {
    LINK_STATUS_NOT_FOUND,
    "404",
    LINK_STATUS_DNS_ERROR,
    LINK_STATUS_UNREACHABLE,
    LINK_STATUS_SERVER_ERROR,
    LINK_STATUS_TIMEOUT,
}

_RUNTIME_LOCK = threading.Lock()
_RUNTIME_STATE: dict[int, dict] = {}
_STOP_REQUESTED: set[int] = set()


@dataclass
class _LinkTarget:
    bookmark_id: int
    url: str
    title: str | None


def start_dead_link_job(
    app: Flask,
    user_id: int,
    job_id: int,
    bookmark_ids: list[int] | None = None,
) -> None:
    worker = threading.Thread(
        target=_run_dead_link_job,
        args=(app, user_id, job_id, tuple(bookmark_ids or [])),
        daemon=True,
        name=f"dead-link-job-{job_id}",
    )
    worker.start()


def get_dead_link_job_details(job: DeadLinkJob) -> dict:
    payload = job.as_dict()
    runtime = _runtime_snapshot(job.id)
    payload.update(
        {
            "current_title": runtime.get("current_title"),
            "current_url": runtime.get("current_url"),
            "items_per_second": runtime.get("items_per_second"),
            "eta_seconds": runtime.get("eta_seconds"),
            "elapsed_seconds": runtime.get("elapsed_seconds"),
            "status_message": runtime.get("status_message"),
            "can_stop": job.status in {"pending", "running"},
            "can_delete": job.status in {"done", "failed", "stopped"},
        }
    )
    return payload


def request_dead_link_job_stop(job_id: int, user_id: int) -> bool:
    job = DeadLinkJob.query.filter_by(id=job_id, user_id=user_id).first()
    if not job or job.status not in {"pending", "running"}:
        return False

    with _RUNTIME_LOCK:
        _STOP_REQUESTED.add(job_id)

    _runtime_update(
        job_id, {"status_message": "Stop requested. Finishing active checks..."}
    )
    return True


def clear_dead_link_job_runtime(job_id: int) -> None:
    with _RUNTIME_LOCK:
        _RUNTIME_STATE.pop(job_id, None)
        _STOP_REQUESTED.discard(job_id)


def _run_dead_link_job(
    app: Flask,
    user_id: int,
    job_id: int,
    bookmark_ids: tuple[int, ...] = (),
) -> None:
    with app.app_context():
        db.session.remove()
        started_at = utcnow()
        _runtime_update(
            job_id,
            {
                "started_at": started_at,
                "status_message": "Preparing dead-link targets...",
            },
        )

        try:
            job = DeadLinkJob.query.filter_by(id=job_id, user_id=user_id).first()
            if not job:
                return

            if _stop_requested(job_id):
                _finish_job(
                    job_id=job_id,
                    started_at=started_at,
                    status="stopped",
                    error_message="Stopped by user.",
                )
                return

            job.status = "running"
            job.progress = 0
            job.total_targets = 0
            job.total_checked = 0
            job.total_alive = 0
            job.total_problematic = 0
            job.total_errors = 0
            job.error_message = None
            db.session.commit()

            targets = _load_targets(user_id=user_id, bookmark_ids=list(bookmark_ids))
            total_targets = len(targets)
            if total_targets == 0:
                _finish_job(
                    job_id=job_id,
                    started_at=started_at,
                    status="done",
                    error_message="No bookmarks need checking.",
                )
                return

            job.total_targets = total_targets
            db.session.commit()

            timeout = float(app.config["CONTENT_FETCH_TIMEOUT"])
            max_bytes = int(app.config["CONTENT_MAX_BYTES"])
            max_workers = int(app.config.get("DEAD_LINK_WORKERS", 12))
            max_workers = max(2, min(max_workers, 24))

            checked = 0
            alive = 0
            problematic = 0
            errors = 0

            executor = ThreadPoolExecutor(max_workers=max_workers)
            futures = {
                executor.submit(
                    fetch_and_extract,
                    target.url,
                    timeout=timeout,
                    max_bytes=max_bytes,
                ): target
                for target in targets
            }
            stop_job = False
            try:
                for future in as_completed(futures):
                    if _stop_requested(job_id):
                        stop_job = True
                        break

                    target = futures[future]
                    extracted = _resolve_future(future)
                    status_message = f"Checked {target.url}"
                    try:
                        bookmark = Bookmark.query.filter_by(
                            id=target.bookmark_id,
                            user_id=user_id,
                        ).first()
                        if bookmark:
                            bookmark.link_status = extracted.status
                            bookmark.last_checked_at = utcnow()
                            content = bookmark.content or BookmarkContent(
                                bookmark_id=bookmark.id
                            )
                            content.extracted_text = extracted.text
                            content.extracted_at = utcnow()
                            content.fetch_status = extracted.status
                            content.fetch_error = extracted.error
                            content.content_hash = hashlib.sha256(
                                extracted.text.encode("utf-8")
                            ).hexdigest()

                            extracted_notes = (extracted.text or "").strip()
                            if extracted_notes:
                                bookmark.notes = extracted_notes
                            elif (bookmark.notes or "").strip().lower() == "none":
                                bookmark.notes = None

                            db.session.add(content)
                            check = LinkCheck()
                            check.bookmark_id = bookmark.id
                            check.status_code = extracted.status_code
                            check.final_url = extracted.final_url
                            check.result_type = extracted.status
                            check.latency_ms = None
                            check.error = extracted.error
                            db.session.add(check)
                            db.session.commit()
                    except Exception as exc:
                        db.session.rollback()
                        extracted = ExtractedContent(
                            title=None,
                            text="",
                            status=LINK_STATUS_UNREACHABLE,
                            error=str(exc),
                            status_code=None,
                            final_url=None,
                        )
                        status_message = (
                            f"Failed to store check result: {str(exc)[:140]}"
                        )

                    checked += 1
                    if extracted.status in PROBLEMATIC_RESULTS:
                        problematic += 1
                    else:
                        alive += 1
                    if extracted.error:
                        errors += 1

                    _persist_progress(
                        job_id=job_id,
                        checked=checked,
                        total=total_targets,
                        alive=alive,
                        problematic=problematic,
                        errors=errors,
                        started_at=started_at,
                        current_title=target.title,
                        current_url=target.url,
                        status_message=status_message,
                    )
            finally:
                if stop_job:
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                else:
                    executor.shutdown(wait=True)

            if stop_job:
                _finish_job(
                    job_id=job_id,
                    started_at=started_at,
                    status="stopped",
                    error_message="Stopped by user.",
                )
                return

            completion_message = None
            if errors:
                completion_message = f"Finished with {errors} request/storage errors."
            _finish_job(
                job_id=job_id,
                started_at=started_at,
                status="done",
                error_message=completion_message,
            )
        except Exception as exc:
            db.session.rollback()
            _finish_job(
                job_id=job_id,
                started_at=started_at,
                status="failed",
                error_message=str(exc),
            )
        finally:
            db.session.remove()


def _load_targets(
    user_id: int,
    bookmark_ids: list[int] | None = None,
) -> list[_LinkTarget]:
    query = (
        Bookmark.query.filter_by(user_id=user_id)
        .filter(Bookmark.deleted_at.is_(None))
        .options(joinedload(Bookmark.tags))
        .order_by(Bookmark.last_checked_at.is_not(None), Bookmark.last_checked_at.asc())
    )

    if bookmark_ids:
        unique_ids = list(dict.fromkeys(bookmark_ids))
        query = query.filter(Bookmark.id.in_(unique_ids))

    rows = query.all()
    targets: list[_LinkTarget] = []
    touched_internal = False
    for row in rows:
        if bookmark_is_internal(row):
            set_internal_link_status(row)
            touched_internal = True
            continue
        targets.append(_LinkTarget(bookmark_id=row.id, url=row.url, title=row.title))

    if touched_internal:
        db.session.commit()

    return targets


def _resolve_future(future: Future[ExtractedContent]) -> ExtractedContent:
    try:
        return future.result()
    except Exception as exc:
        return ExtractedContent(
            title=None,
            text="",
            status_code=None,
            final_url=None,
            status=LINK_STATUS_UNREACHABLE,
            error=str(exc),
        )


def _persist_progress(
    job_id: int,
    checked: int,
    total: int,
    alive: int,
    problematic: int,
    errors: int,
    started_at: datetime,
    current_title: str | None,
    current_url: str,
    status_message: str,
) -> None:
    job = DeadLinkJob.query.filter_by(id=job_id).first()
    if job:
        job.progress = int((checked / total) * 100) if total else 100
        job.total_checked = checked
        job.total_alive = alive
        job.total_problematic = problematic
        job.total_errors = errors
        db.session.commit()

    _runtime_update(
        job_id,
        {
            "started_at": started_at,
            "current_title": current_title,
            "current_url": current_url,
            "status_message": status_message,
            "checked": checked,
            "total": total,
        },
    )


def _finish_job(
    job_id: int,
    started_at: datetime,
    status: str,
    error_message: str | None,
) -> None:
    job = DeadLinkJob.query.filter_by(id=job_id).first()
    if not job:
        return

    job.status = status
    if job.total_targets:
        job.progress = int((job.total_checked / job.total_targets) * 100)
    else:
        job.progress = 100
    job.error_message = error_message
    db.session.commit()

    _runtime_update(
        job_id,
        {
            "started_at": started_at,
            "finished_at": utcnow(),
            "status_message": error_message
            if error_message
            else ("Dead-link check complete." if status == "done" else "Check failed."),
        },
    )
    with _RUNTIME_LOCK:
        _STOP_REQUESTED.discard(job_id)


def _stop_requested(job_id: int) -> bool:
    with _RUNTIME_LOCK:
        return job_id in _STOP_REQUESTED


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

    checked = int(state.get("checked", 0) or 0)
    total = int(state.get("total", 0) or 0)

    items_per_second = None
    eta_seconds = None
    if elapsed_seconds and checked > 0:
        items_per_second = round(checked / elapsed_seconds, 2)
        if total > checked and items_per_second > 0:
            eta_seconds = int((total - checked) / items_per_second)

    state["elapsed_seconds"] = elapsed_seconds
    state["items_per_second"] = items_per_second
    state["eta_seconds"] = eta_seconds
    return state
