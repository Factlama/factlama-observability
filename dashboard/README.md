# FactLama Observability Dashboard

OBS-01 scope: Vite + React + TypeScript scaffold with build/lint/type/test
tooling wired and passing. No real view exists yet — see
[LOW_LEVEL_IMPLEMENTATION.md](../../factlama-architecture/factlama-observability/docs/LOW_LEVEL_IMPLEMENTATION.md)
for the routes and cells OBS-08 will implement against the Query API
(OBS-07), neither of which exists yet.

```sh
npm install
npm run dev        # local dev server
npm run build       # tsc -b && vite build
npm run lint         # eslint
npm run typecheck  # tsc -b --noEmit
npm run test         # vitest run
```
