"""Smoke check for MySQL connectivity and schema contract.

Read-only diagnostic:
- validates DB connection from environment config
- validates required tables and columns
- prints row counts
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Iterable

import mysql.connector

# Ensure project root is importable when running as `python scripts/mysql_smoke_check.py`.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import from_env


REQUIRED_SCHEMA: dict[str, set[str]] = {
    "machines": {"id", "Machine", "Area", "proceso", "tonelaje_ton", "activa"},
    "part_prod": {"Part_No", "Customer", "Project", "Workcenter", "SPM_Plan", "peso_kg", "activo"},
    "rutas": {"id", "part_number", "step_order", "process_name", "machine_id", "setup_time_min"},
    "rutas_maquinas": {"ruta_id", "machine_id", "es_preferida"},
    "tiempos_ciclo": {"part_number", "machine_id", "cycle_time_min"},
    "ordenes": {"id", "part_number", "cliente", "quantity", "due_date", "priority", "status"},
    "turnos": {"id", "nombre", "hora_inicio", "hora_fin", "activo"},
}


def _query_dict(cursor: mysql.connector.cursor.MySQLCursorDict, sql: str, params: Iterable[object] = ()) -> list[dict]:
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    return rows or []


def main() -> int:
    cfg = from_env()
    print("[INFO] Starting MySQL smoke check")
    print(f"[INFO] host={cfg.db.host} port={cfg.db.port} db={cfg.db.database} user={cfg.db.user}")

    conn = None
    try:
        conn = mysql.connector.connect(**cfg.db.as_connector_kwargs())
        cursor = conn.cursor(dictionary=True)

        # 1) Required tables
        table_rows = _query_dict(
            cursor,
            """
            SELECT TABLE_NAME
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
            """,
            (cfg.db.database,),
        )
        present_tables = {row["TABLE_NAME"] for row in table_rows}
        missing_tables = sorted(set(REQUIRED_SCHEMA.keys()) - present_tables)
        if missing_tables:
            print(f"[ERROR] Missing tables: {', '.join(missing_tables)}")
            return 1
        print("[OK] Required tables present")

        # 2) Required columns
        col_rows = _query_dict(
            cursor,
            """
            SELECT TABLE_NAME, COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
            """,
            (cfg.db.database,),
        )
        columns_by_table: dict[str, set[str]] = {}
        for row in col_rows:
            columns_by_table.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])

        schema_ok = True
        for table_name, required_cols in REQUIRED_SCHEMA.items():
            present_cols = columns_by_table.get(table_name, set())
            missing_cols = sorted(required_cols - present_cols)
            if missing_cols:
                schema_ok = False
                print(f"[ERROR] {table_name}: missing columns -> {', '.join(missing_cols)}")

        if not schema_ok:
            return 1
        print("[OK] Required columns present")

        # 3) Row counts (sanity only)
        print("[INFO] Row counts:")
        for table_name in REQUIRED_SCHEMA:
            cursor.execute(f"SELECT COUNT(*) AS c FROM `{table_name}`")
            count_row = cursor.fetchone() or {"c": 0}
            print(f"  - {table_name}: {int(count_row['c'])}")

        print("[SUCCESS] MySQL smoke check passed")
        return 0
    except mysql.connector.Error as exc:
        print(f"[ERROR] MySQL failure: {exc}")
        return 1
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
