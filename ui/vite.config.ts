/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The ports live in `.env` at the repository root, beside the API's own
// settings, rather than in a second file here: BACKEND_PORT is the one port a
// demo needs, and a dev server that guesses it is a dev server that proxies to
// nothing the first time someone moves the API.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "..", "");
  const backend = env.BACKEND_PORT || "8100";
  const frontend = Number(env.FRONTEND_PORT || 8101);

  return {
    plugins: [react()],
    server: {
      port: frontend,
      strictPort: true,
      // The whole reason there is no CORS configuration in this project: the
      // browser sees one origin in development too. `/api` is the API's only
      // prefix, so this is the only rule the proxy needs.
      proxy: {
        "/api": {
          target: `http://localhost:${backend}`,
          changeOrigin: true,
          // Chat and analysis progress are server-sent events. Buffering them
          // would turn a token stream into one delivery at the end, which
          // looks exactly like a hung request.
          configure: (proxy) => {
            proxy.on("proxyRes", (proxyRes) => {
              if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
                proxyRes.headers["cache-control"] = "no-cache, no-transform";
              }
            });
          },
        },
      },
    },
    build: {
      // Into the API package, so `StaticFiles` finds the bundle with no copy
      // step and no volume mount. Gitignored: it is a build artefact.
      outDir: "../src/contract_analyzer/api/static",
      emptyOutDir: true,
    },
    test: {
      environment: "node",
      include: ["test/**/*.test.ts"],
    },
  };
});
