"""
scheduler_pkg/service.py
------------------------
Capa de orquestación: único punto de contacto entre el API (Flask) y el motor
de planificación (repository + scheduler).

Responsabilidades:
  1. Abrir / cerrar la conexión a MySQL usando AppConfig.
  2. Llamar a load_planning_data() para obtener todo de una vez.
  3. Construir SolverConfig desde AppConfig.
  4. Invocar al scheduler y recibir el ScheduleResult.
  5. Opcionalmente persistir el plan en production_plan.
  6. Devolver un dict limpio al API (sin objetos internos).

Restricción clave: este archivo NO importa Flask en ningún punto.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import mysql.connector
from mysql.connector import MySQLConnection

from config import from_env                          # config.py raíz
from .repository import load_planning_data, RepositoryDataError, get_stock_summary
from .scheduler import solve
from .config import SolverConfig                     # scheduler_pkg/config.py

logger = logging.getLogger(__name__)

SHIFTS: dict[str, tuple[time, time]] = {
    "T1":  (time(6,  0),  time(14, 30)),
    "T2":  (time(11, 30), time(22,  0)),
    "ALL": (time(6,  0),  time(22,  0)),
}

MIN_STOCK_DAYS = 7


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------

def _get_connection() -> MySQLConnection:
    cfg = from_env()
    return mysql.connector.connect(**cfg.db.as_connector_kwargs())


# ---------------------------------------------------------------------------
# run_schedule()
# ---------------------------------------------------------------------------

def run_schedule(
    plan_date: date | None = None,
    shift: str = "ALL",
    shift_start: time | None = None,
    shift_end: time | None = None,
    save_plan: bool = False,
) -> dict[str, Any]:
    """
    Ejecuta el planificador completo para una fecha y turno dados.
    Devuelve un dict serializable a JSON.
    """
    plan_date = plan_date or date.today()

    # ── Horizonte de tiempo ────────────────────────────────────────────────
    if shift_start and shift_end:
        t_start, t_end = shift_start, shift_end
    else:
        if shift not in SHIFTS:
            raise ValueError(f"Turno '{shift}' no válido. Opciones: {list(SHIFTS.keys())}")
        t_start, t_end = SHIFTS[shift]

    horizon_min = _time_diff_minutes(t_start, t_end)
    if horizon_min <= 0:
        raise ValueError(f"shift_end debe ser posterior a shift_start.")

    logger.info("run_schedule | fecha=%s turno=%s horizonte=%d min", plan_date, shift, horizon_min)

    conn = _get_connection()
    try:
        # ── Cargar todos los datos de una vez ──────────────────────────────
        planning_data = load_planning_data(conn, plan_date)

        if not planning_data.orders:
            return _empty_result(plan_date, horizon_min, "No hay órdenes abiertas.")

        logger.info(
            "Datos cargados: %d órdenes · %d máquinas · %d partes",
            len(planning_data.orders),
            len(planning_data.machines),
            len(planning_data.parts),
        )

        # ── Construir SolverConfig desde AppConfig ─────────────────────────
        app_cfg    = from_env()
        solver_cfg = SolverConfig.from_app_config(app_cfg.solver)
        # El horizonte del turno sobreescribe el del config — el plan es diario
        solver_cfg = SolverConfig(
            time_limit_seconds=solver_cfg.time_limit_seconds,
            num_workers=solver_cfg.num_workers,
            random_seed=solver_cfg.random_seed,
            horizon_minutes=horizon_min,
            minutes_per_slot=solver_cfg.minutes_per_slot,
        )

        # ── Resolver ───────────────────────────────────────────────────────
        result = solve(
            orders=planning_data.orders,
            parts=planning_data.parts,
            machines=planning_data.machines,
            shifts=planning_data.shifts,
            solver_config=solver_cfg,
        )

        # ── Persistir si se solicitó ───────────────────────────────────────
        if save_plan and result.is_feasible:
            _save_production_plan(conn, plan_date, t_start, result)
            conn.commit()
            logger.info("Plan persistido en production_plan.")

        return _format_result(result, plan_date, horizon_min, t_start)

    except RepositoryDataError as exc:
        conn.rollback()
        logger.error("Error de datos: %s", exc)
        return _empty_result(plan_date, horizon_min, str(exc))

    except Exception as exc:
        conn.rollback()
        logger.exception("Error inesperado en run_schedule: %s", exc)
        return {
            "status": "ERROR", "plan_date": plan_date.isoformat(),
            "horizon_min": horizon_min, "tasks": [],
            "makespan_min": 0, "message": f"Error interno: {exc}",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_stock_status()
# ---------------------------------------------------------------------------

def get_stock_status(reference_date: date | None = None) -> dict[str, Any]:
    reference_date = reference_date or date.today()
    deadline = reference_date + timedelta(days=MIN_STOCK_DAYS)
    conn = _get_connection()
    try:
        return get_stock_summary(conn, reference_date, deadline)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _time_diff_minutes(start: time, end: time) -> int:
    dummy = date(2000, 1, 1)
    return int((datetime.combine(dummy, end) - datetime.combine(dummy, start)).total_seconds() // 60)


def _empty_result(plan_date: date, horizon_min: int, message: str) -> dict[str, Any]:
    return {
        "status": "INFEASIBLE", "plan_date": plan_date.isoformat(),
        "horizon_min": horizon_min, "tasks": [],
        "makespan_min": 0, "message": message,
    }


def _format_result(result, plan_date: date, horizon_min: int, shift_start: time) -> dict[str, Any]:
    """Convierte ScheduleResult a dict serializable con horas reales del día."""
    dummy   = date(2000, 1, 1)
    dt_base = datetime.combine(dummy, shift_start)

    tasks = []
    for task in result.tasks:
        start_dt = dt_base + timedelta(minutes=task.start_min)
        end_dt   = dt_base + timedelta(minutes=task.end_min)
        tasks.append({
            "order_id":     task.order_id,
            "part_number":  task.part_number,
            "step_order":   task.step_order,
            "process":      task.process_name,
            "machine_id":   task.machine_id,
            "start_time":   start_dt.strftime("%H:%M"),
            "end_time":     end_dt.strftime("%H:%M"),
            "duration_min": task.duration_min,
            "quantity":     task.quantity,
        })

    messages = {
        "OPTIMAL":    f"Plan óptimo generado con {len(tasks)} tareas.",
        "FEASIBLE":   f"Plan factible generado con {len(tasks)} tareas.",
        "INFEASIBLE": "No se encontró un plan factible.",
        "UNKNOWN":    "El solver no pudo determinar una solución en el tiempo límite.",
    }

    return {
        "status":                result.solver_status,
        "plan_date":             plan_date.isoformat(),
        "horizon_min":           horizon_min,
        "tasks":                 tasks,
        "makespan_min":          result.makespan_min,
        "unscheduled_order_ids": result.unscheduled_order_ids,
        "wall_time_seconds":     result.wall_time_seconds,
        "message":               messages.get(result.solver_status, result.solver_status),
    }


def _save_production_plan(
    conn: MySQLConnection,
    plan_date: date,
    shift_start: time,
    result,
) -> None:
    """Persiste ScheduleResult en production_plan. Idempotente (borra antes de insertar)."""
    cursor  = conn.cursor()
    dummy   = date(2000, 1, 1)
    dt_base = datetime.combine(dummy, shift_start)

    cursor.execute("DELETE FROM production_plan WHERE Date = %s", (plan_date.isoformat(),))

    insert_sql = """
        INSERT INTO production_plan
            (Date, Machine, Part_No, Operation_Code, Quantity,
             Start_Time, End_Time, Duration_Min, Position, Stop_Program,
             SPM, PPM, Hours, Total_Hours)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = []
    for pos, task in enumerate(result.tasks, start=1):
        start_dt = dt_base + timedelta(minutes=task.start_min)
        end_dt   = dt_base + timedelta(minutes=task.end_min)
        dur_min  = task.duration_min
        hours    = round(dur_min / 60, 4)
        rows.append((
            plan_date.isoformat(), task.machine_id, task.part_number,
            task.process_name, task.quantity,
            start_dt.strftime("%H:%M:%S"), end_dt.strftime("%H:%M:%S"),
            dur_min, pos, 0, 0, 0, hours, hours,
        ))

    cursor.executemany(insert_sql, rows)
    cursor.close()