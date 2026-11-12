#!/bin/sh
# Modo «solo web» de la imagen de la web (ver web/nginx-solo-web.conf).
# Lo ejecuta el arranque de la imagen de nginx (/docker-entrypoint.d), después de generar la
# configuración normal a partir de la plantilla. Con FICHAITOR_SOLO_WEB=true la sustituye por la
# de este modo; con cualquier otro valor, o sin la variable, no hace nada.
set -eu

case "${FICHAITOR_SOLO_WEB:-}" in
    true | TRUE | True | 1 | yes | on)
        cp /etc/nginx/fichaitor/solo-web.conf /etc/nginx/conf.d/default.conf
        echo "fichaitor: modo solo web (sin API): /app muestra «Muy pronto» y /api/ responde 503"
        ;;
esac
