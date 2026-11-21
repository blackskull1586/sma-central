"""Select the one TLS proxy trusted by this NGINX instance."""
import ipaddress
import os
from pathlib import Path


def render(template, address):
    try:
        proxy = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        raise SystemExit("SMA_TRUSTED_PROXY deve ser um endereço IPv4.") from None
    return template.replace("__SMA_TRUSTED_PROXY__", str(proxy))


def main():
    template = Path("/etc/nginx/sma-nginx.conf.template").read_text()
    config = Path("/run/sma-nginx.conf")
    config.write_text(render(template, os.environ.get("SMA_TRUSTED_PROXY", "172.30.91.2")))
    os.execlp("nginx", "nginx", "-c", str(config), "-g", "daemon off;")


if __name__ == "__main__":
    main()
