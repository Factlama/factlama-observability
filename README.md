# FactLama Observability

This repository implements the SDK, collector, telemetry processing, query API and dashboard. The authoritative component documentation and OBS task ledger are in [factlama-architecture/factlama-observability](https://github.com/Factlama/factlama-architecture/tree/main/factlama-observability). In the three-repo local workspace, open `../factlama-architecture/factlama-observability/README.md`.

Read the [cross-repository execution plan](https://github.com/Factlama/factlama-architecture/blob/main/EXECUTION_PLAN.md) and [shared contract](https://github.com/Factlama/factlama-architecture/blob/main/CONTRACTS.md) before implementation. Current OBS tasks remain `NOT_STARTED` until their acceptance checks pass -- OBS-01 (foundation) is the exception; see `docs/implementation.md` for its status.

## Layout (OBS-01 foundation)

- `sdk/`, `collector/`, `storage/`, `query/`, `operations/` -- Python packages. Only `operations/` (logging, config, versioning, health primitives) has real code; the rest are empty packages with dependency boundaries enforced by `import-linter` (see `pyproject.toml`). Nothing here implements OBS-02 and later yet.
- `storage/migrations/` -- Alembic, wired against `DATABASE_URL`, zero revisions.
- `dashboard/` -- Vite + React + TypeScript scaffold (`npm install && npm run dev/build/lint/typecheck/test`). No real view yet (OBS-08).
- `tests/` -- covers the `operations/` foundation and the package-layout/dependency-boundary contract itself.

Run the Python checks: `pip install -e ".[dev,storage]" && ruff check . && ruff format --check . && mypy sdk collector storage query operations && lint-imports && pytest`.
