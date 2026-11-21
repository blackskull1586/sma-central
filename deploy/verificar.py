#!/usr/bin/env python3
"""Confere os serviços e o login HTTPS, com validação do nome ou IP no certificado."""
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = ("db-init", "provisionar")
SERVICES = ("db", "backend", "realtime-gateway", "realtime", "media", "gravador", "https", "dominios")

def inspect():
    values = subprocess.check_output(["docker","compose","ps","-aq"], cwd=ROOT, text=True).split()
    if not values:
        return {}
    # Only emit state and service name. Full inspect may contain sensitive environment values.
    fmt = '{{index .Config.Labels "com.docker.compose.service"}} {{.State.Status}} {{.State.ExitCode}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
    lines = subprocess.check_output(["docker","inspect","--format",fmt,*values],cwd=ROOT,text=True).splitlines()
    return {name:(state,code,health) for name,state,code,health in (l.split() for l in lines)}

def statuses(states):
    def ready(name):
        state, code, health = states.get(name, ("missing","-","missing"))
        if name in JOBS:
            return state == "exited" and code == "0"
        return state == "running" and health in ("healthy","none")
    return [n for n in (*JOBS,*SERVICES) if not ready(n)]

def export_ca():
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "https", "cat", "/data/caddy/pki/authorities/local/root.crt"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
    )
    if result.returncode:
        return None
    pem = result.stdout.decode("ascii").strip()
    if not pem.startswith("-----BEGIN CERTIFICATE-----") or pem.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("O certificado público da CA local está inválido.")
    der = ssl.PEM_cert_to_DER_cert(pem)
    path = ROOT / "SMA_CA_Local.crt"
    with tempfile.NamedTemporaryFile(dir=ROOT, delete=False) as output:
        tmp = Path(output.name)
    try:
        tmp.write_text(pem + "\n", encoding="ascii")
        tmp.chmod(0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fingerprint = hashlib.sha256(der).hexdigest().upper()
    print(f"Certificado público para os clientes: {path}. SHA-256 {fingerprint}.")
    return path


def login(host, context):
    try:
        # TCP stays local even when this VPS has a public IP behind NAT. TLS
        # verifies exactly the advertised address, including IP SAN with no SNI.
        with socket.create_connection(("127.0.0.1", 443), timeout=6) as sock:
            with context.wrap_socket(sock, server_hostname=host) as secure:
                conn = http.client.HTTPSConnection(host, timeout=6, context=context)
                conn.sock = secure
                try:
                    conn.request("GET", "/login/")
                    resp = conn.getresponse()
                    return resp.status == 200 and b'type="password"' in resp.read(65536).lower()
                finally:
                    conn.close()
    except (OSError, ValueError, http.client.HTTPException):
        return False


def main():
    deadline = time.monotonic() + 180
    while True:
        states = inspect()
        pending = statuses(states)
        if not pending:
            break
        if time.monotonic() >= deadline or any(states.get(n, (None,"0"))[:2] not in
                [(None,"0"),("running","0"),("created","0"),("exited","0")] for n in JOBS):
            print("Serviços pendentes:", ", ".join(pending), file=sys.stderr)
            return 1
        time.sleep(3)
    print("Serviços locais em execução. Provisionamento e migrações concluídos.")
    config = json.loads((ROOT / "runtime/instancia.json").read_text())
    single = config.get("modo") == "ip"
    ip = config["ipv4_webrtc"]
    private = single and not ipaddress.IPv4Address(ip).is_global
    base = config.get("dominio")
    hosts = [ip] if single else [f"{n}.{base}" for n in ("central","sma1","sma2","sma3","sma4","sma5")]
    deadline = time.monotonic() + 300
    context = None if private else ssl.create_default_context()
    while hosts:
        if context is None:
            cert = export_ca()
            if cert:
                context = ssl.create_default_context(cafile=str(cert))
        if context:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda host: login(host, context), hosts))
            hosts = [h for h, ok in zip(hosts, results) if not ok]
        if not hosts:
            if single:
                print(f"Login HTTPS verificado. Portal: https://{ip}/")
                if private:
                    print("Instale a CA pública em cada dispositivo autorizado e confira o SHA-256 por um canal de confiança.")
            else:
                print("Login HTTPS verificado para a Central e os cinco portais.")
            return 0
        if time.monotonic() >= deadline:
            print("Certificado ou login HTTPS ainda pendente em", ", ".join(hosts), file=sys.stderr)
            return 1
        time.sleep(5)
if __name__ == "__main__":
    sys.exit(main())
