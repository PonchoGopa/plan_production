"""Planning endpoints.

This file is the only layer that knows about HTTP/Flask serialization.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

import mysql.connector
from flask import Blueprint, jsonify, request
from mysql.connector import Error as MySQLError

from config import from_env
from scheduler_pkg import PlanningService, RepositoryDataError, ScheduleResult

planning_bp = Blueprint("planning", __name__)


def _serialize_schedule_result(result: ScheduleResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["tasks"] = [
        {
            **task,
            "duration_min": task["end_min"] - task["start_min"],
        }
        for task in payload["tasks"]
    ]
    payload["generated_at"] = date.today().isoformat()
    return payload


@planning_bp.post("/run")
def run_planning() -> tuple[object, int]:
    body = request.get_json(silent=True) or {}
    order_status = str(body.get("order_status", "pending"))
    only_active = bool(body.get("only_active", True))

    solver_cfg_raw = body.get("solver", {})
    if solver_cfg_raw is None:
        solver_cfg_raw = {}
    if not isinstance(solver_cfg_raw, dict):
        return jsonify({"error": "Field 'solver' must be an object."}), 400

    conn = None
    service = PlanningService()
    try:
        app_config = from_env()
        solver_payload = {
            "horizon_days": int(solver_cfg_raw.get("horizon_days", app_config.solver.horizon_days)),
            "time_limit_seconds": int(solver_cfg_raw.get("time_limit_seconds", app_config.solver.time_limit_seconds)),
            "num_workers": int(solver_cfg_raw.get("num_workers", app_config.solver.num_workers)),
            "random_seed": solver_cfg_raw.get("random_seed", app_config.solver.random_seed),
        }

        if solver_payload["random_seed"] is not None:
            solver_payload["random_seed"] = int(solver_payload["random_seed"])

        conn = mysql.connector.connect(**app_config.db.as_connector_kwargs())
        result = service.generate_plan(
            conn,
            solver_config=solver_payload,
            order_status=order_status,
            only_active=only_active,
        )
        return jsonify(_serialize_schedule_result(result)), 200
    except RepositoryDataError as exc:
        return jsonify({"error": "Repository data error", "detail": str(exc)}), 422
    except MySQLError as exc:
        return jsonify({"error": "Database connection/query error", "detail": str(exc)}), 500
    except (TypeError, ValueError) as exc:
        return jsonify({"error": "Invalid input payload", "detail": str(exc)}), 400
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": "Unexpected error", "detail": str(exc)}), 500
    finally:
        if conn is not None and conn.is_connected():
            conn.close()
