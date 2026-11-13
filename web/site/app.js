/* Fichaitor — movimiento de la portada. Reglas en brand/DESIGN.md, apartado 7. */
(() => {
  "use strict";

  const CONSENT_VERSION = "waitlist-2026-11-11";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const M = window.Motion;
  const EASE = [0.16, 1, 0.3, 1];
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));

  /* ── Titular: rota entre los dos ganchos en cada visita, al azar y sin guardar nada ──
     El HTML trae «Tu ficha de Google, al día.» (lo que ven los buscadores y quien no tiene JavaScript).
     Se cambia antes de que las líneas entren en pantalla (su animación empieza a los 0,15 s). */
  const TITULARES = [
    ["Tu ficha", "de Google,", "al día"],
    ["Ninguna", "reseña sin", "contestar"],
  ];
  const lineasH1 = $$("#h1 .ln > span");
  if (lineasH1.length === 3) {
    const elegido = TITULARES[Math.floor(Math.random() * TITULARES.length)];
    lineasH1.forEach((linea, i) => {
      // La última línea lleva el punto rojo en su propio <span class="pt">: se cambia solo el texto.
      const texto = Array.from(linea.childNodes).find((n) => n.nodeType === Node.TEXT_NODE);
      if (texto) texto.nodeValue = elegido[i];
    });
  }

  /* ── Scroll suave ── */
  let lenis = null;
  if (!reduce && window.Lenis) {
    lenis = new window.Lenis({ lerp: 0.085 });
    const raf = (t) => { lenis.raf(t); requestAnimationFrame(raf); };
    requestAnimationFrame(raf);
  }

  /* ── Secciones apiladas: cada una se queda fija y la siguiente la tapa ── */
  const secciones = $$(".s");
  const menu = $("#menu");
  let tops = [], altos = [], vh = innerHeight;

  function medir() {
    vh = innerHeight;
    let y = 0;
    tops = []; altos = [];
    for (const s of secciones) {
      const h = s.offsetHeight;
      s.style.setProperty("--top", Math.min(0, vh - h) + "px");
      tops.push(y); altos.push(h);
      y += h;
    }
    pintar();
  }

  /* ── Escena de la ficha ── */
  const escena = $(".escena");
  const iEscena = secciones.indexOf(escena);
  const pasos = $$("#pasos .paso");
  const marcas = $$("#avance i");
  const paneles = $$("#ficha .panel");
  const est = $("#est"), cuenta = $("#cuenta"), contesta = $("#contesta"), fest = $("#fest");
  const aviso = $("#aviso"), avisoT = $("#aviso-t");
  const PANEL = [0, 1, 1, 3];
  const AVISO = { 1: "Reseña nueva", 3: "Festivo a la vista" };
  let pasoActual = -1;

  function ponerPaso(i, sub) {
    fest.classList.toggle("hecho", i > 3 || (i === 3 && sub > 0.45));
    if (i === pasoActual) return;
    pasoActual = i;
    pasos.forEach((p, k) => { p.classList.toggle("on", k === i); p.classList.toggle("pasado", k < i); });
    marcas.forEach((m, k) => m.classList.toggle("on", k <= i));
    paneles.forEach((p) => p.classList.toggle("on", Number(p.dataset.p) === PANEL[i]));
    contesta.classList.toggle("on", i >= 2);
    cuenta.textContent = i >= 1 ? "39" : "38";
    aviso.classList.toggle("on", i in AVISO);
    if (i in AVISO) avisoT.textContent = AVISO[i];
  }

  /* ── Promesa: las palabras se encienden con el scroll ── */
  const promesaSec = $(".promesa");
  const iPromesa = secciones.indexOf(promesaSec);
  const promesa = $("#promesa");
  const punto = $(".pt", promesa);
  const palabras = promesa.firstChild.textContent.trim().split(/\s+/);
  promesa.firstChild.remove();
  const frag = document.createDocumentFragment();
  palabras.forEach((w, k) => {
    const s = document.createElement("span");
    s.className = "w";
    s.textContent = w;
    if (k === palabras.length - 1) s.append(punto);
    frag.append(s, " ");
  });
  promesa.prepend(frag);
  const ws = $$(".w", promesa);
  if (reduce) ws.forEach((w) => w.classList.add("on"));

  /* ── Pintado ligado al scroll ── */
  const sello = $("#sello");
  let pendiente = false;

  function pintar() {
    pendiente = false;
    const y = scrollY;

    // Tema del menú: la última sección cuyo inicio ya ha pasado bajo el menú
    let actual = 0;
    for (let i = 0; i < tops.length; i++) if (tops[i] <= y + 48) actual = i;
    menu.dataset.tema = secciones[actual].dataset.tema || "claro";

    // Salida: la sección que queda debajo se encoge y se apaga mientras la siguiente la tapa
    if (!reduce) {
      for (let i = 0; i < secciones.length - 1; i++) {
        const el = $("[data-sale]", secciones[i]) || $(".in", secciones[i]);
        if (!el) continue;
        const p = clamp((y + vh - tops[i + 1]) / vh);
        el.style.transform = p > 0 ? `scale(${1 - 0.07 * p})` : "";
        el.style.opacity = p > 0 ? String(1 - 0.65 * p) : "";
      }
      if (sello) sello.style.transform = `rotate(${-5 + clamp(y / vh) * 14}deg) translateY(${clamp(y / vh) * -60}px)`;
    }

    // Escena
    if (iEscena >= 0) {
      const p = clamp((y - tops[iEscena]) / Math.max(1, altos[iEscena] - vh));
      const f = Math.min(pasos.length - 0.001, p * pasos.length);
      ponerPaso(Math.floor(f), f % 1);
      if (y + vh * 0.6 > tops[iEscena]) est.style.setProperty("--v", "82%");
    }

    // Promesa
    if (iPromesa >= 0 && !reduce) {
      const p = clamp((y - tops[iPromesa] + vh * 0.35) / Math.max(1, altos[iPromesa] - vh * 0.75));
      const n = Math.ceil(p * ws.length);
      ws.forEach((w, k) => w.classList.toggle("on", k < n));
    }
  }

  addEventListener("scroll", () => { if (!pendiente) { pendiente = true; requestAnimationFrame(pintar); } }, { passive: true });
  addEventListener("resize", medir);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(medir);
  addEventListener("load", medir);
  medir();

  /* ── Salto de sección (brand/DESIGN.md, apartado 11) ──
     Al detenerse el scroll, la página se asienta en el punto más cercano en el sentido del
     movimiento: el inicio de una sección o, en las escenas ancladas, cada uno de sus pasos.
     Dentro de una sección que no cabe en pantalla, el scroll es libre. */
  if (lenis) {
    const UMBRAL = 0.08; // fracción de la altura de pantalla que hay que moverse para cambiar de punto
    let asentado = scrollY, saltando = false, reposo = 0;

    const mapa = () => {
      const puntos = [], libres = [];
      const fin = Math.max(0, document.documentElement.scrollHeight - vh);
      secciones.forEach((s, i) => {
        const top = tops[i], sobra = altos[i] - vh;
        if (i === iEscena && sobra > 0) {
          for (let k = 0; k < pasos.length; k++) puntos.push(top + ((k + 0.5) / pasos.length) * sobra);
        } else if (i === iPromesa && sobra > 0) {
          puntos.push(top, top + sobra);
        } else if (sobra > 24) {
          // No cabe en pantalla: se puede leer con scroll libre entre su inicio y su final
          puntos.push(top, top + sobra);
          libres.push([top + 0.2 * vh, top + sobra]);
        } else {
          puntos.push(top);
        }
      });
      const unicos = [...new Set(puntos.map((p) => Math.round(Math.min(p, fin))))].sort((a, b) => a - b);
      return { puntos: unicos, libres };
    };

    const asentar = () => {
      if (saltando || document.documentElement.style.overflow === "hidden") return;
      const y = scrollY;
      const { puntos, libres } = mapa();
      if (libres.some(([a, b]) => y > a && y < b)) { asentado = y; return; }

      let arriba = puntos[0], abajo = puntos[puntos.length - 1];
      for (const p of puntos) { if (p <= y + 1) arriba = p; }
      for (let k = puntos.length - 1; k >= 0; k--) { if (puntos[k] > y + 1) abajo = puntos[k]; }
      if (abajo < arriba) abajo = arriba;

      const baja = y >= asentado;
      const destino = baja ? (y - arriba > UMBRAL * vh ? abajo : arriba) : (abajo - y > UMBRAL * vh ? arriba : abajo);
      asentado = destino;
      if (Math.abs(destino - y) < 2) return;

      saltando = true;
      const soltar = () => { saltando = false; };
      lenis.scrollTo(destino, { duration: 0.8, onComplete: soltar });
      setTimeout(soltar, 1100);
    };

    addEventListener("scroll", () => {
      clearTimeout(reposo);
      if (!saltando) reposo = setTimeout(asentar, 140);
    }, { passive: true });
  }

  /* ── Enlaces internos: van al inicio natural de la sección ── */
  function irA(id) {
    const i = secciones.findIndex((s) => s.id === id);
    if (i < 0) return false;
    if (lenis) lenis.scrollTo(tops[i], { duration: 1.4 });
    else scrollTo({ top: tops[i], behavior: reduce ? "auto" : "smooth" });
    return true;
  }
  $$('a[href^="#"]').forEach((a) => a.addEventListener("click", (e) => {
    if (irA(a.getAttribute("href").slice(1))) { e.preventDefault(); history.replaceState(null, "", a.getAttribute("href")); }
  }));
  if (location.hash) addEventListener("load", () => irA(location.hash.slice(1)));

  /* ── Entradas ── */
  const lineas = (raiz) => $$(".ln > span", raiz);
  const visible = (el) => { el.style.transform = "none"; el.style.opacity = "1"; };

  if (reduce || !M) {
    $$(".ln > span, [data-sube], #hilo > *").forEach(visible);
  } else {
    const { animate, inView, stagger } = M;
    const subirLineas = (els, delay = 0) => els.length && animate(els, { transform: ["translateY(130%)", "translateY(0%)"] }, { duration: 0.9, delay: stagger(0.09, { startDelay: delay }), ease: EASE });
    const subir = (els, delay = 0) => els.length && animate(els, { opacity: [0, 1], transform: ["translateY(36px)", "translateY(0px)"] }, { duration: 0.8, delay: stagger(0.1, { startDelay: delay }), ease: EASE });

    // El héroe entra con CSS (styles.css): no depende de este script
    const hero = $(".hero");

    // Resto, al entrar en pantalla
    secciones.filter((s) => s !== hero).forEach((s) => {
      const ls = lineas(s), bs = $$("[data-sube]", s);
      if (!ls.length && !bs.length) return;
      inView(s, () => { subirLineas(ls); subir(bs, 0.25); }, { amount: 0.15 });
    });

    // Conversación: las burbujas llegan una a una
    const hilo = $("#hilo");
    if (hilo) inView(hilo, () => {
      animate(Array.from(hilo.children), { opacity: [0, 1], transform: ["translateY(16px) scale(0.96)", "translateY(0px) scale(1)"] }, { duration: 0.5, delay: stagger(0.55, { startDelay: 0.2 }), ease: EASE });
    }, { amount: 0.4 });
  }

  /* ── Origen de la visita: parámetro ?s= (contrato web-API) ── */
  const origen = () => {
    const u = new URLSearchParams(location.search).get("s") || "web";
    return /^[A-Za-z0-9_\-.]{1,60}$/.test(u) ? u : "web";
  };
  let ficha = {}; // place_id, business_query (lo que escribió el usuario) y score del último diagnóstico, para la lista de espera

  /* ── Diagnóstico público ── */
  const diag = $("#diag"), panel = $("#resultado");
  if (diag && panel) {
    const entrada = $("#negocio"), nota = $("#diag-salida"), botonD = $("button", diag);
    const AYUDA = nota.textContent;
    const decirD = (t, mal = false) => { nota.textContent = t; nota.classList.toggle("mal", mal); };
    const IMPACTO = { alto: "Impacto alto", medio: "Impacto medio", bajo: "Impacto bajo" };
    const el = (tag, texto, cls) => { const n = document.createElement(tag); if (texto) n.textContent = texto; if (cls) n.className = cls; return n; };

    function contar(nodo, hasta) {
      if (reduce) { nodo.textContent = String(hasta); return; }
      const t0 = performance.now(), dur = 1100;
      const paso = (t) => {
        const p = clamp((t - t0) / dur);
        nodo.textContent = String(Math.round(hasta * (1 - Math.pow(1 - p, 3))));
        if (p < 1) requestAnimationFrame(paso);
      };
      requestAnimationFrame(paso);
    }

    function abrir(d, consulta) {
      // Se guarda lo que escribió el usuario, no el nombre que devuelve Google (términos de Google Places).
      // Tras una captura no hay place_id ni texto de búsqueda: solo se envía la nota.
      ficha = { score: d.score };
      if (d.place_id) ficha.place_id = d.place_id;
      if (consulta) ficha.business_query = consulta;
      const noVisto = (d.not_visible || []).map((n) => (typeof n === "string" ? n : n && n.title)).filter(Boolean);
      $("#res-novisto").replaceChildren(...noVisto.map((t) => el("li", t)));
      $("#res-novisto-b").hidden = noVisto.length === 0;
      const atr = typeof d.attribution === "string" ? d.attribution.trim() : "";
      $("#res-fuente").textContent = !atr || atr === "Google Maps" ? "Datos de Google Maps" : atr;
      $("#res-nombre").textContent = d.name;
      $("#res-dir").textContent = [d.category, d.address].filter(Boolean).join(" · ");
      $("#res-etq").textContent = d.label;
      $(".res-nota", panel).dataset.nivel = d.score < 60 ? "mal" : "bien";
      const lista = $("#res-hallazgos");
      lista.replaceChildren();
      if (!d.findings || !d.findings.length) lista.append(el("li", "No hemos encontrado nada que mejorar con lo que se ve.", "vacio"));
      else for (const f of d.findings) {
        const li = el("li");
        li.append(el("b", f.title), el("span", f.detail));
        if (IMPACTO[f.impact]) li.append(el("em", IMPACTO[f.impact]));
        lista.append(li);
      }
      const candados = $("#res-candados");
      candados.replaceChildren(...(d.locked_checks || []).map((c) => el("li", c.title)));
      const mapa = $("#res-mapa");
      if (d.maps_url && /^https:\/\//.test(d.maps_url)) { mapa.href = d.maps_url; mapa.parentElement.hidden = false; } else mapa.parentElement.hidden = true;

      panel.hidden = false;
      panel.scrollTop = 0;
      if (lenis) lenis.stop();
      document.documentElement.style.overflow = "hidden";
      requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add("abierto")));
      contar($("#res-num"), d.score);
      $("#res-nombre").focus({ preventScroll: true });
    }

    function cerrar() {
      panel.classList.remove("abierto");
      document.documentElement.style.overflow = "";
      if (lenis) lenis.start();
      setTimeout(() => { panel.hidden = true; }, reduce ? 0 : 700);
      (diag.hidden ? $("#entrada .btn") : entrada).focus({ preventScroll: true });
    }
    $("#res-cerrar").addEventListener("click", cerrar);
    addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) cerrar(); });
    $("#res-lista").addEventListener("click", () => { cerrar(); irA("lista"); });

    diag.addEventListener("submit", async (e) => {
      e.preventDefault();
      const query = entrada.value.trim();
      if (query.length < 3) { decirD("Escribe el nombre de tu negocio y tu localidad.", true); entrada.focus(); return; }
      botonD.disabled = true; decirD("Mirando tu ficha…");
      try {
        const r = await fetch("/api/v1/public/diagnosis", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ query, source: origen() }),
        });
        if (r.ok) { decirD(AYUDA); abrir(await r.json(), query); return; }
        let detalle = "";
        try { const j = await r.json(); if (typeof j.detail === "string") detalle = j.detail; } catch (_) { /* sin cuerpo */ }
        const porDefecto = { 404: "No encontramos ese negocio. Prueba con el nombre y la localidad.", 429: "Demasiadas consultas. Prueba dentro de un rato.", 503: "Google no responde ahora mismo. Prueba en un minuto." };
        decirD(detalle || porDefecto[r.status] || "No hemos podido analizar la ficha. Prueba en un minuto.", true);
      } catch (err) {
        console.error("Fichaitor: fallo de red en el diagnóstico", err);
        decirD("No hemos podido conectar. Recarga la página e inténtalo de nuevo.", true);
      } finally {
        botonD.disabled = false;
      }
    });

    /* ── Prueba con una captura de las reseñas (entrada secundaria; la principal es conectar con Google) ── */
    const captura = $("#captura"), prueba = $("#prueba");
    if (captura && prueba) {
      // El buscador por nombre queda como variante: solo con ?v=buscador
      if (new URLSearchParams(location.search).get("v") === "buscador") { diag.hidden = false; $("#entrada").hidden = true; }

      const texto = (v) => (typeof v === "string" ? v.trim() : "");
      function abrirPrueba(d) {
        ficha = {}; // tras la captura no hay nota ni ficha que adjuntar a la lista de espera
        const vistas = Number.isFinite(d.reviews_seen) ? d.reviews_seen : null;
        const sin = Number.isFinite(d.unanswered) ? d.unanswered : null;
        const bajas = Number.isFinite(d.unanswered_low) ? d.unanswered_low : 0;
        const titular = texto(d.headline) || (vistas !== null && sin !== null ? `Veo ${vistas} reseñas y ${sin} están sin contestar.` : "Esto es lo que veo en tu captura.");
        const h = $("#pr-titular");
        h.textContent = titular.replace(/\.$/, "");
        h.append(el("span", ".", "pt"));
        $("#pr-negocio").textContent = texto(d.business_name);
        $("#pr-negocio").hidden = !texto(d.business_name);
        const pb = $("#pr-bajas");
        pb.textContent = bajas === 1 ? "1 de las que están sin contestar es de 1 o 2 estrellas." : `${bajas} de las que están sin contestar son de 1 o 2 estrellas.`;
        // El titular de la API ya menciona las de 1 o 2 estrellas: el aviso aparte solo sale si el titular se compuso aquí.
        pb.hidden = bajas <= 0 || Boolean(texto(d.headline));
        $("#pr-fuente").textContent = texto(d.attribution) || "Según la captura que has enviado";

        const s = d.suggested_reply, respuesta = s ? texto(s.reply) : "";
        $("#pr-respuesta-b").hidden = !respuesta;
        if (respuesta) {
          const n = Number.isFinite(s.stars) ? Math.min(5, Math.max(0, Math.round(s.stars))) : 0;
          $("#pr-estrellas").textContent = n ? "★".repeat(n) + "☆".repeat(5 - n) : "";
          $("#pr-estrellas-t").textContent = n ? `Reseña de ${n} ${n === 1 ? "estrella" : "estrellas"}` : "";
          $("#pr-extracto").textContent = texto(s.review_excerpt) ? `«${texto(s.review_excerpt)}»` : (n ? `Reseña sin texto, ${n} ${n === 1 ? "estrella" : "estrellas"}` : "Reseña sin texto");
          $("#pr-respuesta").textContent = respuesta;
          $("#pr-copiar").textContent = "Copiar respuesta";
        }
        $("#pr-candados").replaceChildren(...(d.locked_checks || []).map((c) => el("li", c.title)));

        prueba.hidden = false;
        prueba.scrollTop = 0;
        if (lenis) lenis.stop();
        document.documentElement.style.overflow = "hidden";
        requestAnimationFrame(() => requestAnimationFrame(() => prueba.classList.add("abierto")));
        h.focus({ preventScroll: true });
      }
      function cerrarPrueba() {
        prueba.classList.remove("abierto");
        document.documentElement.style.overflow = "";
        if (lenis) lenis.start();
        setTimeout(() => { prueba.hidden = true; }, reduce ? 0 : 700);
        $("#imagen").focus({ preventScroll: true });
      }
      $("#pr-cerrar").addEventListener("click", cerrarPrueba);
      addEventListener("keydown", (e) => { if (e.key === "Escape" && !prueba.hidden) cerrarPrueba(); });
      $("#pr-lista").addEventListener("click", () => { cerrarPrueba(); irA("lista"); });
      $("#pr-copiar").addEventListener("click", async () => {
        const boton = $("#pr-copiar"), t = $("#pr-respuesta").textContent;
        try {
          await navigator.clipboard.writeText(t);
          boton.textContent = "Copiada";
        } catch (_) {
          // Sin permiso de portapapeles: se selecciona el texto para copiarlo a mano
          const r = document.createRange(); r.selectNodeContents($("#pr-respuesta"));
          const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
          boton.textContent = "Selecciónala y cópiala";
        }
      });

      const archivo = $("#imagen"), zona = $("#soltar"), textoZona = $("#soltar-texto"), salidaC = $("#cap-salida");
      const TEXTO = textoZona.textContent, AYUDA_C = salidaC.textContent;
      const TIPOS = ["image/png", "image/jpeg", "image/webp"];
      const MAX_LADO = 1800, MAX_BYTES = 8 * 1024 * 1024;
      const decirC = (t, mal = false) => { salidaC.textContent = t; salidaC.classList.toggle("mal", mal); };
      let ocupado = false;
      const ocupar = (si) => { ocupado = si; zona.classList.toggle("ocupado", si); archivo.disabled = si; textoZona.textContent = si ? "Leyendo tu captura…" : TEXTO; };

      // Reduce la imagen en el navegador antes de enviarla: sube más rápido y no viajan los metadatos del archivo.
      async function preparar(f) {
        try {
          const bmp = await createImageBitmap(f);
          const k = Math.min(1, MAX_LADO / Math.max(bmp.width, bmp.height));
          if (k === 1 && f.size <= 1024 * 1024) { bmp.close(); return f; }
          const lienzo = document.createElement("canvas");
          lienzo.width = Math.round(bmp.width * k); lienzo.height = Math.round(bmp.height * k);
          lienzo.getContext("2d").drawImage(bmp, 0, 0, lienzo.width, lienzo.height);
          bmp.close();
          const blob = await new Promise((ok) => lienzo.toBlob(ok, "image/jpeg", 0.88));
          return blob ? new File([blob], "captura.jpg", { type: "image/jpeg" }) : f;
        } catch (_) {
          return f;
        }
      }

      async function enviar(f) {
        if (ocupado || !f) return;
        if (!TIPOS.includes(f.type)) { decirC("Ese archivo no es una imagen PNG, JPEG o WebP. Sube una captura de pantalla.", true); return; }
        ocupar(true); decirC("Tarda unos segundos.");
        try {
          const imagen = await preparar(f);
          if (imagen.size > MAX_BYTES) { decirC("La imagen pesa demasiado. Haz una captura de pantalla normal y súbela.", true); return; }
          const cuerpo = new FormData();
          cuerpo.append("image", imagen, imagen.name || "captura");
          cuerpo.append("source", origen());
          const r = await fetch("/api/v1/public/diagnosis/screenshot", { method: "POST", body: cuerpo });
          if (r.ok) { decirC(AYUDA_C); abrirPrueba(await r.json()); return; }
          let detalle = "";
          try { const j = await r.json(); if (typeof j.detail === "string") detalle = j.detail; } catch (_) { /* sin cuerpo */ }
          const porDefecto = {
            413: "La imagen pesa demasiado. Haz una captura de pantalla normal y súbela.",
            415: "Ese tipo de archivo no vale. Sube una captura en PNG, JPEG o WebP.",
            422: "No veo reseñas en esa captura. Abre tu negocio en Google Maps, toca en «Reseñas» y haz otra.",
            429: "Demasiadas capturas seguidas. Prueba dentro de un rato.",
            503: "No hemos podido leer la captura ahora. Prueba en un minuto.",
          };
          decirC(detalle || porDefecto[r.status] || "No hemos podido leer la captura. Prueba en un minuto.", true);
        } catch (err) {
          console.error("Fichaitor: fallo de red al enviar la captura", err);
          decirC("No hemos podido conectar. Recarga la página e inténtalo de nuevo.", true);
        } finally {
          ocupar(false);
          archivo.value = "";
        }
      }

      archivo.addEventListener("change", () => enviar(archivo.files && archivo.files[0]));
      captura.addEventListener("submit", (e) => e.preventDefault());
      ["dragenter", "dragover"].forEach((ev) => zona.addEventListener(ev, (e) => { e.preventDefault(); zona.classList.add("encima"); }));
      ["dragleave", "drop"].forEach((ev) => zona.addEventListener(ev, (e) => { e.preventDefault(); zona.classList.remove("encima"); }));
      zona.addEventListener("drop", (e) => enviar(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));
    }
  }

  /* ── Lista de espera ── */
  const form = $("#alta");
  if (form) {
    const campo = $("#contacto"), etiqueta = $("#contacto-l"), salida = $("#salida"), acepto = $("#acepto");
    const boton = $("button", form);
    const AJUSTE = {
      whatsapp: { l: "Tu número de WhatsApp", type: "tel", mode: "tel", auto: "tel", ph: "600 11 22 33" },
      email: { l: "Tu correo", type: "email", mode: "email", auto: "email", ph: "nombre@tubar.es" },
    };
    const canal = () => form.elements.channel.value;
    $$('input[name="channel"]', form).forEach((r) => r.addEventListener("change", () => {
      const a = AJUSTE[canal()];
      etiqueta.textContent = a.l; campo.type = a.type; campo.inputMode = a.mode; campo.autocomplete = a.auto; campo.placeholder = a.ph;
      campo.value = ""; decir("");
    }));
    const decir = (t, mal = false) => { salida.textContent = t; salida.classList.toggle("mal", mal); };

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const contact = campo.value.trim();
      if (contact.length < 5) { decir(canal() === "email" ? "Escribe tu correo." : "Escribe tu número de WhatsApp.", true); campo.focus(); return; }
      if (!acepto.checked) { decir("Marca la casilla para que podamos avisarte.", true); acepto.focus(); return; }
      boton.disabled = true; decir("Apuntándote…");
      try {
        const r = await fetch("/api/v1/public/waitlist", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ channel: canal(), contact, consent: true, consent_version: CONSENT_VERSION, source: origen(), ...ficha }),
        });
        if (r.status === 201) {
          const ok = document.createElement("div");
          ok.className = "hecho-ok";
          ok.innerHTML = '<h3 class="cartel">Apuntado<span class="pt">.</span></h3><p class="lead">Te avisamos el día que Fichaitor abra. Hasta entonces, ni un mensaje.</p>';
          form.replaceWith(ok);
          return;
        }
        let detalle = "";
        try { const j = await r.json(); if (typeof j.detail === "string") detalle = j.detail; } catch (_) { /* sin cuerpo */ }
        if (r.status === 422) decir(detalle || "Revisa el dato: no parece válido.", true);
        else if (r.status === 429) decir(detalle || "Demasiados intentos. Prueba dentro de un rato.", true);
        else decir("No hemos podido apuntarte. Prueba en un minuto o escribe a soporte@fichaitor.es.", true);
      } catch (err) {
        console.error("Fichaitor: fallo de red en la lista de espera", err);
        decir("No hemos podido conectar. Recarga la página e inténtalo de nuevo.", true);
      } finally {
        boton.disabled = false;
      }
    });
  }
})();
