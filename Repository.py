"""
scheduler_pkg/repository.py — Capa de acceso a datos.

PRINCIPIOS DE DISEÑO:
1. No importa Flask. Solo mysql-connector y los modelos propios.
2. Recibe una conexión MySQL como parámetro (inyección de dependencias).
   Esto permite:
   - Pruebas unitarias con una conexión mock
   - Reutilización en contextos no-Flask (scripts, tests, workers)
   - Fácil swap de MySQL por SQLite en pruebas
3. Devuelve SOLO objetos Python (dataclasses). Nunca tuples ni dicts crudos.
4. Cada función tiene una sola responsabilidad.
"""

from __future__ import annotations
from typing import Any
import mysql.connector

from .models import (
    Machine, Shift, Part, RouteStep, CycleTime, Order
)


# ── Tipo interno para la conexión ────────────────────────────────
# mysql.connector.connection.MySQLConnection no tiene stubs de mypy,
# usamos Any para evitar ruido en el type checker sin perder claridad.
Connection = Any


# ══════════════════════════════════════════════════════════════════
# HELPERS INTERNOS
# ══════════════════════════════════════════════════════════════════

def _query(conn: Connection, sql: str, params: tuple = ()) -> list[dict]:
    """
    Ejecuta una consulta y devuelve filas como lista de dicts.

    Usamos dictionary=True para acceder a los campos por nombre,
    no por índice. Esto hace el código más robusto ante cambios
    en el orden de columnas.
    """
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()


# ══════════════════════════════════════════════════════════════════
# MÁQUINAS
# ══════════════════════════════════════════════════════════════════

def load_machines(conn: Connection, only_active: bool = True) -> dict[int, Machine]:
    """
    Carga todas las máquinas activas de la planta.

    Retorna un dict indexado por machine.id para acceso O(1) desde
    el scheduler (que necesita buscar máquinas por id frecuentemente).

    La tabla `machines` en la BD tiene: id, Machine, Area.
    El Excel también muestra: proceso, tonelaje.
    Adaptamos la query según lo que esté disponible.

    Nota: si la columna `activa` no existe todavía en tu BD,
    se puede comentar el filtro `AND activa = 1` temporalmente.
    """
    sql = """
        SELECT id, Machine, Area
        FROM machines
    """
    # Si la BD ya tiene columna `activa`, agregar: WHERE activa = 1
    rows = _query(conn, sql)

    machines: dict[int, Machine] = {}
    for row in rows:
        # Inferimos el proceso desde el área (la BD actual no tiene columna proceso)
        area = (row["Area"] or "").upper()
        proceso = _infer_proceso(area, row["Machine"])

        machines[row["id"]] = Machine(
            id=row["id"],
            name=row["Machine"],
            area=area,
            proceso=proceso,
            tonelaje=None,  # se puede enriquecer si se agrega columna tonelaje_ton
            activa=True,
        )

    return machines


def _infer_proceso(area: str, machine_name: str) -> str:
    """
    Infiere el proceso a partir del área/nombre de máquina.

    La BD actual usa nombres como MPR-01 (prensa), MSW-02 (soldadura),
    MAW-01 (soldadura MIG/arco), etc.
    """
    name = machine_name.upper()
    if "MPR" in name or "PRENSA" in area:
        return "prensa"
    if "MSW" in name or "MAW" in name or "SOLD" in area:
        return "soldadura"
    if "MESA" in name or "ENSAM" in area:
        return "ensamble"
    if "PR-5" in name or "INSP" in area or "SHOKI" in area:
        return "inspeccion"
    return area.lower() or "desconocido"


# ══════════════════════════════════════════════════════════════════
# TURNOS
# ══════════════════════════════════════════════════════════════════

def load_shifts(conn: Connection) -> list[Shift]:
    """
    Carga los turnos activos desde la tabla `turnos`.

    Si la tabla no existe aún, devuelve los turnos por defecto
    definidos en config.py. Esto facilita el arranque del sistema
    antes de que existan todos los datos en la BD.

    hora_inicio / hora_fin son TIME en MySQL → Python los devuelve
    como timedelta; los convertimos a minutos desde medianoche.
    """
    try:
        rows = _query(conn, "SELECT id, nombre, hora_inicio, hora_fin, activo FROM turnos")
    except mysql.connector.Error:
        # La tabla no existe todavía; usamos defaults
        return _default_shifts()

    if not rows:
        return _default_shifts()

    shifts = []
    for row in rows:
        if not row["activo"]:
            continue
        # hora_inicio es timedelta en mysql-connector
        start_td = row["hora_inicio"]
        end_td = row["hora_fin"]
        shifts.append(Shift(
            id=row["id"],
            name=row["nombre"],
            start_min=int(start_td.total_seconds() // 60),
            end_min=int(end_td.total_seconds() // 60),
            activo=True,
        ))
    return shifts


def _default_shifts() -> list[Shift]:
    """Turnos por defecto basados en el Excel del plan de producción."""
    return [
        Shift(id=1, name="Turno 1", start_min=6*60, end_min=14*60+30, activo=True),
        Shift(id=2, name="Turno 2", start_min=11*60+30, end_min=22*60, activo=True),
    ]


# ══════════════════════════════════════════════════════════════════
# PARTES Y SUS RUTAS
# ══════════════════════════════════════════════════════════════════

def load_parts(conn: Connection) -> dict[str, Part]:
    """
    Carga todos los números de parte activos con sus rutas y tiempos de ciclo.

    Estrategia: 3 consultas separadas (partes, rutas, tiempos de ciclo)
    y luego ensamblaje en Python. Más rápido que un JOIN enorme y
    más fácil de mantener.

    Por qué dict[str, Part]: el scheduler busca partes por part_number
    constantemente. O(1) vs O(n) si fuera lista.
    """
    parts = _load_base_parts(conn)
    _attach_route_steps(conn, parts)
    _attach_cycle_times(conn, parts)
    return parts


def _load_base_parts(conn: Connection) -> dict[str, Part]:
    """
    Carga los atributos base de cada parte desde part_prod.
    """
    sql = """
        SELECT Part_No, Customer, Project, Workcenter, SPM_Plan, Man
        FROM part_prod
    """
    rows = _query(conn, sql)
    parts: dict[str, Part] = {}
    for row in rows:
        pn = row["Part_No"]
        parts[pn] = Part(
            part_number=pn,
            customer=row["Customer"] or "",
            project=row["Project"] or "",
            workcenter=row["Workcenter"] or "",
            spm_plan=float(row["SPM_Plan"] or 0),
            man=int(row["Man"] or 1),
            activo=True,
        )
    return parts


def _attach_route_steps(conn: Connection, parts: dict[str, Part]) -> None:
    """
    Añade los RouteStep a cada Part, incluyendo máquinas alternativas.

    Ejecuta dos queries:
    1. Pasos principales (tabla rutas)
    2. Alternativas (tabla rutas_maquinas) agrupadas por ruta_id

    Decisión: mutar `parts` in-place en lugar de retornar una copia.
    Es eficiente en memoria y el llamador ya sabe que la función modifica.
    """
    # -- Paso 1: rutas principales --
    try:
        route_rows = _query(conn, """
            SELECT id, part_number, step_order, process_name, machine_id, setup_time_min
            FROM rutas
            ORDER BY part_number, step_order
        """)
    except mysql.connector.Error:
        return  # tabla vacía o inexistente

    # -- Paso 2: máquinas alternativas --
    alt_map: dict[int, list[int]] = {}  # ruta_id → [machine_id, ...]
    try:
        alt_rows = _query(conn, "SELECT ruta_id, machine_id FROM rutas_maquinas")
        for row in alt_rows:
            alt_map.setdefault(row["ruta_id"], []).append(row["machine_id"])
    except mysql.connector.Error:
        pass  # tabla opcional

    # -- Ensamblaje --
    for row in route_rows:
        pn = row["part_number"]
        if pn not in parts:
            continue  # ruta huérfana; se ignora

        step = RouteStep(
            id=row["id"],
            part_number=pn,
            step_order=row["step_order"],
            process_name=row["process_name"],
            machine_id=row["machine_id"],
            setup_time_min=row["setup_time_min"],
            alt_machine_ids=alt_map.get(row["id"], []),
        )
        parts[pn].route_steps.append(step)


def _attach_cycle_times(conn: Connection, parts: dict[str, Part]) -> None:
    """
    Añade los CycleTime a cada Part.

    Si la tabla tiempos_ciclo está vacía, se estiman los tiempos
    a partir de SPM_Plan de part_prod:
        cycle_time_min = 1 / SPM_Plan
    Esto es un fallback; el dato real siempre debe venir de la BD.
    """
    try:
        ct_rows = _query(conn, """
            SELECT part_number, machine_id, cycle_time_min
            FROM tiempos_ciclo
        """)
    except mysql.connector.Error:
        ct_rows = []

    if ct_rows:
        for row in ct_rows:
            pn = row["part_number"]
            if pn not in parts:
                continue
            ct = CycleTime(
                part_number=pn,
                machine_id=row["machine_id"],
                cycle_time_min=float(row["cycle_time_min"]),
            )
            parts[pn].cycle_times[row["machine_id"]] = ct
    else:
        # Fallback: estimar desde SPM_Plan
        for pn, part in parts.items():
            if part.spm_plan and part.spm_plan > 0:
                cycle_min = 1.0 / part.spm_plan
                # Asignar a la máquina del workcenter (no tenemos machine_id directo)
                # Usamos machine_id = 0 como placeholder
                parts[pn].cycle_times[0] = CycleTime(
                    part_number=pn,
                    machine_id=0,
                    cycle_time_min=cycle_min,
                )


# ══════════════════════════════════════════════════════════════════
# ÓRDENES DE PRODUCCIÓN
# ══════════════════════════════════════════════════════════════════

def load_orders(conn: Connection, status: str = "pending") -> list[Order]:
    """
    Carga las órdenes de producción pendientes.

    Filtra por status para no incluir órdenes ya completadas.
    El solver solo necesita saber qué falta producir.

    Ordenadas por prioridad ASC (1 = alta primero) y due_date ASC
    para que el solver las vea en el orden más razonable.
    """
    try:
        rows = _query(conn, """
            SELECT id, part_number, cliente, quantity, due_date, priority, status
            FROM ordenes
            WHERE status = %s
            ORDER BY priority ASC, due_date ASC
        """, (status,))
    except mysql.connector.Error:
        return []

    orders = []
    for row in rows:
        orders.append(Order(
            id=row["id"],
            part_number=row["part_number"],
            cliente=row["cliente"] or "",
            quantity=row["quantity"],
            due_date=row["due_date"],
            priority=row["priority"],
            status=row["status"],
        ))
    return orders


# ══════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL — Carga todo de una vez
# ══════════════════════════════════════════════════════════════════

def load_all_data(conn: Connection) -> dict:
    """
    Carga el estado completo de la planta en una sola llamada.

    Retorna un dict con claves estandarizadas que el scheduler.py
    consume directamente. Separarlos así permite:
    - Caché independiente por sección (e.g. las máquinas cambian poco)
    - Testing de cada sección por separado
    - La API puede serializar solo la sección que necesita

    Retorno:
    {
        "machines": dict[int, Machine],
        "shifts":   list[Shift],
        "parts":    dict[str, Part],
        "orders":   list[Order],
    }
    """
    return {
        "machines": load_machines(conn),
        "shifts":   load_shifts(conn),
        "parts":    load_parts(conn),
        "orders":   load_orders(conn),
    }


# ══════════════════════════════════════════════════════════════════
# ESCRITURA — Guardar el plan generado
# ══════════════════════════════════════════════════════════════════

def save_plan(conn: Connection, result_dict: dict) -> int:
    """
    Guarda el resultado del solver en `planes_generados`.

    result_dict debe tener las claves:
      - solver_status: str
      - makespan_min:  int
      - horizon_dias:  int
      - gantt_json:    str (JSON serializado)

    Retorna el id del plan insertado.
    """
    import json

    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO planes_generados
                (status, solver_status, makespan_min, horizonte_dias, gantt_json)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            "completed",
            result_dict["solver_status"],
            result_dict["makespan_min"],
            result_dict["horizon_dias"],
            json.dumps(result_dict["gantt_json"], ensure_ascii=False),
        ))
        conn.commit()
        return cursor.lastrowid
    finally:
        cursor.close()