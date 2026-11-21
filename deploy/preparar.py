#!/usr/bin/env python3
"""Prepara uma nova instância ou adiciona o acesso pelo IP à réplica existente."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PROXY = """    reverse_proxy media:8080 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-Proto https
        header_up -X-SMA-Internal
        header_up -X-Original-URI
    }
"""
REQUIRED = ("app.json", "bootstrap.json", "admin", "app-db", "recorder-db", "srs.conf",
            "realtime.json", "portal.env", "Caddyfile", "nginx-internal.conf")
CONFIG_FILES = ("portal.env", "realtime.json", "Caddyfile", "revisao.env", "instancia.json")


def domain(value):
    value = value.lower().rstrip(".")
    labels = value.split(".")
    if (len(labels) < 2 or len(value) > 245 or not all(HOST_LABEL.fullmatch(s) for s in labels)
            or not re.fullmatch(r"[a-z][a-z0-9-]+", labels[-1])):
        raise ValueError("Informe um domínio DNS válido, sem protocolo ou caminho.")
    return value


def ipv4(value):
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError("Informe o IPv4 que os espectadores alcançarão.") from None
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_link_local or addr.is_reserved:
        raise ValueError("O endereço IPv4 deve ser alcançável pelos espectadores.")
    return str(addr)


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def write(target, content, mode=0o444):
    target.write_text(content, encoding="utf-8")
    target.chmod(mode)


def configuration(base, address, single):
    tenants = {f"sma{i}.{base}": f"sma{i}" for i in range(1, 6)} if base else {}
    central = "central." + base if base else ""
    domain_hosts = [central, *tenants] if base else []
    hosts = ([address] if single else []) + domain_hosts
    owner = address if single else central
    rtmp = {slug: f"rtmp://{host}:19350/live" for host, slug in tenants.items()}
    public_rtmp = f"rtmp://{address}:19350/live" if single else next(iter(rtmp.values()))
    env = {
        "SMA_SECRET_FILE": "/run/secrets/app.json", "SMA_BOOTSTRAP_FILE": "/run/secrets/bootstrap.json",
        "SMA_ALLOWED_HOSTS": ",".join([*hosts, "127.0.0.1", "localhost", "backend", "media"]),
        "SMA_SINGLE_HOST": "1" if single else "0", "SMA_OWNER_HOST": owner,
        "SMA_OWNER_ALIASES": central if single else "",
        "SMA_TENANT_HOSTS": json.dumps(tenants, separators=(",", ":")),
        "SMA_RTMP_URLS": json.dumps(rtmp, separators=(",", ":")), "RTMP_PUBLIC_URL": public_rtmp,
        "SMA_SECURE_COOKIES": "1", "SMA_ENVIRONMENT_LABEL": "", "HLS_DIR": "/var/www/hls",
        "SMA_REALTIME_ENABLED": "1", "SMA_REALTIME_RELAY": "rtmp://realtime:1935/live",
    }
    options = ["{", "    on_demand_tls {", "        ask http://media:8092/permit", "    }"]
    if single:
        options.append("    default_sni " + address)
    blocks = ["\n".join([*options, "}\n"])]
    if single:
        tls = ("    tls {\n        issuer acme {\n"
               "            dir https://acme-v02.api.letsencrypt.org/directory\n"
               "            profile shortlived\n        }\n    }\n"
               if ipaddress.IPv4Address(address).is_global else "    tls internal\n")
        blocks.append("https://" + address + " {\n" + tls + PROXY + "}\n")
    if domain_hosts:
        blocks.append(", ".join(domain_hosts) + " {\n" + PROXY + "}\n")
    # Only the catch-all site asks for a dynamic certificate. Static sites
    # retain their own IP/hostname issuance policies.
    blocks.append("https:// {\n    tls {\n        on_demand\n"
                  "        issuer acme {\n            dir https://acme-v02.api.letsencrypt.org/directory\n"
                  "        }\n    }\n    forward_auth media:8092 {\n        uri /check-host\n"
                  "        header_up Host {http.request.host}\n    }\n" + PROXY + "}\n")
    return env, hosts, "\n".join(blocks), tenants


def outputs(dest, deployment, rtc, old_env=None):
    single = deployment.get("modo") == "ip"
    env, hosts, caddy, _ = configuration(deployment.get("dominio"), deployment["ipv4_webrtc"], single)
    if old_env is None:
        lines = []
    else:
        # Keep installation-specific settings outside the routing options being changed.
        replace = {"SMA_ALLOWED_HOSTS", "SMA_SINGLE_HOST", "SMA_OWNER_HOST", "SMA_OWNER_ALIASES",
                   "SMA_TENANT_HOSTS", "SMA_RTMP_URLS", "RTMP_PUBLIC_URL"}
        lines = [line for line in old_env.splitlines() if line.split("=",1)[0] not in replace]
        env = {k:v for k,v in env.items() if k in replace}
    lines.extend(f"{key}='{value}'" for key, value in env.items())
    write(dest / "portal.env", "\n".join(lines) + "\n")
    write(dest / "realtime.json", json_text({**rtc, "allowed_hosts": hosts}))
    write(dest / "Caddyfile", caddy)
    # A changed service environment makes Compose remount the updated bind files.
    write(dest / "revisao.env", revision(deployment))
    write(dest / "instancia.json", json_text(deployment), 0o600)


def revision(deployment):
    identity = json.dumps(deployment, sort_keys=True).encode()
    return "SMA_CONFIG_REVISION=" + hashlib.sha256(identity).hexdigest() + "\n"


def create(target, deployment):
    tmp = Path(tempfile.mkdtemp(dir=ROOT, prefix=".runtime-"))
    try:
        single = deployment.get("modo") == "ip"
        _, _, _, tenants = configuration(deployment.get("dominio"), deployment["ipv4_webrtc"], single)
        db_admin, db_app, db_rec, internal, callback = [secrets.token_hex(32) for _ in range(5)]
        write(tmp / "admin", db_admin + "\n")
        write(tmp / "app-db", db_app + "\n")
        write(tmp / "recorder-db", db_rec + "\n")
        write(tmp / "app.json", json_text({"secret_key":secrets.token_urlsafe(64), "internal_token":internal,
                                           "db_password":db_app}))
        bootstrap = {"owner":{"username":"central", "password":secrets.token_urlsafe(24)},
                     "environments":[{"slug":s,"name":f"SMA {s[3:]}","username":f"admin-{s}",
                                      "password":secrets.token_urlsafe(24)} for s in tenants.values()]}
        write(tmp / "bootstrap.json", json_text(bootstrap))
        write(tmp / "nginx-internal.conf", f"proxy_set_header X-SMA-Internal {internal};\n")
        template = (ROOT / "realtime" / "srs.conf.template").read_text()
        write(tmp / "srs.conf", template.replace("__PUBLIC_IP__", deployment["ipv4_webrtc"])
              .replace("__CALLBACK_TOKEN__", callback))
        outputs(tmp, deployment, {"csrf_secret":secrets.token_urlsafe(48),
                                  "callback_token":callback,"internal_token":internal})
        (tmp / "acervo").mkdir(mode=0o700)
        (tmp / "hls").mkdir(mode=0o755)
        if os.geteuid() == 0:
            os.chown(tmp / "acervo", 10001, 10001)
            os.chown(tmp / "hls", 33, 33)
        (tmp / "hls").chmod(0o755)
        tmp.rename(target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def migrate(target, deployment):
    pending = target / ".adicionar-ip-pendente"
    if not pending.exists():
        stage = Path(tempfile.mkdtemp(dir=target, prefix=".preparando-ip-"))
        try:
            backup = stage / "anterior"
            backup.mkdir(mode=0o700)
            for name in CONFIG_FILES:
                if (target / name).is_file():
                    shutil.copy2(target / name, backup / name)
            old_rtc = json.loads((target / "realtime.json").read_text())
            outputs(stage, deployment, old_rtc, (target / "portal.env").read_text())
            stage.rename(pending)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    if json.loads((pending / "instancia.json").read_text()) != deployment:
        raise ValueError("Uma migração está pendente para outro endereço.")
    # Reapply identical staged bytes on retry. Commit metadata last.
    for name in CONFIG_FILES:
        candidate = target / (".adicionar-ip-" + name + ".tmp")
        candidate.unlink(missing_ok=True)
        shutil.copy2(pending / name, candidate)
        os.replace(candidate, target / name)
    backup = target / "configuracao-anterior-ao-ip"
    if not backup.exists():
        (pending / "anterior").rename(backup)
    shutil.rmtree(pending)


def configure(base, address, add_ip=False):
    target = ROOT / "runtime"
    requested = ({"dominio": base, "ipv4_webrtc": address} if base else
                 {"modo": "ip", "ipv4_webrtc": address})
    if not target.exists():
        if add_ip:
            raise ValueError("--adicionar-ip requer uma instalação existente neste diretório.")
        create(target, requested)
        return "Configuração criada."
    if (not (target / "instancia.json").is_file() or not all((target / n).is_file() for n in REQUIRED)
            or not all((target / d).is_dir() for d in ("acervo", "hls"))):
        raise ValueError("O diretório runtime está incompleto. Recupere a configuração desta instalação.")
    old = json.loads((target / "instancia.json").read_text())
    pending = (target / ".adicionar-ip-pendente").exists()
    if pending and not add_ip:
        raise ValueError("Conclua a migração pendente com --adicionar-ip e o mesmo IPv4.")
    if add_ip:
        if old.get("ipv4_webrtc") != address or not old.get("dominio"):
            raise ValueError("--adicionar-ip requer o IPv4 WebRTC já configurado no modo por domínio.")
        requested["dominio"] = domain(old["dominio"])
        if pending or requested != old:
            migrate(target, requested)
            return "Acesso por IPv4 acrescentado. Configuração anterior em runtime/configuracao-anterior-ao-ip/."
    if old.get("modo") == "ip" and not base and old.get("ipv4_webrtc") == address:
        requested = old  # Re-running the one-IP installer supports migrated deployments.
    if old != requested:
        raise ValueError("Os argumentos diferem da instalação existente. Para acrescentar o IP ao domínio, use --adicionar-ip IPV4.")
    if not (target / "revisao.env").exists():
        write(target / "revisao.env", revision(old))  # Existing domain installation from 20–21 NOV.
    return "Configuração existente conferida. Chaves preservadas."


def save_if_changed(path, content):
    if path.is_file() and path.read_text() == content:
        return
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".dominios-")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        tmp.chmod(0o444)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def ipv6_list(values):
    result = set()
    for raw in values:
        if not raw.strip():
            continue
        ip = ipaddress.IPv6Address(raw.strip())
        if not ip.is_global or ip.is_multicast or ip.scope_id is not None or ip.ipv4_mapped:
            raise ValueError("SMA_PUBLIC_IPV6 requer endereços IPv6 globais e unicast.")
        result.add(str(ip))
    return ",".join(sorted(result))


def detect_ipv6():
    # The Linux interface table exposes public addresses without querying an
    # external IP service. Omit temporary/tentative/deprecated addresses.
    interfaces = Path("/proc/net/if_inet6")
    if not interfaces.is_file():
        return ""
    addresses = []
    for line in interfaces.read_text().splitlines():
        raw, _, _, scope, flags, _ = line.split()
        ip = ipaddress.IPv6Address(int(raw, 16))
        if scope == "00" and not (int(flags, 16) & 0x6D) and ip.is_global and not ip.is_multicast:
            addresses.append(str(ip))
    return ipv6_list(addresses)


def domain_feature():
    target = ROOT / "runtime"
    deployment = json.loads((target / "instancia.json").read_text())
    address = deployment["ipv4_webrtc"]
    domain_env = target / "dominios.env"
    if "SMA_PUBLIC_IPV6" in os.environ:
        public6 = ipv6_list(os.environ["SMA_PUBLIC_IPV6"].split(","))
    elif domain_env.exists():
        saved = dict(line.split("=", 1) for line in domain_env.read_text().splitlines() if "=" in line)
        public6 = ipv6_list(saved.get("SMA_PUBLIC_IPV6", "").split(","))
    else:
        public6 = detect_ipv6()
    options = {"SMA_DOMAIN_MANAGEMENT":"1", "SMA_PUBLIC_IP":address, "SMA_PUBLIC_IPV6":public6}
    entries = [line for line in (target / "portal.env").read_text().splitlines()
               if line.split("=", 1)[0] not in options]
    entries.extend(f"{key}='{v}'" for key, v in options.items())
    if not (target / "domains-db").exists():
        save_if_changed(target / "domains-db", secrets.token_hex(32) + "\n")
    # Deterministic files allow an interrupted upgrade to be completed by
    # running the same command. Leave DB and TLS secrets untouched on retry.
    save_if_changed(target / "portal.env", "\n".join(entries) + "\n")
    save_if_changed(domain_env, "".join(f"{k}={v}\n" for k,v in options.items()))
    caddy = configuration(deployment.get("dominio"), address, deployment.get("modo") == "ip")[2]
    save_if_changed(target / "Caddyfile", caddy)
    save_if_changed(target / "revisao.env", revision(deployment) + "SMA_DOMAIN_MANAGEMENT=1\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adicionar-ip", action="store_true", help="acrescenta acesso por IP à instalação por domínio existente")
    parser.add_argument("endereco", help="IPv4, ou domínio do modo legado")
    parser.add_argument("ipv4_webrtc", nargs="?", help="IPv4 no modo legado por domínio")
    args = parser.parse_args()
    if args.adicionar_ip and args.ipv4_webrtc:
        parser.error("--adicionar-ip aceita um único IPv4.")
    try:
        base = domain(args.endereco) if args.ipv4_webrtc else None
        address = ipv4(args.ipv4_webrtc or args.endereco)
        fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            result = configure(base, address, args.adicionar_ip)
            domain_feature()
            print(result)
        finally:
            os.close(fd)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
