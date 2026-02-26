from __future__ import annotations

import html
import hashlib

from flask import (
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    ApiToken,
    Bookmark,
    BookmarkContent,
    DeadLinkJob,
    Folder,
    ImportJob,
    Tag,
    User,
    utcnow,
)
from app.services.common import normalize_url, parse_tags
from app.services.content import fetch_and_extract
from app.services.dead_link_jobs import (
    PROBLEMATIC_RESULTS,
    clear_dead_link_job_runtime,
    get_dead_link_job_details,
    request_dead_link_job_stop,
    start_dead_link_job,
)
from app.services.import_jobs import get_import_job_details, start_import_job
from app.services.internal_links import (
    INTERNAL_LINK_STATUS,
    INTERNAL_LINK_TAG,
    bookmark_is_internal,
    set_internal_link_status,
)
from app.services.search import search_bookmarks
from app.services.sync import (
    log_sync_event,
    serialize_bookmark_for_sync,
    serialize_folder_for_sync,
)
from app.web import web_bp


def _require_admin():
    if not current_user.is_admin:
        abort(403)


def _ensure_tag(bookmark: Bookmark, tag_name: str) -> None:
    normalized = (tag_name or "").strip().lower()
    if not normalized:
        return
    tag = Tag.query.filter_by(user_id=current_user.id, name=normalized).first()
    if not tag:
        tag = Tag(user_id=current_user.id, name=normalized)
        db.session.add(tag)
    if any(
        (existing.name or "").strip().lower() == normalized
        for existing in bookmark.tags
    ):
        return

    if tag not in bookmark.tags:
        bookmark.tags.append(tag)


def _drop_tag(bookmark: Bookmark, tag_name: str) -> None:
    normalized = (tag_name or "").strip().lower()
    if not normalized:
        return
    for tag in list(bookmark.tags):
        if (tag.name or "").strip().lower() == normalized:
            bookmark.tags.remove(tag)


def _set_internal_link(bookmark: Bookmark, enabled: bool) -> None:
    if enabled:
        _ensure_tag(bookmark, INTERNAL_LINK_TAG)
        set_internal_link_status(bookmark)
        return

    _drop_tag(bookmark, INTERNAL_LINK_TAG)
    if bookmark.link_status == INTERNAL_LINK_STATUS:
        bookmark.link_status = None


def _safe_redirect_target(raw_next: str | None, fallback: str) -> str:
    candidate = (raw_next or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return fallback


def _selected_ids_from_form(field_name: str = "bookmark_ids") -> list[int]:
    values = request.form.getlist(field_name)
    parsed: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            parsed_id = int(value)
        except (TypeError, ValueError):
            continue
        if parsed_id > 0 and parsed_id not in seen:
            seen.add(parsed_id)
            parsed.append(parsed_id)
    return parsed


def _selected_active_bookmarks(bookmark_ids: list[int]) -> list[Bookmark]:
    if not bookmark_ids:
        return []
    return (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.id.in_(bookmark_ids))
        .all()
    )


@web_bp.before_app_request
def first_run_gate():
    endpoint = request.endpoint or ""
    allowed_prefixes = {"static", "auth.bootstrap_admin", "auth.login"}
    if User.query.count() == 0 and endpoint not in allowed_prefixes:
        return redirect(url_for("auth.bootstrap_admin"))


def _assign_tags(bookmark: Bookmark, raw_tags: str):
    bookmark.tags.clear()
    for name in parse_tags(raw_tags):
        tag = Tag.query.filter_by(user_id=current_user.id, name=name).first()
        if not tag:
            tag = Tag(user_id=current_user.id, name=name)
            db.session.add(tag)
        bookmark.tags.append(tag)


def _normalize_notes_value(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.lower() == "none":
        return None
    return text


def _refresh_content(bookmark: Bookmark, populate_notes: bool = False):
    if bookmark_is_internal(bookmark):
        set_internal_link_status(bookmark)
        return

    extracted = fetch_and_extract(
        bookmark.url,
        timeout=current_app.config["CONTENT_FETCH_TIMEOUT"],
        max_bytes=current_app.config["CONTENT_MAX_BYTES"],
    )
    content = bookmark.content or BookmarkContent(bookmark_id=bookmark.id)
    if extracted.title and not bookmark.title:
        bookmark.title = extracted.title

    bookmark.link_status = extracted.status
    bookmark.last_checked_at = utcnow()
    content.extracted_text = extracted.text
    content.extracted_at = utcnow()
    content.fetch_status = extracted.status
    content.fetch_error = extracted.error
    if populate_notes:
        extracted_notes = _normalize_notes_value(extracted.text)
        if extracted_notes:
            bookmark.notes = extracted_notes
        elif _normalize_notes_value(bookmark.notes) is None:
            bookmark.notes = None
    content.content_hash = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
    db.session.add(content)


def _folder_options_tree(folders: list[Folder]) -> list[dict]:
    children_by_parent: dict[int | None, list[Folder]] = {}
    for folder in folders:
        children_by_parent.setdefault(folder.parent_id, []).append(folder)

    for child_list in children_by_parent.values():
        child_list.sort(key=lambda row: (row.name or "").lower())

    options: list[dict] = []
    visited: set[int] = set()

    def walk(parent_id: int | None, ancestry_has_more: list[bool]) -> None:
        siblings = children_by_parent.get(parent_id, [])
        for index, row in enumerate(siblings):
            if row.id in visited:
                continue
            is_last = index == (len(siblings) - 1)
            visited.add(row.id)
            if ancestry_has_more:
                prefix = "".join(
                    "│   " if has_more else "    "
                    for has_more in ancestry_has_more[:-1]
                )
                connector = "└── " if is_last else "├── "
                label = f"{prefix}{connector}{row.name}"
            else:
                label = row.name
            options.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "depth": len(ancestry_has_more),
                    "label": label,
                }
            )
            walk(row.id, ancestry_has_more + [not is_last])

    walk(None, [])

    if len(visited) != len(folders):
        leftovers = [
            row
            for row in sorted(folders, key=lambda item: (item.name or "").lower())
            if row.id not in visited
        ]
        for row in leftovers:
            options.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "depth": 0,
                    "label": row.name,
                }
            )

    return options


def _bookmark_status_options() -> list[str]:
    rows = (
        db.session.query(Bookmark.link_status)
        .filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.link_status.is_not(None))
        .distinct()
        .all()
    )
    return sorted({value for (value,) in rows if value})


def _build_folder_children_map(folders: list[Folder]) -> dict[int | None, list[Folder]]:
    children_by_parent: dict[int | None, list[Folder]] = {}
    for folder in folders:
        children_by_parent.setdefault(folder.parent_id, []).append(folder)
    for rows in children_by_parent.values():
        rows.sort(key=lambda folder: ((folder.name or "").lower(), folder.id))
    return children_by_parent


def _build_folder_path_lookup(folders: list[Folder]) -> dict[int, list[str]]:
    by_id = {folder.id: folder for folder in folders}
    cache: dict[int, list[str]] = {}

    def build_path(folder_id: int) -> list[str]:
        if folder_id in cache:
            return cache[folder_id]

        path: list[str] = []
        seen: set[int] = set()
        cursor = by_id.get(folder_id)
        while cursor and cursor.id not in seen:
            seen.add(cursor.id)
            label = (cursor.name or "").strip() or "Untitled Folder"
            path.append(label)
            cursor = by_id.get(cursor.parent_id)

        path.reverse()
        cache[folder_id] = path
        return path

    for folder in folders:
        build_path(folder.id)
    return cache


def _folder_breadcrumb_parts(
    folder_id: int | None,
    folder_paths: dict[int, list[str]],
) -> list[str]:
    if not folder_id:
        return []
    return list(folder_paths.get(folder_id, []))


def _truncate_bookmark_title_for_folders(value: str | None) -> str:
    title = (value or "").strip() or "(untitled)"
    if len(title) <= 200:
        return title
    return f"{title[:197].rstrip()}..."


def _folder_subtree_ids(
    folder_id: int,
    children_by_parent: dict[int | None, list[Folder]],
) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()

    def walk(current_id: int) -> None:
        if current_id in seen:
            return
        seen.add(current_id)
        ordered.append(current_id)
        for child in children_by_parent.get(current_id, []):
            walk(child.id)

    walk(folder_id)
    return ordered


def _folder_subtree_active_bookmark_counts(
    folders: list[Folder],
    active_bookmarks: list[Bookmark],
) -> dict[int, int]:
    children_by_parent = _build_folder_children_map(folders)
    direct_counts: dict[int, int] = {}
    for bookmark in active_bookmarks:
        if bookmark.folder_id:
            direct_counts[bookmark.folder_id] = (
                direct_counts.get(bookmark.folder_id, 0) + 1
            )

    totals: dict[int, int] = {}

    def total_for(folder_id: int) -> int:
        if folder_id in totals:
            return totals[folder_id]
        total = direct_counts.get(folder_id, 0)
        for child in children_by_parent.get(folder_id, []):
            total += total_for(child.id)
        totals[folder_id] = total
        return total

    for folder in folders:
        total_for(folder.id)
    return totals


def _request_optional_parent_id(field_name: str = "parent_id") -> int | None:
    raw = request.form.get(field_name)
    if raw is None:
        payload = request.get_json(silent=True) or {}
        raw = payload.get(field_name)

    if raw is None:
        return None

    if isinstance(raw, int):
        if raw <= 0:
            raise ValueError("invalid parent id")
        return raw

    value = str(raw).strip()
    if not value:
        return None

    parent_id = int(value)
    if parent_id <= 0:
        raise ValueError("invalid parent id")
    return parent_id


def _request_bool(field_name: str) -> bool:
    raw = request.form.get(field_name)
    if raw is None:
        payload = request.get_json(silent=True) or {}
        raw = payload.get(field_name)
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _json_error(message: str, status_code: int = 400):
    return jsonify({"ok": False, "error": message}), status_code


def _build_browser_import_html(user_id: int) -> str:
    folders = Folder.query.filter_by(user_id=user_id).all()
    active_bookmarks = (
        Bookmark.query.filter_by(user_id=user_id)
        .filter(Bookmark.deleted_at.is_(None))
        .all()
    )

    children_by_parent: dict[int | None, list[Folder]] = {}
    for folder in folders:
        children_by_parent.setdefault(folder.parent_id, []).append(folder)
    for rows in children_by_parent.values():
        rows.sort(key=lambda folder: (folder.name or "").lower())

    bookmarks_by_folder: dict[int | None, list[Bookmark]] = {}
    for bookmark in active_bookmarks:
        bookmarks_by_folder.setdefault(bookmark.folder_id, []).append(bookmark)
    for rows in bookmarks_by_folder.values():
        rows.sort(
            key=lambda bookmark: (
                (bookmark.title or "").lower(),
                (bookmark.url or "").lower(),
            )
        )

    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This file is automatically generated by LinkLoom. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]

    def append_bookmarks(folder_id: int | None, depth: int) -> None:
        indent = "    " * depth
        for bookmark in bookmarks_by_folder.get(folder_id, []):
            title = (bookmark.title or "").strip() or bookmark.url
            lines.append(
                f'{indent}<DT><A HREF="{html.escape(bookmark.url, quote=True)}">'
                f"{html.escape(title)}</A>"
            )

    visited: set[int] = set()

    def append_folder(folder: Folder, depth: int) -> None:
        if folder.id in visited:
            return
        visited.add(folder.id)

        indent = "    " * depth
        folder_name = (folder.name or "").strip() or "Untitled Folder"
        lines.append(f"{indent}<DT><H3>{html.escape(folder_name)}</H3>")
        lines.append(f"{indent}<DL><p>")

        append_bookmarks(folder.id, depth + 1)
        for child in children_by_parent.get(folder.id, []):
            append_folder(child, depth + 1)

        lines.append(f"{indent}</DL><p>")

    append_bookmarks(None, depth=1)

    for folder in children_by_parent.get(None, []):
        append_folder(folder, depth=1)

    if len(visited) != len(folders):
        leftovers = [folder for folder in folders if folder.id not in visited]
        leftovers.sort(key=lambda folder: (folder.name or "").lower())
        for folder in leftovers:
            append_folder(folder, depth=1)

    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"


def _bookmarks_query_for_current_user(
    folder_id: int | None,
    tag: str | None,
    status: str | None,
):
    query = Bookmark.query.options(
        joinedload(Bookmark.tags),
        joinedload(Bookmark.folder),
    ).filter_by(user_id=current_user.id)
    query = query.filter(Bookmark.deleted_at.is_(None))
    if folder_id:
        query = query.filter_by(folder_id=folder_id)
    if tag:
        query = query.join(Bookmark.tags).filter(Tag.name == tag.lower())
    if status:
        query = query.filter(Bookmark.link_status == status)
    return query


def _serialize_bookmark_card(
    item: Bookmark,
    score=None,
    reasons=None,
    folder_paths: dict[int, list[str]] | None = None,
) -> dict:
    folder_path = []
    if folder_paths is not None:
        folder_path = _folder_breadcrumb_parts(item.folder_id, folder_paths)
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "folder_name": item.folder.name if item.folder else None,
        "folder_path": folder_path,
        "tags": [tag.name for tag in item.tags],
        "link_status": item.link_status,
        "score": score,
        "reasons": reasons or [],
    }


@web_bp.route("/")
@login_required
def dashboard():
    total = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .count()
    )
    deleted = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_not(None))
        .count()
    )
    dead = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.link_status.in_(list(PROBLEMATIC_RESULTS)))
        .count()
    )
    recent = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.updated_at.desc())
        .limit(8)
        .all()
    )

    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc())
        .all()
    )
    active_bookmarks = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.title.asc(), Bookmark.url.asc())
        .all()
    )

    children_by_parent: dict[int | None, list[Folder]] = {}
    for folder in folders:
        children_by_parent.setdefault(folder.parent_id, []).append(folder)

    bookmarks_by_folder: dict[int | None, list[Bookmark]] = {}
    for item in active_bookmarks:
        bookmarks_by_folder.setdefault(item.folder_id, []).append(item)

    return render_template(
        "dashboard.html",
        app_name="LinkLoom",
        total=total,
        deleted=deleted,
        dead=dead,
        recent=recent,
        children_by_parent=children_by_parent,
        bookmarks_by_folder=bookmarks_by_folder,
    )


@web_bp.route("/export/browser-html")
@login_required
def export_browser_html():
    payload = _build_browser_import_html(current_user.id)
    timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"linkloom-bookmarks-{timestamp}.html"
    return Response(
        payload,
        content_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@web_bp.route("/bookmarks")
@login_required
def bookmarks():
    folder_id = request.args.get("folder_id", type=int)
    tag = request.args.get("tag")
    status = (request.args.get("status") or "").strip() or None
    q = (request.args.get("q") or "").strip()

    query = _bookmarks_query_for_current_user(
        folder_id=folder_id,
        tag=tag,
        status=status,
    )
    search_rows = []
    if q:
        source = (
            query.options(joinedload(Bookmark.content))
            .order_by(Bookmark.updated_at.desc())
            .all()
        )
        search_rows = search_bookmarks(source, q, limit=500)
        items = [row["bookmark"] for row in search_rows]
    else:
        items = query.order_by(Bookmark.updated_at.desc()).all()

    search_meta = {
        row["bookmark"].id: {
            "score": row["score"],
            "reasons": row["reasons"],
        }
        for row in search_rows
    }

    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc())
        .all()
    )
    folder_options = _folder_options_tree(folders)
    folder_path_lookup = _build_folder_path_lookup(folders)
    bookmark_folder_paths = {
        item.id: _folder_breadcrumb_parts(item.folder_id, folder_path_lookup)
        for item in items
    }
    tags = Tag.query.filter_by(user_id=current_user.id).order_by(Tag.name.asc()).all()
    status_options = _bookmark_status_options()
    return render_template(
        "bookmarks.html",
        app_name="LinkLoom",
        items=items,
        folders=folders,
        folder_options=folder_options,
        tags=tags,
        status_options=status_options,
        active_folder_id=folder_id,
        active_tag=tag,
        active_status=status,
        q=q,
        search_meta=search_meta,
        bookmark_folder_paths=bookmark_folder_paths,
    )


@web_bp.route("/bookmarks/live")
@login_required
def bookmarks_live():
    folder_id = request.args.get("folder_id", type=int)
    tag = request.args.get("tag")
    status = (request.args.get("status") or "").strip() or None
    q = (request.args.get("q") or "").strip()
    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc())
        .all()
    )
    folder_path_lookup = _build_folder_path_lookup(folders)

    query = _bookmarks_query_for_current_user(
        folder_id=folder_id,
        tag=tag,
        status=status,
    )
    if q:
        source = (
            query.options(joinedload(Bookmark.content))
            .order_by(Bookmark.updated_at.desc())
            .all()
        )
        ranked = search_bookmarks(source, q, limit=500)
        items = [
            _serialize_bookmark_card(
                item=row["bookmark"],
                score=row["score"],
                reasons=row["reasons"],
                folder_paths=folder_path_lookup,
            )
            for row in ranked
        ]
    else:
        source = query.order_by(Bookmark.updated_at.desc()).all()
        items = [
            _serialize_bookmark_card(item=row, folder_paths=folder_path_lookup)
            for row in source
        ]

    return jsonify({"q": q, "status": status, "items": items})


@web_bp.route("/bookmarks/new", methods=["GET", "POST"])
@login_required
def bookmarks_new():
    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc())
        .all()
    )
    folder_options = _folder_options_tree(folders)
    if request.method == "POST":
        url = (request.form.get("url") or "").strip()
        if not url:
            flash("URL is required.", "error")
            return render_template(
                "bookmark_form.html",
                app_name="LinkLoom",
                folders=folders,
                folder_options=folder_options,
                item=None,
            )

        bookmark = Bookmark(
            user_id=current_user.id,
            url=url,
            normalized_url=normalize_url(url),
            title=(request.form.get("title") or "").strip() or None,
            notes=_normalize_notes_value(request.form.get("notes")),
            folder_id=request.form.get("folder_id", type=int),
        )
        db.session.add(bookmark)
        db.session.flush()
        _assign_tags(bookmark, request.form.get("tags") or "")
        _refresh_content(bookmark)
        log_sync_event(
            current_user.id,
            "bookmark",
            bookmark.id,
            "create",
            serialize_bookmark_for_sync(bookmark),
        )
        db.session.commit()
        flash("Bookmark added and indexed.", "success")
        return redirect(url_for("web.bookmarks"))

    return render_template(
        "bookmark_form.html",
        app_name="LinkLoom",
        folders=folders,
        folder_options=folder_options,
        item=None,
    )


@web_bp.route("/bookmarks/<int:bookmark_id>/edit", methods=["GET", "POST"])
@login_required
def bookmarks_edit(bookmark_id: int):
    item = Bookmark.query.filter_by(
        id=bookmark_id, user_id=current_user.id
    ).first_or_404()
    if item.deleted_at is not None:
        abort(404)

    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc())
        .all()
    )
    folder_options = _folder_options_tree(folders)
    next_url = _safe_redirect_target(
        request.form.get("next") or request.args.get("next"),
        url_for("web.bookmarks"),
    )
    if request.method == "POST":
        item.url = (request.form.get("url") or "").strip()
        item.normalized_url = normalize_url(item.url)
        item.title = (request.form.get("title") or "").strip() or None
        item.notes = _normalize_notes_value(request.form.get("notes"))
        item.folder_id = request.form.get("folder_id", type=int)
        internal_link = request.form.get("internal_link") == "1"
        _assign_tags(item, request.form.get("tags") or "")
        _set_internal_link(item, internal_link)
        if internal_link:
            item.link_status = INTERNAL_LINK_STATUS
            item.last_checked_at = utcnow()
        elif request.form.get("reindex") == "1":
            _refresh_content(item, populate_notes=True)
        log_sync_event(
            current_user.id,
            "bookmark",
            item.id,
            "update",
            serialize_bookmark_for_sync(item),
        )
        db.session.commit()
        flash("Bookmark updated.", "success")
        return redirect(next_url)

    return render_template(
        "bookmark_form.html",
        app_name="LinkLoom",
        folders=folders,
        folder_options=folder_options,
        item=item,
        next_url=next_url,
        is_internal_link=bookmark_is_internal(item),
    )


@web_bp.route("/bookmarks/<int:bookmark_id>/delete", methods=["POST"])
@login_required
def bookmarks_delete(bookmark_id: int):
    item = Bookmark.query.filter_by(
        id=bookmark_id, user_id=current_user.id
    ).first_or_404()
    item.deleted_at = utcnow()
    item.deleted_by = current_user.id
    log_sync_event(
        current_user.id,
        "bookmark",
        item.id,
        "delete",
        serialize_bookmark_for_sync(item),
    )
    db.session.commit()
    flash("Moved bookmark to recycle bin.", "success")
    next_url = _safe_redirect_target(
        request.form.get("next") or request.args.get("next"),
        url_for("web.bookmarks"),
    )
    return redirect(next_url)


@web_bp.route("/bookmarks/delete-selected", methods=["POST"])
@login_required
def bookmarks_delete_selected():
    bookmark_ids = _selected_ids_from_form("bookmark_ids")
    if not bookmark_ids:
        flash("No bookmarks selected.", "error")
        next_url = _safe_redirect_target(
            request.form.get("next"),
            url_for("web.bookmarks"),
        )
        return redirect(next_url)

    rows = _selected_active_bookmarks(bookmark_ids)

    deleted_count = 0
    for item in rows:
        item.deleted_at = utcnow()
        item.deleted_by = current_user.id
        log_sync_event(
            current_user.id,
            "bookmark",
            item.id,
            "delete",
            serialize_bookmark_for_sync(item),
        )
        deleted_count += 1

    db.session.commit()

    noun = "bookmark" if deleted_count == 1 else "bookmarks"
    flash(f"Moved {deleted_count} {noun} to recycle bin.", "success")
    next_url = _safe_redirect_target(
        request.form.get("next"),
        url_for("web.bookmarks"),
    )
    return redirect(next_url)


@web_bp.route("/bookmarks/move-selected", methods=["POST"])
@login_required
def bookmarks_move_selected():
    bookmark_ids = _selected_ids_from_form("bookmark_ids")
    next_url = _safe_redirect_target(
        request.form.get("next"),
        url_for("web.bookmarks"),
    )
    if not bookmark_ids:
        flash("No bookmarks selected.", "error")
        return redirect(next_url)

    raw_folder_id = (request.form.get("folder_id") or "").strip()
    if raw_folder_id:
        try:
            folder_id = int(raw_folder_id)
        except ValueError:
            flash("Select a valid folder.", "error")
            return redirect(next_url)
        folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
        if not folder:
            flash("Selected folder was not found.", "error")
            return redirect(next_url)
    else:
        folder_id = None

    rows = _selected_active_bookmarks(bookmark_ids)
    moved_count = 0
    for item in rows:
        if item.folder_id == folder_id:
            continue
        item.folder_id = folder_id
        log_sync_event(
            current_user.id,
            "bookmark",
            item.id,
            "update",
            serialize_bookmark_for_sync(item),
        )
        moved_count += 1

    if moved_count:
        db.session.commit()

    noun = "bookmark" if moved_count == 1 else "bookmarks"
    flash(f"Moved {moved_count} {noun} to the selected folder.", "success")
    return redirect(next_url)


@web_bp.route("/bookmarks/add-tags-selected", methods=["POST"])
@login_required
def bookmarks_add_tags_selected():
    bookmark_ids = _selected_ids_from_form("bookmark_ids")
    next_url = _safe_redirect_target(
        request.form.get("next"),
        url_for("web.bookmarks"),
    )
    if not bookmark_ids:
        flash("No bookmarks selected.", "error")
        return redirect(next_url)

    tag_names = parse_tags(request.form.get("tags") or "")
    if not tag_names:
        flash("Enter at least one tag to add.", "error")
        return redirect(next_url)

    rows = _selected_active_bookmarks(bookmark_ids)
    updated_count = 0
    for item in rows:
        before = {tag.name for tag in item.tags}
        for tag_name in tag_names:
            _ensure_tag(item, tag_name)
        after = {tag.name for tag in item.tags}
        if after == before:
            continue
        log_sync_event(
            current_user.id,
            "bookmark",
            item.id,
            "update",
            serialize_bookmark_for_sync(item),
        )
        updated_count += 1

    if updated_count:
        db.session.commit()

    noun = "bookmark" if updated_count == 1 else "bookmarks"
    flash(f"Added tags to {updated_count} {noun}.", "success")
    return redirect(next_url)


@web_bp.route("/folders")
@login_required
def folders_page():
    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc(), Folder.id.asc())
        .all()
    )
    active_bookmarks = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.title.asc(), Bookmark.url.asc(), Bookmark.id.asc())
        .all()
    )

    children_by_parent = _build_folder_children_map(folders)
    folder_options = _folder_options_tree(folders)
    folder_subtree_bookmark_counts = _folder_subtree_active_bookmark_counts(
        folders,
        active_bookmarks,
    )

    bookmarks_by_folder: dict[int | None, list[dict]] = {}
    for bookmark in active_bookmarks:
        title = (bookmark.title or "").strip() or "(untitled)"
        bookmarks_by_folder.setdefault(bookmark.folder_id, []).append(
            {
                "id": bookmark.id,
                "title": title,
                "display_title": _truncate_bookmark_title_for_folders(title),
            }
        )

    for rows in bookmarks_by_folder.values():
        rows.sort(key=lambda row: (row["title"].lower(), row["id"]))

    return render_template(
        "folders.html",
        app_name="LinkLoom",
        children_by_parent=children_by_parent,
        bookmarks_by_folder=bookmarks_by_folder,
        folder_options=folder_options,
        folder_subtree_bookmark_counts=folder_subtree_bookmark_counts,
    )


@web_bp.route("/folders/create", methods=["POST"])
@login_required
def folders_create_web():
    payload = request.get_json(silent=True) or {}
    name = (request.form.get("name") or payload.get("name") or "").strip()
    if not name:
        return _json_error("Folder name is required.")

    try:
        parent_id = _request_optional_parent_id("parent_id")
    except (TypeError, ValueError):
        return _json_error("Parent folder is invalid.")

    if parent_id is not None:
        parent = Folder.query.filter_by(id=parent_id, user_id=current_user.id).first()
        if not parent:
            return _json_error("Parent folder was not found.", 404)

    folder = Folder(user_id=current_user.id, name=name, parent_id=parent_id)
    db.session.add(folder)
    try:
        db.session.flush()
        log_sync_event(
            current_user.id,
            "folder",
            folder.id,
            "create",
            serialize_folder_for_sync(folder),
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _json_error(
            "A folder with that name already exists in the selected location.",
            409,
        )

    return jsonify({"ok": True, "folder": folder.as_dict()}), 201


@web_bp.route("/folders/<int:folder_id>/rename", methods=["POST"])
@login_required
def folders_rename_web(folder_id: int):
    folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
    if not folder:
        return _json_error("Folder not found.", 404)

    payload = request.get_json(silent=True) or {}
    name = (request.form.get("name") or payload.get("name") or "").strip()
    if not name:
        return _json_error("Folder name is required.")

    folder.name = name
    log_sync_event(
        current_user.id,
        "folder",
        folder.id,
        "update",
        serialize_folder_for_sync(folder),
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _json_error(
            "A sibling folder already uses that name.",
            409,
        )

    return jsonify({"ok": True, "folder": folder.as_dict()})


@web_bp.route("/folders/<int:folder_id>/move", methods=["POST"])
@login_required
def folders_move_web(folder_id: int):
    folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
    if not folder:
        return _json_error("Folder not found.", 404)

    try:
        parent_id = _request_optional_parent_id("parent_id")
    except (TypeError, ValueError):
        return _json_error("Target folder is invalid.")

    if parent_id == folder.id:
        return _json_error("A folder cannot be moved into itself.")

    all_folders = Folder.query.filter_by(user_id=current_user.id).all()
    by_id = {row.id: row for row in all_folders}

    if parent_id is not None and parent_id not in by_id:
        return _json_error("Target folder was not found.", 404)

    cursor = by_id.get(parent_id)
    while cursor:
        if cursor.id == folder.id:
            return _json_error("A folder cannot be moved inside its own subtree.")
        cursor = by_id.get(cursor.parent_id)

    if folder.parent_id == parent_id:
        return jsonify({"ok": True, "folder": folder.as_dict(), "moved": False})

    folder.parent_id = parent_id
    log_sync_event(
        current_user.id,
        "folder",
        folder.id,
        "update",
        serialize_folder_for_sync(folder),
    )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _json_error(
            "A sibling folder already uses that name in the destination.",
            409,
        )

    return jsonify({"ok": True, "folder": folder.as_dict(), "moved": True})


@web_bp.route("/folders/bookmarks/move", methods=["POST"])
@login_required
def folders_move_bookmarks_web():
    bookmark_ids = _selected_ids_from_form("bookmark_ids")
    if not bookmark_ids:
        return _json_error("Select at least one bookmark to move.")

    try:
        folder_id = _request_optional_parent_id("folder_id")
    except (TypeError, ValueError):
        return _json_error("Target folder is invalid.")

    if folder_id is not None:
        target = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
        if not target:
            return _json_error("Target folder was not found.", 404)

    rows = _selected_active_bookmarks(bookmark_ids)
    moved_count = 0
    for bookmark in rows:
        if bookmark.folder_id == folder_id:
            continue
        bookmark.folder_id = folder_id
        log_sync_event(
            current_user.id,
            "bookmark",
            bookmark.id,
            "update",
            serialize_bookmark_for_sync(bookmark),
        )
        moved_count += 1

    if moved_count:
        db.session.commit()

    return jsonify(
        {
            "ok": True,
            "moved": moved_count,
            "selected": len(bookmark_ids),
            "folder_id": folder_id,
        }
    )


@web_bp.route("/folders/<int:folder_id>/delete", methods=["POST"])
@login_required
def folders_delete_web(folder_id: int):
    folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
    if not folder:
        return _json_error("Folder not found.", 404)

    folders = Folder.query.filter_by(user_id=current_user.id).all()
    children_by_parent = _build_folder_children_map(folders)
    by_id = {row.id: row for row in folders}
    subtree_ids = _folder_subtree_ids(folder.id, children_by_parent)
    subtree_set = set(subtree_ids)

    bookmark_rows = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.folder_id.in_(subtree_ids))
        .all()
    )
    active_bookmark_count = sum(1 for row in bookmark_rows if row.deleted_at is None)
    confirmed = _request_bool("confirm_delete")

    if active_bookmark_count > 0 and not confirmed:
        return _json_error(
            "This folder contains bookmarks. Confirm deletion to continue.",
            409,
        )

    deleted_bookmarks = 0
    for bookmark in bookmark_rows:
        bookmark.folder_id = None
        if bookmark.deleted_at is None:
            bookmark.deleted_at = utcnow()
            bookmark.deleted_by = current_user.id
            log_sync_event(
                current_user.id,
                "bookmark",
                bookmark.id,
                "delete",
                serialize_bookmark_for_sync(bookmark),
            )
            deleted_bookmarks += 1

    ordered_for_delete: list[int] = []
    visited: set[int] = set()

    def walk_for_delete(current_id: int) -> None:
        if current_id in visited:
            return
        visited.add(current_id)
        for child in children_by_parent.get(current_id, []):
            if child.id in subtree_set:
                walk_for_delete(child.id)
        ordered_for_delete.append(current_id)

    walk_for_delete(folder.id)

    deleted_folders = 0
    for candidate_id in ordered_for_delete:
        candidate = by_id.get(candidate_id)
        if not candidate:
            continue
        payload = serialize_folder_for_sync(candidate)
        log_sync_event(current_user.id, "folder", candidate.id, "delete", payload)
        db.session.delete(candidate)
        deleted_folders += 1

    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "deleted_folders": deleted_folders,
            "deleted_bookmarks": deleted_bookmarks,
            "required_confirmation": active_bookmark_count > 0,
        }
    )


@web_bp.route("/recycle-bin")
@login_required
def recycle_bin():
    items = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_not(None))
        .order_by(Bookmark.deleted_at.desc())
        .all()
    )
    return render_template("recycle_bin.html", app_name="LinkLoom", items=items)


@web_bp.route("/recycle-bin/<int:bookmark_id>/restore", methods=["POST"])
@login_required
def recycle_restore(bookmark_id: int):
    item = Bookmark.query.filter_by(
        id=bookmark_id, user_id=current_user.id
    ).first_or_404()
    item.deleted_at = None
    item.deleted_by = None
    log_sync_event(
        current_user.id,
        "bookmark",
        item.id,
        "restore",
        serialize_bookmark_for_sync(item),
    )
    db.session.commit()
    flash("Bookmark restored.", "success")
    return redirect(url_for("web.recycle_bin"))


@web_bp.route("/recycle-bin/<int:bookmark_id>/purge", methods=["POST"])
@login_required
def recycle_purge(bookmark_id: int):
    item = Bookmark.query.filter_by(
        id=bookmark_id, user_id=current_user.id
    ).first_or_404()
    if item.content:
        db.session.delete(item.content)
    item.tags.clear()
    log_sync_event(current_user.id, "bookmark", item.id, "purge", {"id": item.id})
    db.session.delete(item)
    db.session.commit()
    flash("Bookmark permanently deleted.", "success")
    return redirect(url_for("web.recycle_bin"))


@web_bp.route("/recycle-bin/empty", methods=["POST"])
@login_required
def recycle_empty():
    items = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_not(None))
        .all()
    )
    purged = 0
    for item in items:
        if item.content:
            db.session.delete(item.content)
        item.tags.clear()
        log_sync_event(current_user.id, "bookmark", item.id, "purge", {"id": item.id})
        db.session.delete(item)
        purged += 1

    db.session.commit()

    noun = "bookmark" if purged == 1 else "bookmarks"
    flash(f"Permanently deleted {purged} {noun} from recycle bin.", "success")
    return redirect(url_for("web.recycle_bin"))


@web_bp.route("/import", methods=["GET", "POST"])
@login_required
def import_bookmarks():
    latest_jobs = (
        ImportJob.query.filter_by(user_id=current_user.id)
        .order_by(ImportJob.created_at.desc())
        .limit(5)
        .all()
    )
    active_job = next(
        (job for job in latest_jobs if job.status in {"pending", "running"}),
        None,
    )
    if request.method == "POST":
        upload = request.files.get("bookmark_file")
        if not upload or not upload.filename:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Please choose an HTML export file."}), 400
            flash("Please choose an HTML export file.", "error")
            return render_template(
                "import.html",
                app_name="LinkLoom",
                jobs=latest_jobs,
                active_job=active_job,
            )

        html = upload.read().decode("utf-8", errors="ignore")
        job = ImportJob(user_id=current_user.id, status="pending", progress=0)
        db.session.add(job)
        db.session.commit()
        start_import_job(
            app=current_app._get_current_object(),
            user_id=current_user.id,
            job_id=job.id,
            html=html,
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"job": get_import_job_details(job)}), 202

        flash(
            "Import started. Progress will update in the Import status panel.",
            "success",
        )
        return redirect(url_for("web.import_bookmarks"))

    return render_template(
        "import.html", app_name="LinkLoom", jobs=latest_jobs, active_job=active_job
    )


@web_bp.route("/import/jobs/<int:job_id>/status")
@login_required
def import_job_status_web(job_id: int):
    job = ImportJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    return jsonify(get_import_job_details(job))


@web_bp.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    return redirect(url_for("web.bookmarks", q=q))


@web_bp.route("/search/live")
@login_required
def search_live():
    return bookmarks_live()


@web_bp.route("/dead-links", methods=["GET", "POST"])
@login_required
def dead_links():
    latest_jobs = (
        DeadLinkJob.query.filter_by(user_id=current_user.id)
        .order_by(DeadLinkJob.created_at.desc())
        .limit(20)
        .all()
    )
    active_job = next(
        (job for job in latest_jobs if job.status in {"pending", "running"}),
        None,
    )

    if request.method == "POST":
        job = DeadLinkJob(user_id=current_user.id, status="pending", progress=0)
        db.session.add(job)
        db.session.commit()
        start_dead_link_job(
            app=current_app._get_current_object(),
            user_id=current_user.id,
            job_id=job.id,
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"job": get_dead_link_job_details(job)}), 202

        flash(
            "Dead-link scan started. Progress will update in the status panel.",
            "success",
        )
        return redirect(url_for("web.dead_links"))

    items = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.link_status.in_(list(PROBLEMATIC_RESULTS)))
        .order_by(Bookmark.last_checked_at.desc())
        .all()
    )
    folders = (
        Folder.query.filter_by(user_id=current_user.id)
        .order_by(Folder.name.asc())
        .all()
    )
    folder_path_lookup = _build_folder_path_lookup(folders)
    dead_link_folder_paths = {
        item.id: _folder_breadcrumb_parts(item.folder_id, folder_path_lookup)
        for item in items
    }
    return render_template(
        "dead_links.html",
        app_name="LinkLoom",
        items=items,
        jobs=latest_jobs,
        active_job=active_job,
        dead_link_folder_paths=dead_link_folder_paths,
    )


@web_bp.route("/dead-links/recheck-selected", methods=["POST"])
@login_required
def dead_links_recheck_selected():
    bookmark_ids = _selected_ids_from_form("bookmark_ids")
    next_url = _safe_redirect_target(
        request.form.get("next") or request.args.get("next"),
        url_for("web.dead_links"),
    )
    if not bookmark_ids:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "No dead links selected."}), 400
        flash("No dead links selected.", "error")
        return redirect(next_url)

    job = DeadLinkJob(user_id=current_user.id, status="pending", progress=0)
    db.session.add(job)
    db.session.commit()
    start_dead_link_job(
        app=current_app._get_current_object(),
        user_id=current_user.id,
        job_id=job.id,
        bookmark_ids=bookmark_ids,
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"job": get_dead_link_job_details(job)}), 202

    flash(
        "Selected dead-link re-check started. Progress will update in the status panel.",
        "success",
    )
    return redirect(next_url)


@web_bp.route("/dead-links/jobs/<int:job_id>/status")
@login_required
def dead_link_job_status_web(job_id: int):
    job = DeadLinkJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    return jsonify(get_dead_link_job_details(job))


@web_bp.route("/dead-links/jobs/<int:job_id>/stop", methods=["POST"])
@login_required
def dead_link_job_stop_web(job_id: int):
    job = DeadLinkJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    stopped = request_dead_link_job_stop(job_id=job.id, user_id=current_user.id)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        payload = get_dead_link_job_details(job)
        payload["stop_requested"] = stopped
        return jsonify(payload), (202 if stopped else 409)

    if stopped:
        flash("Requested stop for dead-link scan job.", "success")
    else:
        flash("Job is not running.", "error")
    return redirect(url_for("web.dead_links"))


@web_bp.route("/dead-links/jobs/<int:job_id>/delete", methods=["POST"])
@login_required
def dead_link_job_delete_web(job_id: int):
    job = DeadLinkJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    if job.status in {"pending", "running"}:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "Stop the job before deleting it."}), 409
        flash("Stop the job before deleting it.", "error")
        return redirect(url_for("web.dead_links"))

    db.session.delete(job)
    db.session.commit()
    clear_dead_link_job_runtime(job_id)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"deleted": True, "job_id": job_id})

    flash(f"Deleted dead-link job #{job_id}.", "success")
    return redirect(url_for("web.dead_links"))


@web_bp.route("/dead-links/delete-all", methods=["POST"])
@login_required
def dead_links_delete_all():
    items = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.link_status.in_(list(PROBLEMATIC_RESULTS)))
        .all()
    )

    deleted_count = 0
    for item in items:
        item.deleted_at = utcnow()
        item.deleted_by = current_user.id
        log_sync_event(
            current_user.id,
            "bookmark",
            item.id,
            "delete",
            serialize_bookmark_for_sync(item),
        )
        deleted_count += 1

    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"deleted": deleted_count})

    noun = "bookmark" if deleted_count == 1 else "bookmarks"
    flash(f"Moved {deleted_count} dead {noun} to recycle bin.", "success")
    next_url = _safe_redirect_target(
        request.form.get("next") or request.args.get("next"),
        url_for("web.dead_links"),
    )
    return redirect(next_url)


@web_bp.route("/dead-links/delete-selected", methods=["POST"])
@login_required
def dead_links_delete_selected():
    bookmark_ids = _selected_ids_from_form("bookmark_ids")
    next_url = _safe_redirect_target(
        request.form.get("next") or request.args.get("next"),
        url_for("web.dead_links"),
    )
    if not bookmark_ids:
        flash("No dead links selected.", "error")
        return redirect(next_url)

    rows = (
        Bookmark.query.filter_by(user_id=current_user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.link_status.in_(list(PROBLEMATIC_RESULTS)))
        .filter(Bookmark.id.in_(bookmark_ids))
        .all()
    )

    deleted_count = 0
    for item in rows:
        item.deleted_at = utcnow()
        item.deleted_by = current_user.id
        log_sync_event(
            current_user.id,
            "bookmark",
            item.id,
            "delete",
            serialize_bookmark_for_sync(item),
        )
        deleted_count += 1

    db.session.commit()
    noun = "bookmark" if deleted_count == 1 else "bookmarks"
    flash(f"Moved {deleted_count} selected {noun} to recycle bin.", "success")
    return redirect(next_url)


@web_bp.route("/dead-links/<int:bookmark_id>/delete", methods=["POST"])
@login_required
def dead_links_delete_one(bookmark_id: int):
    item = Bookmark.query.filter_by(
        id=bookmark_id, user_id=current_user.id
    ).first_or_404()
    item.deleted_at = utcnow()
    item.deleted_by = current_user.id
    log_sync_event(
        current_user.id,
        "bookmark",
        item.id,
        "delete",
        serialize_bookmark_for_sync(item),
    )
    db.session.commit()
    flash("Dead link moved to recycle bin.", "success")
    next_url = _safe_redirect_target(
        request.form.get("next") or request.args.get("next"),
        url_for("web.dead_links"),
    )
    return redirect(next_url)


@web_bp.route("/admin/users", methods=["GET", "POST"])
@login_required
def admin_users():
    _require_admin()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        is_admin = request.form.get("is_admin") == "1"
        if not username or not password:
            flash("Username and password are required.", "error")
        elif User.query.filter_by(username=username).first():
            flash("Username already exists.", "error")
        else:
            user = User(username=username, is_admin=is_admin, is_active=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("User created.", "success")
            return redirect(url_for("web.admin_users"))

    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin_users.html", app_name="LinkLoom", users=users)


@web_bp.route("/tokens", methods=["GET", "POST"])
@login_required
def tokens():
    issued_token = None
    if request.method == "POST":
        name = (request.form.get("name") or "Extension Token").strip()
        token, token_hash = ApiToken.issue_token()
        row = ApiToken(user_id=current_user.id, name=name, token_hash=token_hash)
        db.session.add(row)
        db.session.commit()
        issued_token = token
        session["latest_token"] = issued_token

    if request.method == "GET":
        issued_token = session.pop("latest_token", None)

    tokens_list = (
        ApiToken.query.filter_by(user_id=current_user.id)
        .filter(ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
        .all()
    )
    return render_template(
        "tokens.html",
        app_name="LinkLoom",
        tokens=tokens_list,
        issued_token=issued_token,
    )


@web_bp.route("/tokens/<int:token_id>/revoke", methods=["POST"])
@login_required
def revoke_token(token_id: int):
    row = ApiToken.query.filter_by(id=token_id, user_id=current_user.id).first_or_404()
    row.revoked_at = utcnow()
    db.session.commit()
    flash("Token revoked.", "success")
    return redirect(url_for("web.tokens"))
