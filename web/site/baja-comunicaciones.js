/* Fichaitor: baja de las comunicaciones comerciales (POST /api/v1/public/unsubscribe).
   Contrato: docs/contrato-web-api.md, «Baja de las comunicaciones comerciales».
   - El token llega en el fragmento (#…) del enlace del correo. Se lee, se quita de la barra de
     direcciones al momento y solo viaja en el cuerpo del POST: ni cookies, ni almacenamiento,
     ni analítica, ni registro de errores.
   - Solo se llama a la API al pulsar el botón: un analizador de enlaces que ejecute JavaScript
     no da de baja a nadie. (El «un clic» del cliente de correo va por List-Unsubscribe.)
   - La API responde siempre 204, valga o no el token: la página enseña siempre el mismo texto.
   Solo existe con API; en el modo «solo web» nginx responde 404 (web/nginx-solo-web.conf). */
(() => {
  "use strict";

  const token = location.hash.replace(/^#/, "").trim();
  if (location.hash) {
    try {
      history.replaceState(null, "", location.pathname + location.search);
    } catch {
      /* sin historial no se puede limpiar la barra; el token sigue sin salir de la página */
    }
  }

  const $ = (id) => document.getElementById(id);
  const pedir = $("baja-pedir");
  const boton = $("baja-boton");
  const error = $("baja-error");
  const hecho = $("baja-hecho");
  const sin = $("baja-sin");
  if (!pedir || !boton || !error || !hecho || !sin) return;

  if (!token) {
    pedir.hidden = true;
    sin.hidden = false;
    return;
  }

  const TEXTO_429 = "Demasiados intentos. Prueba dentro de un rato.";
  const TEXTO_503 = "Ahora mismo no podemos hacerlo. Prueba otra vez en un minuto.";
  let enviando = false;

  const fallo = (texto) => {
    error.textContent = texto;
    error.hidden = false;
    boton.disabled = false;
    enviando = false;
  };

  boton.addEventListener("click", async () => {
    if (enviando) return;
    enviando = true;
    boton.disabled = true;
    error.hidden = true;
    let r;
    try {
      r = await fetch("/api/v1/public/unsubscribe", {
        method: "POST",
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
    } catch {
      fallo(TEXTO_503);
      return;
    }
    if (r.ok) {
      pedir.hidden = true;
      hecho.hidden = false;
      const aviso = $("baja-hecho-texto");
      if (aviso) aviso.focus();
      return;
    }
    fallo(r.status === 429 ? TEXTO_429 : TEXTO_503);
  });

  boton.hidden = false;
})();
