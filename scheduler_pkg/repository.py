"""
scheduler_pkg/repository.py
----------------------------
Capa de acceso a datos. Toda interacción con MySQL vive aquí.

Reglas estrictas:
  - Ninguna función importa Flask.
  - Las conexiones se reciben como parámetro — nunca se crean aquí.
  - Se devuelven dataclasses o dicts planos, nunca cursors ni Row objects.
  - Las queries usan parámetros posicionales (%s) para evitar SQL injection.

Tablas que usa este módulo:
  - machines          : catálogo de máquinas
  - rutas             : pasos de ruta por número de parte
  - rutas_maquinas    : máquinas elegibles por paso (incluyendo alternativas)
  - schedule          : órdenes de producción abiertas
  - part_production   : SPH y operador por número de parte
  - production_plan   : plan guardado por el scheduler
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from mysql.connector import MySQLConnection

from .models import Machine, Order, Route

logger = logging.getLogger(__name__)


# ===========================================================================
# MÁQUINAS
# ===========================================================================

def get_machines(conn: MySQLConnection) -> list[Machine]:
    """
    Devuelve todas las máquinas del catálogo ordenadas por área y nombre.
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

    return [
        Machine(
            id=row["id"],
            name=row["Machine"],
            area=row["Area"] or "",
        )
        for row in rows
    ]


# ===========================================================================
# RUTAS
# ===========================================================================

def get_all_routes(conn: MySQLConnection) -> list[Route]:
    """
    Devuelve todas las rutas con sus máquinas elegibles.

    Hace un JOIN con rutas_maquinas para traer, por cada paso, la lista
    completa de máquinas entre las que el solver puede elegir.
    La máquina registrada en rutas.machine_id se considera la primaria;
    cualquier otra en rutas_maquinas para ese mismo ruta_id es alternativa.
    """
    sql = """
        SELECT
            r.id              AS ruta_id,
            r.part_number,
            r.step_order,
            r.process_name,
            r.setup_time_min,
            r.machine_id      AS primary_machine_id,
            m_primary.Machine AS primary_machine_name,
            rm.machine_id     AS eligible_machine_id,
            m_elig.Machine    AS eligible_machine_name,
            rm.cycle_time_min
        FROM      rutas          r
        JOIN      machines       m_primary ON m_primary.id = r.machine_id
        JOIN      rutas_maquinas rm        ON rm.ruta_id   = r.id
        JOIN      machines       m_elig    ON m_elig.id    = rm.machine_id
        ORDER BY  r.part_number, r.step_order, rm.machine_id
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()

    # Agrupar por ruta_id para construir la lista de máquinas elegibles
    routes_map: dict[int, Route] = {}

    for row in rows:
        rid = row["ruta_id"]
        if rid not in routes_map:
            routes_map[rid] = Route(
                id=rid,
                part_number=row["part_number"],
                step_order=row["step_order"],
                process_name=row["process_name"],
                setup_time_min=row["setup_time_min"],
                primary_machine=row["primary_machine_name"],
                alt_machines=[],
                cycle_time_min=float(row["cycle_time_min"] or 0),
            )

        route = routes_map[rid]
        mach_name = row["eligible_machine_name"]
        if (mach_name != route.primary_machine
                and mach_name not in route.alt_machines):
            route.alt_machines.append(mach_name)

    return list(routes_map.values())


def get_routes_for_part(conn: MySQLConnection, part_number: str) -> list[Route]:
    """
    Devuelve los pasos de ruta de un número de parte específico.
    Reutiliza get_all_routes y filtra en memoria — la tabla rutas es pequeña
    (236 filas) así que no justifica una query adicional.
    """
    all_routes = get_all_routes(conn)
    return [r for r in all_routes if r.part_number == part_number]


# ===========================================================================
# ÓRDENES ABIERTAS
# ===========================================================================

def get_open_orders(conn: MySQLConnection, plan_date: date) -> list[Order]:
    """
    Devuelve las órdenes de producción a planificar para plan_date.

    Fuente: tabla `schedule` (órdenes abiertas del ERP).
    JOIN con part_production para obtener el SPH real de cada parte,
    ya que schedule.Duration puede no estar disponible en todos los registros.

    La due_date se calcula como plan_date + 7 días (objetivo de stock semanal).
    """
    sql = """
        SELECT
            s.ID          AS order_id,
            s.Part_No     AS part_number,
            s.Quantity    AS quantity,
            s.Workcenter  AS workcenter,
            s.Date        AS order_date,
            COALESCE(pp.SPH, 0) AS sph
        FROM  schedule s
        LEFT JOIN part_production pp ON pp.Part_No = s.Part_No
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
        sph = int(row["sph"] or 0)
        cycle_time_min = round(60.0 / sph, 4) if sph > 0 else 0.0

        orders.append(Order(
            id=row["order_id"],
            part_number=row["part_number"],
            quantity=row["quantity"],
            due_date=due_date,
            priority=1,
            cycle_time_min=cycle_time_min,
        ))

    logger.info("get_open_orders | fecha=%s órdenes=%d", plan_date, len(orders))
    return orders


# ===========================================================================
# PLAN GUARDADO  — NUEVA 1/2
# ===========================================================================

def get_plan_by_date(conn: MySQLConnection, plan_date: date) -> list[dict[str, Any]]:
    """
    Devuelve el plan de producción guardado para una fecha específica.

    Consulta production_plan y devuelve una lista de dicts ordenada por
    Position (el orden de ejecución dentro del plan).
    Devuelve lista vacía si no hay plan guardado para esa fecha.

    Nota sobre tipos: convierte Decimal → float y timedelta → str para que
    jsonify no lance TypeError al serializar.

    Usado por: GET /api/planning/plan?date=YYYY-MM-DD
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
        # MySQL devuelve TIME como timedelta; lo convertimos a string "HH:MM:SS"
        start = row["start_time"]
        end   = row["end_time"]

        result.append({
            "id":           row["id"],
            "machine":      row["machine"],
            "part_number":  row["part_number"],
            "operation":    row["operation"],
            "quantity":     row["quantity"],
            "start_time":   _fmt_time(start),
            "end_time":     _fmt_time(end),
            "duration_min": float(row["duration_min"] or 0),
            "hours":        float(row["hours"] or 0),
            "spm":          int(row["spm"] or 0),
            "ppm":          int(row["ppm"] or 0),
            "position":     row["position"],
            "stop_program": row["stop_program"],
        })

    logger.info(
        "get_plan_by_date | fecha=%s registros=%d", plan_date, len(result)
    )
    return result


# ===========================================================================
# ESTADO DE STOCK  — NUEVA 2/2
# ===========================================================================

def get_stock_summary(
    conn: MySQLConnection,
    reference_date: date,
    deadline: date,
) -> dict[str, Any]:
    """
    Calcula el estado de stock por número de parte entre reference_date y deadline.

    Lógica en tres pasos:
      1. Suma unidades planificadas en production_plan para el rango de fechas.
      2. Obtiene el target semanal desde la última orden en schedule.
      3. Compara planned vs target y asigna status:
            ok       → coverage >= 100 %
            warning  → coverage >= 50 %
            critical → coverage <  50 %

    Usado por: GET /api/planning/stock?date=YYYY-MM-DD
    """

    # ── Paso 1: unidades planificadas por parte en el rango ─────────────
    sql_planned = """
        SELECT
            Part_No       AS part_number,
            SUM(Quantity) AS planned_qty
        FROM  production_plan
        WHERE Date >= %s
          AND Date <= %s
        GROUP BY Part_No
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql_planned, (reference_date.isoformat(), deadline.isoformat()))
    planned_rows = cursor.fetchall()
    cursor.close()

    planned_map: dict[str, int] = {
        row["part_number"]: int(row["planned_qty"] or 0)
        for row in planned_rows
    }

    # ── Paso 2: target semanal por parte ──────────────────────────────
    # Se toma la cantidad de la orden activa más reciente hasta el deadline.
    # Subconsulta correlacionada para obtener el MAX(Date) por parte.
    sql_target = """
        SELECT
            s.Part_No  AS part_number,
            s.Quantity AS target_qty
        FROM  schedule s
        INNER JOIN (
            SELECT Part_No, MAX(Date) AS last_date
            FROM   schedule
            WHERE  Date <= %s
            GROUP  BY Part_No
        ) latest ON latest.Part_No = s.Part_No
                 AND latest.last_date = s.Date
    """
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql_target, (deadline.isoformat(),))
    target_rows = cursor.fetchall()
    cursor.close()

    target_map: dict[str, int] = {
        row["part_number"]: int(row["target_qty"] or 0)
        for row in target_rows
    }

    # ── Paso 3: combinar y calcular status ───────────────────────────
    all_parts = set(planned_map.keys()) | set(target_map.keys())

    parts = []
    for part in sorted(all_parts):
        planned = planned_map.get(part, 0)
        target  = target_map.get(part, 0)

        if target > 0:
            coverage_pct = round(planned / target * 100, 1)
        else:
            # Sin orden de referencia: si hay producción planeada → ok
            coverage_pct = 100.0 if planned > 0 else 0.0

        if coverage_pct >= 100:
            status = "ok"
        elif coverage_pct >= 50:
            status = "warning"
        else:
            status = "critical"

        parts.append({
            "part_number":  part,
            "planned_qty":  planned,
            "target_qty":   target,
            "coverage_pct": coverage_pct,
            "status":       status,
        })

    logger.info(
        "get_stock_summary | ref=%s deadline=%s partes=%d",
        reference_date, deadline, len(parts),
    )

    return {
        "reference_date": reference_date.isoformat(),
        "deadline":       deadline.isoformat(),
        "parts":          parts,
    }


# ===========================================================================
# Helpers internos
# ===========================================================================

def _fmt_time(value: Any) -> str | None:
    """
    Convierte un valor TIME de MySQL a string "HH:MM:SS".

    MySQL connector devuelve columnas TIME como objetos timedelta (no time),
    así que no podemos usar .strftime(). Calculamos la conversión manual.
    Devuelve None si el valor es None.
    """
    if value is None:
        return None

    # Si ya es string, devolverlo directo
    if isinstance(value, str):
        return value

    # timedelta: total_seconds() -> horas, minutos, segundos
    try:
        total_secs = int(value.total_seconds())
        hours, remainder = divmod(total_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except AttributeError:
        return str(value)