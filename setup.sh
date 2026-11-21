#!/usr/bin/env bash
# SMA Central. Baixa o código do commit indicado e confere cada arquivo antes da instalação.
set -euo pipefail
umask 077
commit='20a753e54bb3e1f40414327b8959a8424fafe178'
manifest_sha256='140bbcb18f72b34245f4d2d9480bd2b5a57aa8916a22540c68819bbc7456d813'
repo='blackskull1586/sma-central'
dest=/opt/SMA_Central
case "${1:-}" in
    -h|--help) echo "Uso: sudo bash setup.sh [IPV4]"; exit 0 ;;
esac
if (( $# > 1 )); then echo "Uso: sudo bash setup.sh [IPV4]" >&2; exit 2; fi
if (( EUID != 0 )); then echo "Execute com sudo bash setup.sh." >&2; exit 1; fi
if ! command -v python3 >/dev/null; then
    if command -v apt-get >/dev/null; then
        (umask 022; apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3)
    else
        echo "Instale Python 3.9 ou superior e execute novamente." >&2; exit 1
    fi
fi
if (( $# == 1 )); then
    python3 - "$1" <<'PY'
import ipaddress,sys
try:
    addr=ipaddress.IPv4Address(sys.argv[1])
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_link_local or addr.is_reserved:
        raise ValueError
except ValueError:
    sys.exit('Informe o IPv4 alcançável pelos dispositivos que acessarão a Central.')
PY
fi
command -v flock >/dev/null || { echo "Instale util-linux e repita o comando." >&2; exit 1; }
mkdir -p -m 0755 /opt
mkdir -p -m 0700 /run/sma-central
exec 9>/run/sma-central/instalar.lock
flock -n 9 || { echo "Outra instalação do SMA está em andamento." >&2; exit 1; }
if [[ -L "$dest" ]]; then echo "A pasta de instalação não pode ser um link simbólico." >&2; exit 1; fi
if [[ -e "$dest" ]]; then
    if [[ ! -d "$dest" || ! -f "$dest/SHA256SUMS" || -L "$dest/SHA256SUMS" ]]; then
        echo "A pasta /opt/SMA_Central já existe e não corresponde a uma instalação reconhecida." >&2; exit 1
    fi
    cd "$dest"
    echo "$manifest_sha256  SHA256SUMS" | sha256sum --check --status || {
        echo "Há outra versão na pasta /opt/SMA_Central. Preserve runtime e siga o procedimento de atualização dessa instalação." >&2; exit 1
    }
    sha256sum --check --quiet --strict SHA256SUMS || {
        echo "Algum arquivo instalado foi modificado. Confira a alteração e o guia de atualização." >&2; exit 1
    }
else
    work=$(mktemp -d -p /opt .sma-instalacao-XXXXXXXX)
    trap 'rm -rf -- "$work"' EXIT
    archive="$work/release.tar.gz"
    command -v wget >/dev/null || { echo "Instale wget e repita o comando." >&2; exit 1; }
    (ulimit -f 65536; wget --quiet --show-progress --timeout=30 --tries=3 -O "$archive" "https://codeload.github.com/$repo/tar.gz/$commit")
    python3 - "$archive" "$work/pacote" "$manifest_sha256" <<'PY'
import hashlib, os, re, shutil, sys, tarfile
from pathlib import Path, PurePosixPath

archive, stage, pin = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
def stop(msg):
    sys.exit("Pacote SMA inválido. " + msg)

def secure_name(name):
    p=PurePosixPath(name)
    if p.is_absolute() or name != p.as_posix() or '..' in p.parts or not re.fullmatch(r'[A-Za-z0-9._/-]+',name):
        stop("Caminho não permitido.")
    if p.parts[0] in ('runtime','.git','.env') or any(x.startswith('.runtime') or x=='__pycache__' for x in p.parts):
        stop("Arquivo local de instância incluído na distribuição.")
    return name

with tarfile.open(archive,'r:gz') as tar:
    members={}
    prefix=None
    total=0
    for count,m in enumerate(tar):
        if count>4096: stop("Número excessivo de arquivos.")
        if m.size<0 or m.size>64*1024*1024: stop("Arquivo grande demais.")
        total+=m.size
        if total>128*1024*1024: stop("Distribuição grande demais.")
        first,slash,leaf=m.name.partition('/')
        if prefix is None:prefix=first
        if first!=prefix or not re.fullmatch(r'[A-Za-z0-9._-]+',first): stop("Prefixo de repositório inesperado.")
        if not slash and m.isdir():continue
        if not slash:stop("Entrada inválida.")
        leaf=secure_name(leaf)
        if m.isdir(): continue
        if not m.isfile() or leaf in members: stop("Links, duplicatas ou arquivos especiais no arquivo compactado.")
        members[leaf]=m
    manifest=members.get('SHA256SUMS')
    if not manifest or manifest.size>512*1024:stop("Manifesto ausente ou inválido.")
    body=tar.extractfile(manifest).read()
    if hashlib.sha256(body).hexdigest()!=pin:stop("SHA-256 do manifesto divergente.")
    stage.mkdir(mode=0o755)
    seen=set()
    for line in body.decode('ascii').splitlines():
        m=re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9._/-]+)',line)
        if not m:stop("Linha de manifesto inválida.")
        checksum,name=m.groups(); name=secure_name(name)
        if name in seen or name=='SHA256SUMS' or name not in members:stop("Arquivo ausente ou duplicado no manifesto.")
        seen.add(name)
        target=stage/name
        target.parent.mkdir(parents=True,exist_ok=True)
        # Só copiamos arquivos regulares, sem herdar UID, permissões especiais ou links do arquivo compactado.
        with tar.extractfile(members[name]) as source, target.open('xb') as output:
            h=hashlib.sha256()
            while block:=source.read(1024*1024):
                h.update(block);output.write(block)
        if h.hexdigest()!=checksum:stop("SHA-256 divergente para "+name+".")
        target.chmod(0o755 if members[name].mode & 0o111 else 0o644)
    if not {'compose.yaml','instalar.sh','deploy/preparar.py','deploy/verificar.py'} <= seen:stop("Distribuição incompleta.")
    (stage/'SHA256SUMS').write_bytes(body)
    (stage/'SHA256SUMS').chmod(0o644)
    for path in stage.rglob('*'):
        if path.is_dir():path.chmod(0o755)
    stage.chmod(0o755)
PY
    mv -T -- "$work/pacote" "$dest"
    cd "$dest"
    rm -rf -- "$work"
    trap - EXIT
fi
umask 022
exec bash ./instalar.sh "$@"
