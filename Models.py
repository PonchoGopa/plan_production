"""
scheduler_pkg/models.py — Modelos de datos del dominio.

PRINCIPIO FUNDAMENTAL: Este archivo no importa Flask ni nada HTTP.
Solo usa la librería estándar de Python (dataclasses, typing, datetime).

Por qué dataclasses en lugar de dicts:
  - Autocompletado en el IDE
  - Validación implícita de tipos
  - Documentación en el código
  - El solver y el repositorio hablan el mismo "idioma" sin necesitar
    que uno sepa cómo funciona el otro.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from datetime import date


# ══════════════════════════════════════════════════════════════════
# CAPA 1 — Recursos físicos de la planta
# ══════════════════════════════════════════════════════════════════

@dataclass
class Machine:
    """
    Representa una máquina física.

    Mapea la tabla `machines(id, Machine, Area, proceso, tonelaje_ton, activa)`.

    id         : PK de la BD; el solver lo usa como identificador único.
    name       : nombre legible (MPR-01, MSW-02, etc.)
    area       : área de la planta (PRENSA, SOLDADURA, ENSAMBLE, INSPECCION)
    proceso    : nombre del proceso que ejecuta
    tonelaje   : relevante solo para prensas; None en otros procesos
    activa     : si está disponible para programación

    Decisión: tonelaje como float opcional, no como int,
    porque algunos registros tienen decimales (ejemplo: 60.5 ton).
    """
    id: int
    name: str
    area: str
    proceso: str
    tonelaje: Optional[float] = None
    activa: bool = True


@dataclass
class Shift:
    """
    Representa un turno de trabajo.
    Mapea la tabla `turnos(id, nombre, hora_inicio, hora_fin, activo)`.

    start_min / end_min : minutos desde medianoche (0 = 00:00, 360 = 06:00).
    Usar enteros en lugar de time objects simplifica enormemente
    la aritmética del solver (que trabaja en unidades enteras).
    """
    id: int
    name: str
    start_min: int   # minutos desde medianoche
    end_min: int
    activo: bool = True

    @property
    def duration_min(self) -> int:
        return self.end_min - self.start_min


# ══════════════════════════════════════════════════════════════════
# CAPA 2 — Definición de productos y sus rutas
# ══════════════════════════════════════════════════════════════════

@dataclass
class RouteStep:
    """
    Un paso dentro de la ruta de fabricación de una parte.

    Mapea una fila de `rutas(id, part_number, step_order, process_name,
                              machine_id, setup_time_min)`.

    step_order     : orden de ejecución (1, 2, 3...). El solver impone
                     que el paso N+1 solo puede comenzar cuando el paso N
                     ha terminado — esta es la restricción de precedencia.
    machine_id     : máquina principal/fija para este paso.
    alt_machine_ids: lista de máquinas alternativas (de rutas_maquinas).
                     El solver puede elegir cualquiera de ellas.
    setup_time_min : tiempo de preparación ADICIONAL antes de procesar
                     (cambio de herramental, ajuste de parámetros, etc.)
    """
    id: int
    part_number: str
    step_order: int
    process_name: str
    machine_id: int
    setup_time_min: int
    alt_machine_ids: list[int] = field(default_factory=list)

    @property
    def eligible_machines(self) -> list[int]:
        """Todas las máquinas que pueden ejecutar este paso."""
        return [self.machine_id] + self.alt_machine_ids


@dataclass
class CycleTime:
    """
    Tiempo de ciclo de una parte en una máquina específica.
    Mapea `tiempos_ciclo(part_number, machine_id, cycle_time_min)`.

    cycle_time_min: minutos por unidad producida.
    Este valor es la unidad básica de duración en el solver.
    Nota: el Excel usa SPM (Strokes Per Minute); la conversión es:
          cycle_time_min = 1 / SPM
    """
    part_number: str
    machine_id: int
    cycle_time_min: float

    def minutes_for(self, quantity: int) -> float:
        """Tiempo total de producción para una cantidad dada."""
        return self.cycle_time_min * quantity


@dataclass
class Part:
    """
    Número de parte con todos sus metadatos de producción.
    Mapea `part_prod(Part_No, Customer, Project, Workcenter, SPM_Plan, ...)`.

    workcenter : máquina/centro de trabajo principal (del Excel/BD actual)
    spm_plan   : velocidad planificada en strokes por minuto
    route_steps: pasos de fabricación ordenados por step_order
    cycle_times: tiempos de ciclo indexados por machine_id
    """
    part_number: str
    customer: str
    project: str
    workcenter: str
    spm_plan: float
    man: int = 1    # operadores necesarios
    activo: bool = True
    route_steps: list[RouteStep] = field(default_factory=list)
    cycle_times: dict[int, CycleTime] = field(default_factory=dict)  # machine_id → CycleTime

    def get_cycle_time(self, machine_id: int) -> Optional[float]:
        """Retorna minutos/unidad para la máquina dada, o None si no existe."""
        ct = self.cycle_times.get(machine_id)
        return ct.cycle_time_min if ct else None

    def sorted_steps(self) -> list[RouteStep]:
        """Pasos ordenados por step_order."""
        return sorted(self.route_steps, key=lambda s: s.step_order)


# ══════════════════════════════════════════════════════════════════
# CAPA 3 — Demanda (órdenes de producción)
# ══════════════════════════════════════════════════════════════════

@dataclass
class Order:
    """
    Orden de producción.
    Mapea `ordenes(id, part_number, cliente, quantity, due_date, priority, status)`.

    due_date : fecha límite de entrega.
    priority : 1 = alta, 2 = media, 3 = baja. El solver usa esto como
               peso en la función objetivo (minimizar tardanza ponderada).
    status   : 'pending' | 'in_progress' | 'completed'

    stock_target : cantidad mínima a tener lista antes de due_date.
                   El requisito "al menos 1 semana de stock" se implementa
                   creando órdenes con due_date = hoy + 7 días.
    """
    id: int
    part_number: str
    cliente: str
    quantity: int
    due_date: date
    priority: int = 2
    status: str = "pending"
    stock_target: int = 0  # si > 0, el solver lo trata como restricción dura


# ══════════════════════════════════════════════════════════════════
# CAPA 4 — Resultado del solver
# ══════════════════════════════════════════════════════════════════

@dataclass
class ScheduledTask:
    """
    Una tarea programada: la decisión concreta del solver.

    order_id    : a qué orden pertenece
    part_number : número de parte
    step_order  : qué paso de la ruta
    machine_id  : en qué máquina (elegida entre las elegibles)
    start_min   : minuto de inicio desde el inicio del horizonte
    end_min     : minuto de fin
    quantity    : piezas producidas en esta tarea

    Decisión de diseño: el solver devuelve tareas, no el plan entero.
    La API convierte estas tareas al formato Gantt JSON cuando es necesario.
    """
    order_id: int
    part_number: str
    step_order: int
    process_name: str
    machine_id: int
    machine_name: str
    start_min: int
    end_min: int
    quantity: int

    @property
    def duration_min(self) -> int:
        return self.end_min - self.start_min

    def to_gantt_dict(self) -> dict:
        """
        Serialización para el campo gantt_json de planes_generados.
        No incluye lógica de presentación (eso es responsabilidad de la API).
        """
        return {
            "order_id": self.order_id,
            "part_number": self.part_number,
            "step_order": self.step_order,
            "process_name": self.process_name,
            "machine_id": self.machine_id,
            "machine_name": self.machine_name,
            "start_min": self.start_min,
            "end_min": self.end_min,
            "duration_min": self.duration_min,
            "quantity": self.quantity,
        }


@dataclass
class ScheduleResult:
    """
    Resultado completo de una corrida del solver.

    solver_status : 'OPTIMAL' | 'FEASIBLE' | 'INFEASIBLE' | 'UNKNOWN'
    makespan_min  : tiempo total del plan en minutos (span del Gantt)
    tasks         : lista de tareas programadas
    unscheduled_order_ids : órdenes que el solver no pudo incluir
                            (por capacidad insuficiente o infactibilidad)

    Decisión: separar status del solver del status del plan.
    'FEASIBLE' significa "encontré una solución pero no sé si es óptima".
    La API puede decidir aceptarla o pedir más tiempo de cómputo.
    """
    solver_status: str
    makespan_min: int
    tasks: list[ScheduledTask] = field(default_factory=list)
    unscheduled_order_ids: list[int] = field(default_factory=list)
    wall_time_seconds: float = 0.0

    @property
    def is_feasible(self) -> bool:
        return self.solver_status in ("OPTIMAL", "FEASIBLE")

    def to_gantt_json(self) -> list[dict]:
        """Lista de dicts lista para guardar en planes_generados.gantt_json."""
        return [t.to_gantt_dict() for t in self.tasks]