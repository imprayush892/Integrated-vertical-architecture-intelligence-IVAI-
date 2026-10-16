import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the engine runs on 127.0.0.1:8765 (planner serve); vite proxies the API to it.
// Build: the output is copied into the engine package (served by the engine and the desktop shell).
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/rpc": "http://127.0.0.1:8765",
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    target: "es2022",
    sourcemap: false,
    chunkSizeWarningLimit: 2500,
  },
  test: { environment: "node" },
} as never);
