"""
scheduler_pkg/scheduler.py — Motor de optimización CP-SAT.

RESPONSABILIDAD ÚNICA: recibe datos Python puros (dataclasses),
construye y resuelve el modelo CP-SAT, devuelve un ScheduleResult.

No importa Flask. No importa mysql-connector. No abre conexiones.
No lee archivos. Solo matematica de optimización.

CONCEPTOS CLAVE DEL MODELO:
────────────────────────────
Un Job Shop Scheduler resuelve el problema de asignar tareas a
máquinas con restricciones de:
  1. Precedencia    — el paso 2 no puede empezar antes de que termine el paso 1
  2. No-solapamiento — una máquina solo puede hacer una tarea a la vez
  3. Horizonte      — todo debe caber dentro de los días planificados
  4. Capacidad turno — las máquinas solo trabajan dentro de los turnos activos

CP-SAT usa variables enteras (minutos) y propagación de restricciones.
No necesita derivadas ni valores continuos. Es exacto, no heurístico.
"""

from __future__ import annotations

import collections
from typing import Optional
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .models import (
    Machine, Part, Order, RouteStep, Shift,
    ScheduledTask, ScheduleResult,
)
from .config import SolverConfig, ShiftConfig


# ══════════════════════════════════════════════════════════════════
# CONSTANTES DE PROCESO
# Definen el orden canónico de los 4 procesos.
# Si una parte "se salta" un proceso, simplemente no tendrá
# RouteStep para ese proceso — no es un error.
# ══════════════════════════════════════════════════════════════════

PROCESS_ORDER = {
    "prensa":     1,
    "soldadura":  2,
    "ensamble":   3,
    "inspeccion": 4,
}

# Mapa workcenter → proceso (para el fallback cuando rutas está vacía)
WORKCENTER_TO_PROCESS = {
    "MPR": "prensa",
    "PRENSA": "prensa",
    "MSW": "soldadura",
    "MAW": "soldadura",
    "SOLD": "soldadura",
    "MESA": "ensamble",
    "ENSAM": "ensamble",
    "PR-5": "inspeccion",
    "INSP": "inspeccion",
    "SHOKI": "inspeccion",
}


# ══════════════════════════════════════════════════════════════════
# STEP 1 — INFERENCIA DE RUTAS (fallback cuando rutas está vacía)
# ══════════════════════════════════════════════════════════════════

def _infer_process_from_workcenter(workcenter: str) -> Optional[str]:
    """
    Detecta el proceso de una parte a partir de su workcenter.
    Busca prefijos conocidos en el nombre del workcenter.
    """
    wc = workcenter.upper()
    for prefix, proceso in WORKCENTER_TO_PROCESS.items():
        if prefix in wc:
            return proceso
    return None


def _build_implicit_route(
    part: Part,
    machines: dict[int, Machine],
) -> list[RouteStep]:
    """
    DECISIÓN ARQUITECTÓNICA: Opción B — ruta completa inferida.

    Cuando una parte no tiene filas en `rutas`, construimos su ruta
    completa usando dos reglas:

    Regla 1 — Proceso terminal del workcenter:
        El `Workcenter` de la parte es el ÚLTIMO proceso que realiza.
        Ejemplo: si Workcenter = 'MSW-02' (soldadura), la parte
        termina en soldadura.

    Regla 2 — Cadena de prerrequisitos:
        Aplicamos la cadena canónica hacia atrás desde el proceso terminal:
        - inspeccion  ← ensamble ← soldadura ← prensa
        - Pero si el proceso terminal es soldadura, la parte no pasa
          por ensamble ni inspeccion en esta inferencia.
        - Excepción: inspección siempre se infiere como paso ADICIONAL
          al final si el workcenter NO es ya inspección (ya que toda
          parte terminada se inspecciona — esto es configurable).

    Regla 3 — Máquina del step:
        Para el paso del workcenter usamos la máquina que coincide
        con el nombre exacto. Para pasos previos inferidos, tomamos
        la primera máquina activa de ese proceso.

    Por qué este diseño:
        - Funciona HOY con los datos que ya existen en BD
        - Cuando se pueble `rutas`, el scheduler lo usa automáticamente
          sin cambiar nada aquí — simplemente este fallback nunca se llama
        - Los pasos inferidos tienen setup_time_min = 0 (conservador)
    """
    terminal_process = _infer_process_from_workcenter(part.workcenter)
    if not terminal_process:
        return []

    terminal_order = PROCESS_ORDER.get(terminal_process, 1)

    # Construimos la secuencia desde prensa hasta el proceso terminal
    processes_needed = [
        proc for proc, order in sorted(PROCESS_ORDER.items(), key=lambda x: x[1])
        if order <= terminal_order
    ]

    # Indexar máquinas por proceso para búsqueda rápida
    machines_by_process: dict[str, list[Machine]] = collections.defaultdict(list)
    for m in machines.values():
        if m.activa:
            machines_by_process[m.proceso].append(m)

    # Encontrar la máquina del workcenter (máquina principal)
    workcenter_machine: Optional[Machine] = None
    for m in machines.values():
        if m.name.upper() == part.workcenter.upper() and m.activa:
            workcenter_machine = m
            break

    steps: list[RouteStep] = []
    fake_id = -1  # IDs negativos = inferidos (no vienen de BD)

    for step_num, proc in enumerate(processes_needed, start=1):
        # Para el proceso terminal, intentamos usar la máquina del workcenter
        if proc == terminal_process and workcenter_machine:
            machine_id = workcenter_machine.id
        else:
            # Para procesos previos, tomamos la primera máquina activa disponible
            available = machines_by_process.get(proc, [])
            if not available:
                continue  # No hay máquinas para este proceso, saltamos el paso
            machine_id = available[0].id

        steps.append(RouteStep(
            id=fake_id,
            part_number=part.part_number,
            step_order=step_num,
            process_name=proc,
            machine_id=machine_id,
            setup_time_min=0,
            alt_machine_ids=[],
        ))
        fake_id -= 1

    return steps


def build_routes(
    parts: dict[str, Part],
    machines: dict[int, Machine],
) -> dict[str, list[RouteStep]]:
    """
    Para cada parte, devuelve su lista de pasos ordenados.

    Lógica de prioridad:
      1. Si la parte ya tiene route_steps (cargados desde BD) → úsalos
      2. Si no → inferir con _build_implicit_route

    Esta función es el punto de entrada único para obtener rutas.
    El scheduler.py nunca llama a _build_implicit_route directamente.
    """
    routes: dict[str, list[RouteStep]] = {}

    for pn, part in parts.items():
        if part.route_steps:
            # Tenemos ruta real en BD — ordenar y usar
            routes[pn] = sorted(part.route_steps, key=lambda s: s.step_order)
        else:
            # Fallback: inferir ruta desde workcenter
            inferred = _build_implicit_route(part, machines)
            if inferred:
                routes[pn] = inferred
            # Si no podemos inferir nada, la parte no se programa

    return routes


# ══════════════════════════════════════════════════════════════════
# STEP 2 — VENTANAS DE DISPONIBILIDAD DE MÁQUINAS
# Las máquinas solo trabajan durante los turnos. El solver necesita
# saber qué minutos del horizonte están disponibles.
# ══════════════════════════════════════════════════════════════════

def _build_availability_windows(
    shifts: list[Shift],
    horizon_minutes: int,
) -> list[tuple[int, int]]:
    """
    Construye los intervalos [inicio, fin) en los que las máquinas
    pueden trabajar, proyectados sobre todo el horizonte (7 días).

    Ejemplo con 2 turnos (Turno1: 360-870, Turno2: 690-1320):
    Día 0: [(360,870), (690,1320)]   → con traslape, cubre 360-1320
    Día 1: [(1800,2310), (2130,2760)] → +1440 min (1 día)
    ...y así hasta horizon_minutes.

    Por qué listas de tuplas y no un conjunto de minutos individuales:
    CP-SAT trabaja con IntervalVar, que necesita start/end, no conjuntos.
    Una lista de ventanas es mucho más eficiente en memoria.
    """
    minutes_per_day = 24 * 60
    windows: list[tuple[int, int]] = []

    days = horizon_minutes // minutes_per_day + 1
    for day in range(days):
        offset = day * minutes_per_day
        for shift in shifts:
            if not shift.activo:
                continue
            start = offset + shift.start_min
            end = offset + shift.end_min
            if start >= horizon_minutes:
                break
            end = min(end, horizon_minutes)
            windows.append((start, end))

    # Fusionar ventanas solapadas (el traslape de turnos genera solapamiento)
    return _merge_windows(windows)


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Fusiona intervalos solapados en una lista ordenada."""
    if not windows:
        return []
    sorted_w = sorted(windows)
    merged = [sorted_w[0]]
    for start, end in sorted_w[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _total_available_minutes(windows: list[tuple[int, int]]) -> int:
    """Minutos totales disponibles en el horizonte."""
    return sum(end - start for start, end in windows)


# ══════════════════════════════════════════════════════════════════
# STEP 3 — CONSTRUCCIÓN DEL MODELO CP-SAT
# ══════════════════════════════════════════════════════════════════

@dataclass
class _TaskVar:
    """
    Variables CP-SAT para una tarea (orden × paso × máquina candidata).

    Por qué una dataclass interna:
    El modelo CP-SAT genera decenas de variables por tarea. Agruparlas
    en un objeto con nombres claros evita bugs de indexación con tuples.

    interval    : IntervalVar que el solver usa para no-solapamiento
    start       : IntVar — minuto de inicio
    end         : IntVar — minuto de fin
    is_active   : BoolVar — ¿está seleccionada esta máquina para este paso?
                  (Solo una máquina candidata puede ser True por paso)
    duration    : duración fija en minutos (setup + ciclo × cantidad)
    order_id    : referencia a la orden
    step_order  : número de paso en la ruta
    machine_id  : máquina candidata para esta combinación
    """
    interval: cp_model.IntervalVar
    start: cp_model.IntVar
    end: cp_model.IntVar
    is_active: cp_model.BoolVar
    duration: int
    order_id: int
    part_number: str
    step_order: int
    process_name: str
    machine_id: int


def _compute_duration(
    step: RouteStep,
    order: Order,
    parts: dict[str, Part],
    machine_id: int,
    fallback_spm: float = 8.0,
) -> int:
    """
    Calcula la duración total de una tarea en minutos enteros.

    Duración = setup_time_min + ceil(quantity × cycle_time_min)

    cycle_time_min se obtiene de tiempos_ciclo (BD).
    Si no existe para esa máquina específica, buscamos en cualquier
    máquina del mismo proceso, o calculamos desde SPM_Plan.

    Por qué ceil y no round:
    Es más seguro sobreestimar (el plan tiene holgura) que subestimar
    (el plan no cierra). En producción, los tiempos reales varían.
    """
    import math

    part = parts.get(order.part_number)
    if not part:
        # Parte desconocida — estimación conservadora
        return step.setup_time_min + math.ceil(order.quantity / fallback_spm)

    # 1. Buscar tiempo de ciclo exacto para esta máquina
    ct = part.cycle_times.get(machine_id)
    if ct:
        cycle_min = ct.cycle_time_min
    else:
        # 2. Buscar en cualquier máquina disponible
        if part.cycle_times:
            cycle_min = next(iter(part.cycle_times.values())).cycle_time_min
        elif part.spm_plan and part.spm_plan > 0:
            # 3. Calcular desde SPM_Plan
            cycle_min = 1.0 / part.spm_plan
        else:
            cycle_min = 1.0 / fallback_spm

    duration = step.setup_time_min + math.ceil(order.quantity * cycle_min)
    return max(1, duration)  # mínimo 1 minuto


def build_model(
    orders: list[Order],
    parts: dict[str, Part],
    machines: dict[int, Machine],
    routes: dict[str, list[RouteStep]],
    availability: list[tuple[int, int]],
    horizon: int,
    solver_config: SolverConfig,
) -> tuple[cp_model.CpModel, dict]:
    """
    Construye el modelo CP-SAT completo.

    Retorna (model, task_vars) donde task_vars es un dict indexado por
    (order_id, step_order, machine_id) → _TaskVar.

    RESTRICCIONES IMPLEMENTADAS:
    ─────────────────────────────
    R1. No-solapamiento por máquina (AddNoOverlap)
        Una máquina no puede procesar dos tareas al mismo tiempo.
        Esta es la restricción central del Job Shop.

    R2. Precedencia de pasos (modelo lineal: end[paso N] ≤ start[paso N+1])
        El paso 2 no puede empezar hasta que el paso 1 termine.
        Con máquinas alternativas, se toma el end de la tarea activa.

    R3. Exactamente una máquina por paso (AddExactlyOne)
        De las máquinas elegibles para un paso, exactamente una se activa.
        Esto resuelve el problema de máquinas alternativas.

    R4. Horizonte (implícito en IntervalVar)
        Todas las variables start y end están acotadas [0, horizon].

    FUNCIÓN OBJETIVO:
    ─────────────────
    Minimizar la tardanza ponderada:
        sum(priority[i] × max(0, end[last_step_i] - due_date_min[i]))

    Una orden de prioridad 1 (alta) penaliza 3× más que una de prioridad 3.
    Esto dirige el solver a terminar primero las órdenes más urgentes.

    Por qué CP-SAT y no un solver LP/MIP clásico:
    - CP-SAT maneja naturalmente variables booleanas (selección de máquina)
    - La propagación de restricciones reduce el espacio de búsqueda
      drásticamente comparado con branch-and-bound puro
    - OR-Tools CP-SAT es uno de los mejores solvers para Job Shop en benchmarks
    """
    model = cp_model.CpModel()
    task_vars: dict[tuple, _TaskVar] = {}

    # Agrupaciones para AddNoOverlap (una lista de intervals por máquina)
    machine_intervals: dict[int, list[cp_model.IntervalVar]] = collections.defaultdict(list)

    # Para la función objetivo: capturamos el end del último paso de cada orden
    order_end_vars: dict[int, list[cp_model.IntVar]] = collections.defaultdict(list)

    # ── Calcular due_date en minutos desde el horizonte ──────────
    # El horizonte empieza "ahora" (minuto 0). La due_date relativa
    # es la diferencia en minutos entre due_date y la fecha de inicio del plan.
    # Como no tenemos fecha de inicio explícita aquí, usamos horizon como máximo.
    from datetime import date
    today = date.today()

    def due_date_to_minutes(due: date) -> int:
        delta = (due - today).days
        return max(0, min(delta * 24 * 60, horizon))

    # ── Por cada orden, por cada paso, por cada máquina elegible ──
    for order in orders:
        pn = order.part_number
        steps = routes.get(pn)
        if not steps:
            continue  # No hay ruta para esta parte, se omite

        for step in steps:
            eligible = step.eligible_machines  # [machine_id_principal] + alternativas
            step_task_vars: list[_TaskVar] = []

            for mid in eligible:
                if mid not in machines:
                    continue
                if not machines[mid].activa:
                    continue

                duration = _compute_duration(step, order, parts, mid)

                # Variables de tiempo
                start_var = model.NewIntVar(0, horizon, f"s_{order.id}_{step.step_order}_{mid}")
                end_var   = model.NewIntVar(0, horizon, f"e_{order.id}_{step.step_order}_{mid}")
                active    = model.NewBoolVar(f"a_{order.id}_{step.step_order}_{mid}")

                # IntervalVar opcional — solo "ocupa" la máquina si active=True
                interval = model.NewOptionalIntervalVar(
                    start_var, duration, end_var, active,
                    f"iv_{order.id}_{step.step_order}_{mid}"
                )

                tv = _TaskVar(
                    interval=interval,
                    start=start_var,
                    end=end_var,
                    is_active=active,
                    duration=duration,
                    order_id=order.id,
                    part_number=pn,
                    step_order=step.step_order,
                    process_name=step.process_name,
                    machine_id=mid,
                )
                task_vars[(order.id, step.step_order, mid)] = tv
                step_task_vars.append(tv)
                machine_intervals[mid].append(interval)

            if not step_task_vars:
                continue  # No hay máquinas válidas para este paso

            # ── R3: Exactamente una máquina activa por paso ──────
            model.AddExactlyOne([tv.is_active for tv in step_task_vars])

        # ── R2: Precedencia entre pasos consecutivos ─────────────
        # Para cada par de pasos consecutivos (N, N+1), el end del paso N
        # (en la máquina seleccionada) debe ser ≤ start del paso N+1.
        #
        # Patrón CP-SAT correcto para "valor efectivo de una variable
        # cuando active=True, 0 cuando active=False":
        #
        #   contrib = NewIntVar(0, horizon)
        #   model.Add(contrib == var).OnlyEnforceIf(active)
        #   model.Add(contrib == 0).OnlyEnforceIf(active.Not())
        #   effective_value = sum(contrib_i)  ← suma de IntVars, que sí soporta CP-SAT
        #
        # No se puede hacer tv.end * tv.is_active porque CP-SAT no soporta
        # multiplicación de dos variables (solo IntVar × constante).
        for i in range(len(steps) - 1):
            curr_step = steps[i]
            next_step = steps[i + 1]

            curr_tvs = [
                task_vars[(order.id, curr_step.step_order, mid)]
                for mid in curr_step.eligible_machines
                if (order.id, curr_step.step_order, mid) in task_vars
            ]
            next_tvs = [
                task_vars[(order.id, next_step.step_order, mid)]
                for mid in next_step.eligible_machines
                if (order.id, next_step.step_order, mid) in task_vars
            ]

            if not curr_tvs or not next_tvs:
                continue

            # end efectivo del paso actual: suma de contribuciones condicionales
            end_contribs = []
            for j, tv in enumerate(curr_tvs):
                contrib = model.NewIntVar(0, horizon, f"ec_{order.id}_{i}_{j}")
                model.Add(contrib == tv.end).OnlyEnforceIf(tv.is_active)
                model.Add(contrib == 0).OnlyEnforceIf(tv.is_active.Not())
                end_contribs.append(contrib)
            end_curr = model.NewIntVar(0, horizon, f"end_curr_{order.id}_{i}")
            model.Add(end_curr == sum(end_contribs))

            # start efectivo del siguiente paso
            start_contribs = []
            for j, tv in enumerate(next_tvs):
                contrib = model.NewIntVar(0, horizon, f"sc_{order.id}_{i}_{j}")
                model.Add(contrib == tv.start).OnlyEnforceIf(tv.is_active)
                model.Add(contrib == 0).OnlyEnforceIf(tv.is_active.Not())
                start_contribs.append(contrib)
            start_next = model.NewIntVar(0, horizon, f"start_next_{order.id}_{i}")
            model.Add(start_next == sum(start_contribs))

            # Restricción de precedencia
            model.Add(end_curr <= start_next)

        # Capturar el end del último paso para la función objetivo
        last_step = steps[-1]
        last_tvs = [
            task_vars[(order.id, last_step.step_order, mid)]
            for mid in last_step.eligible_machines
            if (order.id, last_step.step_order, mid) in task_vars
        ]
        if last_tvs:
            last_contribs = []
            for j, tv in enumerate(last_tvs):
                contrib = model.NewIntVar(0, horizon, f"lc_{order.id}_{j}")
                model.Add(contrib == tv.end).OnlyEnforceIf(tv.is_active)
                model.Add(contrib == 0).OnlyEnforceIf(tv.is_active.Not())
                last_contribs.append(contrib)
            end_last = model.NewIntVar(0, horizon, f"end_last_{order.id}")
            model.Add(end_last == sum(last_contribs))
            order_end_vars[order.id].append(end_last)

    # ── R1: No-solapamiento por máquina ──────────────────────────
    for mid, intervals in machine_intervals.items():
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)

    # ── Función objetivo: minimizar tardanza ponderada ───────────
    # Peso de prioridad: prioridad 1 → peso 3, prioridad 2 → peso 2, prioridad 3 → peso 1
    tardiness_terms = []
    for order in orders:
        if order.id not in order_end_vars:
            continue
        due_min = due_date_to_minutes(order.due_date)
        weight = max(1, 4 - order.priority)  # priority 1 → weight 3

        end_var = order_end_vars[order.id][0]

        # Tardanza = max(0, end - due_date_min)
        tardiness = model.NewIntVar(0, horizon, f"tard_{order.id}")
        model.AddMaxEquality(tardiness, [end_var - due_min, model.NewConstant(0)])

        tardiness_terms.append(weight * tardiness)

    if tardiness_terms:
        model.Minimize(sum(tardiness_terms))
    else:
        # Sin órdenes con fechas límite: minimizar makespan
        all_ends = [
            tv.end for tv in task_vars.values()
        ]
        if all_ends:
            makespan = model.NewIntVar(0, horizon, "makespan")
            model.AddMaxEquality(makespan, all_ends)
            model.Minimize(makespan)

    return model, task_vars


# ══════════════════════════════════════════════════════════════════
# STEP 4 — EXTRACCIÓN DE RESULTADOS
# ══════════════════════════════════════════════════════════════════

def _extract_results(
    solver: cp_model.CpSolver,
    solve_status,
    task_vars: dict[tuple, _TaskVar],
    orders: list[Order],
    machines: dict[int, Machine],
    wall_time: float,
) -> ScheduleResult:
    """
    Extrae las tareas programadas del solver y las convierte a
    objetos ScheduledTask.

    Solo extrae las tareas donde is_active = True (la máquina fue
    seleccionada por el solver). El resto de combinaciones
    (order × step × máquina no seleccionada) se ignoran.
    """
    status_map = {
        cp_model.OPTIMAL:    "OPTIMAL",
        cp_model.FEASIBLE:   "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN:    "UNKNOWN",
    }
    # solver.Solve() retorna el status; se pasa aquí como parámetro
    status_str = status_map.get(solve_status, "UNKNOWN")

    tasks: list[ScheduledTask] = []
    scheduled_order_ids: set[int] = set()

    for (order_id, step_order, machine_id), tv in task_vars.items():
        if not solver.BooleanValue(tv.is_active):
            continue  # Esta combinación no fue seleccionada

        machine = machines.get(machine_id)
        machine_name = machine.name if machine else f"M{machine_id}"

        task = ScheduledTask(
            order_id=order_id,
            part_number=tv.part_number,
            step_order=step_order,
            process_name=tv.process_name,
            machine_id=machine_id,
            machine_name=machine_name,
            start_min=solver.Value(tv.start),
            end_min=solver.Value(tv.end),
            quantity=0,  # Se llena abajo
        )
        tasks.append(task)
        scheduled_order_ids.add(order_id)

    # Llenar las cantidades desde las órdenes originales
    order_qty = {o.id: o.quantity for o in orders}
    for task in tasks:
        task.quantity = order_qty.get(task.order_id, 0)

    # Ordenar por start_min para el Gantt
    tasks.sort(key=lambda t: (t.start_min, t.machine_id))

    # Makespan = fin de la última tarea
    makespan = max((t.end_min for t in tasks), default=0)

    # Órdenes no programadas
    all_order_ids = {o.id for o in orders}
    unscheduled = sorted(all_order_ids - scheduled_order_ids)

    return ScheduleResult(
        solver_status=status_str,
        makespan_min=makespan,
        tasks=tasks,
        unscheduled_order_ids=unscheduled,
        wall_time_seconds=wall_time,
    )


# ══════════════════════════════════════════════════════════════════
# STEP 5 — FUNCIÓN PÚBLICA PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def solve(
    orders: list[Order],
    parts: dict[str, Part],
    machines: dict[int, Machine],
    shifts: list[Shift],
    solver_config: Optional[SolverConfig] = None,
    shift_config: Optional[ShiftConfig] = None,
) -> ScheduleResult:
    """
    Punto de entrada público del scheduler.

    Pasos internos:
    1. Construir rutas (reales desde BD, o inferidas desde workcenter)
    2. Construir ventanas de disponibilidad desde los turnos
    3. Construir el modelo CP-SAT
    4. Configurar y lanzar el solver
    5. Extraer y devolver resultados

    Parámetros:
        orders  : lista de órdenes a programar
        parts   : dict de partes con sus metadatos y tiempos de ciclo
        machines: dict de máquinas activas
        shifts  : lista de turnos activos
        solver_config: parámetros del solver (tiempo límite, etc.)
        shift_config : parámetros de turno (usado solo si shifts está vacío)

    Retorna:
        ScheduleResult con tareas, status y makespan.
        Si no hay órdenes o no hay rutas posibles, devuelve resultado vacío.
    """
    if solver_config is None:
        from ..config import solver_config as default_sc
        solver_config = default_sc

    # Filtrar órdenes sin partes conocidas
    valid_orders = [o for o in orders if o.part_number in parts]
    if not valid_orders:
        return ScheduleResult(
            solver_status="INFEASIBLE",
            makespan_min=0,
            unscheduled_order_ids=[o.id for o in orders],
        )

    # ── Paso 1: rutas ────────────────────────────────────────────
    routes = build_routes(parts, machines)

    # Filtrar órdenes cuya parte no tiene ruta
    schedulable = [o for o in valid_orders if o.part_number in routes]
    unschedulable = [o for o in valid_orders if o.part_number not in routes]

    if not schedulable:
        return ScheduleResult(
            solver_status="INFEASIBLE",
            makespan_min=0,
            unscheduled_order_ids=[o.id for o in orders],
        )

    # ── Paso 2: ventanas de disponibilidad ──────────────────────
    active_shifts = [s for s in shifts if s.activo]
    if not active_shifts:
        # Sin turnos en BD, usar los defaults del config
        sc = shift_config or ShiftConfig()
        from .models import Shift as ShiftModel
        active_shifts = [
            ShiftModel(id=1, name="Turno 1", start_min=sc.shift1_start_min, end_min=sc.shift1_end_min),
            ShiftModel(id=2, name="Turno 2", start_min=sc.shift2_start_min, end_min=sc.shift2_end_min),
        ]

    availability = _build_availability_windows(active_shifts, solver_config.horizon_minutes)
    horizon = solver_config.horizon_minutes

    # ── Paso 3: modelo ───────────────────────────────────────────
    model, task_vars = build_model(
        orders=schedulable,
        parts=parts,
        machines=machines,
        routes=routes,
        availability=availability,
        horizon=horizon,
        solver_config=solver_config,
    )

    # ── Paso 4: solver ───────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = solver_config.time_limit_seconds
    solver.parameters.num_search_workers  = solver_config.num_workers
    solver.parameters.log_search_progress = False  # True para debug

    solve_status = solver.Solve(model)

    # ── Paso 5: resultados ───────────────────────────────────────
    result = _extract_results(
        solver=solver,
        solve_status=solve_status,
        task_vars=task_vars,
        orders=schedulable,
        machines=machines,
        wall_time=solver.WallTime(),
    )

    # Agregar las órdenes no programables (sin ruta) a la lista de no programadas
    result.unscheduled_order_ids.extend([o.id for o in unschedulable])

    return result