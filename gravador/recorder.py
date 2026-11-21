#!/usr/bin/env python3
"""Passive HLS archive. Never writes to HLS, the SMA database, or live services."""
import argparse, concurrent.futures, datetime as dt, fcntl, json, logging, math, os
from pathlib import Path
import re, shutil, signal, stat, subprocess, tempfile, time, unicodedata, uuid
TZ = dt.timezone(dt.timedelta(hours=-4))
LOG = logging.getLogger('sma-recorder')
SQL = """BEGIN READ ONLY; SET LOCAL statement_timeout='2000ms';
SELECT coalesce(json_agg(x),'[]') FROM (
 SELECT c.stream_id, c.id::text channel_id, c.name channel_name,
 e.id::text environment_id, e.slug environment_slug, e.name environment_name,
 u.id::text unit_id, u.name unit_name, 1::bigint slot
 FROM portal_channel c JOIN portal_environment e ON e.id=c.environment_id
 LEFT JOIN portal_unit u ON u.id=c.unit_id
 UNION ALL
 SELECT i.stream_id, c.id::text, c.name,e.id::text,e.slug,e.name,u.id::text,u.name,i.slot
 FROM portal_channelinput i JOIN portal_channel c ON c.id=i.channel_id
 JOIN portal_environment e ON e.id=c.environment_id LEFT JOIN portal_unit u ON u.id=c.unit_id
) x; COMMIT;"""
def stamp(t=None): return dt.datetime.fromtimestamp(t if t is not None else time.time(), TZ).isoformat(timespec='milliseconds')
def slug(v):
 v=unicodedata.normalize('NFKD',str(v)).encode('ascii','ignore').decode()
 return re.sub(r'[^A-Za-z0-9_-]+','-',v).strip('-')[:70] or 'sem-nome'
def atomic_json(path, data):
 # Use a fresh exclusive sibling, then replace even if the previous process died.
 with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
  temp_path = Path(out.name)
  try:
   json.dump(data, out, ensure_ascii=False, indent=2)
   out.write("\n")
   out.flush()
   os.replace(temp_path, path)
  finally:
   temp_path.unlink(missing_ok=True)

def open_regular(path):
 fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
 if not stat.S_ISREG(os.fstat(fd).st_mode):
  os.close(fd)
  raise OSError("regular file required")
 return os.fdopen(fd, "rb")

def manifest_data(path):
 with open_regular(path) as stream:
  if os.fstat(stream.fileno()).st_size > 4 * 1024 * 1024:
   raise ValueError("oversized manifest")
  data = json.load(stream)
 if not isinstance(data, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("recording_id", ""))) or data["recording_id"] != path.parent.name:
  raise ValueError("invalid recording ID")
 sid = data.get("stream_id")
 if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{32}", sid):
  raise ValueError("invalid source")
 if not isinstance(data.get("segments"), list):
  raise ValueError("invalid segments")
 if data.get("status") in ("recording", "pending_export"):
  pattern = re.compile(re.escape(sid) + r"-[0-9]+\.ts")
  for segment in data["segments"]:
   if not isinstance(segment, dict) or not isinstance(segment.get("file"), str) or not pattern.fullmatch(segment["file"]):
    raise ValueError("invalid archived segment name")
   duration = segment.get("duration")
   if not isinstance(duration, (float, int)) or not 0 < duration < 120:
    raise ValueError("invalid archived segment duration")
   if not isinstance(segment.get("bytes"), int) or segment["bytes"] < 1:
    raise ValueError("invalid archived segment size")
 return data

def media_probe(path, *options, timeout=30):
 result = subprocess.run(
  ["ffprobe", "-v", "error", "-protocol_whitelist", "file", *options, "-of", "json", str(path)],
  capture_output=True, timeout=timeout, check=True
 )
 return json.loads(result.stdout)

def equal_media_packets(source, output, audio_index):
 """Independent completeness check when playlist timing disagrees with media PTS."""
 try:
  selected=[next(s for s in source['streams'] if s['codec_type']=='video')]
  if audio_index is not None:selected.append(next(s for s in source['streams'] if s['index']==audio_index and s['codec_type']=='audio'))
  expected=[(s['codec_type'],int(s['nb_read_packets'])) for s in selected]
  actual=[(s['codec_type'],int(s['nb_read_packets'])) for s in output['streams']]
  return expected==actual and all(n>0 for _,n in expected)
 except (KeyError,ValueError,TypeError,StopIteration):return False
def parse_playlist(path):
 """Only closed, locally named TS segments advertised by nginx are eligible."""
 result=[];duration=None;disc=False;seq=0
 with open_regular(path) as stream:
  body = stream.read(256 * 1024 + 1)
 if len(body) > 256 * 1024:
  raise ValueError('oversized playlist')
 for line in body.decode().splitlines():
  line=line.strip()
  if line.startswith('#EXT-X-MEDIA-SEQUENCE:'): seq=int(line.split(':',1)[1])
  elif line.startswith('#EXT-X-DISCONTINUITY') and line == '#EXT-X-DISCONTINUITY': disc=True
  elif line.startswith('#EXTINF:'):
   duration=float(line.split(':',1)[1].split(',')[0])
   if not math.isfinite(duration) or not 0 < duration < 120: raise ValueError('invalid duration')
  elif line and not line.startswith('#'):
   if not re.fullmatch(re.escape(path.stem)+r'-[0-9]+\.ts',line): raise ValueError('unsafe segment name')
   if duration is None: raise ValueError('missing duration')
   result.append((line,duration,disc,seq));duration=None;disc=False;seq+=1
 return result
class Recorder:
 def __init__(self,cfg):
  self.cfg=cfg;self.root=Path(cfg['archive']);self.root.mkdir(parents=True,exist_ok=True)
  self.hls=Path(cfg['hls']);self.sessions={};self.seen={};self.metadata={};self.running=True
  self.next_meta=0;self.next_scan=0;self.next_status=0;self.blocked=False;self.used=0;self.reserved_bytes=0
  self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1);self.jobs={};self.next_export=0;self.retry_after={}
 def metadata_refresh(self):
  if self.cfg.get('metadata_file'):
   rows=json.loads(Path(self.cfg['metadata_file']).read_text())
  else:
   env = os.environ.copy()
   if secret_file := env.get("SMA_PGPASSWORD_FILE"):
    env["PGPASSWORD"] = Path(secret_file).read_text().rstrip("\r\n")
   env["PGCONNECT_TIMEOUT"] = "2"
   p = subprocess.run(
    ["psql", "-X", "-w", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", SQL],
    capture_output=True, text=True, timeout=4, env=env
   )
   if p.returncode:
    raise RuntimeError("metadata query failed")
   rows = json.loads(p.stdout)
  self.metadata={x['stream_id']:x for x in rows}
 def scan_usage(self):
  total = 0
  for path in self.root.rglob("*"):
   try:
    info = path.lstat()
   except FileNotFoundError:
    continue
   if stat.S_ISREG(info.st_mode):
    total += info.st_size
  self.used = total
 def room(self,n=0,for_export=False):
  free=shutil.disk_usage(self.root).free
  reserved=0 if for_export else self.reserved_bytes
  return free-n-reserved >= self.cfg['reserve_bytes'] and self.used+n+reserved <= self.cfg['max_bytes']
 def new_session(self,sid,first_time):
  meta=self.metadata.get(sid,{})
  # Absence of authenticated operator identity is explicit. A shared key is not a person.
  now=time.time();uid=uuid.uuid4().hex;day=dt.datetime.fromtimestamp(first_time,TZ).strftime('%Y-%m-%d')
  folder=self.root/slug(meta.get('environment_slug','identificacao-pendente'))/day/uid
  folder.mkdir(parents=True);(folder/'segments').mkdir()
  m={'schema_version':1,'recording_id':uid,'stream_id':sid,**meta,'operator_name':None,
     'operator_identity_status':'not_provided_by_shared_key','timezone':'America/Boa_Vista',
     'utc_offset':'-04:00','capture_started_at':stamp(now),'started_at':stamp(first_time),
     'start_time_basis':'first_segment_mtime_minus_duration','last_segment_at':stamp(now),
     'status':'recording','segments':[],'duration_seconds':0.0,'missing_segments':0,
     'naming':'channel-source-gdh; not a verified individual operator','method':'passive_hls_copy'}
  s={'folder':folder,'manifest':m,'last':now};self.sessions[sid]=s;return s
 def persist(self,s):
  m=s['manifest'];atomic_json(s['folder']/'manifest.json',m)
  lines=['#EXTM3U','#EXT-X-VERSION:3',f"#EXT-X-TARGETDURATION:{max(1,math.ceil(max((x['duration'] for x in m['segments']),default=1)))}",'#EXT-X-MEDIA-SEQUENCE:0','#EXT-X-PLAYLIST-TYPE:EVENT' if m['status']=='recording' else '#EXT-X-PLAYLIST-TYPE:VOD']
  for seg in m['segments']:
   if seg.get('discontinuity'):lines.append('#EXT-X-DISCONTINUITY')
   lines += [f"#EXTINF:{seg['duration']:.3f},",'segments/'+seg['file']]
  if m['status']!='recording':lines.append('#EXT-X-ENDLIST')
  p=s['folder']/'recording.m3u8';tmp=p.with_suffix('.tmp');tmp.write_text('\n'.join(lines)+'\n');os.replace(tmp,p)
 def close(self,sid,reason):
  s=self.sessions.pop(sid,None)
  if not s:return
  m=s['manifest'];m.update(status='pending_export',end_reason=reason,closed_at=stamp(),ended_at=m['last_segment_at'])
  self.persist(s);LOG.info('closed recording=%s reason=%s segments=%s',m['recording_id'],reason,len(m['segments']))
 def capture(self,path):
  sid=path.stem
  if not re.fullmatch('[0-9a-f]{32}',sid) or path.is_symlink():return
  entries=parse_playlist(path);seen=self.seen.setdefault(sid,{})
  for name,duration,disc,seq in entries:
   src=self.hls/name
   try: info=src.lstat()
   except FileNotFoundError:continue
   if not stat.S_ISREG(info.st_mode) or info.st_size == 0:continue
   signature = (info.st_mtime_ns, info.st_size, info.st_ino)
   previous = seen.get(name)
   if previous and (previous[1] == signature or (previous[1] is None and info.st_mtime <= previous[0] + .001)):
    continue
   # Do not re-record old playlists after boot/recovery, or stale ended sessions.
   if time.time()-info.st_mtime > self.cfg.get('max_source_age',30):continue
   s=self.sessions.get(sid)
   repeated_name = bool(previous and s and any(seg["file"] == name for seg in s["manifest"]["segments"]))
   if s and (disc or repeated_name or s['manifest']['duration_seconds']+duration>self.cfg['part_seconds']):
    self.close(sid,'discontinuity' if disc or repeated_name else 'part_limit');s=None
   if not self.room(info.st_size + 32768):
    if not self.blocked:LOG.warning('storage guard active; live service unaffected')
    self.blocked=True;self.close(sid,'storage_guard');continue
   self.blocked=False
   if s is None:s=self.new_session(sid,info.st_mtime-duration)
   dest=s['folder']/'segments'/name;tmp=dest.with_suffix('.partial')
   try:
    # Copy, never hard-link: the archive cannot modify source video bytes.
    with open_regular(src) as inf,tmp.open('xb') as outf:
     opened = os.fstat(inf.fileno())
     if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
      raise OSError("source replaced before copy")
     remaining = info.st_size
     while remaining:
      chunk = inf.read(min(remaining, 1024 * 1024))
      if not chunk:
       break
      outf.write(chunk)
      remaining -= len(chunk)
     after = os.fstat(inf.fileno())
     if (after.st_mtime_ns, after.st_size) != (info.st_mtime_ns, info.st_size) or remaining:
      raise OSError("source changed while copying")
    os.replace(tmp,dest)
   except OSError:
    tmp.unlink(missing_ok=True);LOG.warning('segment copy failed for source=%s',sid);continue
   m=s['manifest'];previous=m['segments'][-1]['sequence'] if m['segments'] else None
   gap=previous is not None and seq>previous+1
   if gap:m['missing_segments']+=seq-previous-1;LOG.warning('segment gap recording=%s',m['recording_id'])
   m['segments'].append({'file':name,'duration':duration,'sequence':seq,'bytes':info.st_size,'source_mtime_ns':info.st_mtime_ns,'source_inode':info.st_ino,'discontinuity':disc or gap})
   m['duration_seconds']+=duration;m['last_segment_at']=stamp(info.st_mtime)
   self.used+=info.st_size+512;s['last']=time.time();seen[name]=(info.st_mtime,signature);self.persist(s)
  self.seen[sid]={k:v for k,v in seen.items() if time.time()-v[0]<300}
 def recover(self):
  for path in self.root.rglob('manifest.json'):
   try:
    m=manifest_data(path);sid=m['stream_id']
    if time.time()-dt.datetime.fromisoformat(m['last_segment_at']).timestamp()<300:
     for x in m['segments']:
      sig = (x['source_mtime_ns'], x['bytes'], x['source_inode']) if 'source_mtime_ns' in x and 'source_inode' in x else None
      last = dt.datetime.fromisoformat(m['last_segment_at']).timestamp()
      key = x['file']
      previous = self.seen.setdefault(sid, {}).get(key)
      if previous is None or last >= previous[0]:
       self.seen[sid][key] = (last, sig)
    if m['status'] in ('pending_export','recording'):
     (path.parent / 'export.partial.mp4').unlink(missing_ok=True)
    if m['status']=='recording':
     m.update(status='pending_export',end_reason='recorder_recovery',closed_at=stamp(),ended_at=m['last_segment_at'])
     self.persist({'folder':path.parent,'manifest':m});LOG.warning('recovered recording=%s',m['recording_id'])
   except (ValueError,KeyError,TypeError,OSError):LOG.error('invalid archive manifest: %s',path)
 def export(self, path):
  # A timed-out or failed remux must not permanently consume the export margin.
  try:
   self.write_export(path)
  finally:
   (path.parent / "export.partial.mp4").unlink(missing_ok=True)

 def write_export(self, path):
  m=manifest_data(path);folder=path.parent
  started=dt.datetime.fromisoformat(m['started_at']).astimezone(TZ)
  name=f"{slug(m.get('environment_slug','SMA'))}__{slug(m.get('channel_name','canal-sem-identificacao'))}__fonte-{m.get('slot',1):03d}__{started.strftime('%d%m%Y_%H%M%S')}-UTC-04__{m['recording_id'][:8]}.mp4"
  size=sum(x['bytes'] for x in m['segments'])
  # Preserve originals if there is insufficient temporary space for remux.
  if not self.room(size+32*1024*1024,for_export=True):return
  temp=folder/'export.partial.mp4';out=folder/name
  if not out.exists():
   # Derive the local-only playlist from validated manifest entries on each attempt.
   self.persist({'folder': folder, 'manifest': m})
   try:
    streams = media_probe(folder/'recording.m3u8', '-analyzeduration','10000000','-probesize','10000000','-show_streams')['streams']
   except (KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
    raise RuntimeError("input inspection failed; source preserved") from exc
   # nginx HLS may advertise an empty AAC track for a video-only publisher.
   audio=next((x for x in streams if x.get('codec_type')=='audio' and int(x.get('sample_rate') or 0)>0 and int(x.get('channels') or 0)>0),None)
   m['audio_status']='present' if audio else 'not_detected'
   maps=['-map','0:v:0']+(['-map',f"0:{audio['index']}"] if audio else [])
   cmd = [
    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
    "-protocol_whitelist", "file", "-i", str(folder/"recording.m3u8"), *maps,
    "-c", "copy", "-movflags", "+faststart", str(temp),
   ]
   result=subprocess.run(cmd, capture_output=True, timeout=180)
   if result.returncode:
    temp.unlink(missing_ok=True)
    raise RuntimeError("ffmpeg remux failed; source preserved")
   info=media_probe(temp, '-show_entries','format=duration:stream=codec_type')
   actual=float(info.get('format',{}).get('duration',0))
   if actual<=0 or not any(x.get('codec_type')=='video' for x in info.get('streams',[])):raise RuntimeError('invalid video')
   if not math.isfinite(actual):raise RuntimeError('invalid duration; source preserved')
   declared=m.get('hls_duration_seconds',m['duration_seconds'])
   if abs(actual-declared)>max(3,declared*.05):
    # Some live publishers send irregular timestamps: EXTINF wall time can
    # exceed the actual encoded media duration. Never just relax the limit.
    # Check every mapped packet, then retain the source files for review.
    counts = [
     media_probe(file, '-count_packets', '-show_entries', 'stream=index,codec_type,nb_read_packets', timeout=120)
     for file in (folder/'recording.m3u8', temp)
    ]
    if not equal_media_packets(counts[0],counts[1],audio['index'] if audio else None):raise RuntimeError('packet count mismatch; source preserved')
    m.update(timing_discrepancy=True,source_preserved=True,packet_counts_verified=True)
   m.update(hls_duration_seconds=declared,duration_seconds=actual)
   # Persist validation metadata before publishing the MP4, including recovery
   # after a crash between the rename and the final complete-state update.
   atomic_json(path,m)
   os.replace(temp,out)
  m.update(status='complete',video_file=name,video_bytes=out.stat().st_size,exported_at=stamp())
  atomic_json(path,m)
  # Only finalized, validated recordings lose redundant temporary TS files.
  if not m.get('source_preserved'):
   shutil.rmtree(folder/'segments',ignore_errors=True);(folder/'recording.m3u8').unlink(missing_ok=True)
  LOG.info('export complete recording=%s',m['recording_id'])
 def exports(self):
  for path,fut in list(self.jobs.items()):
   if fut.done():
    try:fut.result()
    except Exception as exc:
     LOG.error('export delayed %s: %s',path.parent.name,type(exc).__name__);self.retry_after[path]=time.time()+300
    del self.jobs[path];self.reserved_bytes=0
  if self.jobs or time.time()<self.next_export:return
  self.next_export=time.time()+10
  for path in sorted(self.root.rglob('manifest.json')):
   try:
    m = manifest_data(path)
    if m.get('status') != 'pending_export' or not m['segments'] or self.retry_after.get(path,0)>time.time():
     continue
    if 'channel_id' not in m:
     source = self.metadata.get(m['stream_id'])
     if not source:
      continue
     m.update(source)
     atomic_json(path,m)
    size = sum(x['bytes'] for x in m['segments']) + 32*1024*1024
    if not self.room(size, for_export=True):
     continue
   except (OSError, ValueError, TypeError, KeyError):
    LOG.warning("ignoring unreadable pending manifest in %s", path.parent.name)
    continue
   self.reserved_bytes = size
   self.jobs[path]=self.pool.submit(self.export,path);break
 def tick(self):
  now=time.time()
  if now>=self.next_meta:
   self.next_meta=now+10
   try:self.metadata_refresh()
   except Exception:LOG.warning('metadata temporarily unavailable; recording remains enabled')
  if now>=self.next_scan:
   self.next_scan=now+30
   try:self.scan_usage()
   except OSError:pass
  for path in self.hls.glob('*.m3u8'):
   try:self.capture(path)
   except (OSError,ValueError):LOG.warning('playlist temporarily unavailable: %s',path.name)
  for sid,s in list(self.sessions.items()):
   if now-s['last']>self.cfg['idle_seconds']:self.close(sid,'stream_idle')
  if self.cfg.get('export_enabled',True):self.exports()
  if now>=self.next_status:
   self.next_status=now+5
   atomic_json(self.root/'status.json',{'updated_at':stamp(),'active_recordings':len(self.sessions),'export_jobs':len(self.jobs),'archive_bytes':self.used,'free_bytes':shutil.disk_usage(self.root).free,'storage_guard':not self.room(),'metadata_sources':len(self.metadata),'ui_changed':False})
 def run(self):
  self.recover()
  while self.running:
   self.tick();time.sleep(self.cfg.get('poll_seconds',0.5))
  for sid in list(self.sessions):self.close(sid,'recorder_stopped')
  self.pool.shutdown(wait=True)
def main():
 os.umask(0o077);logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
 ap=argparse.ArgumentParser();ap.add_argument('--config',required=True);args=ap.parse_args()
 cfg=json.loads(Path(args.config).read_text());r=Recorder(cfg)
 lock=(r.root/'.recorder.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:setattr(r,'running',False))
 r.run()
if __name__=='__main__':main()
