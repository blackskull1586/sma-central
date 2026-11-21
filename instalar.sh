#!/bin/sh
set -eu
cd "$(dirname "$0")"
help() {
    echo "Uso: sudo ./instalar.sh IPV4" >&2
    echo "Instalação por domínio: sudo ./instalar.sh DOMINIO IPV4" >&2
    echo "Adicionar IP à instalação por domínio: sudo ./instalar.sh --adicionar-ip IPV4" >&2
}
case "$#" in
    1) case "$1" in -*) help; exit 2;; esac ;;
    2) case "$1" in --adicionar-ip) ;; -*) help; exit 2;; esac ;;
    *) help; exit 2;;
esac
[ "$(id -u)" = 0 ] || { echo "Execute com sudo na nova VPS." >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "Esta distribuição requer Linux x86-64." >&2; exit 1; }

if ! command -v python3 >/dev/null || ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1 || ! docker buildx version >/dev/null 2>&1; then
    # Ubuntu provides the engine and the v2 plugin in its signed archive.
    # On other distributions, respect the administrator's selected package source.
    . /etc/os-release
    case "${ID:-}:${VERSION_ID:-}" in
        ubuntu:24.04|ubuntu:26.04) ;;
        *) echo "Instale Python 3.9+, Docker Engine, Buildx e Docker Compose v2 e repita o comando." >&2; exit 1;;
    esac
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    if ! command -v python3 >/dev/null; then apt-get install -y --no-install-recommends python3; fi
    if ! command -v docker >/dev/null; then apt-get install -y --no-install-recommends docker.io; fi
    if dpkg-query -W -f='${db:Status-Abbrev}' docker-ce-cli 2>/dev/null | grep -q '^ii'; then
        compose_package=docker-compose-plugin
        buildx_package=docker-buildx-plugin
    else
        compose_package=docker-compose-v2
        buildx_package=docker-buildx
    fi
    if ! docker compose version >/dev/null 2>&1; then apt-get install -y --no-install-recommends "$compose_package"; fi
    if ! docker buildx version >/dev/null 2>&1; then apt-get install -y --no-install-recommends "$buildx_package"; fi
    if ! docker info >/dev/null 2>&1; then systemctl enable --now docker; fi
fi
docker compose version >/dev/null
docker buildx version >/dev/null
docker info >/dev/null
project_name=${COMPOSE_PROJECT_NAME:-sma-replica}
existing=$(docker ps -aq --filter "label=com.docker.compose.project=$project_name")
for cid in $existing; do
    origin=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$cid")
    [ "$origin" = "$(pwd -P)" ] || { echo "O projeto $project_name já existe em outra pasta desta VPS." >&2; exit 1; }
done
if [ ! -f runtime/instancia.json ] && docker volume inspect "${project_name}_database" >/dev/null 2>&1; then
    echo "Já existe um volume do banco SMA nesta VPS. Recupere a configuração original antes de iniciar." >&2
    exit 1
fi
python3 deploy/preparar.py "$@"
chown 0:0 runtime
chmod 700 runtime
chown 10001:10001 runtime/acervo
chown 33:33 runtime/hls
# These configuration sources run under a non-root container UID.
chmod 0644 deploy/provisionar.py deploy/db-init.sh deploy/domain_worker.py deploy/grant-domains.sql gravador/grant-metadata.sql
python3 deploy/preparar_proxy.py
docker compose config --quiet
docker compose build backend realtime-gateway media gravador
docker compose up -d --no-build
python3 deploy/verificar.py
