/**
 * OBS-08 shell + navigation. `docs/LOW_LEVEL_IMPLEMENTATION.md` names
 * `/overview`, `/requests`, `/requests/:traceId`, `/reliability`,
 * `/models` as the target route set. This nav has all six: `/overview`
 * (`OverviewPage`), `/requests` (`RequestsPage` -- `GET /v0.1/ai/requests`,
 * one row per span, the true top-level request list), an `/evaluations`
 * list+detail pair (one row per *evaluation*, not per request -- see
 * `EvaluationsListPage.tsx`'s own docstring for why that is a separate
 * page, not what `/requests` itself renders), `/trace`
 * (`docs/LOW_LEVEL_IMPLEMENTATION.md`'s `/requests/:traceId`, under a
 * clearer top-level name since it is a direct trace_id lookup, not
 * nested under the requests list), `/reliability` (`ReliabilityPage`),
 * and `/models` (`ModelsPage`).
 *
 * Semantic HTML, keyboard-operable nav (`docs/dashboard.md`: "Use
 * semantic HTML and keyboard-operable tables/filters"): a plain `<nav>`
 * of `<NavLink>`s, every one a real focusable anchor-rendered element.
 */

import { NavLink, Outlet } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/overview", label: "Overview" },
  { to: "/requests", label: "Requests" },
  { to: "/evaluations", label: "Evaluations" },
  { to: "/trace", label: "Trace Explorer" },
  { to: "/reliability", label: "Reliability" },
  { to: "/models", label: "Models" },
  { to: "/settings", label: "Settings" },
];

export function AppShell() {
  return (
    <div className="app-shell">
      <header>
        <h1>FactLama Observability</h1>
      </header>
      <nav aria-label="Primary">
        <ul>
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} className={({ isActive }) => (isActive ? "active" : "")}>
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
