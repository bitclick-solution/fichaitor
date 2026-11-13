import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Backoffice de Fichaitor (docs/contrato-backoffice.md). Lo sirve `python -m app.admin` en la raíz de
// http://localhost:8090, por el túnel, junto a /admin/api/*. Mismo origen: sin CORS.
export default defineConfig({
  base: "/",
  plugins: [react()],
  server: {
    port: 5174,
    // Solo accesible desde este equipo.
    host: "127.0.0.1",
    // En desarrollo, contra el proceso del backoffice en local.
    // El backoffice solo acepta Host y Origin propios (127.0.0.1:8090): el proxy los reescribe.
    proxy: {
      "/admin/api": {
        target: "http://127.0.0.1:8090",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => {
            if (req.getHeader("origin")) req.setHeader("origin", "http://127.0.0.1:8090");
          });
        },
      },
    },
    // La fuente de la marca vive fuera de esta carpeta. Se permite solo eso.
    fs: { allow: [".", "../site/fonts"] },
  },
  // Sin mapas de código en producción y sin nada en línea: la CSP solo permite 'self'.
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
});
