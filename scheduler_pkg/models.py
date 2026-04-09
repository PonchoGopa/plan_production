"""Domain models for the scheduler package.

This module is intentionally framework-agnostic: no Flask, no HTTP objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Machine:
    id: int
    name: str
    area: str
    process_name: str
    tonnage_ton: float | None
    active: bool


@dataclass(frozen=True)
class Route:
    id: int
    part_number: str
    step_order: int
    process_name: str
    machine_id: int
    setup_time_min: int
    alternative_machine_ids: list[int] = field(default_factory=list)
    preferred_machine_ids: list[int] = field(default_factory=list)

    @property
    def eligible_machine_ids(self) -> list[int]:
        merged = [self.machine_id, *self.alternative_machine_ids]
        deduplicated: list[int] = []
        seen: set[int] = set()
        for machine_id in merged:
            if machine_id not in seen:
                deduplicated.append(machine_id)
                seen.add(machine_id)
        return deduplicated


@dataclass(frozen=True)
class Order:
    id: int
    part_number: str
    customer: str
    quantity: int
    due_date: date
    priority: int
    status: str


@dataclass(frozen=True)
class Shift:
    id: int
    name: str
    start_min: int
    end_min: int
    active: bool

    @property
    def duration_min(self) -> int:
        return self.end_min - self.start_min


@dataclass(frozen=True)
class CycleTime:
    part_number: str
    machine_id: int
    cycle_time_min: float


@dataclass(frozen=True)
class Part:
    part_number: str
    customer: str
    project: str
    workcenter: str
    spm_plan: float
    weight_kg: float | None
    active: bool


@dataclass(frozen=True)
class ScheduledTask:
    order_id: int
    part_number: str
    route_id: int
    step_order: int
    process_name: str
    machine_id: int
    start_min: int
    end_min: int
    quantity: int

    @property
    def duration_min(self) -> int:
        return self.end_min - self.start_min


@dataclass(frozen=True)
class ScheduleResult:
    solver_status: str
    makespan_min: int
    tasks: list[ScheduledTask] = field(default_factory=list)
    unscheduled_order_ids: list[int] = field(default_factory=list)
    wall_time_seconds: float = 0.0

    @property
    def is_feasible(self) -> bool:
        return self.solver_status in {"OPTIMAL", "FEASIBLE"}


@dataclass(frozen=True)
class PlanningData:
    machines: dict[int, Machine]
    parts: dict[str, Part]
    routes_by_part: dict[str, list[Route]]
    cycle_times: dict[tuple[str, int], CycleTime]
    orders: list[Order]
    shifts: list[Shift]

