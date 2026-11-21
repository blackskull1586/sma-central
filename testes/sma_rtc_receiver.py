#!/usr/bin/env python3
"""Recebe vídeo do canal efêmero de QA. O handoff JSON entra exclusivamente por stdin."""
import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import ssl
import sys
import time
from html.parser import HTMLParser

import aiohttp
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpReceiver, RTCSessionDescription


class Failure(Exception):
    """Erro descritivo sem dados de autenticação."""


class Token(HTMLParser):
    value = None

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "input" and data.get("name") == "csrfmiddlewaretoken":
            self.value = data.get("value")


def require(ok, message):
    if not ok:
        raise Failure(message)


def validate_answer(sdp, address, port):
    udp = False
    for line in sdp.splitlines():
        if not line.startswith("a=candidate:"):
            continue
        parts = line.split()
        require(len(parts) >= 8 and parts[4] == address and parts[5] == str(port),
                "A resposta ICE anuncia um endereço ou porta diferente do destino esperado.")
        udp |= parts[2].lower() == "udp"
    require(udp, "O SRS não anunciou o candidato UDP esperado.")


async def request(http, method, url, expected, **kw):
    async with http.request(method, url, allow_redirects=False, **kw) as response:
        require(response.status in expected, f"{method} {url.split('/')[-2]} retornou HTTP {response.status}."
                if "/session/" not in url else f"Encerramento retornou HTTP {response.status}.")
        data = bytearray()
        async for part in response.content.iter_chunked(8192):
            data.extend(part)
            require(len(data) <= 65536, "Resposta excedeu o tamanho esperado.")
        return response.status, response.headers, data


async def receive(q, args):
    host = args.host.lower()
    require(q["host"] == host and re.fullmatch(r"[a-z0-9.-]+", host), "Host do handoff divergente.")
    require(re.fullmatch(r"qa_e2e_[0-9a-f]{32}_v", q["username"]), "Somente a conta viewer efêmera de QA é aceita.")
    require(re.fullmatch(r"[0-9a-f]{32}", q["stream_id"]), "Canal de QA inválido.")
    require(time.time() < q["expires_unix"] <= time.time() + 150, "Handoff vencido ou fora da janela de QA.")
    base = "https://" + host
    path = base + "/rtc/" + q["stream_id"]
    ctx = ssl.create_default_context(cafile=args.ca)
    conn = aiohttp.TCPConnector(ssl=ctx)
    timeout = aiohttp.ClientTimeout(total=9)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession(connector=conn, timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            _, _, form = await request(http, "GET", base + "/login/", {200})
            token = Token()
            token.feed(form.decode())
            require(token.value, "O formulário de login não incluiu proteção CSRF.")
            origin = {"Origin": base, "Referer": base + "/login/"}
            await request(http, "POST", base + "/login/", {302}, headers=origin,
                          data={"username": q["username"], "password": q["password"],
                                "csrfmiddlewaretoken": token.value})
            _, _, body = await request(http, "GET", path + "/access", {200})
            access = json.loads(body)
            require(access.get("online") and access.get("csrf"), "Canal de QA ainda não anunciou vídeo.")
            headers = {**origin, "Content-Type": "application/sdp", "X-SMA-RTC-CSRF": access["csrf"]}
            tr = pc.addTransceiver("video", direction="recvonly")
            tr.setCodecPreferences([c for c in RTCRtpReceiver.getCapabilities("video").codecs
                                    if c.mimeType.lower() == "video/h264"])
            await pc.setLocalDescription(await pc.createOffer())
            answer = None
            ticket = None
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and time.time() < q["expires_unix"]:
                    code, rsp, answer = await request(http, "POST", path + "/whep", {201, 409, 503},
                                                      headers=headers, data=pc.localDescription.sdp)
                    if code == 201:
                        ticket = rsp.get("X-SMA-RTC-Session", "")
                        require(re.fullmatch(r"[A-Za-z0-9_-]{32}", ticket), "Ticket WHEP inválido.")
                        break
                    await asyncio.sleep(1)
                require(ticket, "Sinalização WHEP não completou durante a transmissão.")
                sdp = answer.decode()
                validate_answer(sdp, args.candidate_ip, args.candidate_port)
                await pc.setRemoteDescription(RTCSessionDescription(sdp, "answer"))
                frames = []
                for _ in range(3):
                    frames.append(await asyncio.wait_for(tr.receiver.track.recv(), timeout=15))
                inbound = [s for s in (await pc.getStats()).values() if s.type == "inbound-rtp" and s.kind == "video"]
                pkts = sum(s.packetsReceived for s in inbound)
                require(pkts > 0 and all((f.width, f.height) == (320, 180) for f in frames),
                        "Vídeo recebido diverge do clipe sintético de QA.")
                print(json.dumps({"webRTC": "ok", "ice": pc.iceConnectionState, "conexao": pc.connectionState,
                                  "video": "H.264", "resolucao": "320x180", "quadros_decodificados": len(frames),
                                  "pacotes_rtp_recebidos": pkts, "segundos": round(time.monotonic() - started, 2)},
                                 ensure_ascii=False), flush=True)
            finally:
                if ticket and re.fullmatch(r"[A-Za-z0-9_-]{32}", ticket):
                    with contextlib.suppress(Failure, aiohttp.ClientError, TimeoutError):
                        await request(http, "DELETE", path + "/session/" + ticket, {204}, headers=headers)
    finally:
        await pc.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True, type=lambda s: str(ipaddress.IPv4Address(s)))
    p.add_argument("--candidate-ip", type=lambda s: str(ipaddress.IPv4Address(s)))
    p.add_argument("--candidate-port", type=int, default=28080)
    p.add_argument("--ca", help="PEM confiável opcional para a emissão da própria bancada.")
    args = p.parse_args()
    args.candidate_ip = args.candidate_ip or args.host
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(4097)
        require(len(raw) <= 4096, "Handoff muito grande.")
        handoff = json.loads(raw)
        asyncio.run(asyncio.wait_for(receive(handoff, args), timeout=53))
    except Failure as exc:
        sys.exit("Falha no teste WebRTC: " + str(exc))
    except (TimeoutError, asyncio.TimeoutError):
        sys.exit("Tempo esgotado para autenticar, negociar ICE ou decodificar o vídeo.")
    except Exception as exc:
        sys.exit("Teste WebRTC interrompido: " + type(exc).__name__ + ".")


if __name__ == "__main__":
    main()
