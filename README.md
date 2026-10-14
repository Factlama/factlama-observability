# FactLama Observability

This repository implements the SDK, collector, telemetry processing, query API and dashboard. The authoritative component documentation and OBS task ledger are in [factlama-architecture/factlama-observability](https://github.com/Factlama/factlama-architecture/tree/main/factlama-observability). In the three-repo local workspace, open `../factlama-architecture/factlama-observability/README.md`.

Use [CLAUDE.md](CLAUDE.md) or [CODEX.md](CODEX.md) to select only the architecture sections relevant to the task. The central `docs/implementation.md` owns current OBS task status and acceptance checks.

## Layout

- `schemas/` -- pure data layer, no internal dependencies (`import-linter`-enforced): `TenantContext` (OBS-02) and `ReliabilityEvent` (OBS-03, contracts/v0.1's metadata-only projection from Reliability's outbox). `AITrace`/span/resource types aren't here yet -- no executable schema for them exists in `contracts/v0.1` yet, only prose.
- `sdk/`, `collector/`, `storage/`, `query/`, `operations/` -- Python packages. `operations/` (logging, config, versioning, health, bounded-payload-validation primitives) has real code; `collector/ingress/` now has a real authenticated internal receiver, `POST /internal/v0.1/reliability-events` (OBS-04, first slice), backed by `storage/postgres/`'s `TelemetryStore` (OBS-06, first slice: a `reliability_events` table only). The OTLP/HTTP trace path and `query/` remain empty -- both need a canonical `AITrace`/span schema that does not exist in `contracts/v0.1` yet, the same gap OBS-03 already deferred; dependency boundaries are enforced by `import-linter` (see `pyproject.toml`).
- `storage/migrations/` -- Alembic, wired against `FACTLAMA_DATABASE_URL` via the async `postgresql+asyncpg://` engine; one real revision (`reliability_events`).
- `dashboard/` -- Vite + React + TypeScript scaffold (`npm install && npm run dev/build/lint/typecheck/test`). No real view yet (OBS-08).
- `tests/` -- covers `operations/`, `schemas/`, the package-layout/dependency-boundary contract, (`test_contract_fixtures.py`) every canonical `contracts/v0.1` `ReliabilityEvent` fixture parsing through `schemas.ReliabilityEvent`, and (`test_collector_ingress.py`) the same canonical fixtures posted through the live ingress endpoint. `tests/conformance/` runs the storage dedup/idempotency guarantees against a real, throwaway PostgreSQL via `testcontainers` (Docker required locally; already present in CI).

Run the Python checks: `pip install -e ".[dev]" && ruff check . && ruff format --check . && mypy schemas sdk collector storage query operations && lint-imports && pytest`. The contract-fixture tests need `contracts/v0.1` locally reachable at `../factlama-architecture` (the three-repo workspace layout) or via `FACTLAMA_CONTRACTS_DIR`; they skip cleanly otherwise.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for the signed-off commit and pull-request workflow. FactLama Observability is licensed under the [Apache License 2.0](LICENSE); third-party SDKs, exporters and storage dependencies retain their own licenses.
