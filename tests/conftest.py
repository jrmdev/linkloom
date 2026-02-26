import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()
