"""Tests for planning API endpoints."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from api.app import create_app
from scheduler_pkg import RepositoryDataError, ScheduleResult, ScheduledTask


class PlanningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = self.app.test_client()

    def test_health_endpoint_returns_ok(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    @patch("api.routes.planning.from_env")
    @patch("api.routes.planning.mysql.connector.connect")
    @patch("api.routes.planning.PlanningService")
    def test_run_planning_success(self, service_cls: MagicMock, connect_mock: MagicMock, from_env_mock: MagicMock) -> None:
        fake_conn = MagicMock()
        fake_conn.is_connected.return_value = True
        connect_mock.return_value = fake_conn

        fake_cfg = MagicMock()
        fake_cfg.db.as_connector_kwargs.return_value = {"host": "x"}
        fake_cfg.solver.horizon_days = 7
        fake_cfg.solver.time_limit_seconds = 60
        fake_cfg.solver.num_workers = 4
        fake_cfg.solver.random_seed = None
        from_env_mock.return_value = fake_cfg

        service_instance = service_cls.return_value
        service_instance.generate_plan.return_value = ScheduleResult(
            solver_status="FEASIBLE",
            makespan_min=120,
            tasks=[
                ScheduledTask(
                    order_id=1,
                    part_number="P-100",
                    route_id=10,
                    step_order=1,
                    process_name="prensa",
                    machine_id=3,
                    start_min=5,
                    end_min=25,
                    quantity=40,
                )
            ],
            unscheduled_order_ids=[],
            wall_time_seconds=0.42,
        )

        response = self.client.post(
            "/planning/run",
            json={
                "order_status": "pending",
                "only_active": True,
                "solver": {"horizon_days": 9, "time_limit_seconds": 20, "num_workers": 2, "random_seed": 99},
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["solver_status"], "FEASIBLE")
        self.assertEqual(payload["makespan_min"], 120)
        self.assertEqual(payload["tasks"][0]["duration_min"], 20)
        self.assertIn("generated_at", payload)

        service_instance.generate_plan.assert_called_once_with(
            fake_conn,
            solver_config={"horizon_days": 9, "time_limit_seconds": 20, "num_workers": 2, "random_seed": 99},
            order_status="pending",
            only_active=True,
        )
        fake_conn.close.assert_called_once()

    def test_run_planning_rejects_non_object_solver(self) -> None:
        response = self.client.post("/planning/run", json={"solver": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Field 'solver' must be an object.")

    @patch("api.routes.planning.from_env")
    @patch("api.routes.planning.mysql.connector.connect")
    @patch("api.routes.planning.PlanningService")
    def test_run_planning_repository_error_returns_422(
        self, service_cls: MagicMock, connect_mock: MagicMock, from_env_mock: MagicMock
    ) -> None:
        fake_conn = MagicMock()
        fake_conn.is_connected.return_value = True
        connect_mock.return_value = fake_conn

        fake_cfg = MagicMock()
        fake_cfg.db.as_connector_kwargs.return_value = {"host": "x"}
        fake_cfg.solver.horizon_days = 7
        fake_cfg.solver.time_limit_seconds = 60
        fake_cfg.solver.num_workers = 4
        fake_cfg.solver.random_seed = None
        from_env_mock.return_value = fake_cfg

        service_instance = service_cls.return_value
        service_instance.generate_plan.side_effect = RepositoryDataError("bad mapping")

        response = self.client.post("/planning/run", json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"], "Repository data error")
        fake_conn.close.assert_called_once()

    @patch("api.routes.planning.mysql.connector.connect")
    def test_run_planning_db_error_returns_500(self, connect_mock: MagicMock) -> None:
        import mysql.connector

        connect_mock.side_effect = mysql.connector.Error("db down")
        response = self.client.post("/planning/run", json={})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "Database connection/query error")

    def test_run_planning_invalid_solver_value_returns_400(self) -> None:
        response = self.client.post("/planning/run", json={"solver": {"horizon_days": "abc"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Invalid input payload")


if __name__ == "__main__":
    unittest.main()
