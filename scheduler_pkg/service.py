"""Application service layer for planning orchestration.

The service composes repository + scheduler while staying HTTP-agnostic.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable

from .models import PlanningData, ScheduleResult
from .repository import Connection, load_planning_data
from .scheduler import JobShopScheduler, SolverOptions


class PlanningService:
    """Orchestrates planning data retrieval and scheduling execution."""

    def __init__(
        self,
        *,
        data_loader: Callable[..., PlanningData] = load_planning_data,
        scheduler: JobShopScheduler | None = None,
    ) -> None:
        self._data_loader = data_loader
        self._scheduler = scheduler or JobShopScheduler()

    def load_data(
        self,
        conn: Connection,
        *,
        order_status: str = "pending",
        only_active: bool = True,
    ) -> PlanningData:
        return self._data_loader(conn, order_status=order_status, only_active=only_active)

    def generate_plan(
        self,
        conn: Connection,
        *,
        solver_config: Any = None,
        order_status: str = "pending",
        only_active: bool = True,
    ) -> ScheduleResult:
        planning_data = self.load_data(conn, order_status=order_status, only_active=only_active)
        options = self._resolve_solver_options(solver_config)
        return self._scheduler.solve(planning_data, options=options)

    @staticmethod
    def _resolve_solver_options(solver_config: Any) -> SolverOptions:
        if solver_config is None:
            return SolverOptions()
        if isinstance(solver_config, SolverOptions):
            return solver_config
        if isinstance(solver_config, dict):
            return SolverOptions(**solver_config)
        if is_dataclass(solver_config):
            return SolverOptions(**asdict(solver_config))

        supported_fields = ("horizon_days", "time_limit_seconds", "num_workers", "random_seed")
        payload = {field: getattr(solver_config, field) for field in supported_fields if hasattr(solver_config, field)}
        return SolverOptions(**payload)
