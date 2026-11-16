#!/usr/bin/env python3
import unittest
import threading
import time
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch
import check_live_stability as q

class Clock:
    def __init__(self): self.now=0.
    def monotonic(self): return self.now
    def sleep(self,n): self.now+=n

class StabilityTests(unittest.TestCase):
    def test_cli_defaults_cctv_and_supplementary_audio_decode(self):
        import contextlib,io,json,sys
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            manifest=Path(tmp,'manifest.json');output=Path(tmp,'out.json')
            manifest.write_text(json.dumps({'live':[{'name':'fixture','url':'https://a.test/list'}]}))
            body=b'#EXTM3U\n#EXTINF:-1,CCTV-1\nhttps://a.test/1.m3u8\n#EXTINF:-1,CGTN\nhttps://a.test/news.m3u8\n'
            decoded={'status':'decoded','warnings':'','progress':[{'frame':'500','out_time_us':'20000000','dup_frames':'0','drop_frames':'0'}]}
            with patch.object(sys,'argv',['audit','--manifest',str(manifest),'--json-out',str(output),'--seconds','1']),patch.object(q,'run_lock',return_value=contextlib.nullcontext()),patch.object(q.base,'ipv6_check',return_value=(False,'fixture')),patch.object(q.base,'fetch',return_value=SimpleNamespace(ok=True,body=body)),patch.object(q,'audit',return_value={'channel':'CCTV-1','status':'measured','stable_sample':True,'summary':{}}),patch.object(q,'decode_sample',return_value=decoded) as decode,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(q.main(),0)
            result=json.loads(output.read_text());self.assertEqual(result['coverage'][0]['selected'],['CCTV-1']);self.assertEqual(len(result['coverage'][0]['missing_patterns']),17)
            self.assertEqual(decode.call_args.args[1:],(20,8,True));self.assertEqual(result['decode_seconds'],20)

    def test_decode_timeout_keeps_partial_evidence(self):
        import subprocess
        exc=subprocess.TimeoutExpired(['ffmpeg'],25,output=b'frame=20\nout_time_us=800000\nprogress=continue\n',stderr=b'partial decode warning')
        with patch.object(q.shutil,'which',return_value='/usr/bin/ffmpeg'),patch.object(q.subprocess,'run',side_effect=exc):
            result=q.decode_sample(('test','https://a.test/a'),5,1,True)
        self.assertEqual(result['status'],'timeout');self.assertIn('partial decode',result['warnings']);self.assertEqual(result['progress'][-1]['frame'],'20');self.assertFalse(q.clean_decode_sample(result,5))

    def test_optional_audio_decode_is_explicit(self):
        from types import SimpleNamespace
        done=SimpleNamespace(returncode=0,stderr='',stdout='frame=25\nout_time_us=1000000\ndup_frames=0\ndrop_frames=0\nprogress=end\n')
        with patch.object(q.shutil,'which',return_value='/usr/bin/ffmpeg'),patch.object(q.subprocess,'run',return_value=done) as run:
            result=q.decode_sample(('test','https://a.test/live.m3u8'),1,1,True)
            self.assertIn('0:a:0?',run.call_args.args[0]);self.assertNotIn('-an',run.call_args.args[0]);self.assertTrue(result['audio_requested']);self.assertTrue(q.clean_decode_sample(result,1))
            q.decode_sample(('test','https://a.test/live.m3u8'),1,1)
            self.assertIn('-an',run.call_args.args[0])

    def test_refreshes_resolved_playlist_not_scheduling_entry(self):
        clock=Clock();calls=[];counter=[0]
        def fetch(url,headers,timeout,cap,br=None,**kwargs):
            clock.now+=.01;calls.append(url)
            if '.ts' not in url:
                counter[0]+=1;n=counter[0];body=('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%d\n#EXTINF:2,\n%d.ts\n'%(n,n)).encode();actual='https://cdn.test/actual.m3u8?sig=keep'
            else:body=(b'G'+bytes(187))*3+url.encode();actual=url
            return body,dict(url=actual,ttfb=.005,seconds=.01,bytes=len(body),status=200)
        with patch.object(q.time,'monotonic',clock.monotonic),patch.object(q.time,'sleep',clock.sleep),patch.object(q,'request',fetch):
            result=q.audit(('test','https://entry.test/live'),8)
        self.assertEqual(calls.count('https://entry.test/live'),1);self.assertGreaterEqual(calls.count('https://cdn.test/actual.m3u8?sig=keep'),1);self.assertTrue(result['stable_sample'])

    def test_parse_sequence_range_variant(self):
        p=q.playlist('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:42\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\n#EXT-X-BYTERANGE:100@50\na.ts\n#EXTINF:4,\n#EXT-X-BYTERANGE:80\na.ts','https://a.test/live/index.m3u8')
        self.assertEqual(p['segments'][1]['seq'],43)
        self.assertEqual(p['segments'][1]['range'],(150,229))
        self.assertEqual(p['segments'][0]['url'],'https://a.test/live/a.ts')
        v=q.playlist('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1920x1080\nhd.m3u8','https://a.test/x')
        self.assertEqual(v['variants'][0]['RESOLUTION'],'1920x1080')
    def run_case(self,mode):
        clock=Clock(); counter=[0]
        def fetch(url,headers,timeout,cap,br=None,**kwargs):
            clock.now+=.01
            if url.endswith('.m3u8'):
                counter[0]+=1
                seq=counter[0] if mode!='stale' else 1
                end='#EXT-X-ENDLIST\n' if mode=='vod' else ''
                body=('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%d\n#EXTINF:2,\n%d.ts\n%s'%(seq,seq,end)).encode()
            else:
                body=b'G'+b'0'*187+b'G'+b'0'*187+b'G'+b'0'*187
                if mode!='replay': body+=url.encode()
            return body,dict(url=url,ttfb=.005,seconds=.01,bytes=len(body),status=200)
        with patch.object(q.time,'monotonic',clock.monotonic),patch.object(q.time,'sleep',clock.sleep),patch.object(q,'request',fetch):
            return q.audit(('test','https://a.test/a.m3u8'),seconds=8)
    def test_advancing_segments(self):
        o=self.run_case('live'); self.assertTrue(o['stable_sample']); self.assertGreaterEqual(o['summary']['unique_segments'],3)
    def test_overlapping_window_is_not_rollback(self):
        clock=Clock();counter=[0]
        def fetch(url,headers,timeout,cap,br=None,**kwargs):
            clock.now+=.01
            if url.endswith('.m3u8'):
                counter[0]+=1;n=counter[0]
                body=('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%d\n'%n + ''.join('#EXTINF:2,\n%d.ts\n'%i for i in range(n,n+3))).encode()
            else: body=(b'G'+bytes(187))*3+url.encode()
            return body,dict(url=url,ttfb=.005,seconds=.01,bytes=len(body),status=200)
        with patch.object(q.time,'monotonic',clock.monotonic),patch.object(q.time,'sleep',clock.sleep),patch.object(q,'request',fetch):
            o=q.audit(('overlap','https://a.test/a.m3u8'),seconds=8)
        self.assertTrue(o['stable_sample']);self.assertEqual(o['summary']['failures'],0)
    def test_cached_playlist_no_credit(self):
        o=self.run_case('stale'); self.assertFalse(o['stable_sample']); self.assertEqual(o['summary']['unique_segments'],1)
    def test_replayed_payload_rejected(self):
        o=self.run_case('replay'); self.assertFalse(o['stable_sample']); self.assertGreater(o['summary']['failures'],0)
    def test_endlist_not_live(self):
        o=self.run_case('vod'); self.assertEqual(o['status'],'not_live_endlist'); self.assertFalse(o['stable_sample'])
    def test_real_http_direct_flv_is_not_an_hls_timeout(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                body=b'FLV'+bytes(32)
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            out=q.audit(('FLV fixture','http://127.0.0.1:%d/live'%server.server_port),seconds=3,timeout=1)
            self.assertEqual(out['status'],'unsupported_non_hls')
            self.assertEqual(out['summary']['request_failures'],0)
            self.assertFalse(out['stable_sample'])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)
    def test_real_http_trickle_obeys_total_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200);self.end_headers()
                try:
                    for i in range(40):
                        self.wfile.write(b'x');self.wfile.flush();time.sleep(.04)
                except (BrokenPipeError,ConnectionResetError): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        started=time.monotonic()
        try:
            with self.assertRaises(TimeoutError):q.request('http://127.0.0.1:%d/x'%server.server_port,{},.2,1000)
            self.assertLess(time.monotonic()-started,1.)
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_encrypted_stream_is_not_bypassed(self):
        body=b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:6,\na.ts'
        with patch.object(q,'request',return_value=(body,dict(url='https://example.com/a.m3u8',ttfb=.01,seconds=.01,bytes=len(body),status=200))) as fetch:
            result=q.audit(('x','https://example.com/a.m3u8'),3)
        self.assertEqual(result['status'],'unsupported_encrypted_hls');self.assertFalse(result['stable_sample']);self.assertEqual(fetch.call_count,1)

    def test_lock_refuses_second_owner(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            path=Path(folder)/'lock'
            with q.run_lock(path):
                owner=(path/'owner.json').read_text()
                with self.assertRaises(FileExistsError):
                    with q.run_lock(path):pass
                self.assertEqual((path/'owner.json').read_text(),owner)
            self.assertFalse(path.exists())

    def test_zero_exit_with_corrupt_frames_is_not_clean_decode(self):
        result={'status':'decoded','progress':[{'frame':'1500','out_time_us':'60000000','dup_frames':'0','drop_frames':'0'}],'warnings':''}
        self.assertTrue(q.clean_decode_sample(result,60))
        result['warnings']='corrupt decoded frame'
        self.assertFalse(q.clean_decode_sample(result,60))

    def test_fetch_failure_never_stable(self):
        with patch.object(q,'request',side_effect=TimeoutError('fixture')):
            o=q.audit(('test','https://a.test/a.m3u8'),seconds=1)
        self.assertFalse(o['stable_sample']); self.assertEqual(o['status'],'failed')

if __name__=='__main__': unittest.main()
