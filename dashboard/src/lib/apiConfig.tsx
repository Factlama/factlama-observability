/**
 * OBS-08: where this dashboard gets its Query API base URL and API key.
 *
 * Deliberately not an env-only config: a real deployment serves one
 * dashboard build to many tenants, each with their own credential, so
 * the API key cannot be a single build-time constant. Instead this is a
 * small per-browser connection form (`ConnectionSettingsPage.tsx`)
 * backed by `localStorage` -- the key never leaves this origin except in
 * the `Authorization` header sent to the configured Query API itself
 * (`api/client.ts`).
 *
 * `baseUrl` defaults to `""` (same-origin, relative paths) rather than a
 * hardcoded host: in local development, `vite.config.ts`'s dev-server
 * proxy forwards `/v0.1/*` to a real Query API process, so the browser
 * never needs a cross-origin request (and the Query API itself does not
 * need CORS support -- see `vite.config.ts`'s own comment for why that
 * was a deliberate scope decision, not an oversight). In production, a
 * reverse proxy is expected to serve both under one origin the same way.
 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

const STORAGE_KEY_API_KEY = "factlama.dashboard.apiKey";
const STORAGE_KEY_BASE_URL = "factlama.dashboard.baseUrl";

function readStoredApiKey(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY_API_KEY) ?? "";
  } catch {
    return "";
  }
}

function readStoredBaseUrl(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY_BASE_URL) ?? import.meta.env.VITE_QUERY_API_BASE_URL ?? "";
  } catch {
    return import.meta.env.VITE_QUERY_API_BASE_URL ?? "";
  }
}

interface ApiConfigValue {
  baseUrl: string;
  apiKey: string;
  isConfigured: boolean;
  setBaseUrl: (value: string) => void;
  setApiKey: (value: string) => void;
}

const ApiConfigContext = createContext<ApiConfigValue | null>(null);

export function ApiConfigProvider({ children }: { children: ReactNode }) {
  const [baseUrl, setBaseUrlState] = useState<string>(readStoredBaseUrl);
  const [apiKey, setApiKeyState] = useState<string>(readStoredApiKey);

  const setBaseUrl = useCallback((value: string) => {
    setBaseUrlState(value);
    try {
      window.localStorage.setItem(STORAGE_KEY_BASE_URL, value);
    } catch {
      // Storage can be unavailable (private browsing, quota) -- the
      // in-memory state above still works for the rest of this session.
    }
  }, []);

  const setApiKey = useCallback((value: string) => {
    setApiKeyState(value);
    try {
      window.localStorage.setItem(STORAGE_KEY_API_KEY, value);
    } catch {
      // Same fallback as setBaseUrl above.
    }
  }, []);

  const value = useMemo<ApiConfigValue>(
    () => ({ baseUrl, apiKey, isConfigured: apiKey.length > 0, setBaseUrl, setApiKey }),
    [baseUrl, apiKey, setBaseUrl, setApiKey],
  );

  return <ApiConfigContext.Provider value={value}>{children}</ApiConfigContext.Provider>;
}

export function useApiConfig(): ApiConfigValue {
  const value = useContext(ApiConfigContext);
  if (!value) {
    throw new Error("useApiConfig must be used within an ApiConfigProvider");
  }
  return value;
}
