"""
scheduler_pkg/repository.py
----------------------------
Capa de acceso a datos. Toda interacción con MySQL vive aquí.

Reglas estrictas:
  - Ninguna función importa Flask.
  - Las conexiones se reciben como parámetro — nunca se crean aquí.
  - Se devuelven dataclasses o dicts planos, nunca cursors ni Row objects.
  - Las queries usan parámetros posicionales (%s) para evitar SQL injection.

Tablas usadas:
  - machines          : catálogo de máquinas
  - rutas             : pasos de ruta por número de parte
  - rutas_maquinas    : máquinas elegibles por paso (incluyendo alternativas)
  - part_prod         : metadatos de parte (customer, project, workcenter, SPM)
  - schedule          : órdenes de producción abiertas
  - part_production   : SPH por número de parte
  - production_plan   : plan guardado por el scheduler
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from mysql.connector import MySQLConnection

from .models import (
    CycleTime,
    Machine,
    Order,
    Part,
    PlanningData,
    Route,
    ScheduledTask,
    ScheduleResult,
    Shift,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# EXCEPCIÓN PÚBLICA — exportada por __init__.py
# ===========================================================================

class RepositoryDataError(Exception):
    """
    Se lanza cuando los datos cargados desde la BD son inconsistentes
    o insuficientes para construir un plan válido.

    Ejemplos:
      - No hay máquinas activas en la BD
      - Una parte tiene ruta pero sus máquinas no existen en machines
      - La tabla rutas_maquinas está vacía después del seed
    """


# ===========================================================================
# FUNCIÓN PRINCIPAL — load_planning_data()
# ===========================================================================

def load_planning_data(
    conn: MySQLConnection,
    plan_date: date,
) -> PlanningData:
    """
    Carga todos los datos necesarios para una corrida del scheduler
    en una sola llamada.

    Devuelve un PlanningData con:
      - machines        : dict[int, Machine]
      - parts           : dict[str, Part]
      - routes_by_part  : dict[str, list[Route]]
      - cycle_times     : dict[tuple[str, int], CycleTime]
      - orders          : list[Order]
      - shifts          : list[Shift]  (vacío si no hay tabla shifts)

    Lanza RepositoryDataError si los datos son inválidos.

    Esta función es el punto de entrada único para service.py — en lugar
    de llamar a get_machines(), get_routes(), etc. por separado, se llama
    a load_planning_data() una sola vez y se obtiene todo.
    """
    machines   = _load_machines(conn)
    parts      = _load_parts(conn)
    routes     = _load_routes(conn)
    cycle_times = _load_cycle_times(conn)
    orders     = _load_orders(conn, plan_date)
    shifts     = _load_shifts(conn)

    if not machines:
        raise RepositoryDataError(
            "No hay máquinas activas en la BD. "
            "Verifica que el seed_rutas.sql se aplicó correctamente."
        )

    logger.info(
        "load_planning_data | fecha=%s máquinas=%d partes=%d rutas=%d órdenes=%d",
        plan_date, len(machines), len(parts), sum(len(v) for v in routes.values()), len(orders),
    )

    return PlanningData(
        machines=machines,
        parts=parts,
        routes_by_part=routes,
        cycle_times=cycle_times,
        orders=orders,
        shifts=shifts,
    )


# ===========================================================================
# LOADERS PRIVADOS
# ===========================================================================

def _load_machines(conn: MySQLConnection) -> dict[int, Machine]:
    """
    Carga todas las máquinas.
    Mapea el campo Area de la BD al process_name del modelo.
    """
    sql = """
        SELECT id, Machine, Area
        FROM   machines
        ORDER  BY Area, Machine
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()

    result: dict[int, Machine] = {}
    for row in rows:
        result[row["id"]] = Machine(
            id=row["id"],
            name=row["Machine"],
            area=row["Area"] or "",
            process_name=row["Area"] or "",   # Area == proceso en nuestro schema
            tonnage_ton=None,                  # No está en el schema actual
            active=True,                       # Todas las de BD se consideran activas
        )
    return result


def _load_parts(conn: MySQLConnection) -> dict[str, Part]:
    """
    Carga metadatos de partes desde part_prod.
    part_prod tiene: Part_No, Customer, Project, Workcenter, SPM_Plan, Man, SPC
    """
    sql = """
        SELECT Part_No, Customer, Project, Workcenter,
               COALESCE(SPM_Plan, 0) AS SPM_Plan
        FROM   part_prod
        ORDER  BY Part_No
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()

    result: dict[str, Part] = {}
    for row in rows:
        result[row["Part_No"]] = Part(
            part_number=row["Part_No"],
            customer=row["Customer"] or "",
            project=row["Project"] or "",
            workcenter=row["Workcenter"] or "",
            spm_plan=float(row["SPM_Plan"] or 0),
            weight_kg=None,
            active=True,
        )
    return result


def _load_routes(conn: MySQLConnection) -> dict[str, list[Route]]:
    """
    Carga rutas con sus máquinas elegibles desde rutas + rutas_maquinas.

    Por cada paso (ruta_id) se agrupan todas las máquinas elegibles en
    Route.alternative_machine_ids, siguiendo el modelo real de models.py.

    Route.machine_id       = máquina primaria (la de rutas.machine_id)
    Route.alternative_machine_ids = las demás máquinas de rutas_maquinas
    """
    sql = """
        SELECT
            r.id              AS ruta_id,
            r.part_number,
            r.step_order,
            r.process_name,
            r.setup_time_min,
            r.machine_id      AS primary_machine_id,
            rm.machine_id     AS eligible_machine_id
        FROM      rutas          r
        JOIN      rutas_maquinas rm ON rm.ruta_id = r.id
        ORDER BY  r.part_number, r.step_order, rm.machine_id
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()

    # Agrupar por ruta_id
    # {ruta_id: {"base": row, "eligible": [machine_id, ...]}}
    ruta_map: dict[int, dict] = {}
    for row in rows:
        rid = row["ruta_id"]
        if rid not in ruta_map:
            ruta_map[rid] = {
                "base":     row,
                "eligible": [],
            }
        ruta_map[rid]["eligible"].append(row["eligible_machine_id"])

    # Construir Route objects
    routes_by_part: dict[str, list[Route]] = {}
    for rid, data in ruta_map.items():
        base    = data["base"]
        primary = base["primary_machine_id"]
        alts    = [m for m in data["eligible"] if m != primary]

        route = Route(
            id=rid,
            part_number=base["part_number"],
            step_order=base["step_order"],
            process_name=base["process_name"],
            machine_id=primary,
            setup_time_min=base["setup_time_min"],
            alternative_machine_ids=alts,
        )
        routes_by_part.setdefault(base["part_number"], []).append(route)

    # Ordenar pasos por step_order dentro de cada parte
    for part_number in routes_by_part:
        routes_by_part[part_number].sort(key=lambda r: r.step_order)

    return routes_by_part


def _load_cycle_times(conn: MySQLConnection) -> dict[tuple[str, int], CycleTime]:
    """
    Carga tiempos de ciclo desde rutas_maquinas.
    Llave: (part_number, machine_id)

    cycle_time_min en rutas_maquinas fue calculado como 60/SPM
    durante el seed, así que ya está en minutos por pieza.
    """
    sql = """
        SELECT
            r.part_number,
            rm.machine_id,
            rm.cycle_time_min
        FROM  rutas_maquinas rm
        JOIN  rutas r ON r.id = rm.ruta_id
        WHERE rm.cycle_time_min > 0
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()

    result: dict[tuple[str, int], CycleTime] = {}
    for row in rows:
        key = (row["part_number"], row["machine_id"])
        # Si hay múltiples pasos para la misma (parte, máquina), guardamos
        # el primero encontrado — son tiempos de ciclo del mismo proceso
        if key not in result:
            result[key] = CycleTime(
                part_number=row["part_number"],
                machine_id=row["machine_id"],
                cycle_time_min=float(row["cycle_time_min"]),
            )
    return result


def _load_orders(conn: MySQLConnection, plan_date: date) -> list[Order]:
    """
    Carga órdenes abiertas para plan_date desde la tabla schedule.
    due_date = plan_date + 7 días (objetivo de stock semanal).
    """
    sql = """
        SELECT
            s.ID       AS order_id,
            s.Part_No  AS part_number,
            s.Quantity AS quantity,
            s.Date     AS order_date,
            COALESCE(pp.Customer, '') AS customer
        FROM  schedule s
        LEFT JOIN part_prod pp ON pp.Part_No = s.Part_No
        WHERE s.Date = %s
        ORDER BY s.ID
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, (plan_date.isoformat(),))
    rows = cursor.fetchall()
    cursor.close()

    due_date = plan_date + timedelta(days=7)

    orders = []
    for row in rows:
        orders.append(Order(
            id=row["order_id"],
            part_number=row["part_number"],
            customer=row["customer"],
            quantity=row["quantity"],
            due_date=due_date,
            priority=1,
            status="open",
        ))

    logger.info("_load_orders | fecha=%s órdenes=%d", plan_date, len(orders))
    return orders


def _load_shifts(conn: MySQLConnection) -> list[Shift]:
    """
    Intenta cargar turnos desde la BD.
    Si la tabla no existe o está vacía, devuelve lista vacía —
    scheduler.py usará el ShiftConfig por defecto (fallback seguro).
    """
    try:
        sql = """
            SELECT id, name, start_min, end_min, active
            FROM   shifts
            WHERE  active = 1
            ORDER  BY id
        """
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql)
        rows = cursor.fetchall()
        cursor.close()

        return [
            Shift(
                id=row["id"],
                name=row["name"],
                start_min=row["start_min"],
                end_min=row["end_min"],
                active=bool(row["active"]),
            )
            for row in rows
        ]
    except Exception:
        # La tabla shifts puede no existir aún — es un escenario válido
        logger.info("Tabla shifts no disponible — se usará ShiftConfig por defecto.")
        return []


# ===========================================================================
# FUNCIONES PÚBLICAS ADICIONALES (usadas por planning.py directamente)
# ===========================================================================

def get_machines(conn: MySQLConnection) -> list[Machine]:
    """Devuelve lista de Machine para los endpoints de catálogo."""
    return list(_load_machines(conn).values())


def get_routes_for_part(conn: MySQLConnection, part_number: str) -> list[Route]:
    """Devuelve los pasos de ruta de un número de parte específico."""
    routes = _load_routes(conn)
    return routes.get(part_number, [])


def get_plan_by_date(conn: MySQLConnection, plan_date: date) -> list[dict[str, Any]]:
    """
    Devuelve el plan guardado para una fecha desde production_plan.
    Usado por GET /api/planning/plan
    """
    sql = """
        SELECT
            id,
            Machine        AS machine,
            Part_No        AS part_number,
            Operation_Code AS operation,
            Quantity       AS quantity,
            Start_Time     AS start_time,
            End_Time       AS end_time,
            Duration_Min   AS duration_min,
            Hours          AS hours,
            SPM            AS spm,
            PPM            AS ppm,
            Position       AS position,
            Stop_Program   AS stop_program
        FROM  production_plan
        WHERE Date = %s
        ORDER BY Position ASC
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, (plan_date.isoformat(),))
    rows = cursor.fetchall()
    cursor.close()

    result = []
    for row in rows:
        result.append({
            "id":           row["id"],
            "machine":      row["machine"],
            "part_number":  row["part_number"],
            "operation":    row["operation"],
            "quantity":     row["quantity"],
            "start_time":   _fmt_time(row["start_time"]),
            "end_time":     _fmt_time(row["end_time"]),
            "duration_min": float(row["duration_min"] or 0),
            "hours":        float(row["hours"] or 0),
            "spm":          int(row["spm"] or 0),
            "ppm":          int(row["ppm"] or 0),
            "position":     row["position"],
            "stop_program": row["stop_program"],
        })

    logger.info("get_plan_by_date | fecha=%s registros=%d", plan_date, len(result))
    return result


def get_stock_summary(
    conn: MySQLConnection,
    reference_date: date,
    deadline: date,
) -> dict[str, Any]:
    """
    Estado de stock por parte entre reference_date y deadline.
    Usado por GET /api/planning/stock
    """
    sql_planned = """
        SELECT Part_No AS part_number, SUM(Quantity) AS planned_qty
        FROM   production_plan
        WHERE  Date >= %s AND Date <= %s
        GROUP  BY Part_No
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql_planned, (reference_date.isoformat(), deadline.isoformat()))
    planned_map = {r["part_number"]: int(r["planned_qty"] or 0)
                   for r in cursor.fetchall()}
    cursor.close()

    sql_target = """
        SELECT s.Part_No AS part_number, s.Quantity AS target_qty
        FROM   schedule s
        INNER JOIN (
            SELECT Part_No, MAX(Date) AS last_date
            FROM   schedule
            WHERE  Date <= %s
            GROUP  BY Part_No
        ) latest ON latest.Part_No = s.Part_No AND latest.last_date = s.Date
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql_target, (deadline.isoformat(),))
    target_map = {r["part_number"]: int(r["target_qty"] or 0)
                  for r in cursor.fetchall()}
    cursor.close()

    all_parts = set(planned_map) | set(target_map)
    parts = []
    for part in sorted(all_parts):
        planned = planned_map.get(part, 0)
        target  = target_map.get(part, 0)
        coverage_pct = round(planned / target * 100, 1) if target > 0 else (100.0 if planned > 0 else 0.0)
        status = "ok" if coverage_pct >= 100 else ("warning" if coverage_pct >= 50 else "critical")
        parts.append({
            "part_number":  part,
            "planned_qty":  planned,
            "target_qty":   target,
            "coverage_pct": coverage_pct,
            "status":       status,
        })

    logger.info("get_stock_summary | ref=%s deadline=%s partes=%d",
                reference_date, deadline, len(parts))
    return {
        "reference_date": reference_date.isoformat(),
        "deadline":       deadline.isoformat(),
        "parts":          parts,
    }


# ===========================================================================
# Helpers internos
# ===========================================================================

def _fmt_time(value: Any) -> str | None:
    """Convierte TIME de MySQL (timedelta) a string HH:MM:SS."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        total_secs = int(value.total_seconds())
        h, rem = divmod(total_secs, 3600)
        m, s   = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except AttributeError:
        return str(value)