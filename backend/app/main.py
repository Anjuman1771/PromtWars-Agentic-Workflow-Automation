"""Uvicorn entry point: ``uvicorn backend.app.main:app``."""

from .api import create_app

app = create_app()
