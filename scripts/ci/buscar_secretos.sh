#!/usr/bin/env bash
# Busca secretos en todo el historial de git con gitleaks.
#
# Se descarga el binario oficial de una versión fija y se comprueba su suma
# SHA-256 antes de ejecutarlo. No se usa gitleaks-action porque pide una
# licencia (un secreto) en repositorios de organizaciones.
#
# Para actualizar gitleaks: cambia GITLEAKS_VERSION y las dos sumas con las de
# https://github.com/gitleaks/gitleaks/releases (archivo *_checksums.txt).
#
# Necesita el historial completo (en Actions: fetch-depth: 0).
#
# Uso, desde la raíz del repositorio (Linux):
#   bash scripts/ci/buscar_secretos.sh
set -euo pipefail

GITLEAKS_VERSION="8.30.1"
SHA256_X64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
SHA256_ARM64="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"

case "$(uname -m)" in
    x86_64) arquitectura="x64" suma="${SHA256_X64}" ;;
    aarch64 | arm64) arquitectura="arm64" suma="${SHA256_ARM64}" ;;
    *)
        echo "Arquitectura no prevista: $(uname -m)" >&2
        exit 1
        ;;
esac

destino="$(mktemp -d)"
trap 'rm -rf "${destino}"' EXIT

archivo="gitleaks_${GITLEAKS_VERSION}_linux_${arquitectura}.tar.gz"
curl --fail --silent --show-error --location \
    --output "${destino}/${archivo}" \
    "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${archivo}"
echo "${suma}  ${destino}/${archivo}" | sha256sum --check --quiet
tar -xzf "${destino}/${archivo}" -C "${destino}" gitleaks

# --redact: si aparece un secreto, no se escribe en el registro de la ejecución.
"${destino}/gitleaks" git --no-banner --redact --verbose --exit-code 1 .
