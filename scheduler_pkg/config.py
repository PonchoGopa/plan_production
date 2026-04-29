"""
scheduler_pkg/config.py
-----------------------
Configuración interna del paquete scheduler.

IMPORTANTE: Este archivo es DISTINTO del config.py de la raíz del proyecto.
  - config.py (raíz)          → AppConfig, DBConfig, SolverConfig para Flask/service
  - scheduler_pkg/config.py   → SolverConfig y ShiftConfig para el motor CP-SAT

scheduler.py importa desde aquí con:
    from ..config import SolverConfig, ShiftConfig

Esto es un import relativo que sube un nivel (..) desde scheduler_pkg/
hasta la raíz y luego entra a config.py. Lo mantenemos así para que
scheduler_pkg sea un paquete independiente y portable.

Los valores por defecto reflejan la operación real de Kimex:
  - Turno 1: 06:00–14:30  (360–870 min desde medianoche)
  - Turno 2: 11:30–22:00  (690–1320 min desde medianoche)
  - Horizonte: 7 días = 10080 minutos
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SolverConfig:
    """
    Parámetros que controlan el comportamiento del solver CP-SAT.

    time_limit_seconds : Tiempo máximo antes de devolver la mejor solución
                         encontrada. 60s es suficiente para planes diarios.
    num_workers        : Workers paralelos del solver. 4 es un buen balance
                         para un servidor de producción con 8 núcleos.
    random_seed        : Semilla para resultados reproducibles. None = aleatorio.
    horizon_minutes    : Ventana total de tiempo que el solver considera.
                         Default 7 días = 10080 min (objetivo de stock semanal).
    minutes_per_slot   : Granularidad del modelo. 1 = máxima precisión.
                         Aumentar a 5 o 10 si el modelo es muy grande y lento.
    """
    time_limit_seconds: int   = 60
    num_workers:        int   = 4
    random_seed:        int | None = None
    horizon_minutes:    int   = 7 * 24 * 60   # 10080
    minutes_per_slot:   int   = 1

    @classmethod
    def from_app_config(cls, app_solver_cfg) -> "SolverConfig":
        """
        Construye un SolverConfig desde el SolverConfig del config.py raíz.
        Permite que los valores del .env lleguen hasta el motor del solver.

        Uso en service.py:
            from config import from_env
            from scheduler_pkg.config import SolverConfig
            cfg = from_env()
            solver_cfg = SolverConfig.from_app_config(cfg.solver)
        """
        return cls(
            time_limit_seconds=app_solver_cfg.time_limit_seconds,
            num_workers=app_solver_cfg.num_workers,
            random_seed=app_solver_cfg.random_seed,
            horizon_minutes=app_solver_cfg.horizon_days * 24 * 60,
            minutes_per_slot=app_solver_cfg.minutes_per_slot,
        )


@dataclass(frozen=True)
class ShiftConfig:
    """
    Configuración de turnos usada como fallback cuando la tabla shifts
    de la BD está vacía.

    Valores en minutos desde medianoche:
      360  = 06:00  (inicio turno 1)
      870  = 14:30  (fin turno 1)
      690  = 11:30  (inicio turno 2)
      1320 = 22:00  (fin turno 2)

    El traslape 690–870 (11:30–14:30) es intencional — ambos turnos
    operan simultáneamente durante esas 3 horas.
    """
    shift1_start_min: int = 360    # 06:00
    shift1_end_min:   int = 870    # 14:30
    shift2_start_min: int = 690    # 11:30
    shift2_end_min:   int = 1320   # 22:00


# Instancias por defecto — scheduler.py las importa directamente si no
# recibe configuración explícita:
#   from ..config import solver_config as default_sc
solver_config = SolverConfig()
shift_config  = ShiftConfig()