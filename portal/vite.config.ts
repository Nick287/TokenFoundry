import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: `vite dev` on :5173 proxies /api to the FastAPI backend on :8000.
// Prod: the build output is baked into the backend image and served by FastAPI
// StaticFiles (same origin) — no separate web server.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
