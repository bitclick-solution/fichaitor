import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// La aplicación se sirve bajo /app/. El sitio público (web/site) lo sirve nginx en la raíz.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    // Solo accesible desde este equipo.
    host: "127.0.0.1",
    proxy: { "/api": "http://localhost:8000" },
    // La fuente y el logo viven fuera de esta carpeta. Se permite solo eso, no todo el repositorio.
    fs: { allow: [".", "../site/fonts", "../../brand/logo.svg"] },
  },
  build: { outDir: "dist", sourcemap: false },
});
