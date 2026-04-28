"""
api/app.py
----------
Application factory de Flask.

Patrón application factory: create_app() construye y configura la app.
Esto permite instanciarla con distintas configuraciones (testing, producción)
sin efectos secundarios al importar el módulo.

No contiene lógica de negocio ni acceso a BD — solo inicialización de Flask.
"""

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify
from flask_cors import CORS

from .routes.planning import planning_bp


def create_app(test_config: dict | None = None) -> Flask:
    """
    Construye la aplicación Flask.

    Parámetros
    ----------
    test_config : dict opcional con valores que sobreescriben la config normal.
                  Útil para tests unitarios sin tocar el .env.

    Devuelve
    --------
    Flask app configurada y lista para correr o testear.
    """
    app = Flask(__name__, instance_relative_config=False)

    # ── Configuración base ─────────────────────────────────────────────────
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-key-change-in-prod"),
        JSON_SORT_KEYS=False,          # El orden de las claves JSON es intencional
        PROPAGATE_EXCEPTIONS=False,    # Los errores los manejamos con handlers propios
    )

    if test_config:
        app.config.update(test_config)

    # ── CORS ───────────────────────────────────────────────────────────────
    # Permite peticiones desde el dashboard (distinto origen en desarrollo).
    # En producción, reemplaza "*" con el dominio exacto del frontend.
    CORS(app, resources={r"/api/*": {"origins": os.environ.get("CORS_ORIGIN", "*")}})

    # ── Logging ────────────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Blueprints ─────────────────────────────────────────────────────────
    # Cada blueprint agrupa un dominio funcional.
    # planning_bp maneja todo lo relativo al scheduler y al plan de producción.
    app.register_blueprint(planning_bp, url_prefix="/api/planning")

    # ── Manejadores de error globales ──────────────────────────────────────
    # Devuelven JSON en lugar de HTML para que los clientes API no rompan.

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": "bad_request", "message": str(e)}), 400

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "not_found", "message": str(e)}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "method_not_allowed", "message": str(e)}), 405

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"error": "internal_server_error", "message": str(e)}), 500

    # ── Health-check mínimo ────────────────────────────────────────────────
    @app.get("/health")
    def health():
        """Endpoint de liveness para monitores y load balancers."""
        return jsonify({"status": "ok"}), 200

    return app