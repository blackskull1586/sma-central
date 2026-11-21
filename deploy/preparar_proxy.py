#!/usr/bin/env python3
"""Adapta o Caddyfile com o binário da imagem usada na instalação."""
import hashlib
import json
from pathlib import Path
import subprocess

from preparar import ROOT, save_if_changed


def finish(config, deployment):
    # A TLS ClientHello for an IP normally has an empty SNI. Match both
    # identifiers to the fixed IP's issuer before any on-demand wildcard rule.
    if deployment.get("modo") == "ip":
        addr = deployment["ipv4_webrtc"]
        policies = config["apps"]["tls"]["automation"]["policies"]
        matching = [p for p in policies if addr in p.get("subjects", ())]
        if len(matching) != 1 or matching[0].get("on_demand"):
            raise RuntimeError("O Caddy não gerou uma política TLS exclusiva para o IPv4.")
        subjects = matching[0]["subjects"]
        if "" not in subjects:
            subjects.append("")
    return json.dumps(config, ensure_ascii=False, separators=(",", ":")) + "\n"


def main():
    rt = ROOT / "runtime"
    compose = json.loads(subprocess.check_output(["docker", "compose", "config", "--format", "json"], cwd=ROOT))
    image = compose["services"]["https"]["image"]
    if "@sha256:" not in image:
        raise ValueError("A imagem Caddy deve estar fixada por digest.")
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE",
         "--security-opt", "no-new-privileges", "-i", "--entrypoint", "caddy", image,
         "adapt", "--config", "-", "--adapter", "caddyfile"],
        input=(rt / "Caddyfile").read_bytes(), stdout=subprocess.PIPE, cwd=ROOT, check=True,
    )
    converted = finish(json.loads(result.stdout), json.loads((rt / "instancia.json").read_text()))
    save_if_changed(rt / "caddy.json", converted)
    revision = rt / "revisao.env"
    entries = [line for line in revision.read_text().splitlines() if not line.startswith("SMA_PROXY_POLICY=")]
    entries.append("SMA_PROXY_POLICY=" + hashlib.sha256(converted.encode()).hexdigest())
    save_if_changed(revision, "\n".join(entries) + "\n")


if __name__ == "__main__":
    main()
