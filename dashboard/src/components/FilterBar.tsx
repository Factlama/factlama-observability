/**
 * OBS-08: the shared time-window/project/application filter control --
 * `lib/dashboardFilters.ts` owns the URL state this renders/edits.
 * Plain, keyboard-operable form controls (`docs/dashboard.md`: "Use
 * semantic HTML and keyboard-operable tables/filters"), not a custom
 * widget.
 */

import { WINDOW_PRESETS, type DashboardFilters, type WindowPreset } from "../lib/dashboardFilters";

const WINDOW_PRESET_LABELS: Record<WindowPreset, string> = {
  "1h": "Last hour",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
};

export function FilterBar({ filters }: { filters: DashboardFilters }) {
  const { windowPreset, projectId, applicationId, setWindowPreset, setProjectId, setApplicationId } = filters;
  return (
    <form className="filter-bar" aria-label="Filters" onSubmit={(event) => event.preventDefault()}>
      <label htmlFor="filter-window">
        Time window
        <select
          id="filter-window"
          value={windowPreset}
          onChange={(event) => setWindowPreset(event.target.value as WindowPreset)}
        >
          {WINDOW_PRESETS.map((preset) => (
            <option key={preset} value={preset}>
              {WINDOW_PRESET_LABELS[preset]}
            </option>
          ))}
        </select>
      </label>
      <label htmlFor="filter-project">
        Project
        <input
          id="filter-project"
          type="text"
          value={projectId}
          placeholder="all authorized projects"
          onChange={(event) => setProjectId(event.target.value)}
        />
      </label>
      <label htmlFor="filter-application">
        Application
        <input
          id="filter-application"
          type="text"
          value={applicationId}
          placeholder="all authorized applications"
          onChange={(event) => setApplicationId(event.target.value)}
        />
      </label>
    </form>
  );
}
