from __future__ import annotations

from sqlalchemy import inspect, text

from app.extensions import db
from app.models import Bookmark, Tag
from app.services.common import parse_tags


def migrate_keywords_into_tags_and_drop_column() -> bool:
    engine = db.engine
    if engine.dialect.name != "sqlite":
        return False

    inspector = inspect(engine)
    if not inspector.has_table("bookmarks"):
        return False

    columns = {column["name"] for column in inspector.get_columns("bookmarks")}
    if "keywords_text" not in columns:
        return False

    rows = db.session.execute(
        text(
            """
            SELECT id, user_id, keywords_text
            FROM bookmarks
            WHERE keywords_text IS NOT NULL
              AND TRIM(keywords_text) != ''
            """
        )
    ).mappings()

    for row in rows:
        bookmark = Bookmark.query.filter_by(
            id=row["id"], user_id=row["user_id"]
        ).first()
        if not bookmark:
            continue

        existing_tag_names = {tag.name for tag in bookmark.tags}
        for tag_name in parse_tags(row["keywords_text"] or ""):
            if tag_name in existing_tag_names:
                continue

            tag = Tag.query.filter_by(user_id=row["user_id"], name=tag_name).first()
            if not tag:
                tag = Tag(user_id=row["user_id"], name=tag_name)
                db.session.add(tag)
            bookmark.tags.append(tag)
            existing_tag_names.add(tag_name)

    db.session.commit()
    db.session.execute(text("ALTER TABLE bookmarks DROP COLUMN keywords_text"))
    db.session.commit()
    return True
