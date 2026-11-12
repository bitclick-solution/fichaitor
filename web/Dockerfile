# Web de Fichaitor: sitio público estático + aplicación de /app, servidos por nginx sin privilegios.
#
# El contexto de compilación es la RAÍZ del repositorio, porque el sitio se genera
# a partir de brand/ y docs/legal/:
#
#     docker build -f web/Dockerfile -t fichaitor-web .
#
# En docker-compose:  build: { context: ., dockerfile: web/Dockerfile }

# 1. Aplicación de /app (React + Vite + TypeScript)
FROM node:22-alpine AS app
WORKDIR /src
COPY web/app/package.json web/app/package-lock.json web/app/
RUN cd web/app && npm ci --no-audit --no-fund
COPY brand/logo.svg brand/logo.svg
COPY web/site/fonts web/site/fonts
COPY web/app web/app
RUN cd web/app && npm run build

# 2. Sitio público: portada y páginas legales
FROM python:3.12-alpine AS site
WORKDIR /src
RUN pip install --no-cache-dir markdown==3.7
COPY brand brand
COPY docs/legal docs/legal
COPY web/build.py web/build.py
COPY web/src web/src
COPY web/site web/site
# Con --build-arg LEGAL_ESTRICTO=1 la compilación falla si los textos legales tienen marcas abiertas.
ARG LEGAL_ESTRICTO=0
RUN if [ "$LEGAL_ESTRICTO" = "1" ]; then python web/build.py --estricto; else python web/build.py; fi

# 3. Servidor
FROM nginxinc/nginx-unprivileged:1.27-alpine
# La configuración es una plantilla: la imagen sustituye ${API_UPSTREAM} al arrancar.
ENV API_UPSTREAM=api:8000
COPY web/nginx.conf /etc/nginx/templates/default.conf.template
# Cabeceras de seguridad, incluidas en cada location (ver la nota del archivo).
COPY web/nginx-seguridad.conf /etc/nginx/snippets/seguridad.conf
COPY --from=site /src/web/site /usr/share/nginx/html
COPY --from=app /src/web/app/dist /usr/share/nginx/html/app
EXPOSE 8080
