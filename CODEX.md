# Codex context guide

The source of truth is `../factlama-architecture/factlama-observability/` in the three-repo workspace. In a standalone clone, use the same paths in `https://github.com/Factlama/factlama-architecture/tree/main/factlama-observability`. Use local files when available.

Load context for the task, not the whole documentation set:

1. Identify the OBS task ID from the request or affected code. Find that task's heading in `docs/implementation.md` and read its section, including acceptance criteria. If the task is unclear, scan headings and code first.
2. Read the matching subsystem spec only: SDK (`docs/SDK.md`), telemetry (`docs/telemetry-model.md`), collector/ingress/processing (`docs/collector.md`), storage/query (`docs/storage-query.md`), or dashboard (`docs/dashboard.md`). Use `docs/LOW_LEVEL_IMPLEMENTATION.md` only for the relevant module or acceptance checklist. Use the component `README.md` only for an unfamiliar product boundary or module layout.
3. Read the relevant gate in `../factlama-architecture/EXECUTION_PLAN.md` when the work changes build order or crosses repositories. Read the relevant section of `../factlama-architecture/CONTRACTS.md` and, for exact wire shapes, the affected file under `contracts/v0.1/` when touching shared schemas, events, tenancy, provenance, or compatibility. Read related ADRs only when their decision applies.
4. Inspect the affected code and tests before editing. Expand to other documents when a concrete dependency or conflict calls for them; avoid loading entire plans, contracts, or unrelated subsystem specs by default.

The central `docs/implementation.md` owns OBS status. Keep the dashboard behind query APIs and content capture disabled by default. Test the relevant success, failure, and tenant boundaries. Update the central task ledger only after acceptance criteria pass.
