# Observability implementation context

Read `factlama-architecture/CONTRACTS.md`, `DECISION_REGISTER.md`, this repo's `README.md` and `docs/implementation.md` before implementation. The latter owns OBS task status. Work one vertical slice at a time. Do not alter shared IDs, event shapes, tenant boundaries or query semantics without an architecture ADR. Keep dashboard behind query APIs and content capture disabled by default.

For each task: inspect existing code, implement the smallest complete slice, test success/failure/tenant isolation, run lint/type checks, review architectural drift, update docs and only then mark COMPLETE. Do not claim the Docker MVP works until a fresh-checkout smoke test passes.
