"""Data loading layer for scheduler domain objects.

This module is framework-agnostic and returns only Python dataclasses.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

import mysql.connector

from .models import CycleTime, Machine, Order, Part, PlanningData, Route, Shift

Connection = Any


class RepositoryDataError(RuntimeError):
    """Raised when source data is missing, inconsistent, or unreadable."""


def _query(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        return rows if rows is not None else []
    except mysql.connector.Error as exc:
        raise RepositoryDataError(f"Database query failed: {exc}") from exc
    finally:
        cursor.close()


def _to_minutes(value: Any, field_name: str) -> int:
    if isinstance(value, timedelta):
        return int(value.total_seconds() // 60)
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) >= 2:
            hours = int(parts[0])
            minutes = int(parts[1])
            return hours * 60 + minutes
    raise RepositoryDataError(f"Invalid time value for {field_name}: {value!r}")


def _to_date(value: Any, field_name: str) -> date:
    if isinstance(value, date):
        return value
    raise RepositoryDataError(f"Invalid date value for {field_name}: {value!r}")


def _load_machines(conn: Connection, only_active: bool) -> dict[int, Machine]:
    sql = """
        SELECT id, Machine, Area, proceso, tonelaje_ton, activa
        FROM machines
    """
    params: tuple[Any, ...] = ()
    if only_active:
        sql += " WHERE activa = %s"
        params = (1,)

    rows = _query(conn, sql, params)
    machines: dict[int, Machine] = {}

    for row in rows:
        machine_id = int(row["id"])
        machines[machine_id] = Machine(
            id=machine_id,
            name=str(row.get("Machine") or ""),
            area=str(row.get("Area") or ""),
            process_name=str(row.get("proceso") or ""),
            tonnage_ton=float(row["tonelaje_ton"]) if row.get("tonelaje_ton") is not None else None,
            active=bool(row.get("activa", True)),
        )

    return machines


def _load_parts(conn: Connection, only_active: bool) -> dict[str, Part]:
    sql = """
        SELECT Part_No, Customer, Project, Workcenter, SPM_Plan, peso_kg, activo
        FROM part_prod
    """
    params: tuple[Any, ...] = ()
    if only_active:
        sql += " WHERE activo = %s"
        params = (1,)

    rows = _query(conn, sql, params)
    parts: dict[str, Part] = {}

    for row in rows:
        part_number = str(row["Part_No"])
        parts[part_number] = Part(
            part_number=part_number,
            customer=str(row.get("Customer") or ""),
            project=str(row.get("Project") or ""),
            workcenter=str(row.get("Workcenter") or ""),
            spm_plan=float(row["SPM_Plan"]) if row.get("SPM_Plan") is not None else 0.0,
            weight_kg=float(row["peso_kg"]) if row.get("peso_kg") is not None else None,
            active=bool(row.get("activo", True)),
        )

    return parts


def _load_routes(conn: Connection) -> tuple[dict[str, list[Route]], dict[int, Route]]:
    route_rows = _query(
        conn,
        """
        SELECT id, part_number, step_order, process_name, machine_id, setup_time_min
        FROM rutas
        ORDER BY part_number, step_order, id
        """,
    )
    alt_rows = _query(
        conn,
        """
        SELECT ruta_id, machine_id, es_preferida
        FROM rutas_maquinas
        """,
    )

    alternatives_by_route: dict[int, list[int]] = {}
    preferred_by_route: dict[int, list[int]] = {}
    for row in alt_rows:
        route_id = int(row["ruta_id"])
        machine_id = int(row["machine_id"])
        alternatives_by_route.setdefault(route_id, []).append(machine_id)
        if bool(row.get("es_preferida", False)):
            preferred_by_route.setdefault(route_id, []).append(machine_id)

    routes_by_part: dict[str, list[Route]] = {}
    routes_by_id: dict[int, Route] = {}
    for row in route_rows:
        route_id = int(row["id"])
        route = Route(
            id=route_id,
            part_number=str(row["part_number"]),
            step_order=int(row["step_order"]),
            process_name=str(row.get("process_name") or ""),
            machine_id=int(row["machine_id"]),
            setup_time_min=int(row.get("setup_time_min") or 0),
            alternative_machine_ids=alternatives_by_route.get(route_id, []),
            preferred_machine_ids=preferred_by_route.get(route_id, []),
        )
        routes_by_part.setdefault(route.part_number, []).append(route)
        routes_by_id[route_id] = route

    return routes_by_part, routes_by_id


def _load_cycle_times(conn: Connection) -> dict[tuple[str, int], CycleTime]:
    rows = _query(
        conn,
        """
        SELECT part_number, machine_id, cycle_time_min
        FROM tiempos_ciclo
        """,
    )

    cycle_times: dict[tuple[str, int], CycleTime] = {}
    for row in rows:
        part_number = str(row["part_number"])
        machine_id = int(row["machine_id"])
        key = (part_number, machine_id)
        cycle_times[key] = CycleTime(
            part_number=part_number,
            machine_id=machine_id,
            cycle_time_min=float(row["cycle_time_min"]),
        )

    return cycle_times


def _load_orders(conn: Connection, order_status: str) -> list[Order]:
    rows = _query(
        conn,
        """
        SELECT id, part_number, cliente, quantity, due_date, priority, status
        FROM ordenes
        WHERE status = %s
        ORDER BY priority ASC, due_date ASC, id ASC
        """,
        (order_status,),
    )

    orders: list[Order] = []
    for row in rows:
        orders.append(
            Order(
                id=int(row["id"]),
                part_number=str(row["part_number"]),
                customer=str(row.get("cliente") or ""),
                quantity=int(row["quantity"]),
                due_date=_to_date(row["due_date"], "ordenes.due_date"),
                priority=int(row.get("priority") or 0),
                status=str(row.get("status") or ""),
            )
        )

    return orders


def _load_shifts(conn: Connection, only_active: bool) -> list[Shift]:
    sql = """
        SELECT id, nombre, hora_inicio, hora_fin, activo
        FROM turnos
    """
    params: tuple[Any, ...] = ()
    if only_active:
        sql += " WHERE activo = %s"
        params = (1,)
    sql += " ORDER BY id"

    rows = _query(conn, sql, params)

    shifts: list[Shift] = []
    for row in rows:
        start_min = _to_minutes(row["hora_inicio"], "turnos.hora_inicio")
        end_min = _to_minutes(row["hora_fin"], "turnos.hora_fin")
        if end_min <= start_min:
            raise RepositoryDataError(
                f"Shift crosses midnight or is invalid: shift_id={row['id']}, start={start_min}, end={end_min}"
            )

        shifts.append(
            Shift(
                id=int(row["id"]),
                name=str(row.get("nombre") or ""),
                start_min=start_min,
                end_min=end_min,
                active=bool(row.get("activo", True)),
            )
        )

    return shifts


def _validate_integrity(
    *,
    machines: dict[int, Machine],
    parts: dict[str, Part],
    routes_by_part: dict[str, list[Route]],
    cycle_times: dict[tuple[str, int], CycleTime],
    orders: list[Order],
) -> None:
    for order in orders:
        if order.part_number not in parts:
            raise RepositoryDataError(
                f"Order {order.id} references missing part_number '{order.part_number}'."
            )

    for part_number, routes in routes_by_part.items():
        for route in routes:
            for machine_id in route.eligible_machine_ids:
                if machine_id not in machines:
                    raise RepositoryDataError(
                        f"Route {route.id} references unknown machine_id {machine_id}."
                    )
                if (part_number, machine_id) not in cycle_times:
                    raise RepositoryDataError(
                        f"Missing cycle time for part '{part_number}' on machine_id {machine_id}."
                    )

    part_numbers_with_orders = {order.part_number for order in orders}
    for part_number in part_numbers_with_orders:
        if not routes_by_part.get(part_number):
            raise RepositoryDataError(
                f"Part '{part_number}' has orders but no routes defined in rutas."
            )


def load_planning_data(
    conn: Connection,
    *,
    order_status: str = "pending",
    only_active: bool = True,
) -> PlanningData:
    """Load all scheduler input data from MySQL into domain dataclasses."""
    machines = _load_machines(conn, only_active=only_active)
    parts = _load_parts(conn, only_active=only_active)
    routes_by_part, _routes_by_id = _load_routes(conn)
    cycle_times = _load_cycle_times(conn)
    orders = _load_orders(conn, order_status=order_status)
    shifts = _load_shifts(conn, only_active=only_active)

    _validate_integrity(
        machines=machines,
        parts=parts,
        routes_by_part=routes_by_part,
        cycle_times=cycle_times,
        orders=orders,
    )

    return PlanningData(
        machines=machines,
        parts=parts,
        routes_by_part=routes_by_part,
        cycle_times=cycle_times,
        orders=orders,
        shifts=shifts,
    )

