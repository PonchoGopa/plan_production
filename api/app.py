"""Flask app factory for the scheduling API layer."""

from __future__ import annotations

from flask import Flask, jsonify

from .routes.planning import planning_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(planning_bp, url_prefix="/planning")

    @app.get("/health")
    def health() -> tuple[object, int]:
        return jsonify({"status": "ok"}), 200

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
