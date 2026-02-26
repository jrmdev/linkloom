import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "default-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'linkloom.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1") == "1"
    CONTENT_FETCH_TIMEOUT = float(os.environ.get("CONTENT_FETCH_TIMEOUT", "10"))
    CONTENT_MAX_BYTES = int(os.environ.get("CONTENT_MAX_BYTES", "2500000"))
    IMPORT_WORKERS = int(os.environ.get("IMPORT_WORKERS", "16"))
    DEAD_LINK_WORKERS = int(os.environ.get("DEAD_LINK_WORKERS", "16"))
    SYNC_ENRICHMENT_WORKERS = int(os.environ.get("SYNC_ENRICHMENT_WORKERS", "8"))
    DEAD_LINK_CHECK_INTERVAL_MINUTES = int(
        os.environ.get("DEAD_LINK_CHECK_INTERVAL_MINUTES", "1440")
    )
    SYNC_CONFIRM_TTL_SECONDS = int(os.environ.get("SYNC_CONFIRM_TTL_SECONDS", "900"))


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SCHEDULER_ENABLED = False
