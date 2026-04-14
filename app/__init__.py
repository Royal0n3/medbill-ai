import os
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# Project root is one level above this file (app/)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def create_app(config_overrides: dict = None) -> Flask:
    """Application factory for the Medical Billing Error Detection service."""
    app = Flask(__name__, instance_relative_config=True)
    CORS(app, origins=["https://mdbillify.netlify.app", "http://localhost:5000"])

    # Core configuration
    app.config.update(
        SECRET_KEY=os.environ["FLASK_SECRET_KEY"],
        ANTHROPIC_API_KEY=os.environ["ANTHROPIC_API_KEY"],
        BREVO_API_KEY=os.environ.get("BREVO_API_KEY", ""),
        DATABASE=os.path.join(_PROJECT_ROOT, "medbill.db"),
        UPLOAD_FOLDER=os.path.join(_PROJECT_ROOT, "uploads"),
        OUTPUT_FOLDER=os.path.join(_PROJECT_ROOT, "outputs"),
        LOG_FOLDER=os.path.join(_PROJECT_ROOT, "logs"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 MB upload limit
    )

    if config_overrides:
        app.config.update(config_overrides)

    # Ensure runtime directories exist
    for folder_key in ("UPLOAD_FOLDER", "OUTPUT_FOLDER", "LOG_FOLDER"):
        os.makedirs(app.config[folder_key], exist_ok=True)

    # Initialise database (creates tables if absent, registers teardown)
    from .db import init_db
    init_db(app)

    # Register blueprints
    from .routes import main as main_blueprint  # noqa: F401 — imported for side effects
    app.register_blueprint(main_blueprint)

    return app
