from __future__ import annotations

from datetime import datetime, timezone

from dateutil import parser as dt_parser
from itsdangerous import BadData, URLSafeTimedSerializer

from app.extensions import db
from app.models import Bookmark, Folder, SyncClient, SyncEvent, Tag, utcnow
from app.services.common import normalize_url, parse_tags


SYNC_MODE_REPLACE_LOCAL = "replace_local_with_server"
SYNC_MODE_REPLACE_SERVER = "replace_server_with_local"
SYNC_MODE_TWO_WAY = "two_way_merge"

SYNC_FIRST_MODES = {
    SYNC_MODE_REPLACE_LOCAL,
    SYNC_MODE_REPLACE_SERVER,
    SYNC_MODE_TWO_WAY,
}

SYNC_CONFIRM_PHRASES = {
    SYNC_MODE_REPLACE_LOCAL: "DELETE ALL LOCAL BOOKMARKS",
    SYNC_MODE_REPLACE_SERVER: "DELETE ALL SERVER BOOKMARKS",
    SYNC_MODE_TWO_WAY: "SYNC BOOKMARKS BOTH WAYS",
}

# Backward-compatible alias for older call sites/tests.
CONFIRM_PHRASE = SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_LOCAL]


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret_key, salt="sync-first-overwrite")


def create_confirmation_token(
    secret_key: str,
    user_id: int,
    client_id: str,
    mode: str,
    local_count: int,
    server_count: int,
    ttl_seconds: int,
) -> str:
    serializer = _serializer(secret_key)
    payload = {
        "user_id": user_id,
        "client_id": client_id,
        "mode": mode,
        "local_count": local_count,
        "server_count": server_count,
        "issued_at": int(datetime.now(timezone.utc).timestamp()),
        "ttl": ttl_seconds,
    }
    return serializer.dumps(payload)


def verify_confirmation_token(
    secret_key: str,
    token: str,
    max_age: int,
    expected_user_id: int,
    expected_client_id: str,
    expected_mode: str | None = None,
) -> dict | None:
    serializer = _serializer(secret_key)
    try:
        payload = serializer.loads(token, max_age=max_age)
    except BadData:
        return None

    if (
        payload.get("user_id") != expected_user_id
        or payload.get("client_id") != expected_client_id
    ):
        return None
    if expected_mode and payload.get("mode") != expected_mode:
        return None
    return payload


def serialize_bookmark_for_sync(bookmark: Bookmark) -> dict:
    return {
        "id": bookmark.id,
        "url": bookmark.url,
        "title": bookmark.title,
        "notes": bookmark.notes,
        "folder_id": bookmark.folder_id,
        "tags": [tag.name for tag in bookmark.tags],
        "updated_at": bookmark.updated_at.isoformat(),
        "deleted_at": bookmark.deleted_at.isoformat() if bookmark.deleted_at else None,
    }


def serialize_folder_for_sync(folder: Folder) -> dict:
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "updated_at": folder.updated_at.isoformat(),
    }


def ensure_sync_client(
    user_id: int, client_id: str, platform: str | None = None
) -> SyncClient:
    client = SyncClient.query.filter_by(user_id=user_id, client_id=client_id).first()
    if not client:
        client = SyncClient(user_id=user_id, client_id=client_id, platform=platform)
        db.session.add(client)
        db.session.commit()
    elif platform and client.platform != platform:
        client.platform = platform
        db.session.commit()
    return client


def log_sync_event(
    user_id: int, entity_type: str, entity_id: int | None, action: str, payload: dict
):
    event = SyncEvent(
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        payload=payload,
    )
    db.session.add(event)


def _parse_client_time(value: str | None):
    if not value:
        return None
    try:
        return dt_parser.isoparse(value)
    except Exception:
        return None


def _normalize_notes_value(value) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.lower() == "none":
        return None
    return text


def _server_time_is_newer_or_equal(server_time, client_time) -> bool:
    if server_time is None or client_time is None:
        return False
    server_value = server_time
    if server_value.tzinfo is None:
        server_value = server_value.replace(tzinfo=timezone.utc)
    return server_value >= client_time


def _apply_tags(user_id: int, bookmark: Bookmark, tag_names: list[str]):
    clean = parse_tags(",".join(tag_names))
    bookmark.tags.clear()
    for name in clean:
        tag = Tag.query.filter_by(user_id=user_id, name=name).first()
        if not tag:
            tag = Tag(user_id=user_id, name=name)
            db.session.add(tag)
        bookmark.tags.append(tag)


def _delete_folder_tree_for_sync(user, folder: Folder) -> None:
    for child in list(folder.children):
        _delete_folder_tree_for_sync(user, child)

    for bookmark in list(folder.bookmarks):
        bookmark.folder_id = None
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "update",
            serialize_bookmark_for_sync(bookmark),
        )

    payload = serialize_folder_for_sync(folder)
    log_sync_event(user.id, "folder", folder.id, "delete", payload)
    db.session.delete(folder)


def _apply_folder_push_operation(user, op: dict) -> dict:
    action = (op.get("op") or "").lower()
    folder_data = op.get("folder") or {}
    folder_id = folder_data.get("id") or op.get("id")
    client_updated_at = _parse_client_time(
        folder_data.get("updated_at") or op.get("updated_at")
    )

    folder = None
    if folder_id is not None:
        folder = Folder.query.filter_by(id=folder_id, user_id=user.id).first()

    if action == "create":
        name = (folder_data.get("name") or "").strip() or "Untitled Folder"
        parent_id = folder_data.get("parent_id")

        if parent_id is not None:
            parent = Folder.query.filter_by(id=parent_id, user_id=user.id).first()
            if not parent:
                parent_id = None

        existing = Folder.query.filter_by(
            user_id=user.id,
            name=name,
            parent_id=parent_id,
        ).first()
        if existing:
            return {"status": "exists", "folder_id": existing.id}

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
        return {"status": "created", "folder_id": folder.id}

    if not folder:
        return {"status": "skipped", "reason": "folder_not_found"}

    if _server_time_is_newer_or_equal(folder.updated_at, client_updated_at):
        return {"status": "skipped", "reason": "server_newer_or_equal"}

    if action in {"update", "move"}:
        changed = False
        if "name" in folder_data:
            name = (folder_data.get("name") or "").strip() or "Untitled Folder"
            if folder.name != name:
                folder.name = name
                changed = True

        if "parent_id" in folder_data:
            new_parent_id = folder_data.get("parent_id")
            if new_parent_id == folder.id:
                new_parent_id = None
            if new_parent_id is not None:
                parent = Folder.query.filter_by(
                    id=new_parent_id, user_id=user.id
                ).first()
                if not parent:
                    new_parent_id = None
            if folder.parent_id != new_parent_id:
                folder.parent_id = new_parent_id
                changed = True

        if changed:
            log_sync_event(
                user.id,
                "folder",
                folder.id,
                "update",
                serialize_folder_for_sync(folder),
            )
            return {"status": "updated", "folder_id": folder.id}
        return {"status": "skipped", "reason": "no_changes"}

    if action == "delete":
        _delete_folder_tree_for_sync(user, folder)
        return {"status": "deleted", "folder_id": folder_id}

    return {"status": "skipped", "reason": "unsupported_operation"}


def _apply_bookmark_push_operation(user, op: dict) -> dict:
    action = (op.get("op") or "").lower()
    bookmark_data = op.get("bookmark") or {}
    bookmark_id = bookmark_data.get("id") or op.get("id")
    client_updated_at = _parse_client_time(
        bookmark_data.get("updated_at") or op.get("updated_at")
    )

    bookmark = None
    if bookmark_id:
        bookmark = Bookmark.query.filter_by(id=bookmark_id, user_id=user.id).first()

    if action == "create":
        raw_url = (bookmark_data.get("url") or "").strip()
        normalized_url = normalize_url(raw_url)
        if not normalized_url:
            return {"status": "skipped", "reason": "invalid_url"}

        existing_active = (
            Bookmark.query.filter_by(user_id=user.id, normalized_url=normalized_url)
            .filter(Bookmark.deleted_at.is_(None))
            .first()
        )
        if existing_active:
            return {"status": "exists", "bookmark_id": existing_active.id}

        existing_deleted = (
            Bookmark.query.filter_by(user_id=user.id, normalized_url=normalized_url)
            .filter(Bookmark.deleted_at.is_not(None))
            .order_by(Bookmark.updated_at.desc())
            .first()
        )
        if existing_deleted:
            existing_deleted.deleted_at = None
            existing_deleted.deleted_by = None
            existing_deleted.url = raw_url
            existing_deleted.normalized_url = normalized_url
            if bookmark_data.get("title"):
                existing_deleted.title = bookmark_data.get("title")
            incoming_notes = _normalize_notes_value(bookmark_data.get("notes"))
            if incoming_notes:
                existing_deleted.notes = incoming_notes
            if "folder_id" in bookmark_data:
                existing_deleted.folder_id = bookmark_data.get("folder_id")
            if "tags" in bookmark_data:
                _apply_tags(user.id, existing_deleted, bookmark_data.get("tags") or [])
            log_sync_event(
                user.id,
                "bookmark",
                existing_deleted.id,
                "restore",
                serialize_bookmark_for_sync(existing_deleted),
            )
            return {"status": "restored", "bookmark_id": existing_deleted.id}

        bookmark = Bookmark(
            user_id=user.id,
            url=raw_url,
            normalized_url=normalized_url,
            title=bookmark_data.get("title"),
            notes=_normalize_notes_value(bookmark_data.get("notes")),
            folder_id=bookmark_data.get("folder_id"),
        )
        db.session.add(bookmark)
        db.session.flush()
        _apply_tags(user.id, bookmark, bookmark_data.get("tags") or [])
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "create",
            serialize_bookmark_for_sync(bookmark),
        )
        return {"status": "created", "bookmark_id": bookmark.id}

    if not bookmark:
        return {"status": "skipped", "reason": "bookmark_not_found"}

    if _server_time_is_newer_or_equal(bookmark.updated_at, client_updated_at):
        return {"status": "skipped", "reason": "server_newer_or_equal"}

    if action == "update":
        if "url" in bookmark_data:
            bookmark.url = bookmark_data.get("url") or bookmark.url
            bookmark.normalized_url = normalize_url(bookmark.url)
        for field in ["title", "folder_id"]:
            if field in bookmark_data:
                setattr(bookmark, field, bookmark_data.get(field))
        if "notes" in bookmark_data:
            incoming_notes = _normalize_notes_value(bookmark_data.get("notes"))
            if incoming_notes:
                bookmark.notes = incoming_notes
        if "tags" in bookmark_data:
            _apply_tags(user.id, bookmark, bookmark_data.get("tags") or [])
        bookmark.deleted_at = None
        bookmark.deleted_by = None
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "update",
            serialize_bookmark_for_sync(bookmark),
        )
        return {"status": "updated", "bookmark_id": bookmark.id}

    if action == "delete":
        bookmark.deleted_at = utcnow()
        bookmark.deleted_by = user.id
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "delete",
            serialize_bookmark_for_sync(bookmark),
        )
        return {"status": "deleted", "bookmark_id": bookmark.id}

    if action == "restore":
        bookmark.deleted_at = None
        bookmark.deleted_by = None
        log_sync_event(
            user.id,
            "bookmark",
            bookmark.id,
            "restore",
            serialize_bookmark_for_sync(bookmark),
        )
        return {"status": "restored", "bookmark_id": bookmark.id}

    return {"status": "skipped", "reason": "unsupported_operation"}


def apply_push_operation(user, op: dict) -> dict:
    entity_type = (op.get("entity_type") or "bookmark").strip().lower()
    if entity_type == "folder":
        return _apply_folder_push_operation(user, op)
    return _apply_bookmark_push_operation(user, op)
