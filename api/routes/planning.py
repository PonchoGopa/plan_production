"""
api/routes/planning.py
----------------------
Blueprint de Flask con todos los endpoints del planificador.

Responsabilidades de esta capa (y SOLO estas):
  - Parsear y validar los parámetros HTTP entrantes.
  - Llamar a scheduler_pkg.service con datos limpios.
  - Serializar la respuesta a JSON.
  - Devolver el código HTTP correcto.

Nada de SQL, nada de lógica de negocio, nada de OR-Tools aquí.
"""

from __future__ import annotations

from datetime import date, time

import mysql.connector
from flask import Blueprint, jsonify, request

from config import from_env               # config.py en la raíz del proyecto
from scheduler_pkg import repository, service

planning_bp = Blueprint("planning", __name__)


# ---------------------------------------------------------------------------
# Helper: conexión reutilizable en este módulo
# ---------------------------------------------------------------------------

def _get_conn(autocommit: bool = True):
    """
    Abre una conexión usando AppConfig.
    autocommit=True para lecturas (GET endpoints).
    autocommit=False para escrituras (lo maneja service.py en POST /run).
    """
    cfg = from_env()
    kwargs = cfg.db.as_connector_kwargs()
    kwargs["autocommit"] = autocommit
    return mysql.connector.connect(**kwargs)


# ---------------------------------------------------------------------------
# Helpers de validación y respuesta
# ---------------------------------------------------------------------------

def _parse_date(value: str | None, param_name: str) -> date:
    if not value:
        raise ValueError(f"El parámetro '{param_name}' es requerido (formato: YYYY-MM-DD).")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"'{param_name}' tiene formato inválido: '{value}'. Use YYYY-MM-DD.")


def _parse_time(value: str | None, param_name: str) -> time | None:
    if not value:
        return None
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except (ValueError, TypeError):
        raise ValueError(f"'{param_name}' tiene formato inválido: '{value}'. Use HH:MM.")


def _ok(data: dict, status: int = 200):
    return jsonify({"ok": True, "data": data}), status


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# ---------------------------------------------------------------------------
# POST /api/planning/run
# ---------------------------------------------------------------------------

@planning_bp.post("/run")
def run_schedule():
    """
    Ejecuta el planificador para una fecha y turno dados.

    Body JSON:
    {
        "date":        "2025-06-10",   -- requerido
        "shift":       "ALL",          -- opcional (T1 | T2 | ALL), default ALL
        "shift_start": "06:00",        -- opcional, sobreescribe shift
        "shift_end":   "22:00",        -- opcional, sobreescribe shift
        "save_plan":   false           -- opcional, default false
    }
    """
    body = request.get_json(silent=True) or {}

    try:
        plan_date   = _parse_date(body.get("date"), "date")
        shift       = body.get("shift", "ALL").upper()
        shift_start = _parse_time(body.get("shift_start"), "shift_start")
        shift_end   = _parse_time(body.get("shift_end"),   "shift_end")
        save_plan   = bool(body.get("save_plan", False))
    except ValueError as exc:
        return _error(str(exc), 400)

    result = service.run_schedule(
        plan_date=plan_date,
        shift=shift,
        shift_start=shift_start,
        shift_end=shift_end,
        save_plan=save_plan,
    )

    http_status = 500 if result["status"] == "ERROR" else 200
    return _ok(result, http_status)


# ---------------------------------------------------------------------------
# GET /api/planning/plan?date=YYYY-MM-DD
# ---------------------------------------------------------------------------

@planning_bp.get("/plan")
def get_plan():
    """
    Devuelve el plan guardado para una fecha.
    Consulta production_plan directamente (no corre el solver).
    """
    try:
        plan_date = _parse_date(request.args.get("date"), "date")
    except ValueError as exc:
        return _error(str(exc), 400)

    conn = _get_conn()
    try:
        tasks = repository.get_plan_by_date(conn, plan_date)
    finally:
        conn.close()

    return _ok({
        "plan_date":   plan_date.isoformat(),
        "tasks":       tasks,
        "total_tasks": len(tasks),
    })


# ---------------------------------------------------------------------------
# GET /api/planning/stock?date=YYYY-MM-DD
# ---------------------------------------------------------------------------

@planning_bp.get("/stock")
def get_stock_status():
    """
    Estado de stock por número de parte para los próximos 7 días.
    date es opcional; si se omite usa hoy.
    """
    date_str = request.args.get("date")
    try:
        ref_date = _parse_date(date_str, "date") if date_str else date.today()
    except ValueError as exc:
        return _error(str(exc), 400)

    result = service.get_stock_status(ref_date)
    return _ok(result)


# ---------------------------------------------------------------------------
# GET /api/planning/machines?area=prensa
# ---------------------------------------------------------------------------

@planning_bp.get("/machines")
def get_machines():
    """
    Catálogo de máquinas. Filtro opcional por área:
    prensa | soldadura | ensamble | inspeccion
    """
    area_filter = request.args.get("area", "").strip().lower() or None
    valid_areas = {"prensa", "soldadura", "ensamble", "inspeccion"}

    if area_filter and area_filter not in valid_areas:
        return _error(
            f"Área inválida: '{area_filter}'. Opciones: {sorted(valid_areas)}", 400
        )

    conn = _get_conn()
    try:
        machines = repository.get_machines(conn)
    finally:
        conn.close()

    result = [
        {"id": m.id, "name": m.name, "area": m.area}
        for m in machines
        if area_filter is None or m.area == area_filter
    ]

    return _ok({"machines": result, "total": len(result)})


# ---------------------------------------------------------------------------
# GET /api/planning/routes?part_number=17575 6LB0A
# ---------------------------------------------------------------------------

@planning_bp.get("/routes")
def get_routes():
    """
    Ruta de proceso completa de un número de parte.
    Incluye máquinas alternativas en cada paso cuando existen.
    """
    part_number = request.args.get("part_number", "").strip()
    if not part_number:
        return _error("El parámetro 'part_number' es requerido.", 400)

    conn = _get_conn()
    try:
        routes = repository.get_routes_for_part(conn, part_number)
    finally:
        conn.close()

    if not routes:
        return _error(f"No se encontró ruta para '{part_number}'.", 404)

    steps = [
        {
            "step_order":      r.step_order,
            "process":         r.process_name,
            "machine_id":      r.machine_id,
            "alt_machine_ids": r.alternative_machine_ids,
            "eligible_ids":    r.eligible_machine_ids,
            "setup_time_min":  r.setup_time_min,
        }
        for r in routes
    ]

    return _ok({"part_number": part_number, "steps": steps})