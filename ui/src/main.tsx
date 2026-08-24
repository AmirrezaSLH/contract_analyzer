import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { App } from "./App";
import { UploadView } from "./views/Upload/UploadView";
import { LibraryView } from "./views/Library/LibraryView";
import { AnalysisView } from "./views/Analysis/AnalysisView";
import { ChatView } from "./views/Chat/ChatView";
import { MetricsView } from "./views/Metrics/MetricsView";
import { LogsView } from "./views/Logs/LogsView";
import { NotFoundView } from "./views/NotFoundView";
import { RootRedirect } from "./views/RootRedirect";
import "./styles/global.css";

/**
 * The gap-state fixture page, in development only.
 *
 * `lazy` around a dynamic `import()` inside a dead `import.meta.env.DEV`
 * branch is what keeps 24 KB of constructed compliance data out of the
 * production bundle. A static import would not: Vite replaces the flag with
 * `false`, but a top-level `import` of the view keeps its JSON reachable, and
 * Rollup ships it. This shape leaves nothing behind to ship.
 */
const GapFixtureView = import.meta.env.DEV
  ? lazy(() =>
      import("./views/Analysis/GapFixtureView").then((m) => ({ default: m.GapFixtureView })),
    )
  : null;

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // The window regaining focus is not news about a contract. The one thing
      // that does poll is the analysis, and it says so itself.
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 10_000,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Routes>
          <Route element={<App />}>
            <Route index element={<RootRedirect />} />
            <Route path="upload" element={<UploadView />} />
            <Route path="library" element={<LibraryView />} />
            {/* `:id` is the single source of truth for scope. The sidebar
                reads it; nothing writes a copy of it into React state. */}
            <Route path="documents/:id/analysis" element={<AnalysisView />} />
            <Route path="documents/:id/chat" element={<ChatView />} />
            {/* Application-level: it spans every document, so it carries no
                `:id` and shows no document tabs. */}
            <Route path="metrics" element={<MetricsView />} />
            <Route path="logs" element={<LogsView />} />
            {/* The sample contract is all-green, so every gap state is
                unreachable from real data. This is where they get looked at. */}
            {GapFixtureView ? (
              <Route
                path="_gaps"
                element={
                  <Suspense fallback={null}>
                    <GapFixtureView />
                  </Suspense>
                }
              />
            ) : null}
            <Route path="*" element={<NotFoundView />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);

export { Navigate };
