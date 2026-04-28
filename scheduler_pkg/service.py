"""
scheduler_pkg/service.py
------------------------
Capa de orquestación: único punto de contacto entre el API (Flask) y el motor
de planificación (repository + scheduler).

Responsabilidades:
  1. Abrir / cerrar la conexión a MySQL usando AppConfig.
  2. Llamar a repository para obtener órdenes, rutas y máquinas.
  3. Construir el horizonte de planificación a partir de los parámetros del turno.
  4. Invocar al scheduler y recibir el resultado.
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

from config import from_env           # config.py en la raíz del proyecto
from . import repository
from . import scheduler as sched

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de turno
# ---------------------------------------------------------------------------
SHIFTS: dict[str, tuple[time, time]] = {
    "T1":  (time(6,  0),  time(14, 30)),   # Turno 1: 06:00 – 14:30
    "T2":  (time(11, 30), time(22,  0)),   # Turno 2: 11:30 – 22:00
    "ALL": (time(6,  0),  time(22,  0)),   # Día completo (ambos turnos)
}

MIN_STOCK_DAYS = 7


# ---------------------------------------------------------------------------
# Conexión a la base de datos
# ---------------------------------------------------------------------------

def _get_connection() -> MySQLConnection:
    """
    Crea una conexión MySQL usando AppConfig.db.as_connector_kwargs().
    autocommit queda en False (valor por defecto en DBConfig) para que el
    llamador controle explícitamente commit / rollback.
    """
    cfg = from_env()
    return mysql.connector.connect(**cfg.db.as_connector_kwargs())


# ---------------------------------------------------------------------------
# Función principal: run_schedule()
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

    Parámetros
    ----------
    plan_date   : Fecha a planificar. Si None, usa hoy.
    shift       : Clave de SHIFTS ("T1", "T2", "ALL"). Ignorado si se pasan
                  shift_start / shift_end explícitos.
    shift_start : Hora de inicio del turno (sobreescribe `shift`).
    shift_end   : Hora de fin del turno (sobreescribe `shift`).
    save_plan   : Si True, persiste el resultado en la tabla production_plan.

    Devuelve
    --------
    dict con las claves:
        status      : "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "ERROR"
        plan_date   : fecha planificada (ISO string)
        horizon_min : duración del horizonte en minutos
        tasks       : lista de tareas asignadas
        makespan_min: duración total del plan en minutos
        message     : texto descriptivo del resultado
    """
    plan_date = plan_date or date.today()

    # ── 1. Resolver horizonte de tiempo ────────────────────────────────────
    if shift_start and shift_end:
        t_start, t_end = shift_start, shift_end
    else:
        if shift not in SHIFTS:
            raise ValueError(
                f"Turno '{shift}' no válido. Opciones: {list(SHIFTS.keys())}"
            )
        t_start, t_end = SHIFTS[shift]

    horizon_min = _time_diff_minutes(t_start, t_end)
    if horizon_min <= 0:
        raise ValueError(
            f"shift_end ({t_end}) debe ser posterior a shift_start ({t_start})."
        )

    logger.info(
        "run_schedule | fecha=%s turno=%s inicio=%s fin=%s horizonte=%d min",
        plan_date, shift, t_start, t_end, horizon_min,
    )

    # ── 2. Conectar a la BD e invocar repositorio ──────────────────────────
    conn = _get_connection()
    try:
        orders   = repository.get_open_orders(conn, plan_date)
        routes   = repository.get_all_routes(conn)
        machines = repository.get_machines(conn)

        if not orders:
            logger.warning("No hay órdenes abiertas para %s.", plan_date)
            return _empty_result(plan_date, horizon_min, "No hay órdenes abiertas.")

        logger.info(
            "Datos cargados: %d órdenes · %d rutas · %d máquinas",
            len(orders), len(routes), len(machines),
        )

        # ── 3. Pasar configuración del solver desde AppConfig ──────────────
        cfg = from_env()
        result = sched.solve(
            orders=orders,
            routes=routes,
            machines=machines,
            horizon_minutes=horizon_min,
            time_limit_seconds=cfg.solver.time_limit_seconds,
            num_workers=cfg.solver.num_workers,
            random_seed=cfg.solver.random_seed,
        )

        # ── 4. Persistir si se solicitó ────────────────────────────────────
        if save_plan and result["status"] in ("OPTIMAL", "FEASIBLE"):
            _save_production_plan(conn, plan_date, t_start, result["tasks"])
            conn.commit()
            logger.info("Plan persistido en production_plan.")

        # ── 5. Formatear y devolver ────────────────────────────────────────
        return _format_result(result, plan_date, horizon_min, t_start)

    except Exception as exc:
        conn.rollback()
        logger.exception("Error en run_schedule: %s", exc)
        return {
            "status":       "ERROR",
            "plan_date":    plan_date.isoformat(),
            "horizon_min":  horizon_min,
            "tasks":        [],
            "makespan_min": 0,
            "message":      f"Error interno: {exc}",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Función auxiliar: get_stock_status()
# ---------------------------------------------------------------------------

def get_stock_status(reference_date: date | None = None) -> dict[str, Any]:
    """
    Devuelve el estado de stock actual comparado con el objetivo de una semana.
    """
    reference_date = reference_date or date.today()
    deadline = reference_date + timedelta(days=MIN_STOCK_DAYS)

    conn = _get_connection()
    try:
        return repository.get_stock_summary(conn, reference_date, deadline)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _time_diff_minutes(start: time, end: time) -> int:
    dummy = date(2000, 1, 1)
    dt_start = datetime.combine(dummy, start)
    dt_end   = datetime.combine(dummy, end)
    return int((dt_end - dt_start).total_seconds() // 60)


def _empty_result(plan_date: date, horizon_min: int, message: str) -> dict[str, Any]:
    return {
        "status":       "INFEASIBLE",
        "plan_date":    plan_date.isoformat(),
        "horizon_min":  horizon_min,
        "tasks":        [],
        "makespan_min": 0,
        "message":      message,
    }


def _format_result(
    raw: dict[str, Any],
    plan_date: date,
    horizon_min: int,
    shift_start: time,
) -> dict[str, Any]:
    """
    Convierte los offsets de minutos internos del solver en horas reales del día.
    Ejemplo: shift_start=06:00, offset=120 → start_time="08:00"
    """
    dummy   = date(2000, 1, 1)
    dt_base = datetime.combine(dummy, shift_start)

    formatted_tasks = []
    for task in raw.get("tasks", []):
        start_dt = dt_base + timedelta(minutes=task["start_min"])
        end_dt   = dt_base + timedelta(minutes=task["end_min"])
        formatted_tasks.append({
            "order_id":     task["order_id"],
            "part_number":  task["part_number"],
            "step_order":   task["step_order"],
            "process":      task["process"],
            "machine":      task["machine"],
            "start_time":   start_dt.strftime("%H:%M"),
            "end_time":     end_dt.strftime("%H:%M"),
            "duration_min": task["end_min"] - task["start_min"],
            "is_late":      task.get("is_late", False),
        })

    makespan = max((t["end_min"] for t in raw.get("tasks", [])), default=0)

    return {
        "status":       raw["status"],
        "plan_date":    plan_date.isoformat(),
        "horizon_min":  horizon_min,
        "tasks":        formatted_tasks,
        "makespan_min": makespan,
        "message":      _status_message(raw["status"], len(formatted_tasks)),
    }


def _status_message(status: str, n_tasks: int) -> str:
    messages = {
        "OPTIMAL":    f"Plan óptimo generado con {n_tasks} tareas.",
        "FEASIBLE":   f"Plan factible generado con {n_tasks} tareas (no garantizado óptimo).",
        "INFEASIBLE": "No se encontró un plan factible con las restricciones actuales.",
        "UNKNOWN":    "El solver no pudo determinar una solución en el tiempo límite.",
    }
    return messages.get(status, f"Estado desconocido: {status}")


def _save_production_plan(
    conn: MySQLConnection,
    plan_date: date,
    shift_start: time,
    tasks: list[dict[str, Any]],
) -> None:
    """
    Persiste las tareas en production_plan.
    Borra primero el día para que sea idempotente (re-ejecutar no duplica).
    """
    cursor  = conn.cursor()
    dummy   = date(2000, 1, 1)
    dt_base = datetime.combine(dummy, shift_start)

    cursor.execute(
        "DELETE FROM production_plan WHERE Date = %s",
        (plan_date.isoformat(),)
    )

    insert_sql = """
        INSERT INTO production_plan
            (Date, Machine, Part_No, Operation_Code, Quantity,
             Start_Time, End_Time, Duration_Min, Position, Stop_Program,
             SPM, PPM, Hours, Total_Hours)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    rows = []
    for pos, task in enumerate(tasks, start=1):
        start_dt = dt_base + timedelta(minutes=task["start_min"])
        end_dt   = dt_base + timedelta(minutes=task["end_min"])
        dur_min  = task["end_min"] - task["start_min"]
        hours    = round(dur_min / 60, 4)

        rows.append((
            plan_date.isoformat(),
            task["machine"],
            task["part_number"],
            task.get("process", ""),
            task.get("quantity", 0),
            start_dt.strftime("%H:%M:%S"),
            end_dt.strftime("%H:%M:%S"),
            dur_min,
            pos,
            0,
            task.get("spm", 0),
            task.get("ppm", 0),
            hours,
            hours,
        ))

    cursor.executemany(insert_sql, rows)
    cursor.close()