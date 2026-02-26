import hashlib
from functools import wraps

from flask import g, jsonify, request
from flask_login import current_user

from app.extensions import db
from app.models import ApiToken, utcnow


def _user_from_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_row = ApiToken.query.filter_by(token_hash=token_hash).first()
    if not token_row or token_row.revoked_at is not None:
        return None
    token_row.last_used_at = utcnow()
    db.session.commit()
    return token_row.user


def get_authenticated_api_user(token_only=False):
    if not token_only and current_user.is_authenticated:
        return current_user
    return _user_from_bearer_token()


def api_auth_required(admin=False, token_only=False):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            user = get_authenticated_api_user(token_only=token_only)
            if not user:
                return jsonify({"error": "authentication required"}), 401
            if admin and not user.is_admin:
                return jsonify({"error": "admin access required"}), 403
            g.api_user = user
            return func(*args, **kwargs)

        return wrapped

    return decorator
