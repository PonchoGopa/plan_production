# MySQL Connection Checklist (Pre-GoLive)

## 1) Network and server reachability
- MySQL host reachable from app host (`DB_HOST`).
- Port open (`DB_PORT`, usually `3306`).
- No firewall/VPN rule blocking the connection.

## 2) Credentials and database
- `DB_USER` exists and can authenticate from app host.
- `DB_PASSWORD` is valid.
- `DB_NAME` exists (`kimexproduction`).
- Charset compatible (`utf8mb4` recommended).

## 3) Required permissions
- `SELECT` on:
  - `machines`
  - `part_prod`
  - `rutas`
  - `rutas_maquinas`
  - `tiempos_ciclo`
  - `ordenes`
  - `turnos`
- Optional for later (save plans): `INSERT` on `planes_generados`.

## 4) Required tables/columns (current repository contract)
- `machines(id, Machine, Area, proceso, tonelaje_ton, activa)`
- `part_prod(Part_No, Customer, Project, Workcenter, SPM_Plan, peso_kg, activo)`
- `rutas(id, part_number, step_order, process_name, machine_id, setup_time_min)`
- `rutas_maquinas(ruta_id, machine_id, es_preferida)`
- `tiempos_ciclo(part_number, machine_id, cycle_time_min)`
- `ordenes(id, part_number, cliente, quantity, due_date, priority, status)`
- `turnos(id, nombre, hora_inicio, hora_fin, activo)`

## 5) Environment variables
- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- Optional:
  - `DB_CHARSET`
  - `DB_AUTOCOMMIT`

## 6) Quick validation command
Run:

```bash
python scripts/mysql_smoke_check.py
```

This verifies:
- connection/authentication
- required tables
- required columns
- basic row counts

It does not mutate data.

