"""CP-SAT scheduler engine.

This module is framework-agnostic and only depends on domain dataclasses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from ortools.sat.python import cp_model

from .models import PlanningData, ScheduleResult, ScheduledTask


@dataclass(frozen=True)
class SolverOptions:
    horizon_days: int = 7
    time_limit_seconds: int = 60
    num_workers: int = 4
    random_seed: int | None = None

    @property
    def horizon_minutes(self) -> int:
        return self.horizon_days * 24 * 60


class JobShopScheduler:
    """Builds and solves a flexible job-shop model using OR-Tools CP-SAT."""

    def solve(self, planning_data: PlanningData, options: SolverOptions | None = None) -> ScheduleResult:
        solver_options = options or SolverOptions()
        if not planning_data.orders:
            return ScheduleResult(solver_status="FEASIBLE", makespan_min=0, tasks=[])

        model = cp_model.CpModel()
        horizon = solver_options.horizon_minutes

        machine_to_intervals: dict[int, list[cp_model.IntervalVar]] = {}
        task_handles: list[dict[str, object]] = []
        order_last_step_end_vars: dict[int, cp_model.IntVar] = {}

        for order in planning_data.orders:
            routes = sorted(planning_data.routes_by_part[order.part_number], key=lambda route: route.step_order)
            prev_end_var: cp_model.IntVar | None = None

            for route in routes:
                eligible_machine_ids = route.eligible_machine_ids
                duration_by_machine: dict[int, int] = {}
                for machine_id in eligible_machine_ids:
                    cycle = planning_data.cycle_times[(order.part_number, machine_id)]
                    duration_min = math.ceil(cycle.cycle_time_min * order.quantity + route.setup_time_min)
                    duration_by_machine[machine_id] = max(1, duration_min)

                if len(eligible_machine_ids) == 1:
                    machine_id = eligible_machine_ids[0]
                    duration = duration_by_machine[machine_id]
                    start_var = model.NewIntVar(0, horizon, f"start_o{order.id}_r{route.id}")
                    end_var = model.NewIntVar(0, horizon, f"end_o{order.id}_r{route.id}")
                    interval = model.NewIntervalVar(start_var, duration, end_var, f"int_o{order.id}_r{route.id}")
                    machine_to_intervals.setdefault(machine_id, []).append(interval)
                    chosen_machine_literals = {machine_id: None}
                    start_by_machine = {machine_id: start_var}
                    end_by_machine = {machine_id: end_var}
                else:
                    start_var = model.NewIntVar(0, horizon, f"start_o{order.id}_r{route.id}")
                    end_var = model.NewIntVar(0, horizon, f"end_o{order.id}_r{route.id}")
                    literals: dict[int, cp_model.BoolVar] = {}
                    start_by_machine: dict[int, cp_model.IntVar] = {}
                    end_by_machine: dict[int, cp_model.IntVar] = {}
                    machine_presence: list[cp_model.BoolVar] = []

                    for machine_id in eligible_machine_ids:
                        duration = duration_by_machine[machine_id]
                        lit = model.NewBoolVar(f"use_o{order.id}_r{route.id}_m{machine_id}")
                        m_start = model.NewIntVar(0, horizon, f"start_o{order.id}_r{route.id}_m{machine_id}")
                        m_end = model.NewIntVar(0, horizon, f"end_o{order.id}_r{route.id}_m{machine_id}")
                        interval = model.NewOptionalIntervalVar(
                            m_start,
                            duration,
                            m_end,
                            lit,
                            f"int_o{order.id}_r{route.id}_m{machine_id}",
                        )
                        literals[machine_id] = lit
                        start_by_machine[machine_id] = m_start
                        end_by_machine[machine_id] = m_end
                        machine_to_intervals.setdefault(machine_id, []).append(interval)
                        machine_presence.append(lit)

                        model.Add(start_var == m_start).OnlyEnforceIf(lit)
                        model.Add(end_var == m_end).OnlyEnforceIf(lit)

                    model.AddExactlyOne(machine_presence)
                    chosen_machine_literals = literals

                if prev_end_var is not None:
                    model.Add(start_var >= prev_end_var)

                task_handles.append(
                    {
                        "order_id": order.id,
                        "part_number": order.part_number,
                        "quantity": order.quantity,
                        "route_id": route.id,
                        "step_order": route.step_order,
                        "process_name": route.process_name,
                        "start_var": start_var,
                        "end_var": end_var,
                        "chosen_machine_literals": chosen_machine_literals,
                        "start_by_machine": start_by_machine,
                        "end_by_machine": end_by_machine,
                    }
                )
                prev_end_var = end_var

            if prev_end_var is None:
                continue
            order_last_step_end_vars[order.id] = prev_end_var

        for intervals in machine_to_intervals.values():
            model.AddNoOverlap(intervals)

        makespan = model.NewIntVar(0, horizon, "makespan")
        model.AddMaxEquality(makespan, list(order_last_step_end_vars.values()))

        today = date.today()
        weighted_terms: list[cp_model.LinearExpr] = []
        for order in planning_data.orders:
            order_end = order_last_step_end_vars[order.id]
            due_min = max(0, (order.due_date - today).days * 24 * 60)
            tardiness = model.NewIntVar(0, horizon, f"tardy_o{order.id}")
            model.Add(tardiness >= order_end - due_min)
            model.Add(tardiness >= 0)
            priority_weight = 5 if order.priority <= 1 else 3 if order.priority == 2 else 1
            weighted_terms.append(tardiness * priority_weight)

        model.Minimize(sum(weighted_terms) + makespan)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(solver_options.time_limit_seconds)
        solver.parameters.num_search_workers = int(solver_options.num_workers)
        if solver_options.random_seed is not None:
            solver.parameters.random_seed = int(solver_options.random_seed)

        status = solver.Solve(model)
        status_name = solver.StatusName(status)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return ScheduleResult(
                solver_status=status_name,
                makespan_min=0,
                tasks=[],
                unscheduled_order_ids=[order.id for order in planning_data.orders],
                wall_time_seconds=solver.WallTime(),
            )

        tasks: list[ScheduledTask] = []
        for handle in task_handles:
            chosen_machine_id = None
            chosen_literals: dict[int, cp_model.BoolVar | None] = handle["chosen_machine_literals"]  # type: ignore[assignment]
            if len(chosen_literals) == 1 and next(iter(chosen_literals.values())) is None:
                chosen_machine_id = next(iter(chosen_literals.keys()))
                start_value = int(solver.Value(handle["start_var"]))  # type: ignore[index]
                end_value = int(solver.Value(handle["end_var"]))  # type: ignore[index]
            else:
                for machine_id, literal in chosen_literals.items():
                    if literal is not None and solver.Value(literal) == 1:
                        chosen_machine_id = machine_id
                        start_value = int(solver.Value(handle["start_by_machine"][machine_id]))  # type: ignore[index]
                        end_value = int(solver.Value(handle["end_by_machine"][machine_id]))  # type: ignore[index]
                        break
                if chosen_machine_id is None:
                    continue

            tasks.append(
                ScheduledTask(
                    order_id=int(handle["order_id"]),
                    part_number=str(handle["part_number"]),
                    route_id=int(handle["route_id"]),
                    step_order=int(handle["step_order"]),
                    process_name=str(handle["process_name"]),
                    machine_id=chosen_machine_id,
                    start_min=start_value,
                    end_min=end_value,
                    quantity=int(handle["quantity"]),
                )
            )

        tasks.sort(key=lambda t: (t.start_min, t.machine_id, t.order_id, t.step_order))
        scheduled_ids = {task.order_id for task in tasks}
        unscheduled = [order.id for order in planning_data.orders if order.id not in scheduled_ids]

        return ScheduleResult(
            solver_status=status_name,
            makespan_min=int(solver.Value(makespan)),
            tasks=tasks,
            unscheduled_order_ids=unscheduled,
            wall_time_seconds=solver.WallTime(),
        )
