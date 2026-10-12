import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
  // `vite preview` serves the production bundle for the local real-stack browser run; it needs the
  // same same-origin proxy that nginx provides in the container topology.
  preview: {
    proxy: {
      "/api": `http://127.0.0.1:${process.env.E2E_API_PORT ?? "8000"}`,
      "/health": `http://127.0.0.1:${process.env.E2E_API_PORT ?? "8000"}`,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    css: true,
    coverage: {
      reporter: ["text", "html"],
    },
  },
});
