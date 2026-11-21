"""Session-protected WebRTC signaling for the existing SMA domains."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs
from aiohttp import web, ClientSession, ClientTimeout, DummyCookieJar

STREAM = re.compile(r'[0-9a-f]{32}')
CLIENT = re.compile(r'[A-Za-z0-9_-]{1,100}')


def valid_domain(host):
    if len(host) > 253 or "." not in host or host.split(".")[-1].isdigit():
        return False
    return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in host.split("."))


class Gateway:
    def __init__(self, config):
        self.config = config
        self.allowed_hosts = set(config['allowed_hosts'])
        self.dynamic_hosts = os.environ.get('SMA_DOMAIN_MANAGEMENT') == '1'
        self.backend = os.environ.get('SMA_RTC_BACKEND', 'http://backend:8000')
        self.srs = os.environ.get('SMA_RTC_SRS', 'http://realtime:1985')
        self.grants = {}
        self.http = None
        self.task = None
        self.pending_kicks = set()
        self.startup_cleanup = True

    def csrf(self, host, cookie):
        return hmac.new(self.config['csrf_secret'].encode(), (host+'\n'+cookie).encode(), hashlib.sha256).hexdigest()

    async def authorize(self, host, cookie, stream):
        # Dynamic names are authorized centrally on every access and during grant rechecks.
        # HTTP requests always go to the configured internal backend, independent of Host.
        if host not in self.allowed_hosts and not (self.dynamic_hosts and valid_domain(host)):
            return None
        if not cookie or len(cookie) > 4096 or not STREAM.fullmatch(stream):
            return None
        try:
            async with self.http.get(self.backend+'/internal/realtime/view/'+stream+'/', headers={
                'Host': host, 'Cookie': cookie, 'X-Forwarded-Proto': 'https',
                'X-SMA-Internal': self.config['internal_token']}, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                value = await response.json()
                return value if value.get('allowed') is True else None
        except Exception:
            return None

    async def access(self, request, mutate=False):
        host = request.host.lower()
        cookie = request.headers.get('Cookie', '')
        stream = request.match_info['stream']
        permission = await self.authorize(host, cookie, stream)
        if permission is None:
            raise web.HTTPForbidden(text='Acesso à transmissão indisponível.')
        if mutate:
            if request.headers.get('Origin') != 'https://'+host:
                raise web.HTTPForbidden(text='Origem inválida.')
            if not hmac.compare_digest(request.headers.get('X-SMA-RTC-CSRF', ''), self.csrf(host, cookie)):
                raise web.HTTPForbidden(text='Solicitação inválida.')
        return host, cookie, stream, permission

    async def info(self, request):
        host, cookie, stream, permission = await self.access(request)
        return web.json_response({'csrf': self.csrf(host, cookie), 'online': permission.get('online', False)})

    async def kick(self, client):
        if not CLIENT.fullmatch(str(client)):
            return
        try:
            async with self.http.delete(self.srs+'/api/v1/clients/'+client) as response:
                await response.read()
                if response.status in (200, 404):
                    self.pending_kicks.discard(client)
                    return
        except Exception:
            pass
        self.pending_kicks.add(client)

    async def revoke(self, ticket):
        grant = self.grants.pop(ticket, None)
        if grant:
            await asyncio.gather(*(self.kick(c) for c in list(grant['clients'])))

    async def whep(self, request):
        host, cookie, stream, permission = await self.access(request, mutate=True)
        if not permission.get('online'):
            raise web.HTTPConflict(text='Aguardando a transmissão.')
        try:
            # SRS defaults to ten rows; 1000 covers the configured 192-connection ceiling.
            async with self.http.get(self.srs+'/api/v1/streams/?count=1000') as response:
                result = await response.json()
                ready = response.status == 200 and result.get('code') == 0 and any(
                    row.get('name') == stream and row.get('publish', {}).get('active') and row.get('video')
                    for row in result.get('streams', []))
        except Exception:
            ready = False
        if not ready:
            raise web.HTTPServiceUnavailable(text='Use a reprodução compatível enquanto o vídeo em tempo real está indisponível.')
        if request.content_type != 'application/sdp':
            raise web.HTTPUnsupportedMediaType()
        body = await request.text()
        if not body.startswith('v=0') or len(body) > 32768:
            raise web.HTTPBadRequest()
        owner = hashlib.sha256((host+'\n'+cookie).encode()).hexdigest()
        if len(self.grants) >= 64 or sum(g['owner'] == owner for g in self.grants.values()) >= 32:
            raise web.HTTPTooManyRequests(text='Limite de reproduções simultâneas atingido.')
        ticket = secrets.token_urlsafe(24)
        self.grants[ticket] = {'stream': stream, 'host': host, 'cookie': cookie, 'owner': owner,
                               'expires': time.monotonic()+75, 'clients': set()}
        try:
            async with self.http.post(self.srs+'/rtc/v1/whep/', params={'app': 'live', 'stream': stream, 'ticket': ticket},
                    data=body, headers={'Content-Type': 'application/sdp'}) as response:
                answer = await response.text()
                if response.status not in (200, 201):
                    raise web.HTTPBadGateway(text='Não foi possível iniciar a reprodução em tempo real.')
                return web.Response(text=answer, status=201, content_type='application/sdp',
                                    headers={'X-SMA-RTC-Session': ticket})
        except Exception:
            await self.revoke(ticket)
            raise web.HTTPBadGateway(text='Não foi possível iniciar a reprodução em tempo real.') from None

    async def session(self, request):
        host, cookie, stream, _ = await self.access(request, mutate=True)
        ticket = request.match_info['ticket']
        grant = self.grants.get(ticket)
        owner = hashlib.sha256((host+'\n'+cookie).encode()).hexdigest()
        if not grant or grant['stream'] != stream or not hmac.compare_digest(grant['owner'], owner):
            raise web.HTTPNotFound()
        if request.method == 'DELETE':
            await self.revoke(ticket)
        else:
            grant['expires'] = time.monotonic()+75
        return web.Response(status=204)

    async def callback(self, request):
        supplied = request.query.getall('secret', [])
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0], self.config['callback_token']):
            raise web.HTTPForbidden()
        try:
            data = await request.json()
            stream, action, client = data.get('stream', ''), data.get('action'), str(data.get('client_id', ''))
            if data.get('app') != 'live' or not STREAM.fullmatch(stream) or not CLIENT.fullmatch(client):
                raise ValueError()
            params = parse_qs(data.get('param', '').lstrip('?'), keep_blank_values=True, max_num_fields=16)
            if action == 'on_publish':
                tokens = params.get('token', [])
                if len(tokens) != 1:
                    raise ValueError()
                async with self.http.post(self.backend+'/internal/realtime/publish/',
                        data={'name': stream, 'token': tokens[0]}, headers={
                            'Host': 'backend', 'X-SMA-Internal': self.config['internal_token']}) as response:
                    if response.status != 204:
                        raise ValueError()
            elif action == 'on_play':
                values = params.get('ticket', [])
                grant = self.grants.get(values[0]) if len(values) == 1 else None
                if not grant or grant['stream'] != stream or grant['expires'] <= time.monotonic():
                    raise ValueError()
                if not await self.authorize(grant['host'], grant['cookie'], stream):
                    raise ValueError()
                # Authorization awaited network I/O. Revocation may have completed meanwhile.
                if self.grants.get(values[0]) is not grant or grant['expires'] <= time.monotonic():
                    raise ValueError()
                # A ticket is single-use for establishing a media reader.
                if grant.get('bound_client') not in (None, client):
                    raise ValueError()
                grant['bound_client'] = client
                grant['clients'].add(client)
            elif action == 'on_stop':
                for grant in self.grants.values():
                    grant['clients'].discard(client)
            elif action != 'on_unpublish':
                raise ValueError()
        except Exception:
            return web.json_response({'code': 403}, status=403)
        return web.json_response({'code': 0})

    async def guard_once(self):
        if self.startup_cleanup:
            try:
                async with self.http.get(self.srs+'/api/v1/clients/?count=1000') as response:
                    result = await response.json()
                    if response.status != 200 or result.get('code') != 0:
                        raise ValueError()
                known = {c for g in self.grants.values() for c in g['clients']}
                await asyncio.gather(*(self.kick(c['id']) for c in result.get('clients', [])
                    if not c.get('publish') and c['id'] not in known))
                self.startup_cleanup = False
            except Exception:
                pass
        semaphore = asyncio.Semaphore(8)
        async def check(ticket, grant):
            async with semaphore:
                valid = grant['expires'] > time.monotonic()
                if valid:
                    valid = await self.authorize(grant['host'], grant['cookie'], grant['stream']) is not None
                if not valid:
                    await self.revoke(ticket)
        await asyncio.gather(*(check(t, g) for t, g in list(self.grants.items())))
        await asyncio.gather(*(self.kick(c) for c in list(self.pending_kicks)))

    async def guard(self):
        while True:
            await self.guard_once()
            await asyncio.sleep(5)

    async def start(self, app):
        self.http = ClientSession(timeout=ClientTimeout(total=6), cookie_jar=DummyCookieJar())
        self.task = asyncio.create_task(self.guard())

    async def close(self, app):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.gather(*(self.revoke(t) for t in list(self.grants)))
        await self.http.close()


@web.middleware
async def safe_headers(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def applications(gateway):
    public = web.Application(middlewares=[safe_headers], client_max_size=32768)
    public.router.add_get('/healthz', lambda request: web.json_response({'ok': True}))
    public.router.add_get('/rtc/{stream:[0-9a-f]{32}}/access', gateway.info)
    public.router.add_post('/rtc/{stream:[0-9a-f]{32}}/whep', gateway.whep)
    for method in ('POST', 'DELETE'):
        public.router.add_route(method, '/rtc/{stream:[0-9a-f]{32}}/session/{ticket:[A-Za-z0-9_-]{32}}', gateway.session)
    public.on_startup.append(gateway.start)
    public.on_cleanup.append(gateway.close)
    internal = web.Application(client_max_size=32768)
    internal.router.add_post('/internal/callback', gateway.callback)
    return public, internal


async def main():
    config = json.loads(Path('/run/secrets/realtime.json').read_text())
    gateway = Gateway(config)
    public, internal = applications(gateway)
    runners = []
    try:
        for app, port in ((public, 8090), (internal, 8091)):
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, '0.0.0.0', port).start()
            runners.append(runner)
        await asyncio.Event().wait()
    finally:
        for runner in reversed(runners):
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
