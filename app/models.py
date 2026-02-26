import hashlib
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


bookmark_tags = db.Table(
    "bookmark_tags",
    db.Column(
        "bookmark_id", db.Integer, db.ForeignKey("bookmarks.id"), primary_key=True
    ),
    db.Column("tag_id", db.Integer, db.ForeignKey("tags.id"), primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    bookmarks = db.relationship(
        "Bookmark", backref="user", lazy=True, foreign_keys="Bookmark.user_id"
    )
    folders = db.relationship("Folder", backref="user", lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))


class Folder(db.Model):
    __tablename__ = "folders"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    name = db.Column(db.String(255), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("folders.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    children = db.relationship("Folder", backref=db.backref("parent", remote_side=[id]))

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "name", "parent_id", name="uq_folder_user_name_parent"
        ),
    )

    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class Bookmark(db.Model):
    __tablename__ = "bookmarks"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    folder_id = db.Column(
        db.Integer, db.ForeignKey("folders.id"), nullable=True, index=True
    )
    deleted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    url = db.Column(db.Text, nullable=False)
    normalized_url = db.Column(db.Text, nullable=False, index=True)
    title = db.Column(db.String(512), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    link_status = db.Column(db.String(64), nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    folder = db.relationship("Folder", backref="bookmarks")
    deleted_by_user = db.relationship("User", foreign_keys=[deleted_by])
    content = db.relationship("BookmarkContent", backref="bookmark", uselist=False)
    tags = db.relationship("Tag", secondary=bookmark_tags, backref="bookmarks")

    __table_args__ = (
        db.Index("ix_bookmark_user_deleted", "user_id", "deleted_at"),
        db.Index("ix_bookmark_user_updated", "user_id", "updated_at"),
    )

    def as_dict(self, include_content=False):
        notes = (self.notes or "").strip()
        if notes.lower() == "none":
            notes = ""
        payload = {
            "id": self.id,
            "url": self.url,
            "normalized_url": self.normalized_url,
            "title": self.title,
            "notes": notes,
            "folder_id": self.folder_id,
            "folder_name": self.folder.name if self.folder else None,
            "tags": [tag.name for tag in self.tags],
            "link_status": self.link_status,
            "last_checked_at": self.last_checked_at.isoformat()
            if self.last_checked_at
            else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }
        if include_content:
            payload["content_text"] = (
                self.content.extracted_text
                if self.content and self.content.extracted_text
                else ""
            )
        return payload


class BookmarkContent(db.Model):
    __tablename__ = "bookmark_content"

    id = db.Column(db.Integer, primary_key=True)
    bookmark_id = db.Column(
        db.Integer,
        db.ForeignKey("bookmarks.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    extracted_text = db.Column(db.Text, nullable=True)
    extracted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    content_hash = db.Column(db.String(128), nullable=True)
    fetch_status = db.Column(db.String(64), nullable=True)
    fetch_error = db.Column(db.Text, nullable=True)


class Tag(db.Model):
    __tablename__ = "tags"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    name = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "name", name="uq_tag_user_name"),)


class LinkCheck(db.Model):
    __tablename__ = "link_checks"

    id = db.Column(db.Integer, primary_key=True)
    bookmark_id = db.Column(
        db.Integer, db.ForeignKey("bookmarks.id"), nullable=False, index=True
    )
    checked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    status_code = db.Column(db.Integer, nullable=True)
    final_url = db.Column(db.Text, nullable=True)
    result_type = db.Column(db.String(64), nullable=False)
    latency_ms = db.Column(db.Integer, nullable=True)
    error = db.Column(db.Text, nullable=True)


class ImportJob(db.Model):
    __tablename__ = "import_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    status = db.Column(db.String(32), nullable=False, default="pending")
    progress = db.Column(db.Integer, nullable=False, default=0)
    total_created = db.Column(db.Integer, nullable=False, default=0)
    total_skipped = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def as_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "total_created": self.total_created,
            "total_skipped": self.total_skipped,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class DeadLinkJob(db.Model):
    __tablename__ = "dead_link_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    status = db.Column(db.String(32), nullable=False, default="pending")
    progress = db.Column(db.Integer, nullable=False, default=0)
    total_targets = db.Column(db.Integer, nullable=False, default=0)
    total_checked = db.Column(db.Integer, nullable=False, default=0)
    total_alive = db.Column(db.Integer, nullable=False, default=0)
    total_problematic = db.Column(db.Integer, nullable=False, default=0)
    total_errors = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def as_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "total_targets": self.total_targets,
            "total_checked": self.total_checked,
            "total_alive": self.total_alive,
            "total_problematic": self.total_problematic,
            "total_errors": self.total_errors,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ApiToken(db.Model):
    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    token_hash = db.Column(db.String(128), nullable=False, unique=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    user = db.relationship("User", backref="api_tokens")

    @staticmethod
    def issue_token(prefix="ll"):
        token = f"{prefix}_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return token, token_hash


class SyncClient(db.Model):
    __tablename__ = "sync_clients"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(db.String(120), nullable=False)
    platform = db.Column(db.String(64), nullable=True)
    last_cursor = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_sync_client"),
    )


class SyncEvent(db.Model):
    __tablename__ = "sync_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    entity_type = db.Column(db.String(64), nullable=False)
    entity_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(32), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (db.Index("ix_sync_user_cursor", "user_id", "id"),)
