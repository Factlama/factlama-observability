/**
 * OBS-01 scope: build/lint/type/test tooling for the dashboard package
 * only. The real routes (`/overview`, `/requests`, `/requests/:traceId`,
 * `/reliability`, `/models`) described in
 * ../../factlama-architecture/factlama-observability/docs/LOW_LEVEL_IMPLEMENTATION.md
 * are OBS-08 work against a Query API that does not exist yet (OBS-07).
 * This placeholder exists so the toolchain has something real to build,
 * lint, type-check and test.
 */
export function App() {
  return (
    <main>
      <h1>FactLama Observability</h1>
      <p>Dashboard views are not implemented yet (OBS-08).</p>
    </main>
  );
}
