"""Confirma endereço DNS e certificado no proxy da própria réplica."""
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import time

import psycopg
from psycopg.rows import dict_row

LOG = logging.getLogger("sma.dominios")
PORTAL = ("https", 443)
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
DUE_SQL = """
    SELECT id, hostname, enabled, status, updated_at FROM portal_customdomain
    WHERE (NOT enabled AND status <> 'disabled')
       OR (enabled AND (
           status = 'pending' OR checked_at IS NULL OR checked_at < now() -
           CASE WHEN status = 'active' THEN interval '10 minutes'
                ELSE interval '2 minutes' END))
    ORDER BY (NOT enabled) DESC, checked_at NULLS FIRST, id LIMIT 16
"""
STATE_SQL = """
    UPDATE portal_customdomain SET status=%s, detail=%s, checked_at=now()
    WHERE id=%s AND enabled=%s AND updated_at=%s
"""


def valid_hostname(hostname):
    return (isinstance(hostname, str) and len(hostname) <= 253 and "." in hostname
            and not hostname.split(".")[-1].isdigit()
            and all(LABEL.fullmatch(label) for label in hostname.split(".")))


def address_sets(hostname):
    # A trailing dot prevents resolver search domains from changing the subject.
    def lookup(family):
        try:
            values = socket.getaddrinfo(hostname + ".", 443, family=family,
                                        type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            if exc.errno not in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}:
                raise
            return set()
        return {ipaddress.ip_address(entry[4][0]) for entry in values}

    return lookup(socket.AF_INET), lookup(socket.AF_INET6)


def check_dns(hostname, public_ipv4, public_ipv6):
    try:
        addresses4, addresses6 = address_sets(hostname)
    except (ValueError, OSError):
        return "O resolvedor ainda não confirmou o DNS do domínio."
    if addresses4 != {public_ipv4}:
        return "O registro A deve apontar exclusivamente para o IPv4 desta VPS."
    if not addresses6.issubset(public_ipv6):
        return "Há registro AAAA que não corresponde a um IPv6 público configurado nesta VPS."
    return ""


def check_tls(hostname, context):
    # TCP goes to the edge service. User-supplied hostnames are used for TLS
    # identity only. Connecting by name here would make this an outbound probe.
    with socket.create_connection(PORTAL, timeout=5) as sock:
        sock.settimeout(25)
        with context.wrap_socket(sock, server_hostname=hostname):
            pass


def state(db, row, status, detail):
    return db.execute(STATE_SQL, (status, detail, row["id"], row["enabled"],
                                  row["updated_at"])).rowcount == 1


def reconcile(db, row, public_ipv4, public_ipv6, context):
    if not row["enabled"]:
        state(db, row, "disabled", "O acesso pelo domínio está desativado.")
        return
    hostname = row["hostname"]
    if not valid_hostname(hostname):
        state(db, row, "error", "Nome de domínio inválido.")
        return
    error = check_dns(hostname, public_ipv4, public_ipv6)
    if error:
        state(db, row, "dns_error", error)
        return
    # Keep active sites available during their routine TLS checks. A new
    # site's provisioning status permits issuance but does not yet serve HTTP.
    if row["status"] != "active" and not state(
        db, row, "provisioning", "DNS confirmado. Aguardando o certificado HTTPS."
    ):
        return
    try:
        check_tls(hostname, context)
    except (OSError, ValueError):
        state(db, row, "tls_error", "HTTPS ainda não confirmado. Confira as portas 80 e 443, o DNS e a política CAA.")
        return
    state(db, row, "active", "Endereços DNS e certificado HTTPS confirmados.")


def run():
    public_ipv4 = ipaddress.IPv4Address(os.environ["SMA_PUBLIC_IP"])
    public_ipv6 = {ipaddress.IPv6Address(value.strip())
                   for value in os.environ.get("SMA_PUBLIC_IPV6", "").split(",") if value.strip()}
    password = Path(os.environ["SMA_PGPASSWORD_FILE"]).read_text().rstrip("\r\n")
    context = ssl.create_default_context()
    while True:
        try:
            with psycopg.connect(password=password, connect_timeout=5, autocommit=True,
                                 row_factory=dict_row, application_name="sma-dominios") as db:
                # An advisory session lock also releases automatically on disconnect.
                leader = db.execute("SELECT pg_try_advisory_lock(6361249532001) AS leader").fetchone()["leader"]
                Path("/tmp/sma-dominios.heartbeat").touch()
                if leader:
                    while True:
                        rows = db.execute(DUE_SQL).fetchall()
                        Path("/tmp/sma-dominios.heartbeat").touch()
                        for row in rows:
                            reconcile(db, row, public_ipv4, public_ipv6, context)
                            Path("/tmp/sma-dominios.heartbeat").touch()
                        time.sleep(1 if len(rows) == 16 else 10)
        except psycopg.Error:
            LOG.warning("Banco indisponível para verificação de domínios. Nova tentativa em dez segundos.")
        time.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
