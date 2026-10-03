import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Asset URL base.  "/" (default) is required for the self-hosted server
  // (`python -m server`) so deep links like /trials/NCT/x can reload — the
  // SPA history fallback serves index.html there, and relative "./assets"
  // URLs would resolve against the deep path and come back as text/html.
  // The GitHub Pages build keeps "./" via CT_WEB_BASE (see .github/workflows/web.yml).
  base: process.env.CT_WEB_BASE || "/",
  // `npm run dev` proxies API calls to the local FastAPI server
  // (`venv/bin/python -m server`), so the live-data mode works in dev too.
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
