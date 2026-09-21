import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to dist/, which the Starlette app serves at "/".
// During `npm run dev`, /api is proxied to the running Hyqs backend.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8787" },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.js"],
  },
});
