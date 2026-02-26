import io
from datetime import datetime, timezone

from sqlalchemy import text

from app.extensions import db
from app.models import (
    Bookmark,
    BookmarkContent,
    DeadLinkJob,
    Folder,
    ImportJob,
    LinkCheck,
    Tag,
    User,
    utcnow,
)
from app.services.content import ExtractedContent, LinkCheckResult, classify_status
from app.services.sync import (
    SYNC_CONFIRM_PHRASES,
    SYNC_MODE_REPLACE_LOCAL,
    SYNC_MODE_REPLACE_SERVER,
    SYNC_MODE_TWO_WAY,
)


def _create_user(username: str, password: str, is_admin=False):
    user = User(username=username, is_admin=is_admin, is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _token(client, username: str, password: str):
    response = client.post(
        "/api/v1/auth/token",
        json={"username": username, "password": password, "token_name": "pytest"},
    )
    assert response.status_code == 200
    return response.get_json()["token"]


def _web_login(client, username: str, password: str):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_bootstrap_admin(client):
    response = client.get("/bootstrap")
    assert response.status_code == 200

    response = client.post(
        "/bootstrap",
        data={"username": "admin", "password": "secret", "confirm_password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_only_user_creation(client, app):
    with app.app_context():
        _create_user("admin", "secret", is_admin=True)
        _create_user("member", "secret", is_admin=False)

    member_token = _token(client, "member", "secret")
    response = client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {member_token}"},
        json={"username": "blocked", "password": "test"},
    )
    assert response.status_code == 403

    admin_token = _token(client, "admin", "secret")
    response = client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"username": "newuser", "password": "secret2", "is_admin": False},
    )
    assert response.status_code == 201


def test_recycle_restore_purge_flow(client, app):
    with app.app_context():
        _create_user("u1", "secret")
    token = _token(client, "u1", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/v1/bookmarks",
        headers=auth,
        json={"url": "https://example.com", "title": "Example", "fetch_content": False},
    )
    assert response.status_code == 201
    bookmark_id = response.get_json()["id"]

    response = client.delete(f"/api/v1/bookmarks/{bookmark_id}", headers=auth)
    assert response.status_code == 200

    response = client.get("/api/v1/recycle", headers=auth)
    assert response.status_code == 200
    assert len(response.get_json()["items"]) == 1

    response = client.post(f"/api/v1/recycle/{bookmark_id}/restore", headers=auth)
    assert response.status_code == 200

    response = client.delete(f"/api/v1/bookmarks/{bookmark_id}", headers=auth)
    assert response.status_code == 200

    response = client.delete(f"/api/v1/recycle/{bookmark_id}/purge", headers=auth)
    assert response.status_code == 200


def test_api_bookmark_notes_none_serializes_as_empty_string(client, app):
    with app.app_context():
        user = _create_user("notes-empty-api", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://notes-empty.example",
            normalized_url="https://notes-empty.example",
            title="Notes Empty",
            notes=None,
        )
        db.session.add(bookmark)
        db.session.commit()
        bookmark_id = bookmark.id

    token = _token(client, "notes-empty-api", "secret")
    response = client.get(
        f"/api/v1/bookmarks/{bookmark_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["notes"] == ""


def test_sync_first_apply_requires_confirmation(client, app):
    with app.app_context():
        user = _create_user("syncuser", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://example.org",
            normalized_url="https://example.org/",
            title="Example",
        )
        db.session.add(bookmark)
        db.session.commit()

    token = _token(client, "syncuser", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    preflight = client.post(
        "/api/v1/sync/first/preflight",
        headers=auth,
        json={
            "client_id": "firefox-1",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "local_bookmarks": [{"title": "Local", "url": "https://local.test"}],
        },
    )
    assert preflight.status_code == 200
    preflight_payload = preflight.get_json()
    confirmation_token = preflight_payload["confirmation_token"]
    assert (
        preflight_payload["required_phrase"]
        == SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_LOCAL]
    )

    bad_apply = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-1",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "confirmation_token": confirmation_token,
            "typed_phrase": "WRONG",
            "confirm_checked": True,
        },
    )
    assert bad_apply.status_code == 400

    good_apply = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-1",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "confirmation_token": confirmation_token,
            "typed_phrase": SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_LOCAL],
            "confirm_checked": True,
        },
    )
    assert good_apply.status_code == 200
    assert len(good_apply.get_json()["bookmarks"]) == 1


def test_sync_first_replace_local_noops_when_server_empty(client, app):
    with app.app_context():
        _create_user("empty-server-sync", "secret")

    token = _token(client, "empty-server-sync", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    preflight = client.post(
        "/api/v1/sync/first/preflight",
        headers=auth,
        json={
            "client_id": "firefox-empty-server",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "local_bookmarks": [{"title": "Local", "url": "https://local-only.test"}],
        },
    )
    assert preflight.status_code == 200
    payload = preflight.get_json()
    assert payload["would_noop"] is True
    assert payload["no_op_reason"] == "server_empty"

    apply_response = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-empty-server",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "confirmation_token": payload["confirmation_token"],
            "typed_phrase": SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_LOCAL],
            "confirm_checked": True,
            "local_bookmarks": [{"title": "Local", "url": "https://local-only.test"}],
        },
    )
    assert apply_response.status_code == 200
    result = apply_response.get_json()
    assert result["status"] == "no_op"
    assert result["reason"] == "server_empty"


def test_sync_first_replace_server_noops_when_local_empty(client, app):
    with app.app_context():
        user = _create_user("empty-local-sync", "secret")
        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://server-only.example",
                normalized_url="https://server-only.example/",
                title="Server Only",
            )
        )
        db.session.commit()

    token = _token(client, "empty-local-sync", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    preflight = client.post(
        "/api/v1/sync/first/preflight",
        headers=auth,
        json={
            "client_id": "firefox-empty-local",
            "mode": SYNC_MODE_REPLACE_SERVER,
            "local_bookmarks": [],
        },
    )
    assert preflight.status_code == 200
    payload = preflight.get_json()
    assert payload["would_noop"] is True
    assert payload["no_op_reason"] == "local_empty"

    apply_response = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-empty-local",
            "mode": SYNC_MODE_REPLACE_SERVER,
            "confirmation_token": payload["confirmation_token"],
            "typed_phrase": SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_SERVER],
            "confirm_checked": True,
            "local_bookmarks": [],
        },
    )
    assert apply_response.status_code == 200
    result = apply_response.get_json()
    assert result["status"] == "no_op"
    assert result["reason"] == "local_empty"


def test_sync_first_replace_local_snapshot_preserves_created_order(client, app):
    folder_id = None
    with app.app_context():
        user = _create_user("sync-order-user", "secret")
        folder = Folder(user_id=user.id, name="Ordered Folder")
        db.session.add(folder)
        db.session.flush()
        folder_id = folder.id

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.session.add_all(
            [
                Bookmark(
                    user_id=user.id,
                    folder_id=folder.id,
                    url="https://example.com/one",
                    normalized_url="https://example.com/one",
                    title="One",
                    created_at=base,
                    updated_at=base,
                ),
                Bookmark(
                    user_id=user.id,
                    folder_id=folder.id,
                    url="https://example.com/two",
                    normalized_url="https://example.com/two",
                    title="Two",
                    created_at=base.replace(day=2),
                    updated_at=base.replace(day=2),
                ),
                Bookmark(
                    user_id=user.id,
                    folder_id=folder.id,
                    url="https://example.com/three",
                    normalized_url="https://example.com/three",
                    title="Three",
                    created_at=base.replace(day=3),
                    updated_at=base.replace(day=3),
                ),
            ]
        )
        db.session.commit()

    token = _token(client, "sync-order-user", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    preflight = client.post(
        "/api/v1/sync/first/preflight",
        headers=auth,
        json={
            "client_id": "firefox-order-client",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "local_bookmarks": [{"title": "Local", "url": "https://local.test"}],
        },
    )
    assert preflight.status_code == 200
    preflight_payload = preflight.get_json()

    apply_response = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-order-client",
            "mode": SYNC_MODE_REPLACE_LOCAL,
            "confirmation_token": preflight_payload["confirmation_token"],
            "typed_phrase": SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_LOCAL],
            "confirm_checked": True,
            "local_bookmarks": [{"title": "Local", "url": "https://local.test"}],
        },
    )
    assert apply_response.status_code == 200
    payload = apply_response.get_json()

    folder_rows = [
        row["title"] for row in payload["bookmarks"] if row["folder_id"] == folder_id
    ]
    assert folder_rows == ["One", "Two", "Three"]


def test_sync_push_supports_folder_create_operations(client, app):
    with app.app_context():
        _create_user("folder-sync-user", "secret")

    token = _token(client, "folder-sync-user", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    push_response = client.post(
        "/api/v1/sync/push",
        headers=auth,
        json={
            "client_id": "firefox-folder-client",
            "operations": [
                {
                    "entity_type": "folder",
                    "op": "create",
                    "folder": {
                        "name": "Browser Folder",
                        "updated_at": utcnow().isoformat(),
                    },
                }
            ],
        },
    )

    assert push_response.status_code == 200
    result = push_response.get_json()
    assert result["results"][0]["status"] in {"created", "exists"}

    pull_response = client.get("/api/v1/sync/pull?since=0", headers=auth)
    assert pull_response.status_code == 200
    events = pull_response.get_json()["events"]
    assert any(event["entity_type"] == "folder" for event in events)


def test_sync_first_replace_server_creates_immediately_and_starts_background_fetch(
    client, app, monkeypatch
):
    with app.app_context():
        _create_user("sync-fetch-user", "secret")

    monkeypatch.setattr(
        "app.api.routes.fetch_and_extract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("replace-server apply must not fetch inline")
        ),
    )
    started = {"user_id": None, "bookmark_ids": []}

    def _capture_start(*, app, user_id, bookmark_ids):
        started["user_id"] = user_id
        started["bookmark_ids"] = list(bookmark_ids)

    monkeypatch.setattr(
        "app.api.routes.start_sync_first_replace_server_enrichment",
        _capture_start,
    )

    token = _token(client, "sync-fetch-user", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    local_bookmarks = [
        {
            "id": "local-1",
            "title": "Local Sync Bookmark",
            "url": "https://sync-fetch.example",
            "updated_at": utcnow().isoformat(),
        }
    ]

    preflight = client.post(
        "/api/v1/sync/first/preflight",
        headers=auth,
        json={
            "client_id": "firefox-sync-fetch",
            "mode": SYNC_MODE_REPLACE_SERVER,
            "local_bookmarks": local_bookmarks,
            "local_folders": [],
        },
    )
    assert preflight.status_code == 200
    preflight_payload = preflight.get_json()

    apply_response = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-sync-fetch",
            "mode": SYNC_MODE_REPLACE_SERVER,
            "confirmation_token": preflight_payload["confirmation_token"],
            "typed_phrase": SYNC_CONFIRM_PHRASES[SYNC_MODE_REPLACE_SERVER],
            "confirm_checked": True,
            "local_bookmarks": local_bookmarks,
            "local_folders": [],
        },
    )
    assert apply_response.status_code == 200

    with app.app_context():
        bookmark = Bookmark.query.filter_by(url="https://sync-fetch.example").first()
        assert bookmark is not None
        assert bookmark.notes is None
        assert bookmark.last_checked_at is None
        content = BookmarkContent.query.filter_by(bookmark_id=bookmark.id).first()
        assert content is None
        assert started["user_id"] == bookmark.user_id
        assert started["bookmark_ids"] == [bookmark.id]


def test_sync_replace_server_enrichment_worker_populates_notes_and_status(
    app, monkeypatch
):
    from app.services.sync_enrichment_jobs import (
        _run_sync_first_replace_server_enrichment,
    )

    with app.app_context():
        user = _create_user("sync-enrichment-worker", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://sync-enrich.example",
            normalized_url="https://sync-enrich.example",
            title="Sync Enrich",
            notes=None,
        )
        db.session.add(bookmark)
        db.session.commit()
        bookmark_id = bookmark.id
        user_id = user.id

    monkeypatch.setattr(
        "app.services.sync_enrichment_jobs.fetch_and_extract",
        lambda *_args, **_kwargs: ExtractedContent(
            title="Fetched",
            text="Fetched sync text",
            status="alive",
            error=None,
            status_code=200,
            final_url="https://sync-enrich.example",
        ),
    )

    _run_sync_first_replace_server_enrichment(app, user_id, (bookmark_id,))

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        assert bookmark is not None
        assert bookmark.notes == "Fetched sync text"
        assert bookmark.link_status == "alive"
        assert bookmark.last_checked_at is not None
        content = BookmarkContent.query.filter_by(bookmark_id=bookmark.id).first()
        assert content is not None
        assert content.extracted_text == "Fetched sync text"


def test_sync_replace_server_enrichment_worker_uses_default_thread_count(
    app, monkeypatch
):
    from app.services import sync_enrichment_jobs

    with app.app_context():
        user = _create_user("sync-enrichment-workers", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://sync-workers.example",
            normalized_url="https://sync-workers.example",
            title="Sync Workers",
        )
        db.session.add(bookmark)
        db.session.commit()
        user_id = user.id
        bookmark_id = bookmark.id

    observed = {"max_workers": None}

    class _FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class _FakeExecutor:
        def __init__(self, max_workers=None, **_kwargs):
            observed["max_workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            return _FakeFuture(fn(*args, **kwargs))

    monkeypatch.setattr(
        "app.services.sync_enrichment_jobs.ThreadPoolExecutor",
        _FakeExecutor,
    )
    monkeypatch.setattr(
        "app.services.sync_enrichment_jobs.as_completed",
        lambda futures: list(futures),
    )
    monkeypatch.setattr(
        "app.services.sync_enrichment_jobs.fetch_and_extract",
        lambda *_args, **_kwargs: ExtractedContent(
            title="Fetched",
            text="Fetched sync text",
            status="alive",
            error=None,
            status_code=200,
            final_url="https://sync-workers.example",
        ),
    )

    sync_enrichment_jobs._run_sync_first_replace_server_enrichment(
        app,
        user_id,
        (bookmark_id,),
    )

    assert observed["max_workers"] == 8


def test_sync_first_two_way_merge_keeps_existing_notes_for_duplicate_urls(client, app):
    with app.app_context():
        user = _create_user("sync-merge-user", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://merge-dup.example",
            normalized_url="https://merge-dup.example/",
            title="Server Copy",
            notes="Existing indexed notes",
        )
        db.session.add(bookmark)
        db.session.commit()

    token = _token(client, "sync-merge-user", "secret")
    auth = {"Authorization": f"Bearer {token}"}
    local_bookmarks = [
        {
            "id": "dup-1",
            "title": "Browser Copy",
            "url": "https://merge-dup.example",
            "notes": "",
            "updated_at": "2030-01-01T00:00:00+00:00",
        }
    ]

    preflight = client.post(
        "/api/v1/sync/first/preflight",
        headers=auth,
        json={
            "client_id": "firefox-merge-dups",
            "mode": SYNC_MODE_TWO_WAY,
            "local_bookmarks": local_bookmarks,
            "local_folders": [],
        },
    )
    assert preflight.status_code == 200
    preflight_payload = preflight.get_json()

    apply_response = client.post(
        "/api/v1/sync/first/apply",
        headers=auth,
        json={
            "client_id": "firefox-merge-dups",
            "mode": SYNC_MODE_TWO_WAY,
            "confirmation_token": preflight_payload["confirmation_token"],
            "typed_phrase": SYNC_CONFIRM_PHRASES[SYNC_MODE_TWO_WAY],
            "confirm_checked": True,
            "local_bookmarks": local_bookmarks,
            "local_folders": [],
        },
    )
    assert apply_response.status_code == 200

    with app.app_context():
        bookmark = Bookmark.query.filter_by(url="https://merge-dup.example").first()
        assert bookmark is not None
        assert bookmark.notes == "Existing indexed notes"


def test_sync_push_create_skips_existing_bookmark_without_clearing_notes(client, app):
    with app.app_context():
        user = _create_user("sync-push-dup", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://push-dup.example",
            normalized_url="https://push-dup.example/",
            title="Existing",
            notes="Persisted notes",
        )
        db.session.add(bookmark)
        db.session.commit()

    token = _token(client, "sync-push-dup", "secret")
    auth = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/v1/sync/push",
        headers=auth,
        json={
            "client_id": "firefox-push-dup",
            "operations": [
                {
                    "entity_type": "bookmark",
                    "op": "create",
                    "bookmark": {
                        "url": "https://push-dup.example",
                        "title": "Incoming Duplicate",
                        "notes": "",
                        "updated_at": utcnow().isoformat(),
                    },
                }
            ],
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"][0]["status"] == "exists"

    with app.app_context():
        rows = Bookmark.query.filter_by(
            normalized_url="https://push-dup.example/"
        ).all()
        assert len(rows) == 1
        assert rows[0].notes == "Persisted notes"


def test_manual_dead_link_bulk_ajax_starts_server_job(client, app, monkeypatch):
    with app.app_context():
        user = _create_user("ajax-checker", "secret")
        user_id = user.id
        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://example.com/private",
                normalized_url="https://example.com/private",
                title="Private",
            )
        )
        db.session.commit()

    started = {"job_id": None}

    def _fake_start(*, app, user_id, job_id):
        started["job_id"] = job_id

    monkeypatch.setattr("app.web.routes.start_dead_link_job", _fake_start)
    _web_login(client, "ajax-checker", "secret")

    start_response = client.post(
        "/dead-links",
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert start_response.status_code == 202
    start_payload = start_response.get_json()
    assert start_payload["job"]["status"] == "pending"
    assert "targets" not in start_payload
    assert started["job_id"] == start_payload["job"]["id"]

    with app.app_context():
        job = DeadLinkJob.query.filter_by(id=started["job_id"], user_id=user_id).first()
        assert job is not None


def test_non_ajax_dead_link_bulk_starts_server_job(client, app, monkeypatch):
    with app.app_context():
        user = _create_user("fallback-checker", "secret")
        user_id = user.id
        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://example.org",
                normalized_url="https://example.org",
                title="Example",
            )
        )
        db.session.commit()

    started = {"job_id": None}

    def _fake_start(*, app, user_id, job_id):
        started["job_id"] = job_id

    monkeypatch.setattr("app.web.routes.start_dead_link_job", _fake_start)
    _web_login(client, "fallback-checker", "secret")

    response = client.post("/dead-links", follow_redirects=False)
    assert response.status_code == 302
    assert started["job_id"] is not None

    with app.app_context():
        job = DeadLinkJob.query.filter_by(id=started["job_id"], user_id=user_id).first()
        assert job is not None


def test_dead_links_recheck_selected_starts_job_for_selected_ids(
    client, app, monkeypatch
):
    with app.app_context():
        user = _create_user("selected-recheck", "secret")
        first = Bookmark(
            user_id=user.id,
            url="https://selected-1.example",
            normalized_url="https://selected-1.example",
            title="Selected 1",
            link_status="unreachable",
        )
        second = Bookmark(
            user_id=user.id,
            url="https://selected-2.example",
            normalized_url="https://selected-2.example",
            title="Selected 2",
            link_status="server_error",
        )
        db.session.add(first)
        db.session.add(second)
        db.session.commit()
        selected_ids = [first.id, second.id]

    started = {"job_id": None, "bookmark_ids": None}

    def _fake_start(*, app, user_id, job_id, bookmark_ids=None):
        started["job_id"] = job_id
        started["bookmark_ids"] = bookmark_ids

    monkeypatch.setattr("app.web.routes.start_dead_link_job", _fake_start)
    _web_login(client, "selected-recheck", "secret")

    response = client.post(
        "/dead-links/recheck-selected",
        data={
            "bookmark_ids": [str(selected_ids[0]), str(selected_ids[1])],
            "next": "/dead-links",
        },
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["job"]["status"] == "pending"
    assert started["job_id"] == payload["job"]["id"]
    assert started["bookmark_ids"] == selected_ids


def test_dead_link_status_rules():
    assert classify_status(None, "Name or service not known") == "dns_error"
    assert classify_status(None, "operation timed out") == "timeout"
    assert (
        classify_status(
            None, "[SSL: CERTIFICATE_VERIFY_FAILED] self signed certificate"
        )
        == "alive"
    )
    assert classify_status(None, "connection refused") == "unreachable"
    assert classify_status(500, None) == "server_error"
    assert classify_status(404, None) == "not_found"
    assert classify_status(403, None) == "alive"


def test_dead_link_job_populates_notes_and_content_text(app, monkeypatch):
    with app.app_context():
        user = _create_user("dead-link-notes", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://dead-link-refresh.example",
            normalized_url="https://dead-link-refresh.example",
            title="Needs Refresh",
        )
        db.session.add(bookmark)
        db.session.flush()
        job = DeadLinkJob(user_id=user.id, status="pending", progress=0)
        db.session.add(job)
        db.session.commit()
        bookmark_id = bookmark.id
        user_id = user.id
        job_id = job.id

    monkeypatch.setattr(
        "app.services.dead_link_jobs.fetch_and_extract",
        lambda *_args, **_kwargs: ExtractedContent(
            title="Fetched",
            text="Dead-link scan extracted text",
            status="alive",
            error=None,
            status_code=200,
            final_url="https://dead-link-refresh.example",
        ),
    )

    from app.services.dead_link_jobs import _run_dead_link_job

    _run_dead_link_job(app, user_id, job_id)

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        assert bookmark is not None
        assert bookmark.notes == "Dead-link scan extracted text"
        assert bookmark.last_checked_at is not None

        content = BookmarkContent.query.filter_by(bookmark_id=bookmark_id).first()
        assert content is not None
        assert content.extracted_text == "Dead-link scan extracted text"

        checks = LinkCheck.query.filter_by(bookmark_id=bookmark_id).all()
        assert len(checks) == 1
        assert checks[0].result_type == "alive"


def test_bookmark_status_filter_applies_to_html_and_live_routes(client, app):
    with app.app_context():
        user = _create_user("status-filter-user", "secret")
        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://example.com/alive",
                normalized_url="https://example.com/alive",
                title="Alive Link",
                link_status="alive",
            )
        )
        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://example.com/dead",
                normalized_url="https://example.com/dead",
                title="Dead Link",
                link_status="unreachable",
            )
        )
        db.session.commit()

    _web_login(client, "status-filter-user", "secret")

    response = client.get("/bookmarks?status=unreachable")
    assert response.status_code == 200
    assert b"Dead Link" in response.data
    assert b"Alive Link" not in response.data

    live = client.get("/bookmarks/live?status=alive")
    assert live.status_code == 200
    payload = live.get_json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["title"] == "Alive Link"


def test_import_job_keeps_empty_title_and_sets_checked_at(client, app, monkeypatch):
    with app.app_context():
        user = _create_user("import-job-user", "secret")
        job = ImportJob(user_id=user.id, status="pending", progress=0)
        db.session.add(job)
        db.session.commit()
        user_id = user.id
        job_id = job.id

    monkeypatch.setattr(
        "app.services.import_jobs.fetch_and_extract",
        lambda *_args, **_kwargs: ExtractedContent(
            title="Fetched Page Title",
            text="Extracted body text",
            status="alive",
            error=None,
            status_code=200,
            final_url="https://example.com/no-title",
        ),
    )

    html = """
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><A HREF="https://example.com/no-title"></A>
</DL><p>
"""

    from app.services.import_jobs import _run_import_job

    _run_import_job(app, user_id, job_id, html)

    with app.app_context():
        bookmark = Bookmark.query.filter_by(user_id=user_id).first()
        assert bookmark is not None
        assert bookmark.title is None
        assert bookmark.last_checked_at is not None

    _web_login(client, "import-job-user", "secret")
    bookmarks_page = client.get("/bookmarks")
    assert bookmarks_page.status_code == 200
    assert b"(untitled)" in bookmarks_page.data

    with app.app_context():
        bookmark_id = Bookmark.query.filter_by(user_id=user_id).first().id
    edit_page = client.get(f"/bookmarks/{bookmark_id}/edit")
    assert edit_page.status_code == 200
    assert b'name="title" type="text" value=""' in edit_page.data
    assert b">None</textarea>" not in edit_page.data


def test_api_import_keeps_empty_title_when_anchor_title_missing(
    client, app, monkeypatch
):
    with app.app_context():
        _create_user("api-import-user", "secret")

    monkeypatch.setattr(
        "app.api.routes.fetch_and_extract",
        lambda *_args, **_kwargs: ExtractedContent(
            title="Fetched Title Should Not Override",
            text="Body",
            status="alive",
            error=None,
            status_code=200,
            final_url="https://example.com/no-title-api",
        ),
    )

    html = """
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><A HREF="https://example.com/no-title-api"></A>
</DL><p>
"""

    token = _token(client, "api-import-user", "secret")
    response = client.post(
        "/api/v1/import/browser-html",
        headers={"Authorization": f"Bearer {token}"},
        data={"file": (io.BytesIO(html.encode("utf-8")), "bookmarks.html")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200

    with app.app_context():
        bookmark = Bookmark.query.filter_by(
            url="https://example.com/no-title-api"
        ).first()
        assert bookmark is not None
        assert bookmark.title is None
        assert bookmark.last_checked_at is not None


def test_api_bulk_check_defaults_to_all_targets(client, app, monkeypatch):
    with app.app_context():
        user = _create_user("bulk-checker", "secret")
        for idx in range(130):
            db.session.add(
                Bookmark(
                    user_id=user.id,
                    url=f"https://example.com/{idx}",
                    normalized_url=f"https://example.com/{idx}",
                    title=f"Item {idx}",
                )
            )
        db.session.commit()

    def _fake_check_link(_url, timeout):
        return LinkCheckResult(
            status_code=200,
            final_url="https://example.com/ok",
            result_type="alive",
            latency_ms=25,
            error=None,
        )

    monkeypatch.setattr("app.api.routes.check_link", _fake_check_link)

    token = _token(client, "bulk-checker", "secret")
    response = client.post(
        "/api/v1/checks/run",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 200
    assert response.get_json()["checked"] == 130


def test_dead_link_job_delete_route_removes_history(client, app):
    with app.app_context():
        user = _create_user("job-cleaner", "secret")
        job = DeadLinkJob(user_id=user.id, status="done", progress=100)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    _web_login(client, "job-cleaner", "secret")
    response = client.post(f"/dead-links/jobs/{job_id}/delete", follow_redirects=False)
    assert response.status_code == 302

    with app.app_context():
        job = DeadLinkJob.query.filter_by(id=job_id).first()
        assert job is None


def test_recycle_bin_empty_purges_all_deleted_bookmarks(client, app):
    with app.app_context():
        user = _create_user("bin-owner", "secret")
        for idx in range(3):
            db.session.add(
                Bookmark(
                    user_id=user.id,
                    url=f"https://deleted.example/{idx}",
                    normalized_url=f"https://deleted.example/{idx}",
                    title=f"Deleted {idx}",
                    deleted_at=utcnow(),
                )
            )
        db.session.commit()

    _web_login(client, "bin-owner", "secret")
    response = client.post("/recycle-bin/empty", follow_redirects=False)
    assert response.status_code == 302

    with app.app_context():
        deleted_items = Bookmark.query.filter(Bookmark.deleted_at.is_not(None)).all()
        assert deleted_items == []


def test_search_page_redirects_to_bookmarks(client, app):
    with app.app_context():
        _create_user("search-user", "secret")

    _web_login(client, "search-user", "secret")
    response = client.get("/search?q=python", follow_redirects=False)
    assert response.status_code == 302
    assert "/bookmarks?q=python" in response.headers["Location"]


def test_bookmark_folder_filter_renders_indented_hierarchy(client, app):
    with app.app_context():
        user = _create_user("folder-user", "secret")
        parent = Folder(user_id=user.id, name="Parent")
        db.session.add(parent)
        db.session.flush()
        db.session.add(Folder(user_id=user.id, name="Child", parent_id=parent.id))
        db.session.commit()

    _web_login(client, "folder-user", "secret")
    response = client.get("/bookmarks")
    assert response.status_code == 200
    assert "└── Child" in response.get_data(as_text=True)


def test_bookmark_and_dead_link_pages_render_folder_breadcrumbs(client, app):
    with app.app_context():
        user = _create_user("breadcrumb-user", "secret")
        parent = Folder(user_id=user.id, name="Projects")
        db.session.add(parent)
        db.session.flush()
        child = Folder(user_id=user.id, name="Python", parent_id=parent.id)
        db.session.add(child)
        db.session.flush()

        db.session.add(
            Bookmark(
                user_id=user.id,
                folder_id=child.id,
                url="https://breadcrumbs.example",
                normalized_url="https://breadcrumbs.example",
                title="Breadcrumb Link",
                link_status="unreachable",
            )
        )
        db.session.commit()

    _web_login(client, "breadcrumb-user", "secret")

    bookmarks_page = client.get("/bookmarks")
    assert bookmarks_page.status_code == 200
    assert "Projects / Python" in bookmarks_page.get_data(as_text=True)

    live = client.get("/bookmarks/live")
    assert live.status_code == 200
    payload = live.get_json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["folder_path"] == ["Projects", "Python"]

    dead_links_page = client.get("/dead-links")
    assert dead_links_page.status_code == 200
    assert "Projects / Python" in dead_links_page.get_data(as_text=True)


def test_folders_page_truncates_bookmark_titles_to_200_chars(client, app):
    with app.app_context():
        user = _create_user("folders-truncate", "secret")
        folder = Folder(user_id=user.id, name="Long Titles")
        db.session.add(folder)
        db.session.flush()
        long_title = "A" * 220
        db.session.add(
            Bookmark(
                user_id=user.id,
                folder_id=folder.id,
                url="https://truncate.example",
                normalized_url="https://truncate.example",
                title=long_title,
            )
        )
        db.session.commit()

    _web_login(client, "folders-truncate", "secret")
    response = client.get("/folders")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ("A" * 197) + "..." in body
    assert "A" * 220 not in body


def test_folders_routes_create_rename_move_and_move_bookmarks(client, app):
    with app.app_context():
        user = _create_user("folders-routes", "secret")
        parent = Folder(user_id=user.id, name="Parent")
        db.session.add(parent)
        db.session.flush()
        bookmark = Bookmark(
            user_id=user.id,
            url="https://move-folder.example",
            normalized_url="https://move-folder.example",
            title="Move Me",
        )
        db.session.add(bookmark)
        db.session.commit()
        parent_id = parent.id
        bookmark_id = bookmark.id

    _web_login(client, "folders-routes", "secret")

    create_response = client.post(
        "/folders/create",
        data={"name": "Inbox"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert create_response.status_code == 201
    created_folder_id = create_response.get_json()["folder"]["id"]

    rename_response = client.post(
        f"/folders/{created_folder_id}/rename",
        data={"name": "Inbox Renamed"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert rename_response.status_code == 200

    move_response = client.post(
        f"/folders/{created_folder_id}/move",
        data={"parent_id": str(parent_id)},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert move_response.status_code == 200

    move_bookmark_response = client.post(
        "/folders/bookmarks/move",
        data={
            "bookmark_ids": [str(bookmark_id)],
            "folder_id": str(created_folder_id),
        },
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert move_bookmark_response.status_code == 200
    assert move_bookmark_response.get_json()["moved"] == 1

    with app.app_context():
        folder = Folder.query.filter_by(id=created_folder_id).first()
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        assert folder is not None
        assert folder.name == "Inbox Renamed"
        assert folder.parent_id == parent_id
        assert bookmark.folder_id == created_folder_id


def test_folders_delete_requires_confirmation_and_deletes_subtree_bookmarks(
    client, app
):
    with app.app_context():
        user = _create_user("folders-delete", "secret")
        parent = Folder(user_id=user.id, name="Delete Parent")
        db.session.add(parent)
        db.session.flush()
        child = Folder(user_id=user.id, name="Delete Child", parent_id=parent.id)
        db.session.add(child)
        db.session.flush()

        first = Bookmark(
            user_id=user.id,
            folder_id=parent.id,
            url="https://delete-parent.example",
            normalized_url="https://delete-parent.example",
            title="Parent Bookmark",
        )
        second = Bookmark(
            user_id=user.id,
            folder_id=child.id,
            url="https://delete-child.example",
            normalized_url="https://delete-child.example",
            title="Child Bookmark",
        )
        db.session.add(first)
        db.session.add(second)
        db.session.commit()
        parent_id = parent.id
        first_id = first.id
        second_id = second.id

    _web_login(client, "folders-delete", "secret")

    blocked = client.post(
        f"/folders/{parent_id}/delete",
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert blocked.status_code == 409

    confirmed = client.post(
        f"/folders/{parent_id}/delete",
        data={"confirm_delete": "1"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert confirmed.status_code == 200
    payload = confirmed.get_json()
    assert payload["deleted_folders"] == 2
    assert payload["deleted_bookmarks"] == 2

    with app.app_context():
        parent = Folder.query.filter_by(id=parent_id).first()
        first = Bookmark.query.filter_by(id=first_id).first()
        second = Bookmark.query.filter_by(id=second_id).first()
        assert parent is None
        assert first.deleted_at is not None
        assert second.deleted_at is not None
        assert first.folder_id is None
        assert second.folder_id is None


def test_dashboard_export_downloads_browser_importable_html(client, app):
    with app.app_context():
        user = _create_user("export-user", "secret")
        parent = Folder(user_id=user.id, name="Work")
        db.session.add(parent)
        db.session.flush()

        child = Folder(user_id=user.id, name="Docs", parent_id=parent.id)
        db.session.add(child)

        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://root.example",
                normalized_url="https://root.example",
                title="Root Link",
            )
        )
        db.session.add(
            Bookmark(
                user_id=user.id,
                folder_id=child.id,
                url="https://docs.example",
                normalized_url="https://docs.example",
                title="Docs Link",
            )
        )
        db.session.add(
            Bookmark(
                user_id=user.id,
                url="https://deleted.example",
                normalized_url="https://deleted.example",
                title="Deleted Link",
                deleted_at=utcnow(),
            )
        )
        db.session.commit()

    _web_login(client, "export-user", "secret")
    response = client.get("/export/browser-html")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "linkloom-bookmarks-" in response.headers["Content-Disposition"]

    payload = response.get_data(as_text=True)
    assert "<!DOCTYPE NETSCAPE-Bookmark-file-1>" in payload
    assert "<DT><H3>Work</H3>" in payload
    assert "<DT><H3>Docs</H3>" in payload
    assert '<A HREF="https://root.example">Root Link</A>' in payload
    assert '<A HREF="https://docs.example">Docs Link</A>' in payload
    assert "Deleted Link" not in payload


def test_edit_bookmark_without_reindex_skips_fetch_and_keeps_filter_redirect(
    client, app, monkeypatch
):
    with app.app_context():
        user = _create_user("edit-no-refresh", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://example.com/original",
            normalized_url="https://example.com/original",
            title="Original",
        )
        db.session.add(bookmark)
        db.session.commit()
        bookmark_id = bookmark.id

    refresh_called = {"count": 0}

    def _fail_refresh(_bookmark):
        refresh_called["count"] += 1
        raise AssertionError(
            "_refresh_content should not run when reindex is unchecked"
        )

    monkeypatch.setattr("app.web.routes._refresh_content", _fail_refresh)

    _web_login(client, "edit-no-refresh", "secret")
    response = client.post(
        f"/bookmarks/{bookmark_id}/edit",
        data={
            "url": "https://example.com/changed",
            "title": "Updated",
            "notes": "",
            "tags": "",
            "next": "/bookmarks?q=updated&status=alive",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/bookmarks?q=updated&status=alive")
    assert refresh_called["count"] == 0

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        assert bookmark.url == "https://example.com/changed"


def test_edit_bookmark_reindex_populates_notes_from_extracted_text(
    client, app, monkeypatch
):
    with app.app_context():
        user = _create_user("edit-reindex", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://example.com/reindex",
            normalized_url="https://example.com/reindex",
            title="Needs Reindex",
            notes=None,
        )
        db.session.add(bookmark)
        db.session.commit()
        bookmark_id = bookmark.id

    monkeypatch.setattr(
        "app.web.routes.fetch_and_extract",
        lambda *_args, **_kwargs: ExtractedContent(
            title="Fetched Title",
            text="Reindexed page body text",
            status="alive",
            error=None,
            status_code=200,
            final_url="https://example.com/reindex",
        ),
    )

    _web_login(client, "edit-reindex", "secret")
    response = client.post(
        f"/bookmarks/{bookmark_id}/edit",
        data={
            "url": "https://example.com/reindex",
            "title": "Needs Reindex",
            "notes": "",
            "tags": "",
            "reindex": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        assert bookmark.notes == "Reindexed page body text"
        assert bookmark.last_checked_at is not None


def test_edit_bookmark_internal_link_sets_na_and_internal_tag(client, app, monkeypatch):
    with app.app_context():
        user = _create_user("internal-flag", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://intranet.local/page",
            normalized_url="https://intranet.local/page",
            title="Intranet",
            link_status="timeout",
        )
        db.session.add(bookmark)
        db.session.commit()
        bookmark_id = bookmark.id

    def _fail_refresh(_bookmark):
        raise AssertionError("Internal bookmarks should skip refresh")

    monkeypatch.setattr("app.web.routes._refresh_content", _fail_refresh)

    _web_login(client, "internal-flag", "secret")
    response = client.post(
        f"/bookmarks/{bookmark_id}/edit",
        data={
            "url": "https://intranet.local/page",
            "title": "Intranet",
            "notes": "",
            "tags": "team",
            "internal_link": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        tag_names = sorted(tag.name for tag in bookmark.tags)
        assert "internal" in tag_names
        assert bookmark.link_status == "N/A"


def test_bookmarks_delete_selected_moves_items_and_preserves_query(client, app):
    with app.app_context():
        user = _create_user("bulk-delete-user", "secret")
        ids = []
        for idx in range(3):
            bookmark = Bookmark(
                user_id=user.id,
                url=f"https://bulk.example/{idx}",
                normalized_url=f"https://bulk.example/{idx}",
                title=f"Bulk {idx}",
            )
            db.session.add(bookmark)
            db.session.flush()
            ids.append(bookmark.id)
        db.session.commit()

    _web_login(client, "bulk-delete-user", "secret")
    response = client.post(
        "/bookmarks/delete-selected",
        data={
            "bookmark_ids": [str(ids[0]), str(ids[2])],
            "next": "/bookmarks?q=bulk&folder_id=1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/bookmarks?q=bulk&folder_id=1")

    with app.app_context():
        first = Bookmark.query.filter_by(id=ids[0]).first()
        second = Bookmark.query.filter_by(id=ids[1]).first()
        third = Bookmark.query.filter_by(id=ids[2]).first()
        assert first.deleted_at is not None
        assert second.deleted_at is None
        assert third.deleted_at is not None


def test_bookmarks_move_selected_updates_folder_and_preserves_query(client, app):
    with app.app_context():
        user = _create_user("bulk-move-user", "secret")
        source_folder = Folder(user_id=user.id, name="Source")
        target_folder = Folder(user_id=user.id, name="Target")
        db.session.add(source_folder)
        db.session.add(target_folder)
        db.session.flush()

        first = Bookmark(
            user_id=user.id,
            folder_id=source_folder.id,
            url="https://move.example/1",
            normalized_url="https://move.example/1",
            title="Move 1",
        )
        second = Bookmark(
            user_id=user.id,
            folder_id=source_folder.id,
            url="https://move.example/2",
            normalized_url="https://move.example/2",
            title="Move 2",
        )
        db.session.add(first)
        db.session.add(second)
        db.session.commit()
        first_id = first.id
        second_id = second.id
        target_folder_id = target_folder.id

    _web_login(client, "bulk-move-user", "secret")
    response = client.post(
        "/bookmarks/move-selected",
        data={
            "bookmark_ids": [str(first_id)],
            "folder_id": str(target_folder_id),
            "next": "/bookmarks?q=move&folder_id=1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/bookmarks?q=move&folder_id=1")

    with app.app_context():
        first = Bookmark.query.filter_by(id=first_id).first()
        second = Bookmark.query.filter_by(id=second_id).first()
        assert first.folder_id == target_folder_id
        assert second.folder_id != target_folder_id


def test_bookmarks_add_tags_selected_appends_unique_tags(client, app):
    with app.app_context():
        user = _create_user("bulk-tags-user", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://tags.example",
            normalized_url="https://tags.example",
            title="Tags",
        )
        other = Bookmark(
            user_id=user.id,
            url="https://tags-other.example",
            normalized_url="https://tags-other.example",
            title="Other",
        )
        existing_tag = Tag(user_id=user.id, name="existing")
        bookmark.tags.append(existing_tag)
        db.session.add(existing_tag)
        db.session.add(bookmark)
        db.session.add(other)
        db.session.commit()
        bookmark_id = bookmark.id
        other_id = other.id

    _web_login(client, "bulk-tags-user", "secret")
    response = client.post(
        "/bookmarks/add-tags-selected",
        data={
            "bookmark_ids": [str(bookmark_id)],
            "tags": "Existing, alpha; beta",
            "next": "/bookmarks",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        other = Bookmark.query.filter_by(id=other_id).first()
        assert sorted(tag.name for tag in bookmark.tags) == [
            "alpha",
            "beta",
            "existing",
        ]
        assert [tag.name for tag in other.tags] == []


def test_keywords_schema_migration_moves_values_to_tags(app):
    with app.app_context():
        from app.schema_migrations import migrate_keywords_into_tags_and_drop_column

        user = _create_user("keywords-migrate-user", "secret")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://migrate.example",
            normalized_url="https://migrate.example",
            title="Migrate",
        )
        db.session.add(bookmark)
        db.session.commit()

        db.session.execute(text("ALTER TABLE bookmarks ADD COLUMN keywords_text TEXT"))
        db.session.execute(
            text("UPDATE bookmarks SET keywords_text = :value WHERE id = :bookmark_id"),
            {"value": "python, Flask; docs", "bookmark_id": bookmark.id},
        )
        db.session.commit()

        migrated = migrate_keywords_into_tags_and_drop_column()
        assert migrated is True

        bookmark = Bookmark.query.filter_by(id=bookmark.id).first()
        assert bookmark is not None
        assert sorted(tag.name for tag in bookmark.tags) == ["docs", "flask", "python"]

        columns = db.session.execute(text("PRAGMA table_info(bookmarks)")).all()
        column_names = {row[1] for row in columns}
        assert "keywords_text" not in column_names


def test_dead_links_delete_selected_only_deletes_problematic(client, app):
    with app.app_context():
        user = _create_user("dead-select-user", "secret")
        dead = Bookmark(
            user_id=user.id,
            url="https://dead.example",
            normalized_url="https://dead.example",
            title="Dead",
            link_status="unreachable",
        )
        alive = Bookmark(
            user_id=user.id,
            url="https://alive.example",
            normalized_url="https://alive.example",
            title="Alive",
            link_status="alive",
        )
        db.session.add(dead)
        db.session.add(alive)
        db.session.commit()
        dead_id = dead.id
        alive_id = alive.id

    _web_login(client, "dead-select-user", "secret")
    response = client.post(
        "/dead-links/delete-selected",
        data={
            "bookmark_ids": [str(dead_id), str(alive_id)],
            "next": "/dead-links",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dead-links")

    with app.app_context():
        dead = Bookmark.query.filter_by(id=dead_id).first()
        alive = Bookmark.query.filter_by(id=alive_id).first()
        assert dead.deleted_at is not None
        assert alive.deleted_at is None


def test_api_bulk_check_skips_internal_bookmark_and_sets_na(client, app, monkeypatch):
    with app.app_context():
        user = _create_user("internal-api", "secret")
        internal_tag = Tag(user_id=user.id, name="internal")
        bookmark = Bookmark(
            user_id=user.id,
            url="https://internal.example",
            normalized_url="https://internal.example",
            title="Internal",
        )
        bookmark.tags.append(internal_tag)
        db.session.add(internal_tag)
        db.session.add(bookmark)
        db.session.commit()
        bookmark_id = bookmark.id

    def _fail_check(*_args, **_kwargs):
        raise AssertionError("check_link should not run for internal bookmarks")

    monkeypatch.setattr("app.api.routes.check_link", _fail_check)
    token = _token(client, "internal-api", "secret")
    response = client.post(
        "/api/v1/checks/run",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 200

    with app.app_context():
        bookmark = Bookmark.query.filter_by(id=bookmark_id).first()
        checks = LinkCheck.query.filter_by(bookmark_id=bookmark_id).all()
        assert bookmark.link_status == "N/A"
        assert checks == []
