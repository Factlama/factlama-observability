/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// OBS-08: `npm run dev` proxies `/v0.1/*` to a real local Query API
// process (default `http://localhost:8001`, override with
// `FACTLAMA_DASHBOARD_QUERY_API_PROXY_TARGET`) so the browser's requests
// stay same-origin -- `docs/LOW_LEVEL_IMPLEMENTATION.md`: "No direct
// database, collector or provider call from the browser," and the Query
// API (`query/api/app.py`) deliberately has no CORS middleware: a
// browser calling a *different* origin directly would be blocked by the
// browser's own CORS enforcement regardless. This proxy, and a reverse
// proxy serving both under one origin in production, are this dashboard's
// chosen way to stay same-origin; adding CORS to the Query API itself
// was considered and deferred as an unnecessary alternative, not an
// oversight.
const QUERY_API_PROXY_TARGET =
  process.env.FACTLAMA_DASHBOARD_QUERY_API_PROXY_TARGET ?? "http://localhost:8001";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v0.1": {
        target: QUERY_API_PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
