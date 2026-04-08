"""
config.py — Configuración central del sistema.

DECISIÓN DE DISEÑO: Todo lo configurable vive aquí.
No hay strings de conexión ni constantes mágicas dispersas
por el código. scheduler_pkg importa sólo los dataclasses
de config (no Flask, no rutas HTTP).
"""

import os
from dataclasses import dataclass, field


# ── Configuración de la base de datos ─────────────────────────
@dataclass
class DBConfig:
    """
    Parámetros de conexión a MySQL.
    Se leen desde variables de entorno; los valores por defecto
    son sólo para desarrollo local.

    Por qué dataclass: es inmutable una vez construido, facilita
    pasarla como parámetro y documentarla con type hints.
    """
    host: str = field(default_factory=lambda: os.getenv("DB_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "3306")))
    user: str = field(default_factory=lambda: os.getenv("DB_USER", "root"))
    password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
    database: str = field(default_factory=lambda: os.getenv("DB_NAME", "productionkimex"))

    def as_dict(self) -> dict:
        """Formato que acepta mysql-connector-python."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }


# ── Configuración del solver CP-SAT ───────────────────────────
@dataclass
class SolverConfig:
    """
    Parámetros del solver OR-Tools CP-SAT.

    horizon_days : ventana de planificación en días.
        El solver busca soluciones dentro de este horizonte.
        1 semana = 7 días (incluye stock de seguridad).

    time_limit_seconds : cuánto tiempo puede correr el solver
        antes de devolver la mejor solución encontrada hasta ese momento.
        En producción 60s suele ser suficiente; en pruebas se baja a 10s.

    num_workers : hilos del solver. 0 = usar todos los cores disponibles.
    
    minutes_per_slot : granularidad del tiempo. 1 minuto por slot
        permite precisión suficiente sin explotar el espacio de búsqueda.
    """
    horizon_days: int = field(default_factory=lambda: int(os.getenv("SOLVER_HORIZON_DAYS", "7")))
    time_limit_seconds: int = field(default_factory=lambda: int(os.getenv("SOLVER_TIME_LIMIT", "60")))
    num_workers: int = field(default_factory=lambda: int(os.getenv("SOLVER_WORKERS", "4")))
    minutes_per_slot: int = 1  # constante; cambiarlo requeriría revalidar toda la lógica

    @property
    def horizon_minutes(self) -> int:
        """Horizonte total en minutos (unidad interna del solver)."""
        return self.horizon_days * 24 * 60


# ── Configuración de los turnos ────────────────────────────────
@dataclass
class ShiftConfig:
    """
    Horarios de turno.
    La planta opera 2 turnos con traslape de ~3 horas.
    Todos los tiempos son en minutos desde medianoche (0:00).

    Turno 1: 06:00 – 14:30  (510 min desde medianoche → 870 min)
    Turno 2: 11:30 – 22:00  (690 min → 1320 min)
    Traslape: 11:30 – 14:30 (180 min)

    Estos valores son el punto de partida; la tabla `turnos` de
    la BD puede sobreescribirlos en tiempo de ejecución.
    """
    shift1_start_min: int = 6 * 60        # 06:00
    shift1_end_min: int = 14 * 60 + 30    # 14:30
    shift2_start_min: int = 11 * 60 + 30  # 11:30
    shift2_end_min: int = 22 * 60         # 22:00

    @property
    def daily_available_minutes(self) -> int:
        """
        Minutos productivos netos por día (sin contar traslape doble).
        Turno1: 510 min | Turno2: 630 min | Traslape: 180 min
        Tiempo único cubierto: 06:00 – 22:00 = 960 min
        """
        return self.shift2_end_min - self.shift1_start_min  # 960


# ── Instancias por defecto (para importación rápida) ──────────
db_config = DBConfig()
solver_config = SolverConfig()
shift_config = ShiftConfig()