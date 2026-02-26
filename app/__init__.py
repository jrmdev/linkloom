from flask import Flask

from app.api import api_bp
from app.auth import auth_bp
from app.config import Config
from app.extensions import db, login_manager, migrate
from app.jobs.scheduler import start_scheduler
from app.schema_migrations import migrate_keywords_into_tags_and_drop_column
from app.web import web_bp


def create_app(config_object=Config):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(config_object)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    @app.cli.command("init-db")
    def init_db_command():
        db.create_all()
        migrate_keywords_into_tags_and_drop_column()
        print("Initialized LinkLoom database.")

    @app.context_processor
    def inject_globals():
        return {"app_name": "LinkLoom"}

    with app.app_context():
        db.create_all()
        migrate_keywords_into_tags_and_drop_column()

    start_scheduler(app)
    return app
