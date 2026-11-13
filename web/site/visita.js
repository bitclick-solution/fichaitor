/* Fichaitor: cuenta una visita a la portada (POST /api/v1/public/visit).
   Sin cookies, sin almacenamiento y sin identificadores: solo el origen de la campaña, si lo hay.
   Solo se carga en la portada con API; en el modo «solo web» no se incluye (web/build.py). */
(() => {
  "use strict";
  if (!navigator.sendBeacon) return;

  // utm_source o ref, en minúsculas, solo [a-z0-9_-] y 40 caracteres como mucho. Si no queda nada, sin origen.
  const q = new URLSearchParams(location.search);
  const crudo = (q.get("utm_source") || q.get("ref") || "").trim().toLowerCase();
  const limpio = crudo.replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  const cuerpo = JSON.stringify(limpio ? { source: limpio } : {});

  const enviar = () => {
    try {
      navigator.sendBeacon("/api/v1/public/visit", cuerpo);
    } catch {
      /* contar la visita nunca puede romper la página */
    }
  };

  // Una página precargada por el navegador no es una visita hasta que se muestra.
  if (document.prerendering) document.addEventListener("prerenderingchange", enviar, { once: true });
  else enviar();
})();
