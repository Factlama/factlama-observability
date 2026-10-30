/**
 * OBS-08: the dashboard's route tree. See `components/AppShell.tsx`'s
 * own docstring for the route-naming rationale (`/requests` is the real
 * per-span list now that `GET /v0.1/ai/requests` exists; `/evaluations`
 * and `/trace` keep their own separate, non-"requests" names for the
 * reasons each page's own docstring gives).
 */
import { Navigate, Route, BrowserRouter, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell.tsx";
import { ApiConfigProvider } from "./lib/apiConfig.tsx";
import { EvaluationDetailPage } from "./pages/EvaluationDetailPage.tsx";
import { EvaluationsListPage } from "./pages/EvaluationsListPage.tsx";
import { ModelsPage } from "./pages/ModelsPage.tsx";
import { OverviewPage } from "./pages/OverviewPage.tsx";
import { ReliabilityPage } from "./pages/ReliabilityPage.tsx";
import { RequestsPage } from "./pages/RequestsPage.tsx";
import { SettingsPage } from "./pages/SettingsPage.tsx";
import { TraceExplorerPage } from "./pages/TraceExplorerPage.tsx";

export function App() {
  return (
    <ApiConfigProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Navigate to="/overview" replace />} />
            <Route path="overview" element={<OverviewPage />} />
            <Route path="requests" element={<RequestsPage />} />
            <Route path="evaluations" element={<EvaluationsListPage />} />
            <Route path="evaluations/:evaluationId" element={<EvaluationDetailPage />} />
            <Route path="trace" element={<TraceExplorerPage />} />
            <Route path="reliability" element={<ReliabilityPage />} />
            <Route path="models" element={<ModelsPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ApiConfigProvider>
  );
}
