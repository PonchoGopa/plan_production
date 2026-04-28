"""
wsgi.py
-------
Punto de entrada de la aplicación.

Desarrollo  : python wsgi.py
Producción  : gunicorn wsgi:app
"""

from api.app import create_app

app = create_app()

if __name__ == "__main__":
    import os
    port = int(os.environ.get("FLASK_PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"

    print(f"\n  Kimex Scheduler API")
    print(f"  Corriendo en http://localhost:{port}")
    print(f"  Modo: {'desarrollo' if debug else 'producción'}\n")

    app.run(host="0.0.0.0", port=port, debug=debug)