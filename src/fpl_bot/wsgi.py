"""Production WSGI entry point with no import-time application construction."""

from flask import Flask

from fpl_bot.production import create_production_app


def create_app() -> Flask:
    """Build the existing production application when the WSGI server starts."""
    return create_production_app()
