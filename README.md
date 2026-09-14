# FactLama Observability

This repository implements the SDK, collector, telemetry processing, query API and dashboard. The authoritative component documentation and OBS task ledger are in [factlama-architecture/factlama-observability](https://github.com/Factlama/factlama-architecture/tree/main/factlama-observability). In the three-repo local workspace, open `../factlama-architecture/factlama-observability/README.md`.

Read the [cross-repository execution plan](https://github.com/Factlama/factlama-architecture/blob/main/EXECUTION_PLAN.md) and [shared contract](https://github.com/Factlama/factlama-architecture/blob/main/CONTRACTS.md) before implementation. Current OBS tasks remain `NOT_STARTED` until their acceptance checks pass -- OBS-01 (foundation) is `COMPLETE`; see `docs/implementation.md` for the rest.

## Layout

- `schemas/` -- pure data layer, no internal dependencies (`import-linter`-enforced): `TenantContext` (OBS-02) and `ReliabilityEvent` (OBS-03, contracts/v0.1's metadata-only projection from Reliability's outbox). `AITrace`/span/resource types aren't here yet -- no executable schema for them exists in `contracts/v0.1` yet, only prose.
- `sdk/`, `collector/`, `storage/`, `query/`, `operations/` -- Python packages. `operations/` (logging, config, versioning, health, bounded-payload-validation primitives) has real code; the rest are still mostly empty with dependency boundaries enforced by `import-linter` (see `pyproject.toml`). Nothing here implements a real ingress/collector/query service yet (OBS-04+).
- `storage/migrations/` -- Alembic, wired against `DATABASE_URL`, zero revisions.
- `dashboard/` -- Vite + React + TypeScript scaffold (`npm install && npm run dev/build/lint/typecheck/test`). No real view yet (OBS-08).
- `tests/` -- covers `operations/`, `schemas/`, the package-layout/dependency-boundary contract, and (`test_contract_fixtures.py`) every canonical `contracts/v0.1` `ReliabilityEvent` fixture parsing through `schemas.ReliabilityEvent`.

Run the Python checks: `pip install -e ".[dev,storage]" && ruff check . && ruff format --check . && mypy schemas sdk collector storage query operations && lint-imports && pytest`. The contract-fixture tests need `contracts/v0.1` locally reachable at `../factlama-architecture` (the three-repo workspace layout) or via `FACTLAMA_CONTRACTS_DIR`; they skip cleanly otherwise.
