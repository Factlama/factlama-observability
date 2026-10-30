/**
 * OBS-08: shared URL-represented filter state (time window, project,
 * application) -- `docs/storage-query.md`: "authorized project/application
 * filters[,] bounded UTC from/to." Before this, every page computed its
 * own fixed last-24h window in local state (`OverviewPage.tsx`'s own
 * former `useOverviewWindow`), invisible to the URL -- a bookmarked or
 * shared link always reopened to the same default window/scope, never
 * the one actually being looked at. `query/api/app.py`'s `_narrow_tenant`
 * is the server-side half of the project/application filter: a caller
 * authorized for a broad scope may narrow one read; a caller already
 * narrowed by its own API key gets a 403 (`ApiError`'s `"forbidden"`
 * kind) if it asks for a different one.
 */

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export const WINDOW_PRESETS = ["1h", "24h", "7d", "30d"] as const;
export type WindowPreset = (typeof WINDOW_PRESETS)[number];

const DEFAULT_WINDOW_PRESET: WindowPreset = "24h";

const WINDOW_PRESET_MS: Record<WindowPreset, number> = {
  "1h": 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
};

function isWindowPreset(value: string | null): value is WindowPreset {
  return value !== null && (WINDOW_PRESETS as readonly string[]).includes(value);
}

export interface DashboardFilters {
  windowPreset: WindowPreset;
  since: string;
  until: string;
  projectId: string;
  applicationId: string;
  setWindowPreset: (preset: WindowPreset) => void;
  setProjectId: (value: string) => void;
  setApplicationId: (value: string) => void;
}

/**
 * Reads/writes `window`, `project_id`, `application_id` search params.
 * `since`/`until` are derived from `windowPreset` and "now" at the moment
 * this hook runs -- recomputed only when the preset itself changes (not
 * on every render), the same one-shot-per-selection rationale
 * `OverviewPage.tsx`'s former `useOverviewWindow` already used for its
 * own fixed window.
 */
export function useDashboardFilters(): DashboardFilters {
  const [searchParams, setSearchParams] = useSearchParams();

  const windowParam = searchParams.get("window");
  const windowPreset = isWindowPreset(windowParam) ? windowParam : DEFAULT_WINDOW_PRESET;
  const projectId = searchParams.get("project_id") ?? "";
  const applicationId = searchParams.get("application_id") ?? "";

  const { since, until } = useMemo(() => {
    const untilDate = new Date();
    const sinceDate = new Date(untilDate.getTime() - WINDOW_PRESET_MS[windowPreset]);
    return { since: sinceDate.toISOString(), until: untilDate.toISOString() };
  }, [windowPreset]);

  const setWindowPreset = useCallback(
    (preset: WindowPreset) => {
      const next = new URLSearchParams(searchParams);
      if (preset === DEFAULT_WINDOW_PRESET) next.delete("window");
      else next.set("window", preset);
      setSearchParams(next);
    },
    [searchParams, setSearchParams],
  );

  const setProjectId = useCallback(
    (value: string) => {
      const next = new URLSearchParams(searchParams);
      if (value) next.set("project_id", value);
      else next.delete("project_id");
      setSearchParams(next);
    },
    [searchParams, setSearchParams],
  );

  const setApplicationId = useCallback(
    (value: string) => {
      const next = new URLSearchParams(searchParams);
      if (value) next.set("application_id", value);
      else next.delete("application_id");
      setSearchParams(next);
    },
    [searchParams, setSearchParams],
  );

  return { windowPreset, since, until, projectId, applicationId, setWindowPreset, setProjectId, setApplicationId };
}
