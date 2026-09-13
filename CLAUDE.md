# Claude implementation context

Read the central documentation before changing this repository. In the local three-repo workspace start at `../factlama-architecture/factlama-observability/README.md`, then read `../factlama-architecture/factlama-observability/docs/implementation.md`, `../factlama-architecture/EXECUTION_PLAN.md`, and `../factlama-architecture/CONTRACTS.md`. For a standalone clone, use the matching paths in `https://github.com/Factlama/factlama-architecture`.

The central `docs/implementation.md` owns OBS task status. Keep the dashboard behind query APIs and content capture disabled by default. Work through the execution-plan gates, test success/failure/tenant boundaries, and update the central task ledger only after acceptance criteria pass.
