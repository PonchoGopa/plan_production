"""Application-level configuration for database and solver settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from dotenv import load_dotenv

# Load .env from project root if present (no-op if file is missing).
load_dotenv()


def _env_str(env: Mapping[str, str], key: str, default: str) -> str:
    return env.get(key, default)


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    if value is None or value == "":
        return default
    return int(value)


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DBConfig:
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "kimexproduction"
    charset: str = "utf8mb4"
    autocommit: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DBConfig":
        source = os.environ if env is None else env
        return cls(
            host=_env_str(source, "DB_HOST", "localhost"),
            port=_env_int(source, "DB_PORT", 3306),
            user=_env_str(source, "DB_USER", "root"),
            password=_env_str(source, "DB_PASSWORD", ""),
            database=_env_str(source, "DB_NAME", "kimexproduction"),
            charset=_env_str(source, "DB_CHARSET", "utf8mb4"),
            autocommit=_env_bool(source, "DB_AUTOCOMMIT", False),
        )

    def as_connector_kwargs(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
            "autocommit": self.autocommit,
        }


@dataclass(frozen=True)
class SolverConfig:
    horizon_days: int = 7
    time_limit_seconds: int = 60
    num_workers: int = 4
    random_seed: int | None = None
    minutes_per_slot: int = 1

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SolverConfig":
        source = os.environ if env is None else env
        seed_raw = source.get("SOLVER_RANDOM_SEED")
        seed = int(seed_raw) if seed_raw not in (None, "") else None
        return cls(
            horizon_days=_env_int(source, "SOLVER_HORIZON_DAYS", 7),
            time_limit_seconds=_env_int(source, "SOLVER_TIME_LIMIT_SECONDS", 60),
            num_workers=_env_int(source, "SOLVER_NUM_WORKERS", 4),
            random_seed=seed,
            minutes_per_slot=_env_int(source, "SOLVER_MINUTES_PER_SLOT", 1),
        )

    @property
    def horizon_minutes(self) -> int:
        return self.horizon_days * 24 * 60


@dataclass(frozen=True)
class AppConfig:
    db: DBConfig
    solver: SolverConfig

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppConfig":
        source = os.environ if env is None else env
        return cls(
            db=DBConfig.from_env(source),
            solver=SolverConfig.from_env(source),
        )


def from_env(env: Mapping[str, str] | None = None) -> AppConfig:
    """Convenience entrypoint for callers that need full app config."""
    return AppConfig.from_env(env)


