import os
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.extensions import db
from app.models import Bookmark, LinkCheck, utcnow
from app.services.content import check_link
from app.services.internal_links import bookmark_is_internal, set_internal_link_status


scheduler = BackgroundScheduler()


def run_dead_link_sweep(app):
    with app.app_context():
        stale_before = utcnow() - timedelta(days=7)
        bookmarks = (
            Bookmark.query.filter(Bookmark.deleted_at.is_(None))
            .filter(
                (Bookmark.last_checked_at.is_(None))
                | (Bookmark.last_checked_at < stale_before)
            )
            .order_by(
                Bookmark.last_checked_at.is_not(None), Bookmark.last_checked_at.asc()
            )
            .limit(50)
            .all()
        )

        for bookmark in bookmarks:
            if bookmark_is_internal(bookmark):
                set_internal_link_status(bookmark)
                continue

            result = check_link(
                bookmark.url, timeout=app.config["CONTENT_FETCH_TIMEOUT"]
            )
            check = LinkCheck(
                bookmark_id=bookmark.id,
                status_code=result.status_code,
                final_url=result.final_url,
                result_type=result.result_type,
                latency_ms=result.latency_ms,
                error=result.error,
            )
            bookmark.link_status = result.result_type
            bookmark.last_checked_at = utcnow()
            db.session.add(check)

        db.session.commit()


def start_scheduler(app):
    if not app.config.get("SCHEDULER_ENABLED", True):
        return
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return

    interval_minutes = app.config["DEAD_LINK_CHECK_INTERVAL_MINUTES"]
    if not scheduler.get_jobs():
        scheduler.add_job(
            run_dead_link_sweep,
            "interval",
            minutes=interval_minutes,
            kwargs={"app": app},
            id="dead_link_sweep",
            replace_existing=True,
        )
        scheduler.start()
