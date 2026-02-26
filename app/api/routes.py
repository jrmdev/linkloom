from __future__ import annotations

import hashlib

from flask import current_app, g, jsonify, request

from app.api import api_bp
from app.extensions import db
from app.models import (
    ApiToken,
    Bookmark,
    BookmarkContent,
    Folder,
    ImportJob,
    LinkCheck,
    SyncEvent,
    Tag,
    User,
    utcnow,
)
from app.services.bookmark_import import parse_bookmark_html
from app.services.common import normalize_url, parse_tags
from app.services.content import check_link, fetch_and_extract
from app.services.dead_link_jobs import PROBLEMATIC_RESULTS
from app.services.internal_links import bookmark_is_internal, set_internal_link_status
from app.services.search import search_bookmarks
from app.services.security import api_auth_required
from app.services.sync_enrichment_jobs import (
    start_sync_first_replace_server_enrichment,
)
from app.services.sync import (
    CONFIRM_PHRASE,
    SYNC_CONFIRM_PHRASES,
    SYNC_FIRST_MODES,
    SYNC_MODE_REPLACE_LOCAL,
    SYNC_MODE_REPLACE_SERVER,
    SYNC_MODE_TWO_WAY,
    apply_push_operation,
    create_confirmation_token,
    ensure_sync_client,
    log_sync_event,
    serialize_bookmark_for_sync,
    serialize_folder_for_sync,
    verify_confirmation_token,
)


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_notes_value(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.lower() == "none":
        return None
    return text


def _assign_tags(user_id: int, bookmark: Bookmark, tags_input):
    if isinstance(tags_input, list):
        names = parse_tags(
            ",".join(str(item) for item in tags_input if item is not None)
        )
    else:
        names = parse_tags(tags_input or "")

    bookmark.tags.clear()
    for name in names:
        tag = Tag.query.filter_by(user_id=user_id, name=name).first()
        if not tag:
            tag = Tag(user_id=user_id, name=name)
            db.session.add(tag)
        bookmark.tags.append(tag)


def _refresh_content(
    bookmark: Bookmark,
    populate_notes: bool = False,
    populate_title: bool = True,
):
    if bookmark_is_internal(bookmark):
        set_internal_link_status(bookmark)
        return

    extracted = fetch_and_extract(
        bookmark.url,
        timeout=current_app.config["CONTENT_FETCH_TIMEOUT"],
        max_bytes=current_app.config["CONTENT_MAX_BYTES"],
    )
    content = bookmark.content or BookmarkContent(bookmark_id=bookmark.id)
    if populate_title and extracted.title and not bookmark.title:
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
    db.session.add(content)


def _get_user_bookmark_or_404(user_id: int, bookmark_id: int):
    bookmark = Bookmark.query.filter_by(id=bookmark_id, user_id=user_id).first()
    if not bookmark:
        return None, (jsonify({"error": "bookmark not found"}), 404)
    return bookmark, None


def _normalize_sync_mode(raw_mode: str | None) -> str:
    mode = (raw_mode or "").strip().lower() or SYNC_MODE_REPLACE_LOCAL
    if mode not in SYNC_FIRST_MODES:
        return ""
    return mode


def _parse_local_folders(payload: dict) -> list[dict]:
    rows = payload.get("local_folders") or []
    if not isinstance(rows, list):
        return []

    parsed: list[dict] = []
    seen_ids: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        local_id = str(item.get("id") or "").strip()
        if not local_id or local_id in seen_ids:
            continue
        seen_ids.add(local_id)
        parent_raw = item.get("parent_id")
        parent_id = str(parent_raw).strip() if parent_raw is not None else None
        if parent_id == "":
            parent_id = None
        if parent_id == local_id:
            parent_id = None
        title = (
            item.get("title") or item.get("name") or ""
        ).strip() or "Untitled Folder"
        parsed.append({"id": local_id, "parent_id": parent_id, "title": title})
    return parsed


def _parse_local_bookmarks(payload: dict) -> list[dict]:
    rows = payload.get("local_bookmarks") or []
    if not isinstance(rows, list):
        return []

    parsed: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        normalized = normalize_url(url)
        if not normalized:
            continue

        tags_input = item.get("tags")
        tags: list[str]
        if isinstance(tags_input, list):
            tags = parse_tags(
                ",".join(str(value) for value in tags_input if value is not None)
            )
        else:
            tags = parse_tags(str(tags_input or ""))

        parent_raw = item.get("folder_local_id")
        if parent_raw is None:
            parent_raw = item.get("parent_id")
        parent_id = str(parent_raw).strip() if parent_raw is not None else None
        if parent_id == "":
            parent_id = None

        folder_path = item.get("folder_path")
        clean_folder_path: list[str] = []
        if isinstance(folder_path, list):
            clean_folder_path = [
                str(part).strip() for part in folder_path if str(part).strip()
            ]

        parsed.append(
            {
                "id": str(item.get("id") or "").strip() or None,
                "url": url,
                "normalized_url": normalized,
                "title": (item.get("title") or "").strip() or None,
                "notes": _normalize_notes_value(item.get("notes")),
                "tags": tags,
                "folder_local_id": parent_id,
                "folder_path": clean_folder_path,
                "updated_at": item.get("updated_at"),
            }
        )
    return parsed


def _active_server_bookmarks(user_id: int) -> list[Bookmark]:
    return (
        Bookmark.query.filter_by(user_id=user_id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.updated_at.desc())
        .all()
    )


def _sync_snapshot_server_bookmarks(user_id: int) -> list[Bookmark]:
    return (
        Bookmark.query.filter_by(user_id=user_id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.created_at.asc(), Bookmark.id.asc())
        .all()
    )


def _active_server_folders(user_id: int) -> list[Folder]:
    return Folder.query.filter_by(user_id=user_id).order_by(Folder.name.asc()).all()


def _estimate_two_way_merge_counts(
    local_bookmarks: list[dict],
    server_bookmarks: list[Bookmark],
) -> dict:
    local_counts: dict[str, int] = {}
    server_counts: dict[str, int] = {}

    for item in local_bookmarks:
        normalized = item["normalized_url"]
        local_counts[normalized] = local_counts.get(normalized, 0) + 1
    for bookmark in server_bookmarks:
        normalized = bookmark.normalized_url or ""
        if not normalized:
            continue
        server_counts[normalized] = server_counts.get(normalized, 0) + 1

    local_only = 0
    server_only = 0
    matched = 0
    for normalized in set(local_counts) | set(server_counts):
        local_count = local_counts.get(normalized, 0)
        server_count = server_counts.get(normalized, 0)
        overlap = min(local_count, server_count)
        matched += overlap
        local_only += local_count - overlap
        server_only += server_count - overlap

    return {
        "local_add_to_server": local_only,
        "server_add_to_local": server_only,
        "matched": matched,
    }


def _preflight_warning(mode: str) -> str:
    if mode == SYNC_MODE_REPLACE_LOCAL:
        return (
            "This operation can replace your entire browser bookmark tree with "
            "LinkLoom server bookmarks."
        )
    if mode == SYNC_MODE_REPLACE_SERVER:
        return (
            "This operation can replace all LinkLoom server bookmarks with your "
            "current browser bookmark tree."
        )
    return (
        "This operation merges missing bookmarks in both directions without "
        "mass deletion."
    )


def _build_sync_preflight_payload(
    mode: str, local_bookmarks: list[dict], user_id: int
) -> dict:
    server_bookmarks = _active_server_bookmarks(user_id)
    local_count = len(local_bookmarks)
    server_count = len(server_bookmarks)

    would_noop = False
    no_op_reason = None
    impact: dict

    if mode == SYNC_MODE_REPLACE_LOCAL:
        would_noop = server_count == 0
        no_op_reason = "server_empty" if would_noop else None
        impact = {
            "local_deletions": 0 if would_noop else local_count,
            "local_additions": 0 if would_noop else server_count,
            "server_deletions": 0,
            "server_additions": 0,
        }
    elif mode == SYNC_MODE_REPLACE_SERVER:
        would_noop = local_count == 0
        no_op_reason = "local_empty" if would_noop else None
        impact = {
            "local_deletions": 0,
            "local_additions": 0,
            "server_deletions": 0 if would_noop else server_count,
            "server_additions": 0 if would_noop else local_count,
        }
    else:
        merge_counts = _estimate_two_way_merge_counts(local_bookmarks, server_bookmarks)
        impact = {
            "local_deletions": 0,
            "local_additions": merge_counts["server_add_to_local"],
            "server_deletions": 0,
            "server_additions": merge_counts["local_add_to_server"],
            "matched": merge_counts["matched"],
        }

    return {
        "mode": mode,
        "warning": _preflight_warning(mode),
        "required_phrase": SYNC_CONFIRM_PHRASES[mode],
        "local_bookmark_count": local_count,
        "server_bookmark_count": server_count,
        "impact": impact,
        "would_noop": would_noop,
        "no_op_reason": no_op_reason,
    }


def _server_snapshot(user_id: int) -> dict:
    bookmarks = _sync_snapshot_server_bookmarks(user_id)
    folders = _active_server_folders(user_id)
    return {
        "bookmarks": [serialize_bookmark_for_sync(bookmark) for bookmark in bookmarks],
        "folders": [serialize_folder_for_sync(folder) for folder in folders],
    }


def _delete_server_folder_tree(user_id: int) -> int:
    folders = Folder.query.filter_by(user_id=user_id).all()
    if not folders:
        return 0

    children_by_parent: dict[int | None, list[Folder]] = {}
    by_id: dict[int, Folder] = {}
    for folder in folders:
        children_by_parent.setdefault(folder.parent_id, []).append(folder)
        by_id[folder.id] = folder

    visited: set[int] = set()
    ordered: list[Folder] = []

    def walk(folder: Folder) -> None:
        if folder.id in visited:
            return
        visited.add(folder.id)
        for child in children_by_parent.get(folder.id, []):
            walk(child)
        ordered.append(folder)

    for root in children_by_parent.get(None, []):
        walk(root)
    for folder in folders:
        walk(folder)

    for folder in ordered:
        payload = serialize_folder_for_sync(folder)
        log_sync_event(user_id, "folder", folder.id, "delete", payload)
        db.session.delete(folder)

    return len(ordered)


def _ensure_server_folders_from_local(
    user_id: int,
    local_folders: list[dict],
    local_bookmarks: list[dict],
) -> dict[str, int]:
    mapping: dict[str, int] = {}
    cache: dict[tuple[int | None, str], int] = {}

    def ensure_path(path_parts: list[str]) -> int | None:
        parent_id = None
        for part in path_parts:
            name = (part or "").strip()
            if not name:
                continue
            key = (parent_id, name)
            if key in cache:
                parent_id = cache[key]
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

    pending = {row["id"]: row for row in local_folders}
    while pending:
        progressed = False
        for local_id, row in list(pending.items()):
            parent_local_id = row.get("parent_id")
            if parent_local_id and parent_local_id not in mapping:
                continue
            parent_server_id = mapping.get(parent_local_id)
            folder = Folder.query.filter_by(
                user_id=user_id,
                name=row["title"],
                parent_id=parent_server_id,
            ).first()
            if not folder:
                folder = Folder(
                    user_id=user_id,
                    name=row["title"],
                    parent_id=parent_server_id,
                )
                db.session.add(folder)
                db.session.flush()
                log_sync_event(
                    user_id,
                    "folder",
                    folder.id,
                    "create",
                    serialize_folder_for_sync(folder),
                )
            mapping[local_id] = folder.id
            cache[(parent_server_id, row["title"])] = folder.id
            pending.pop(local_id)
            progressed = True
        if not progressed:
            for local_id, row in list(pending.items()):
                folder = Folder.query.filter_by(
                    user_id=user_id,
                    name=row["title"],
                    parent_id=None,
                ).first()
                if not folder:
                    folder = Folder(user_id=user_id, name=row["title"], parent_id=None)
                    db.session.add(folder)
                    db.session.flush()
                    log_sync_event(
                        user_id,
                        "folder",
                        folder.id,
                        "create",
                        serialize_folder_for_sync(folder),
                    )
                mapping[local_id] = folder.id
                cache[(None, row["title"])] = folder.id
                pending.pop(local_id)

    for bookmark in local_bookmarks:
        local_folder_id = bookmark.get("folder_local_id")
        if local_folder_id and local_folder_id in mapping:
            continue
        if bookmark.get("folder_path"):
            ensured = ensure_path(bookmark["folder_path"])
            if local_folder_id and ensured:
                mapping[local_folder_id] = ensured

    return mapping


def _create_server_bookmark_from_local(
    user: User,
    item: dict,
    folder_map: dict[str, int],
    populate_content: bool = True,
) -> Bookmark | None:
    url = (item.get("url") or "").strip()
    normalized = normalize_url(url)
    if not normalized:
        return None
    local_folder_id = item.get("folder_local_id")
    folder_id = folder_map.get(local_folder_id) if local_folder_id else None

    bookmark = Bookmark(
        user_id=user.id,
        folder_id=folder_id,
        url=url,
        normalized_url=normalized,
        title=item.get("title"),
        notes=item.get("notes"),
    )
    db.session.add(bookmark)
    db.session.flush()
    _assign_tags(user.id, bookmark, item.get("tags") or [])
    if populate_content:
        extracted = fetch_and_extract(
            bookmark.url,
            timeout=current_app.config["CONTENT_FETCH_TIMEOUT"],
            max_bytes=current_app.config["CONTENT_MAX_BYTES"],
        )
        content = bookmark.content or BookmarkContent(bookmark_id=bookmark.id)
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
    log_sync_event(
        user.id,
        "bookmark",
        bookmark.id,
        "create",
        serialize_bookmark_for_sync(bookmark),
    )
    return bookmark


def _replace_server_with_local_snapshot(
    user: User,
    local_folders: list[dict],
    local_bookmarks: list[dict],
) -> tuple[dict[str, int], dict[str, int], dict, list[int]]:
    active_server_bookmarks = _active_server_bookmarks(user.id)
    for bookmark in active_server_bookmarks:
        bookmark.deleted_at = utcnow()
        bookmark.deleted_by = user.id
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "delete",
            serialize_bookmark_for_sync(bookmark),
        )

    all_bookmarks = Bookmark.query.filter_by(user_id=user.id).all()
    for bookmark in all_bookmarks:
        bookmark.folder_id = None

    deleted_folders = _delete_server_folder_tree(user.id)
    folder_map = _ensure_server_folders_from_local(
        user.id, local_folders, local_bookmarks
    )

    bookmark_map: dict[str, int] = {}
    created_bookmark_ids: list[int] = []
    created_count = 0
    for item in local_bookmarks:
        created = _create_server_bookmark_from_local(
            user,
            item,
            folder_map,
            populate_content=False,
        )
        if not created:
            continue
        created_count += 1
        created_bookmark_ids.append(created.id)
        local_id = item.get("id")
        if local_id:
            bookmark_map[local_id] = created.id

    counts = {
        "server_deleted": len(active_server_bookmarks),
        "folders_deleted": deleted_folders,
        "server_created": created_count,
    }
    return folder_map, bookmark_map, counts, created_bookmark_ids


def _two_way_merge_snapshot(
    user: User,
    local_folders: list[dict],
    local_bookmarks: list[dict],
) -> tuple[dict[str, int], dict[str, int], dict]:
    folder_map = _ensure_server_folders_from_local(
        user.id, local_folders, local_bookmarks
    )
    server_bookmarks = _active_server_bookmarks(user.id)
    by_normalized: dict[str, list[Bookmark]] = {}
    for bookmark in server_bookmarks:
        by_normalized.setdefault(bookmark.normalized_url, []).append(bookmark)

    used_server_ids: set[int] = set()
    bookmark_map: dict[str, int] = {}
    created_count = 0
    updated_count = 0

    for item in local_bookmarks:
        normalized = item["normalized_url"]
        matches = by_normalized.get(normalized, [])
        matched: Bookmark | None = None
        for candidate in matches:
            if candidate.id in used_server_ids:
                continue
            matched = candidate
            break

        if matched:
            used_server_ids.add(matched.id)
            local_id = item.get("id")
            if local_id:
                bookmark_map[local_id] = matched.id
            continue

        created = _create_server_bookmark_from_local(user, item, folder_map)
        if not created:
            continue
        created_count += 1
        local_id = item.get("id")
        if local_id:
            bookmark_map[local_id] = created.id

    counts = {
        "server_created": created_count,
        "server_updated": updated_count,
    }
    return folder_map, bookmark_map, counts


@api_bp.route("/health")
def health():
    return jsonify({"status": "ok", "service": "LinkLoom"})


@api_bp.route("/auth/bootstrap-admin", methods=["POST"])
def bootstrap_admin_api():
    if User.query.count() > 0:
        return jsonify({"error": "bootstrap already completed"}), 409

    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    admin = User(username=username, is_admin=True, is_active=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    return jsonify({"status": "created", "user_id": admin.id}), 201


@api_bp.route("/auth/token", methods=["POST"])
def create_token_with_credentials():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    token_name = (payload.get("token_name") or "LinkLoom API Token").strip()

    user = User.query.filter_by(username=username).first()
    if not user or not user.is_active or not user.check_password(password):
        return jsonify({"error": "invalid credentials"}), 401

    token, token_hash = ApiToken.issue_token()
    row = ApiToken(user_id=user.id, name=token_name, token_hash=token_hash)
    db.session.add(row)
    db.session.commit()
    return jsonify({"token": token, "token_name": token_name, "user_id": user.id})


@api_bp.route("/admin/users", methods=["POST"])
@api_auth_required(admin=True)
def admin_create_user():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    is_admin = _to_bool(payload.get("is_admin"), default=False)

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "username already exists"}), 409

    user = User(username=username, is_admin=is_admin, is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({"status": "created", "user_id": user.id}), 201


@api_bp.route("/admin/users", methods=["GET"])
@api_auth_required(admin=True)
def admin_list_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify(
        {
            "items": [
                {
                    "id": user.id,
                    "username": user.username,
                    "is_admin": user.is_admin,
                    "is_active": user.is_active,
                    "created_at": user.created_at.isoformat(),
                }
                for user in users
            ]
        }
    )


@api_bp.route("/folders", methods=["GET"])
@api_auth_required()
def folders_list():
    user = g.api_user
    items = Folder.query.filter_by(user_id=user.id).order_by(Folder.name.asc()).all()
    return jsonify({"items": [item.as_dict() for item in items]})


@api_bp.route("/folders", methods=["POST"])
@api_auth_required()
def folders_create():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    parent_id = payload.get("parent_id")
    if not name:
        return jsonify({"error": "folder name is required"}), 400

    folder = Folder(user_id=user.id, name=name, parent_id=parent_id)
    db.session.add(folder)
    db.session.flush()
    log_sync_event(
        user.id,
        "folder",
        folder.id,
        "create",
        serialize_folder_for_sync(folder),
    )
    db.session.commit()
    return jsonify(folder.as_dict()), 201


@api_bp.route("/folders/<int:folder_id>", methods=["PATCH"])
@api_auth_required()
def folders_update(folder_id: int):
    user = g.api_user
    folder = Folder.query.filter_by(id=folder_id, user_id=user.id).first()
    if not folder:
        return jsonify({"error": "folder not found"}), 404

    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        folder.name = (payload.get("name") or "").strip() or folder.name
    if "parent_id" in payload:
        folder.parent_id = payload.get("parent_id")
    log_sync_event(
        user.id,
        "folder",
        folder.id,
        "update",
        serialize_folder_for_sync(folder),
    )
    db.session.commit()
    return jsonify(folder.as_dict())


@api_bp.route("/folders/<int:folder_id>", methods=["DELETE"])
@api_auth_required()
def folders_delete(folder_id: int):
    user = g.api_user
    folder = Folder.query.filter_by(id=folder_id, user_id=user.id).first()
    if not folder:
        return jsonify({"error": "folder not found"}), 404

    for bookmark in folder.bookmarks:
        bookmark.folder_id = None
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "update",
            serialize_bookmark_for_sync(bookmark),
        )
    for child in folder.children:
        child.parent_id = None
        log_sync_event(
            user.id,
            "folder",
            child.id,
            "update",
            serialize_folder_for_sync(child),
        )
    payload = serialize_folder_for_sync(folder)
    log_sync_event(user.id, "folder", folder.id, "delete", payload)
    db.session.delete(folder)
    db.session.commit()
    return jsonify({"status": "deleted"})


@api_bp.route("/tags", methods=["GET"])
@api_auth_required()
def tags_list():
    user = g.api_user
    tags = Tag.query.filter_by(user_id=user.id).order_by(Tag.name.asc()).all()
    return jsonify({"items": [{"id": tag.id, "name": tag.name} for tag in tags]})


@api_bp.route("/bookmarks", methods=["GET"])
@api_auth_required()
def bookmarks_list_api():
    user = g.api_user
    include_deleted = _to_bool(request.args.get("include_deleted"), default=False)
    folder_id = request.args.get("folder_id", type=int)
    query = Bookmark.query.filter_by(user_id=user.id)
    if include_deleted:
        pass
    else:
        query = query.filter(Bookmark.deleted_at.is_(None))
    if folder_id:
        query = query.filter_by(folder_id=folder_id)
    items = query.order_by(Bookmark.updated_at.desc()).all()
    return jsonify({"items": [item.as_dict() for item in items]})


@api_bp.route("/bookmarks", methods=["POST"])
@api_auth_required()
def bookmarks_create_api():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    bookmark = Bookmark(
        user_id=user.id,
        folder_id=payload.get("folder_id"),
        url=url,
        normalized_url=normalize_url(url),
        title=(payload.get("title") or "").strip() or None,
        notes=_normalize_notes_value(payload.get("notes")),
    )
    db.session.add(bookmark)
    db.session.flush()
    _assign_tags(user.id, bookmark, payload.get("tags") or [])
    if bookmark_is_internal(bookmark):
        set_internal_link_status(bookmark)
    elif _to_bool(payload.get("fetch_content"), default=True):
        _refresh_content(bookmark)
    log_sync_event(
        user.id,
        "bookmark",
        bookmark.id,
        "create",
        serialize_bookmark_for_sync(bookmark),
    )
    db.session.commit()
    return jsonify(bookmark.as_dict(include_content=True)), 201


@api_bp.route("/bookmarks/<int:bookmark_id>", methods=["GET"])
@api_auth_required()
def bookmarks_get_api(bookmark_id: int):
    user = g.api_user
    bookmark, error = _get_user_bookmark_or_404(user.id, bookmark_id)
    if error:
        return error
    return jsonify(bookmark.as_dict(include_content=True))


@api_bp.route("/bookmarks/<int:bookmark_id>", methods=["PATCH"])
@api_auth_required()
def bookmarks_update_api(bookmark_id: int):
    user = g.api_user
    bookmark, error = _get_user_bookmark_or_404(user.id, bookmark_id)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    old_url = bookmark.url
    for field in ["title", "folder_id"]:
        if field in payload:
            setattr(bookmark, field, payload.get(field))
    if "notes" in payload:
        bookmark.notes = _normalize_notes_value(payload.get("notes"))
    if "url" in payload:
        bookmark.url = (payload.get("url") or "").strip() or bookmark.url
        bookmark.normalized_url = normalize_url(bookmark.url)
    if "tags" in payload:
        _assign_tags(user.id, bookmark, payload.get("tags") or [])
    if bookmark_is_internal(bookmark):
        set_internal_link_status(bookmark)
    elif (
        _to_bool(payload.get("fetch_content"), default=False) or bookmark.url != old_url
    ):
        _refresh_content(bookmark, populate_notes=True)
    log_sync_event(
        user.id,
        "bookmark",
        bookmark.id,
        "update",
        serialize_bookmark_for_sync(bookmark),
    )
    db.session.commit()
    return jsonify(bookmark.as_dict(include_content=True))


@api_bp.route("/bookmarks/<int:bookmark_id>", methods=["DELETE"])
@api_auth_required()
def bookmarks_delete_api(bookmark_id: int):
    user = g.api_user
    bookmark, error = _get_user_bookmark_or_404(user.id, bookmark_id)
    if error:
        return error

    bookmark.deleted_at = utcnow()
    bookmark.deleted_by = user.id
    log_sync_event(
        user.id,
        "bookmark",
        bookmark.id,
        "delete",
        serialize_bookmark_for_sync(bookmark),
    )
    db.session.commit()
    return jsonify({"status": "recycled", "bookmark": bookmark.as_dict()})


@api_bp.route("/recycle", methods=["GET"])
@api_auth_required()
def recycle_list_api():
    user = g.api_user
    items = (
        Bookmark.query.filter_by(user_id=user.id)
        .filter(Bookmark.deleted_at.is_not(None))
        .order_by(Bookmark.deleted_at.desc())
        .all()
    )
    return jsonify({"items": [item.as_dict() for item in items]})


@api_bp.route("/recycle/<int:bookmark_id>/restore", methods=["POST"])
@api_auth_required()
def recycle_restore_api(bookmark_id: int):
    user = g.api_user
    bookmark, error = _get_user_bookmark_or_404(user.id, bookmark_id)
    if error:
        return error
    bookmark.deleted_at = None
    bookmark.deleted_by = None
    log_sync_event(
        user.id,
        "bookmark",
        bookmark.id,
        "restore",
        serialize_bookmark_for_sync(bookmark),
    )
    db.session.commit()
    return jsonify({"status": "restored", "bookmark": bookmark.as_dict()})


@api_bp.route("/recycle/<int:bookmark_id>/purge", methods=["DELETE"])
@api_auth_required()
def recycle_purge_api(bookmark_id: int):
    user = g.api_user
    bookmark, error = _get_user_bookmark_or_404(user.id, bookmark_id)
    if error:
        return error
    if bookmark.content:
        db.session.delete(bookmark.content)
    bookmark.tags.clear()
    log_sync_event(user.id, "bookmark", bookmark.id, "purge", {"id": bookmark.id})
    db.session.delete(bookmark)
    db.session.commit()
    return jsonify({"status": "purged"})


@api_bp.route("/import/browser-html", methods=["POST"])
@api_auth_required()
def import_browser_html_api():
    user = g.api_user
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "file field is required"}), 400

    html = upload.read().decode("utf-8", errors="ignore")
    entries = parse_bookmark_html(html)
    job = ImportJob(user_id=user.id, status="running", progress=0)
    db.session.add(job)
    db.session.commit()

    created = 0
    skipped = 0
    total = len(entries)

    folder_cache = {}

    def ensure_folder(path_parts: list[str]):
        parent_id = None
        for part in path_parts:
            key = (parent_id, part)
            if key in folder_cache:
                parent_id = folder_cache[key]
                continue
            folder = Folder.query.filter_by(
                user_id=user.id, name=part, parent_id=parent_id
            ).first()
            if not folder:
                folder = Folder(user_id=user.id, name=part, parent_id=parent_id)
                db.session.add(folder)
                db.session.flush()
                log_sync_event(
                    user.id,
                    "folder",
                    folder.id,
                    "create",
                    serialize_folder_for_sync(folder),
                )
            parent_id = folder.id
            folder_cache[key] = parent_id
        return parent_id

    try:
        for idx, entry in enumerate(entries, start=1):
            folder_id = ensure_folder(entry.folder_path)
            normalized = normalize_url(entry.url)
            existing = Bookmark.query.filter_by(
                user_id=user.id, normalized_url=normalized
            ).first()
            if existing and existing.deleted_at is None:
                skipped += 1
            elif existing:
                existing.deleted_at = None
                existing.deleted_by = None
                existing.title = existing.title or entry.title
                existing.folder_id = existing.folder_id or folder_id
                _refresh_content(existing, populate_notes=True, populate_title=False)
                log_sync_event(
                    user.id,
                    "bookmark",
                    existing.id,
                    "restore",
                    serialize_bookmark_for_sync(existing),
                )
                created += 1
            else:
                bookmark = Bookmark(
                    user_id=user.id,
                    folder_id=folder_id,
                    url=entry.url,
                    normalized_url=normalized,
                    title=entry.title or None,
                )
                db.session.add(bookmark)
                db.session.flush()
                _refresh_content(bookmark, populate_notes=True, populate_title=False)
                log_sync_event(
                    user.id,
                    "bookmark",
                    bookmark.id,
                    "create",
                    serialize_bookmark_for_sync(bookmark),
                )
                created += 1

            job.progress = int((idx / total) * 100) if total else 100

        job.status = "done"
        job.total_created = created
        job.total_skipped = skipped
        db.session.commit()
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)
        db.session.commit()
        return jsonify({"error": "import failed", "job": job.as_dict()}), 500

    return jsonify({"status": "done", "job": job.as_dict()})


@api_bp.route("/import/jobs/<int:job_id>", methods=["GET"])
@api_auth_required()
def import_job_status(job_id: int):
    user = g.api_user
    job = ImportJob.query.filter_by(id=job_id, user_id=user.id).first()
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job.as_dict())


@api_bp.route("/search", methods=["GET"])
@api_auth_required()
def search_api():
    user = g.api_user
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"items": []})

    source = (
        Bookmark.query.filter_by(user_id=user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.updated_at.desc())
        .all()
    )
    ranked = search_bookmarks(
        source, query, limit=request.args.get("limit", type=int) or 50
    )
    return jsonify(
        {
            "items": [
                {
                    **item["bookmark"].as_dict(),
                    "score": item["score"],
                    "match_reasons": item["reasons"],
                }
                for item in ranked
            ]
        }
    )


@api_bp.route("/bookmarks/<int:bookmark_id>/check", methods=["POST"])
@api_auth_required()
def check_single_bookmark(bookmark_id: int):
    user = g.api_user
    bookmark, error = _get_user_bookmark_or_404(user.id, bookmark_id)
    if error:
        return error
    if bookmark_is_internal(bookmark):
        set_internal_link_status(bookmark)
        db.session.commit()
        return jsonify(
            {
                "status": "checked",
                "bookmark": bookmark.as_dict(),
                "result": bookmark.link_status,
            }
        )

    result = check_link(
        bookmark.url, timeout=current_app.config["CONTENT_FETCH_TIMEOUT"]
    )
    bookmark.link_status = result.result_type
    bookmark.last_checked_at = utcnow()
    check = LinkCheck(
        bookmark_id=bookmark.id,
        status_code=result.status_code,
        final_url=result.final_url,
        result_type=result.result_type,
        latency_ms=result.latency_ms,
        error=result.error,
    )
    db.session.add(check)
    db.session.commit()
    return jsonify(
        {
            "status": "checked",
            "bookmark": bookmark.as_dict(),
            "result": check.result_type,
        }
    )


@api_bp.route("/checks/run", methods=["POST"])
@api_auth_required()
def check_bulk_bookmarks():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    limit = payload.get("limit")
    targets_query = (
        Bookmark.query.filter_by(user_id=user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.last_checked_at.is_not(None), Bookmark.last_checked_at.asc())
    )
    if limit:
        targets_query = targets_query.limit(int(limit))
    targets = targets_query.all()
    for bookmark in targets:
        if bookmark_is_internal(bookmark):
            set_internal_link_status(bookmark)
            continue

        result = check_link(
            bookmark.url, timeout=current_app.config["CONTENT_FETCH_TIMEOUT"]
        )
        bookmark.link_status = result.result_type
        bookmark.last_checked_at = utcnow()
        check = LinkCheck(
            bookmark_id=bookmark.id,
            status_code=result.status_code,
            final_url=result.final_url,
            result_type=result.result_type,
            latency_ms=result.latency_ms,
            error=result.error,
        )
        db.session.add(check)
    db.session.commit()
    return jsonify({"status": "done", "checked": len(targets)})


@api_bp.route("/checks/dead", methods=["GET"])
@api_auth_required()
def dead_links_api():
    user = g.api_user
    items = (
        Bookmark.query.filter_by(user_id=user.id)
        .filter(Bookmark.deleted_at.is_(None))
        .filter(Bookmark.link_status.in_(list(PROBLEMATIC_RESULTS)))
        .order_by(Bookmark.last_checked_at.desc())
        .all()
    )
    return jsonify({"items": [item.as_dict() for item in items]})


@api_bp.route("/sync/register-client", methods=["POST"])
@api_auth_required(token_only=True)
def sync_register_client():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    client_id = (payload.get("client_id") or "").strip()
    platform = (payload.get("platform") or "").strip() or None
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    client = ensure_sync_client(user.id, client_id, platform=platform)
    return jsonify(
        {
            "status": "registered",
            "client": {
                "client_id": client.client_id,
                "platform": client.platform,
                "last_cursor": client.last_cursor,
            },
        }
    )


@api_bp.route("/sync/first/preflight", methods=["POST"])
@api_auth_required(token_only=True)
def sync_first_preflight():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    client_id = (payload.get("client_id") or "").strip()
    mode = _normalize_sync_mode(payload.get("mode"))
    local_bookmarks = _parse_local_bookmarks(payload)

    if not client_id:
        return jsonify({"error": "client_id is required"}), 400
    if not mode:
        return jsonify({"error": "invalid mode"}), 400

    ensure_sync_client(user.id, client_id, platform=payload.get("platform"))

    preflight = _build_sync_preflight_payload(
        mode=mode,
        local_bookmarks=local_bookmarks,
        user_id=user.id,
    )

    token = create_confirmation_token(
        secret_key=current_app.config["SECRET_KEY"],
        user_id=user.id,
        client_id=client_id,
        mode=mode,
        local_count=preflight["local_bookmark_count"],
        server_count=preflight["server_bookmark_count"],
        ttl_seconds=current_app.config["SYNC_CONFIRM_TTL_SECONDS"],
    )

    sample_removed = []
    for item in local_bookmarks[:10]:
        sample_removed.append(
            {
                "title": item.get("title") or "(untitled)",
                "url": item.get("url") or "",
            }
        )

    return jsonify(
        {
            **preflight,
            "estimated_local_deletions": preflight["impact"]["local_deletions"],
            "sample_local_removals": sample_removed,
            "confirmation_token": token,
            "confirmation_ttl_seconds": current_app.config["SYNC_CONFIRM_TTL_SECONDS"],
        }
    )


@api_bp.route("/sync/first/apply", methods=["POST"])
@api_auth_required(token_only=True)
def sync_first_apply():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    client_id = (payload.get("client_id") or "").strip()
    mode = _normalize_sync_mode(payload.get("mode"))
    confirmation_token = payload.get("confirmation_token")
    typed_phrase = (payload.get("typed_phrase") or "").strip()
    confirm_checked = _to_bool(payload.get("confirm_checked"), default=False)
    local_bookmarks = _parse_local_bookmarks(payload)
    local_folders = _parse_local_folders(payload)

    if not client_id or not confirmation_token or not mode:
        return (
            jsonify(
                {
                    "error": "client_id, mode, and confirmation_token are required",
                }
            ),
            400,
        )

    required_phrase = SYNC_CONFIRM_PHRASES.get(mode, CONFIRM_PHRASE)
    if typed_phrase != required_phrase or not confirm_checked:
        return jsonify({"error": "destructive confirmation failed"}), 400

    token_payload = verify_confirmation_token(
        secret_key=current_app.config["SECRET_KEY"],
        token=confirmation_token,
        max_age=current_app.config["SYNC_CONFIRM_TTL_SECONDS"],
        expected_user_id=user.id,
        expected_client_id=client_id,
        expected_mode=mode,
    )
    if not token_payload:
        return jsonify({"error": "invalid or expired confirmation token"}), 400

    client = ensure_sync_client(user.id, client_id)

    response_status = "snapshot"
    reason = None
    mapping = {
        "local_folder_id_to_server_id": {},
        "local_bookmark_id_to_server_id": {},
    }
    counts: dict = {}
    background_enrichment_ids: list[int] = []

    server_count_now = len(_active_server_bookmarks(user.id))
    local_count_now = len(local_bookmarks)

    if mode == SYNC_MODE_REPLACE_LOCAL and server_count_now == 0:
        return jsonify(
            {
                "status": "no_op",
                "mode": mode,
                "reason": "server_empty",
                "local_bookmark_count": local_count_now,
                "server_bookmark_count": 0,
            }
        )

    if mode == SYNC_MODE_REPLACE_SERVER and local_count_now == 0:
        return jsonify(
            {
                "status": "no_op",
                "mode": mode,
                "reason": "local_empty",
                "local_bookmark_count": 0,
                "server_bookmark_count": server_count_now,
            }
        )

    if mode == SYNC_MODE_REPLACE_SERVER:
        (
            folder_map,
            bookmark_map,
            counts,
            background_enrichment_ids,
        ) = _replace_server_with_local_snapshot(
            user=user,
            local_folders=local_folders,
            local_bookmarks=local_bookmarks,
        )
        mapping["local_folder_id_to_server_id"] = folder_map
        mapping["local_bookmark_id_to_server_id"] = bookmark_map
    elif mode == SYNC_MODE_TWO_WAY:
        folder_map, bookmark_map, counts = _two_way_merge_snapshot(
            user=user,
            local_folders=local_folders,
            local_bookmarks=local_bookmarks,
        )
        mapping["local_folder_id_to_server_id"] = folder_map
        mapping["local_bookmark_id_to_server_id"] = bookmark_map
        response_status = "merged"
    else:
        reason = "replace_local_with_server"

    snapshot = _server_snapshot(user.id)

    db.session.flush()
    latest_cursor = (
        db.session.query(db.func.max(SyncEvent.id)).filter_by(user_id=user.id).scalar()
        or 0
    )
    client.last_cursor = latest_cursor
    db.session.commit()

    if mode == SYNC_MODE_REPLACE_SERVER and background_enrichment_ids:
        try:
            start_sync_first_replace_server_enrichment(
                app=current_app._get_current_object(),
                user_id=user.id,
                bookmark_ids=background_enrichment_ids,
            )
        except Exception as exc:
            current_app.logger.warning(
                "Failed to start replace-server enrichment worker for user %s: %s",
                user.id,
                exc,
            )

    return jsonify(
        {
            "status": response_status,
            "mode": mode,
            "reason": reason,
            "bookmarks": snapshot["bookmarks"],
            "folders": snapshot["folders"],
            "cursor": latest_cursor,
            "mapping": mapping,
            "counts": counts,
            "token_local_count": token_payload.get("local_count"),
            "token_server_count": token_payload.get("server_count"),
            "audit": {
                "confirmed_at": utcnow().isoformat(),
                "client_id": client_id,
                "user_id": user.id,
            },
        }
    )


@api_bp.route("/sync/push", methods=["POST"])
@api_auth_required(token_only=True)
def sync_push():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    operations = payload.get("operations") or []
    client_id = (payload.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400

    client = ensure_sync_client(user.id, client_id)
    results = []
    for operation in operations:
        results.append(apply_push_operation(user, operation))

    db.session.commit()
    latest_cursor = (
        db.session.query(db.func.max(SyncEvent.id)).filter_by(user_id=user.id).scalar()
        or 0
    )
    client.last_cursor = max(client.last_cursor, latest_cursor)
    db.session.commit()
    return jsonify({"status": "ok", "results": results, "cursor": latest_cursor})


@api_bp.route("/sync/pull", methods=["GET"])
@api_auth_required(token_only=True)
def sync_pull():
    user = g.api_user
    since = request.args.get("since", default=0, type=int)
    limit = request.args.get("limit", default=200, type=int)
    events = (
        SyncEvent.query.filter_by(user_id=user.id)
        .filter(SyncEvent.id > since)
        .order_by(SyncEvent.id.asc())
        .limit(limit)
        .all()
    )
    latest_cursor = since
    if events:
        latest_cursor = events[-1].id
    return jsonify(
        {
            "events": [
                {
                    "cursor": event.id,
                    "entity_type": event.entity_type,
                    "entity_id": event.entity_id,
                    "action": event.action,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
            "cursor": latest_cursor,
            "has_more": len(events) == limit,
        }
    )


@api_bp.route("/sync/ack", methods=["POST"])
@api_auth_required(token_only=True)
def sync_ack():
    user = g.api_user
    payload = request.get_json(silent=True) or {}
    client_id = (payload.get("client_id") or "").strip()
    cursor = payload.get("cursor")
    if not client_id or cursor is None:
        return jsonify({"error": "client_id and cursor are required"}), 400
    client = ensure_sync_client(user.id, client_id)
    client.last_cursor = int(cursor)
    db.session.commit()
    return jsonify({"status": "acknowledged", "cursor": client.last_cursor})
