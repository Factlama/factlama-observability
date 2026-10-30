/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** OBS-08: default Query API base URL baked in at build time, used
   * only when the browser has no `localStorage`-saved override. Empty
   * string (the default in `.env`/unset) means same-origin, relative
   * paths -- see `src/lib/apiConfig.tsx`'s own docstring. */
  readonly VITE_QUERY_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
