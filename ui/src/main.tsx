import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { App } from "./App";
import { UploadView } from "./views/Upload/UploadView";
import { LibraryView } from "./views/Library/LibraryView";
import { AnalysisView } from "./views/Analysis/AnalysisView";
import { ChatView } from "./views/Chat/ChatView";
import { NotFoundView } from "./views/NotFoundView";
import { RootRedirect } from "./views/RootRedirect";
import "./styles/global.css";

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
            <Route path="*" element={<NotFoundView />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);

export { Navigate };
