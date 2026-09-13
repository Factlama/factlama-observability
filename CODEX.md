# Codex implementation context

Documentation lives in the sibling `factlama-architecture/factlama-observability/` folder. Read its `README.md` and `docs/implementation.md`, plus the architecture root `EXECUTION_PLAN.md` and `CONTRACTS.md`, before implementation. If the architecture repo is not checked out beside this one, use `https://github.com/Factlama/factlama-architecture/tree/main/factlama-observability` and its root docs.

Use the central OBS task ledger. Implement one tested execution-plan gate at a time, preserve tenant and content boundaries, and update the central docs when behavior or status changes.
