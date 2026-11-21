#!/usr/bin/env python3
"""Valida a instalação SMA em IP único com duas contas e dois servidores temporários."""
import argparse
import http.client
import json
import os
import ipaddress
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import tempfile
import uuid
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode, urlsplit

COMPOSE = ["docker", "compose"]

DB = """
import os,sys,json
os.environ.setdefault('DJANGO_SETTINGS_MODULE','sma.settings')
import django
django.setup()
from django.conf import settings
from django.db import transaction
from portal.models import Environment, User, Channel
from portal.recordings import entries, delete_finished_recording
d=json.loads(sys.stdin.readline())
"""
class TokenParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == 'input' and fields.get('name') == 'csrfmiddlewaretoken':
            self.token = fields.get('value')

def expect(condition, message):
    if not condition:
        raise RuntimeError(message)

def run(args, data=None, timeout=25):
    proc = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if proc.returncode:
        # CLI output or ffmpeg diagnostics may include fixture-only credentials.
        raise RuntimeError('Comando local falhou (código %s).' % proc.returncode)
    return proc.stdout

def db(code, data):
    res = run(COMPOSE + ['exec','-T','backend','python','-c', DB + code],
              (json.dumps(data)+'\n').encode(), timeout=50)
    return json.loads(res)

class Portal:
    def __init__(self, host, port, context):
        self.host=host; self.port=port; self.context=context; self.cookies=SimpleCookie()

    def request(self, method, path, data=None, extra=None):
        expect(path.startswith('/') and not path.startswith('//'), 'Rota inválida.')
        conn = http.client.HTTPSConnection(self.host, timeout=10, context=self.context)
        # Only the socket address is overridden. Server name, certificate check,
        # Host and Origin refer to the configured regional or Central portal.
        conn._create_connection = lambda *a, **k: socket.create_connection(('127.0.0.1', self.port),timeout=10)
        headers = {'Host':self.host, 'Cookie':'; '.join(k+'='+v.value for k,v in self.cookies.items())}
        if extra: headers.update(extra)
        if method in ('POST','DELETE'):
            headers['Origin']='https://'+self.host
            headers['Referer']='https://'+self.host+path
        if isinstance(data,dict):
            data=urlencode(data).encode(); headers['Content-Type']='application/x-www-form-urlencoded'
        try:
            conn.request(method,path,body=data,headers=headers)
            r=conn.getresponse()
            body=r.read(12*1024*1024+1)
            expect(len(body)<=12*1024*1024,'Resposta inesperadamente grande.')
            for k,v in r.getheaders():
                if k.lower()=='set-cookie': self.cookies.load(v)
            return r.status, dict((k.lower(),v) for k,v in r.getheaders()), body
        finally: conn.close()

    def login(self,name,password):
        s,_,body=self.request('GET','/login/')
        p=TokenParser();p.feed(body.decode())
        expect(s==200 and p.token,'Formulário de login indisponível.')
        s,_,_=self.request('POST','/login/',dict(username=name,password=password,csrfmiddlewaretoken=p.token))
        expect(s==302 and 'sma_session' in self.cookies,'Login de QA recusado.')

def wait(check, timeout, reason):
    end=time.monotonic()+timeout
    while True:
        item=check()
        if item:return item
        if time.monotonic()>=end:raise RuntimeError(reason)
        time.sleep(1.5)

def offer():
    fp=':'.join(f'{x:02X}' for x in secrets.token_bytes(32))
    lines=['v=0','o=- 10001 2 IN IP4 127.0.0.1','s=-','t=0 0','a=group:BUNDLE 0',
        'a=msid-semantic: WMS','m=video 9 UDP/TLS/RTP/SAVPF 96','c=IN IP4 0.0.0.0',
        'a=rtcp:9 IN IP4 0.0.0.0','a=ice-ufrag:'+secrets.token_hex(6),
        'a=ice-pwd:'+secrets.token_hex(18),'a=fingerprint:sha-256 '+fp,'a=setup:actpass',
        'a=mid:0','a=recvonly','a=rtcp-mux','a=rtcp-rsize','a=rtpmap:96 H264/90000',
        'a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f',
        'a=rtcp-fb:96 nack','a=rtcp-fb:96 nack pli']
    return ('\r\n'.join(lines)+'\r\n').encode()

def publish_handoff(path, data):
    # Hard-link a fully written 0600 inode. Never overwrite a preexisting file.
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent) as f:
        json.dump(data,f);f.flush();os.fsync(f.fileno())
        os.link(f.name,path)
        return os.fstat(f.fileno()).st_ino

# All cleanup targets derive from the randomly generated run ID. Never adopt other resources.
SEED = """
from django.conf import settings
assert getattr(settings,'SMA_SINGLE_HOST',False) and settings.SMA_OWNER_HOST==d['ip'], 'single host required'
with transaction.atomic():
 assert not User.objects.filter(username__startswith=d['name']).exists()
 assert not Environment.objects.filter(slug__in=d['slugs']).exists()
 User.objects.create_user(username=d['name']+'_o',password=d['password'],role='owner',must_change_password=False)
print(json.dumps(True))
"""
CH_INFO = """
a=Channel.objects.get(pk=d['cid'],name=d['label'],environment__slug=d['slug'])
print(json.dumps(dict(sid=a.stream_id,environment=str(a.environment_id))))
"""
STATUS = """
print(json.dumps([dict(id=x['recording_id'],state=x['status'],offset=x.get('utc_offset'),seconds=x['duration_seconds'])
 for x in entries() if x['channel_id']==d['cid'] and x['stream_id']==d['sid']]))
"""
CLEAN = """
from portal.models import Unit, AuditEvent
from portal.views import revoke_account_sessions
name=d['name']
assert __import__('re').fullmatch('qa_e2e_[0-9a-f]{32}',name)
slugs=['qa-'+name[7:]+'-'+letter for letter in ['a','b']]
users=[name+s for s in ['_o','_a','_b','_v']]
remaining=[]
with transaction.atomic():
 owner=User.objects.select_for_update().filter(username=users[0],role='owner',environment__isnull=True).first()
 if owner is None:
  if Environment.objects.filter(slug__in=slugs).exists() or User.objects.filter(username__in=users).exists():
   raise ValueError('fixture ownership not established')
 else:
  for i,slug in enumerate(slugs):
   env=Environment.objects.select_for_update().filter(slug=slug,name=name+'-serv-'+str(i+1)).first()
   if env is None:continue
   exp_users={users[i+1]: 'admin'}
   if i==0:exp_users[users[3]]='viewer'
   accounts=list(User.objects.select_for_update().filter(environment=env))
   if (any(acc.role!=exp_users.get(acc.username) for acc in accounts)
      or Unit.objects.filter(environment=env).exists()
      or AuditEvent.objects.filter(environment=env).exclude(actor__username__in=users).exists()):
    raise ValueError('unexpected fixture membership')
   channels=list(Channel.objects.select_for_update().filter(environment=env))
   if any(ch.name!=name+'-canal-'+str(i+1) for ch in channels):
    raise ValueError('unexpected fixture channels')
   in_progress=False
   for ch in channels:
    rows=[x for x in entries() if x.get('channel_id')==str(ch.pk)]
    if any(not x['deletable'] for x in rows):
     in_progress=True
     break
   if in_progress:
    for ch in channels:
     ch.active=False;ch.save(update_fields=['active'])
    env.active=False;env.save(update_fields=['active'])
    for acc in accounts:
     revoke_account_sessions(acc.pk)
     acc.is_active=False;acc.save(update_fields=['is_active'])
    remaining.append(slug)
    continue
   for ch in channels:
    for row in entries():
     if row.get('channel_id')==str(ch.pk):
      delete_finished_recording(row,owner)
    ch.delete()
   for acc in accounts:
    revoke_account_sessions(acc.pk);acc.delete()
   env.delete()
  revoke_account_sessions(owner.pk)
  if remaining:
   owner.is_active=False;owner.save(update_fields=['is_active'])
  else:owner.delete()
 print(json.dumps(dict(pending=remaining)))
"""


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values={}
        self.channel=None
        self.feed(html.decode())

    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='input':
            self.values[a.get('id',a.get('name',''))]=a.get('value','')
            if a.get('name')=='csrfmiddlewaretoken':
                self.values['csrfmiddlewaretoken']=a.get('value','')
        if 'data-channel-id' in a:self.channel=a['data-channel-id']


def submit(session, form_url, values):
    s,_,html=session.request('GET',form_url)
    expect(s==200, f'Formulário indisponível: {form_url} ({s}).')
    fields=Inputs(html).values
    token=fields.get('csrfmiddlewaretoken')
    expect(token, 'Formulário sem CSRF.')
    return session.request('POST',form_url,{'csrfmiddlewaretoken':token,**values})


def change(session,user,old,new):
    session.login(user,old)
    code,h,_=session.request('GET','/painel/')
    expect(code==302 and h['location'].split('?')[0]=='/conta/senha/','A troca da senha provisória não foi exigida.')
    code,_,_=submit(session,'/conta/senha/',{'old_password':old,'new_password1':new,'new_password2':new})
    expect(code==302,'A troca de senha da conta temporária falhou.')


def published_port(service,internal):
    mapping=run(COMPOSE+['port',service,str(internal)]).decode().splitlines()[0]
    return int(mapping.rsplit(':',1)[1])


def publish(key,port,duration):
    image=run(COMPOSE+['images','-q','gravador']).decode().strip()
    expect(image and '\n' not in image,'Imagem local do gravador indisponível.')
    # Host networking on the test VPS exercises the port published by Compose.
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-re','-f','lavfi','-i','testsrc2=size=320x180:rate=12',
         '-f','lavfi','-i','sine=frequency=480:sample_rate=44100','-t',str(duration),'-threads','1','-c:v','libx264',
         '-pix_fmt','yuv420p','-profile:v','baseline','-preset','ultrafast','-tune','zerolatency','-g','12','-bf','0',
         '-b:v','230k','-c:a','aac','-b:a','48k','-f','flv',f'rtmp://127.0.0.1:{port}/live/'+key]
    proc=subprocess.Popen(['docker','run','--rm','--network','host','--entrypoint','python3','-i',image,
          '-c','import os,sys,json; c=json.load(sys.stdin);os.execvp(c[0],c)'],stdin=subprocess.PIPE,
          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    proc.stdin.write(json.dumps(cmd).encode());proc.stdin.close()
    return proc


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-dir',type=Path,required=True)
    parser.add_argument('--ip',type=lambda s:str(ipaddress.IPv4Address(s)),required=True)
    parser.add_argument('--ca',type=Path,help='PEM da CA que emitiu a cadeia apresentada pelo IP.')
    parser.add_argument('--wan-handoff',type=Path,help='Arquivo novo de acesso efêmero para o receptor WebRTC externo.')
    parser.add_argument('--cleanup',metavar='RUN_ID',help='Repete exclusivamente a limpeza daquela execução de QA.')
    args=parser.parse_args()
    ca=args.ca.resolve() if args.ca else None
    handoff=args.wan_handoff.absolute() if args.wan_handoff else None
    os.chdir(args.compose_dir)
    expect(Path('compose.yaml').is_file(),'Projeto Compose ausente.')
    if args.cleanup:
        expect(re.fullmatch(r'qa_e2e_[0-9a-f]{32}',args.cleanup),'ID de execução inválido.')
        outcome=db(CLEAN,{'name':args.cleanup})
        expect(not outcome['pending'],'Exportação de QA pendente. Repita a limpeza após finalização.')
        print('Limpeza QA concluída.');return
    if handoff:
        expect(handoff.parent.is_dir() and not os.path.lexists(handoff),'O arquivo de acesso temporário já existe ou a pasta não está disponível.')
    port=published_port('https',443);rtmp_port=published_port('media',1935)
    ctx=ssl.create_default_context()
    if ca:ctx.load_verify_locations(cafile=str(ca))
    else:
        r=subprocess.run(COMPOSE+['exec','-T','https','cat','/data/caddy/pki/authorities/local/root.crt'],capture_output=True)
        if r.returncode==0 and b'BEGIN CERTIFICATE' in r.stdout:ctx.load_verify_locations(cadata=r.stdout.decode())
    free=run(COMPOSE+['exec','-T','gravador','python3','-c',
        "import json,shutil; c=json.load(open('/opt/recorder/config.json')); print(shutil.disk_usage(c['archive']).free>c['reserve_bytes']+10000000)"])
    expect(free.strip()==b'True','Espaço livre insuficiente sobre a reserva configurada.')
    token=uuid.uuid4().hex;name='qa_e2e_'+token
    slugs=['qa-'+token+'-'+x for x in ['a','b']]
    def session():return Portal(args.ip,port,ctx)
    anon=session()
    s,_,page=anon.request('GET','/login/')
    expect(s==200 and Inputs(page).values.get('csrfmiddlewaretoken'),'Acesso HTTPS direto pelo IP indisponível.')
    expect(anon.request('GET','/ambientes/')[0] in (302,401,403),'Portal público indevido.')
    print('ID de execução:',name,flush=True)
    ownerpass=secrets.token_urlsafe(32)
    # Check for resources both if a previous POST timed out and if creation failed midway.
    seeded=False;publisher=None;ids=None;handoff_inode=None
    try:
        db(SEED,dict(name=name,ip=args.ip,password=ownerpass,slugs=slugs));seeded=True
        owner=session();owner.login(name+'_o',ownerpass)
        admins=[]; channels=[]
        for i,letter in enumerate(('a','b')):
            u=name+'_'+letter;p0=secrets.token_urlsafe(32);p1=secrets.token_urlsafe(32)
            code,h,_=submit(owner,'/ambientes/novo/',{'name':name+'-serv-'+str(i+1),'slug':slugs[i],
              'username':u,'password1':p0,'password2':p0})
            expect(code==302 and h['location']=='/ambientes/','Criação do servidor lógico pela Central falhou.')
            a=session();change(a,u,p0,p1);fresh=session();fresh.login(u,p1);admins.append(fresh)
            label=name+'-canal-'+str(i+1)
            code,_,body=submit(fresh,'/canais/novo/',{'name':label})
            form=Inputs(body);key=form.values.get('stream-key','');server=urlsplit(form.values.get('stream-server',''))
            expect(code==200 and form.channel and key,'A interface não forneceu credenciais para o novo canal.')
            expect(server.scheme=='rtmp' and server.hostname==args.ip and (server.port or 1935)==rtmp_port and server.path=='/live','Endereço RTMP apresentado pelo portal diverge do IP/porta publicados.')
            ch=db(CH_INFO,dict(cid=str(uuid.UUID(form.channel)),label=label,slug=slugs[i]))
            channels.append(dict(cid=form.channel,key=key,sid=ch['sid'],label=label,eid=ch['environment']))
        for i,a in enumerate(admins):
            other=channels[1-i];code,_,body=a.request('GET','/painel/')
            expect(code==200 and channels[i]['label'].encode() in body and other['label'].encode() not in body,'Painel sem isolamento nas duas contas.')
            expect(a.request('GET','/ambientes/')[0]==403,'Admin regional abriu administração de servidores.')
            expect(a.request('GET',f"/canais/{other['cid']}/credenciais/")[0] in (403,404),'Chave de outro servidor acessível.')
            expect(a.request('GET','/gravacoes/')[0] in (403,404),'Administrador abriu o acervo da Central.')
        s,_,page=owner.request('GET','/ambientes/')
        expect(s==200 and all(str(x['eid']).encode() in page for x in channels),'Servidores novos ausentes na Central.')
        for x in channels:
            token_ui=Inputs(page).values['csrfmiddlewaretoken']
            s,h,_=owner.request('POST',f"/ambientes/{x['eid']}/abrir/",{'csrfmiddlewaretoken':token_ui})
            expect(s==302 and h['location']=='/painel/','Seleção de servidor na Central falhou.')
            s,_,body=owner.request('GET','/painel/')
            expect(s==200 and x['label'].encode() in body,'Central abriu outro ambiente.')
        viewer_name=name+'_v';vp0=secrets.token_urlsafe(32);vp1=secrets.token_urlsafe(32)
        code,_,_=submit(admins[0],'/usuarios/novo/',dict(username=viewer_name,first_name='QA',role='viewer',password1=vp0,password2=vp0))
        expect(code==302,'Cadastro do visualizador de QA falhou.')
        viewer=session();change(viewer,viewer_name,vp0,vp1)
        print('Dois servidores e dois canais criados na interface; contas autenticadas e isoladas no mesmo IP.',flush=True)
        ids={k:channels[0][k] for k in ('cid','sid')}
        sid=ids['sid'];duration=115 if handoff else 42
        publisher=publish(channels[0]['key'],rtmp_port,duration)
        def catalog():
            expect(publisher.poll() in (None,0),'Publicação RTMP terminou com erro.')
            s,_,b=viewer.request('GET','/streams.json');expect(s==200,'Catálogo do visualizador indisponível.')
            return next((x for x in json.loads(b) if x.get('id')==sid and x.get('channel_id')==ids['cid']),None)
        stream=wait(catalog,28,'Publicação ausente no catálogo do servidor criado.')
        cs,_,cb=admins[1].request('GET','/streams.json')
        expect(cs==200 and sid.encode() not in cb,'Catálogo expôs canal de outro servidor.')
        hls=stream['url'];prefix='/rtc/'+sid
        expect(hls=='/hls/'+sid+'.m3u8' and stream['webrtc_url']==prefix+'/whep','Rotas de mídia divergentes.')
        for unauthorized in [anon,admins[1]]:
            expect(unauthorized.request('GET',hls)[0] in (401,403,404),'HLS abriu fora do servidor autorizado.')
            expect(unauthorized.request('GET',prefix+'/access')[0] in (401,403,404),'Autorizações RTC não isoladas.')
        def fragment():
            s,_,b=viewer.request('GET',hls)
            return next((n for n in b.decode().splitlines() if re.fullmatch(sid+r'-[0-9]+\.ts',n)),None) if s==200 else None
        seg=wait(fragment,15,'Nenhum segmento HLS foi encerrado.')
        s,_,b=viewer.request('GET','/hls/'+seg);expect(s==200 and len(b)>188 and b[0]==0x47,'Falha na mídia HLS.')
        if handoff:
            handoff_inode=publish_handoff(handoff,dict(host=args.ip,stream_id=sid,username=viewer_name,password=vp1,expires_unix=int(time.time())+95))
            print('Receptor externo pode iniciar com a conta de visualização efêmera.',flush=True)
        s,_,b=viewer.request('GET',prefix+'/access');expect(s==200,'Autorização RTC falhou.')
        hdr={'X-SMA-RTC-CSRF':json.loads(b)['csrf'],'Content-Type':'application/sdp'}
        def negotiate():
            resp=viewer.request('POST',prefix+'/whep',offer(),hdr)
            return None if resp[0] in (409,503) else resp
        s,headers,answer=wait(negotiate,12,'Sinalização WHEP indisponível.')
        expect(s==201 and b'm=video' in answer and b'a=ice-ufrag:' in answer,'Resposta WebRTC inválida.')
        ticket=headers.get('x-sma-rtc-session','');expect(re.fullmatch(r'[A-Za-z0-9_-]{32}',ticket),'Ticket de mídia inválido.')
        expect(viewer.request('DELETE',prefix+'/session/'+ticket,extra=hdr)[0]==204,'Não encerrou a sessão WebRTC.')
        print('RTMP na porta publicada, HLS, negação entre servidores e negociação WHEP confirmados.',flush=True)
        expect(publisher.wait(timeout=duration+12)==0,'FFmpeg terminou com erro.')
        def archived():return next((r for r in db(STATUS,ids) if r['state']=='complete'),None)
        item=wait(archived,110,'O gravador não finalizou o MP4.')
        path='/gravacoes/'+str(uuid.UUID(item['id']))+'/baixar/'
        s,h,mp4=owner.request('GET',path)
        expect(s==200 and h.get('content-type','').startswith('video/mp4') and b'ftyp' in mp4[:32],'Gravação não visualizável pelo proprietário.')
        s,h,chunk=owner.request('GET',path,extra={'Range':'bytes=0-31'})
        expect(s==206 and chunk==mp4[:32],'HTTP Range retornou mídia divergente.')
        expect(all(a.request('GET',path)[0] in (403,404) for a in [*admins,viewer]) and anon.request('GET',path)[0] in (302,401,403,404),'Gravação acessível fora da Central.')
        streams=json.loads(run(COMPOSE+['exec','-T','gravador','ffprobe','-v','error','-show_entries','stream=codec_name','-of','json','pipe:0'],mp4))
        expect({'h264','aac'}<={r['codec_name'] for r in streams['streams']} and item['offset']=='-04:00' and float(item['seconds'])>5,'Codecs, fuso ou duração de gravação divergentes.')
        print('MP4 H.264/AAC confirmado em UTC−04; download e Range exclusivos do proprietário.',flush=True)
    finally:
        if handoff_inode and handoff and handoff.is_file() and handoff.lstat().st_ino==handoff_inode:handoff.unlink()
        if publisher is not None:
            try:publisher.wait(timeout=130)
            except subprocess.TimeoutExpired:print('Publicador ainda não encerrou; a limpeza preservará o canal se houver exportação pendente.',flush=True)
            if ids:
                until=time.monotonic()+55
                while time.monotonic()<until:
                    try:
                        rows=db(STATUS,ids)
                        if rows and all(r['state']=='complete' for r in rows):break
                    except RuntimeError:break
                    time.sleep(2)
        if seeded:
            outcome=db(CLEAN,{'name':name});expect(not outcome['pending'],'Exportação ainda pendente. Use --cleanup '+name+' após a finalização.')
            print('Removidos apenas os servidores, contas, sessões, canais e vídeos desta execução.',flush=True)


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('FALHA:',str(e) if isinstance(e,RuntimeError) else type(e).__name__,file=sys.stderr)
        sys.exit(1)
