import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from recorder import Recorder,parse_playlist,stamp,slug,equal_media_packets
class ArchiveTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.p=Path(self.temp.name);self.hls=self.p/'hls';self.hls.mkdir()
  self.cfg={'hls':str(self.hls),'archive':str(self.p/'archive'),'reserve_bytes':0,'max_bytes':10**8,'part_seconds':900,'idle_seconds':15,'export_enabled':False,'ffmpeg_image':'test'}
  self.r=Recorder(self.cfg);self.addCleanup(self.r.pool.shutdown)
  self.sid='a'*32
 def playlist(self,sid=None,seq=0,count=2,disc=False):
  sid=sid or self.sid;lines=['#EXTM3U',f'#EXT-X-MEDIA-SEQUENCE:{seq}']
  for n in range(seq,seq+count):
   name=f'{sid}-{n}.ts';(self.hls/name).write_bytes(b'x'*188)
   if disc and n==seq:lines.append('#EXT-X-DISCONTINUITY')
   lines.extend(['#EXTINF:1.0,',name])
  path=self.hls/(sid+'.m3u8');path.write_text('\n'.join(lines));return path
 def test_simultaneous_shared_channel_is_separate(self):
  for i,sid in enumerate(['a'*32,'b'*32],1):
   self.r.metadata[sid]={'channel_id':'shared','channel_name':'Equipe 1','slot':i,'environment_slug':'sma1'}
   self.r.capture(self.playlist(sid))
  self.assertEqual(len(self.r.sessions),2)
  self.assertEqual(len(list((self.p/'archive').rglob('manifest.json'))),2)
  for s in self.r.sessions.values():self.assertIsNone(s['manifest']['operator_name'])
 def test_idempotency_and_source_untouched(self):
  p=self.playlist();before={x.name:x.read_bytes() for x in self.hls.iterdir()}
  self.r.capture(p);self.r.capture(p)
  self.assertEqual(len(self.r.sessions[self.sid]['manifest']['segments']),2)
  self.assertEqual(before,{x.name:x.read_bytes() for x in self.hls.iterdir()})
 def test_quota_and_reserve_preserve_live(self):
  p=self.playlist();self.r.cfg['max_bytes']=1;self.r.capture(p)
  self.assertFalse(self.r.sessions);self.assertTrue(p.exists())
  self.r.cfg['max_bytes']=10**8;self.r.cfg['reserve_bytes']=10**20;self.r.capture(p)
  self.assertFalse(self.r.sessions)
 def test_export_reservation_protects_capture_budget(self):
  self.r.reserved_bytes=10**8
  self.assertFalse(self.r.room(1))
  self.assertTrue(self.r.room(1,for_export=True))
 def test_recovery_no_duplicate_old_segments(self):
  p=self.playlist();self.r.capture(p);r2=Recorder(self.cfg);self.addCleanup(r2.pool.shutdown);r2.recover();r2.capture(p)
  self.assertFalse(r2.sessions)
  m=json.loads(next((self.p/'archive').rglob('manifest.json')).read_text());self.assertEqual(m['end_reason'],'recorder_recovery')
 def test_discontinuity_splits_sessions(self):
  self.r.capture(self.playlist());self.r.capture(self.playlist(seq=2,disc=True))
  self.assertEqual(len(list((self.p/'archive').rglob('manifest.json'))),2)
 def test_part_limit(self):
  self.r.cfg['part_seconds']=2;self.r.capture(self.playlist(count=5))
  self.assertEqual(len(list((self.p/'archive').rglob('manifest.json'))),3)
 def test_gap_reported(self):
  self.r.capture(self.playlist());self.r.capture(self.playlist(seq=4))
  self.assertEqual(self.r.sessions[self.sid]['manifest']['missing_segments'],2)
 def test_paths_and_invalid_duration(self):
  p=self.hls/(self.sid+'.m3u8')
  for body in ['#EXTINF:1,\n../../secret.ts','#EXTINF:nan,\n'+self.sid+'-1.ts']:
   p.write_text(body)
   with self.assertRaises(ValueError):parse_playlist(p)
 def test_export_failure_retains_original(self):
  self.r.capture(self.playlist());self.r.close(self.sid,'stream_idle');p=next((self.p/'archive').rglob('manifest.json'))
  with patch('recorder.subprocess.run') as run:
   run.return_value.returncode=1
   with self.assertRaises(RuntimeError):self.r.export(p)
  self.assertTrue((p.parent/'segments').exists());self.assertEqual(json.loads(p.read_text())['status'],'pending_export')
 def test_timezone_and_safe_names(self):
  self.assertEqual(stamp(0),'1969-12-31T20:00:00.000-04:00');self.assertEqual(slug('../../Canal Á / 1'),'Canal-A-1')
 def test_packet_completeness_not_duration_tolerance(self):
  video={'index':0,'codec_type':'video','nb_read_packets':'39'}
  audio={'index':2,'codec_type':'audio','nb_read_packets':'52'}
  source={'streams':[video,audio]};output={'streams':[video,{**audio,'index':1}]}
  self.assertTrue(equal_media_packets(source,output,2))
  self.assertTrue(equal_media_packets(source,{'streams':[video]},None))
  self.assertFalse(equal_media_packets(source,{'streams':[video]},2))
  self.assertFalse(equal_media_packets(source,{'streams':[{**video,'nb_read_packets':'38'},audio]},2))
  self.assertFalse(equal_media_packets(source,{'streams':[{**video,'nb_read_packets':'N/A'},audio]},2))
  self.assertFalse(equal_media_packets({'streams':[]},output,None))
if __name__=='__main__':unittest.main(verbosity=2)
