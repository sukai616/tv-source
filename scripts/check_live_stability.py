#!/usr/bin/env python3
"""Low-concurrency, wall-clock HLS supply audit. Not a player/first-frame test."""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.parse
import check_sources as base
from live_composite_policy import TARGETS as BASE_CHANNELS, select_representatives


def playlist(text, url):
    """Preserve media sequence, duration, byte range, encryption and variant metadata."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    seq, duration, variant, byterange = 0, None, None, None
    out = dict(segments=[], variants=[], target=6., end=False, encrypted=False, init=None)
    offset = 0
    for line in lines:
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            seq = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-TARGETDURATION:'):
            out['target'] = max(1., float(line.split(':', 1)[1]))
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',')[0])
        elif line.startswith('#EXT-X-STREAM-INF:'):
            variant = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line))
        elif line.startswith('#EXT-X-BYTERANGE:'):
            v = line.split(':', 1)[1].split('@')
            size = int(v[0]); start = int(v[1]) if len(v) > 1 else offset
            byterange = (start, start + size - 1); offset = start + size
        elif line.startswith('#EXT-X-KEY:') and 'METHOD=NONE' not in line:
            out['encrypted'] = True
        elif line.startswith('#EXT-X-MAP:'):
            m = re.search(r'URI="([^"]+)"', line)
            if m: out['init'] = urllib.parse.urljoin(url, m[1])
        elif line == '#EXT-X-ENDLIST':
            out['end'] = True
        elif not line.startswith('#'):
            absolute = urllib.parse.urljoin(url, line)
            if variant is not None:
                out['variants'].append(dict(url=absolute, **variant)); variant = None
            elif duration is not None:
                out['segments'].append(dict(seq=seq, url=absolute, duration=duration, range=byterange))
                seq += 1; duration = None; byterange = None
    return out


def request(url, headers, timeout, max_bytes, byte_range=None, stream_probe=False):
    """TTFB includes DNS/TLS/headers and first body byte, never first decoded frame."""
    t = time.monotonic()
    h = {'User-Agent': base.OKHTTP_UA, 'Cache-Control': 'no-cache', 'Pragma': 'no-cache', **headers}
    if byte_range: h['Range'] = 'bytes=%d-%d' % tuple(byte_range)
    req = urllib.request.Request(base.clean_url(url), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        first = response.read(1); ttfb = time.monotonic() - t
        if stream_probe:
            first += response.read(2)
            if first.startswith(b'FLV'):
                return first, dict(url=response.url,ttfb=ttfb,seconds=time.monotonic()-t,
                                   status=response.status,age=response.headers.get('Age'),bytes=len(first),stream_kind='FLV')
            if first.startswith(b'G'):
                first += response.read(561)
                if base.is_ts_payload(first):
                    return first, dict(url=response.url,ttfb=ttfb,seconds=time.monotonic()-t,
                                       status=response.status,age=response.headers.get('Age'),bytes=len(first),stream_kind='TS')
        chunks = [first]; n = len(first)
        while True:
            if time.monotonic() - t > timeout: raise TimeoutError('total request deadline')
            chunk = response.read1(min(65536, max_bytes + 1 - n))
            if not chunk: break
            chunks.append(chunk); n += len(chunk)
            if n > max_bytes: raise ValueError('response exceeds byte cap; not a complete segment')
        if byte_range and response.status != 206:
            raise ValueError('byte range ignored')
        return b''.join(chunks), dict(url=response.url, ttfb=ttfb, seconds=time.monotonic()-t,
                                      status=response.status, age=response.headers.get('Age'), bytes=n)


def percentile(values, q):
    if not values: return None
    v = sorted(values)
    return v[min(len(v)-1, math.ceil(q * len(v))-1)]


def summarize(events, elapsed, supplied, startup, longest_gap, missed):
    good = [e for e in events if e.get('kind') == 'segment' and e.get('ok')]
    failures = [e for e in events if not e.get('ok', True)]
    ratios = [e['seconds']/e['duration'] for e in good if e['duration'] > 0]
    return dict(elapsed=elapsed, unique_segments=len(good), supplied_media_seconds=supplied,
                supply_ratio=supplied/elapsed if elapsed else 0,
                startup_complete_segment_seconds=startup,
                first_body_byte_seconds=good[0]['ttfb'] if good else None,
                p95_download_media_ratio=percentile(ratios, .95),
                slow_segments=sum(x >= 1 for x in ratios), failures=len(failures),
                request_failures=sum(e.get('kind') in ('playlist','segment','init') for e in failures),
                validation_failures=sum(e.get('kind')=='segment_validation' for e in failures),
                retries=sum(e.get('retry', 0) for e in events),
                longest_no_new_media_seconds=longest_gap, missed_sequences=missed,
                bytes=sum(e.get('bytes',0) for e in good),
                average_downloaded_media_bitrate=(sum(e.get('bytes',0) for e in good)*8/supplied if supplied else None),
                note='供给模拟而非真实播放器卡顿/首帧；不含解码及手机缓冲策略')


def audit(channel, seconds=180, timeout=8, max_segment_mb=32):
    url, headers = base.media_headers(channel)
    out = dict(channel=channel[0], url=url, events=[], variants=[], status='incomplete')
    events = out['events']; start = time.monotonic(); deadline = start + seconds
    seen, hashes = set(), collections.Counter()
    supplied, startup, last_new, longest, missed = 0., None, start, 0., 0
    previous_seq = None; first_playlist = True; buffer = 0.; last_tick = start
    deficit = 0.; deficit_events = 0; was_empty = False
    media_url = url
    parsed = {'target': 6}
    initialized = set()
    def get(target, cap, kind, br=None):
        for retry in range(2):
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError('audit window ended')
            try:
                body, meta = request(target, headers, timeout, cap, br, stream_probe=(kind=='playlist'))
                meta.update(kind=kind, ok=True, retry=retry)
                return body, meta
            except Exception as exc:
                events.append(dict(kind=kind, url=target, ok=False, retry=retry,
                                   error=str(exc), at=time.monotonic()-start))
                if retry: raise
    try:
        # Resolve master playlists once. Highest bandwidth is deliberate, never silently downgrade.
        for depth in range(4):
            body, meta = get(media_url, 2*1024*1024, 'playlist')
            events.append(meta)
            if b'#EXTM3U' not in body[:1024]:
                out['status'] = 'unsupported_non_hls'; return out
            parsed = playlist(body.decode('utf-8-sig', 'replace'), meta['url'])
            if not parsed['variants']: break
            out['variants'].extend(parsed['variants'])
            selected = max(parsed['variants'], key=lambda x:int(x.get('BANDWIDTH','0')))
            out['selected_variant'] = selected
            media_url = selected['url']
        else:
            raise ValueError('too many HLS nesting levels')
        # Refresh the resolved media playlist, not a scheduling entry that may choose another stream.
        media_url = meta['url']
        out['initial_media_url'] = media_url; out['media_url'] = media_url; out['initial_sequence'] = parsed['segments'][0]['seq'] if parsed['segments'] else None
        while time.monotonic() < deadline:
            if parsed['end']:
                out['status'] = 'not_live_endlist'; break
            if parsed['encrypted']:
                out['status'] = 'unsupported_encrypted_hls'; break
            if parsed['init'] and parsed['init'] not in initialized:
                init, im = get(parsed['init'], 2*1024*1024, 'init'); events.append(im)
                if not base.is_mp4_payload(init) and not base.is_fmp4_payload(init):
                    raise ValueError('invalid fMP4 initialization')
                initialized.add(parsed['init'])
            segments = parsed['segments']
            if segments and previous_seq is not None and max(x['seq'] for x in segments) < previous_seq:
                events.append(dict(kind='playlist_rollback', ok=False, last_seq=segments[-1]['seq'], previous=previous_seq))
            # Start near live edge (last 2 completed segments), never credit full cached history.
            if first_playlist: segments = segments[-2:]; first_playlist = False
            for seg in segments:
                key = (seg['seq'], seg['url'], tuple(seg['range'] or ()))
                if key in seen: continue
                if previous_seq is not None and seg['seq'] <= previous_seq:
                    # Normal overlapping windows include older segments we intentionally skipped
                    # on startup. They are not sequence rollback and earn no fresh-media credit.
                    continue
                if time.monotonic() >= deadline: break
                try:
                    data, sm = get(seg['url'], max_segment_mb*1024*1024, 'segment', seg['range'])
                    if not (base.is_ts_payload(data) or base.is_mp4_payload(data) or base.is_fmp4_payload(data)):
                        raise ValueError('not TS/fMP4 media')
                    digest = hashlib.sha256(data).hexdigest()
                    hashes[digest] += 1
                    # Identical payload under increasing seq/URL must not count as sustained fresh media.
                    if hashes[digest] > 1:
                        raise ValueError('repeated identical segment payload')
                    now = time.monotonic()
                    if startup is not None:
                        used = now-last_tick
                        empty = max(0., used-buffer)
                        deficit += empty
                        if empty > 0 and not was_empty: deficit_events += 1
                        was_empty = empty > 0
                        buffer = max(0., buffer-used)
                    buffer += seg['duration']; last_tick = now
                    longest = max(longest, now-last_new); last_new = now
                    if startup is None: startup = now-start
                    if previous_seq is not None: missed += max(0, seg['seq']-previous_seq-1)
                    previous_seq = seg['seq']; seen.add(key); supplied += seg['duration']
                    sm.update(seq=seg['seq'], duration=seg['duration'], sha256=digest, at=now-start)
                    events.append(sm)
                except Exception as exc:
                    events.append(dict(kind='segment_validation', ok=False, url=seg['url'], error=str(exc)))
            longest = max(longest, time.monotonic()-last_new)
            # Refresh at half target; cached windows with no new seq earn no media seconds.
            time.sleep(min(max(1., parsed['target']/2), max(0., deadline-time.monotonic())))
            if time.monotonic() >= deadline: break
            body, pm = get(media_url, 2*1024*1024, 'playlist'); events.append(pm)
            media_url = pm['url']; out['media_url'] = media_url
            parsed = playlist(body.decode('utf-8-sig','replace'), pm['url'])
        else:
            out['status'] = 'measured'
    except Exception as exc:
        out['error'] = str(exc)
        if time.monotonic() >= deadline-0.2: out['status'] = 'measured'
        else: out['status'] = 'failed'
    finally:
        elapsed = time.monotonic()-start
        if out['status'] == 'incomplete' and elapsed >= seconds*.98:
            out['status'] = 'measured'
        longest = max(longest, time.monotonic()-last_new)
        if startup is not None:
            final_deficit = max(0., time.monotonic()-last_tick-buffer)
            deficit += final_deficit
            if final_deficit > 0 and not was_empty: deficit_events += 1
        out['summary'] = summarize(events, elapsed, supplied, startup, longest, missed)
        out['summary'].update(simulated_buffer_deficit_seconds=deficit,
                              simulated_buffer_deficit_events=deficit_events,
                              media_host=urllib.parse.urlsplit(out.get('media_url',url)).netloc)
        s = out['summary']
        out['stable_sample'] = bool(out['status']=='measured' and elapsed >= seconds*.98 and
            s['unique_segments'] >= 3 and s['supply_ratio'] >= .95 and
            s['p95_download_media_ratio'] is not None and s['p95_download_media_ratio'] < .7 and
            s['failures']==0 and s['missed_sequences']==0 and deficit <= 1. and
            longest <= max(20., parsed.get('target',6)*3))
    return out



def decode_sample(channel, seconds, timeout, include_audio=False):
    """Supplementary paced decode, not supply evidence and never promotes an ENDLIST to live."""
    if not shutil.which('ffmpeg'): return dict(status='skipped_no_ffmpeg')
    url, headers = base.media_headers(channel)
    command = ['ffmpeg','-hide_banner','-nostdin','-loglevel','warning','-rw_timeout',
               str(int(timeout*1000000)), '-user_agent',base.OKHTTP_UA]
    if headers:
        command += ['-headers', ''.join('%s: %s\r\n'%(k,v) for k,v in headers.items())]
    command += ['-re','-i',url,'-t',str(seconds),'-map','0:v:0']
    command += ['-map','0:a:0?'] if include_audio else ['-an']
    command += ['-progress','pipe:1','-nostats','-f','null','-']
    start=time.monotonic()
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=seconds+max(20,timeout*3))
        progress=[dict(re.findall(r'(?m)^([^=\n]+)=(.*)$', x)) for x in result.stdout.split('progress=')[:-1]]
        return dict(status='decoded' if result.returncode==0 else 'failed',returncode=result.returncode,
                    elapsed=time.monotonic()-start, requested_media_seconds=seconds,
                    progress=progress, warnings=result.stderr[-16000:], warning_characters=len(result.stderr), warnings_truncated=len(result.stderr)>16000,
                    audio_requested=include_audio,
                    note='-re paced FFmpeg video/optional-audio decode; supplementary only, not first frame or measured phone stalls')
    except subprocess.TimeoutExpired as exc:
        def text(value): return value.decode('utf-8','replace') if isinstance(value,bytes) else value or ''
        stdout,stderr=text(exc.stdout),text(exc.stderr)
        progress=[dict(re.findall(r'(?m)^([^=\n]+)=(.*)$', x)) for x in stdout.split('progress=')[:-1]]
        return dict(status='timeout',elapsed=time.monotonic()-start, requested_media_seconds=seconds,
                    progress=progress,warnings=stderr[-16000:], warning_characters=len(stderr),warnings_truncated=len(stderr)>16000,audio_requested=include_audio)


def clean_decode_sample(decoded, seconds):
    progress = decoded.get('progress') or []
    last = progress[-1] if progress else {}
    return bool(decoded.get('status') == 'decoded' and not decoded.get('warnings') and
        float(last.get('out_time_us', '0')) / 1000000 >= seconds * .98 and
        int(last.get('frame', '0')) > 0 and int(last.get('dup_frames', '0')) == 0 and
        int(last.get('drop_frames', '0')) == 0)


@contextlib.contextmanager
def run_lock(path):
    # Advisory directory lock across this script's runs. Never auto-remove somebody else's lock.
    os.mkdir(path)
    try:
        Path(path, 'owner.json').write_text(json.dumps(dict(pid=os.getpid(), started=time.time(), owner='check_live_stability/continuous/curated', command=' '.join(__import__('sys').argv))))
        yield
    finally:
        Path(path, 'owner.json').unlink(); os.rmdir(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest', default='builtin.json')
    ap.add_argument('--network-label', default='user Mac; exit not independently verified; not phone', help='explicit observed network scope; never infer mainland from host/timezone')
    ap.add_argument('--source', action='append', help='exact source name; repeatable')
    ap.add_argument('--channel', action='append', default=[], help='channel regex; first match/line only')
    ap.add_argument('--seconds', type=float, default=180)
    ap.add_argument('--decode-seconds', type=float, default=20, help='supplementary paced decode per sample; default20s,0 means supply-only')
    ap.add_argument('--decode-audio', action='store_true', default=True, help='include available first audio track (default; presence is not asserted)')
    ap.add_argument('--video-only-decode', dest='decode_audio', action='store_false', help='omit audio from decode supplement')
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--workers', type=int, choices=(1,2), default=1, help='bounded low concurrency; default 1')
    ap.add_argument('--timeout', type=float, default=8)
    ap.add_argument('--json-out', required=True)
    ap.add_argument('--lock', default='.live-quality.lock')
    args = ap.parse_args()
    if args.seconds < 1 or args.rounds < 1 or args.decode_seconds < 0: ap.error('positive duration/rounds and nonnegative decode-seconds required')
    data = json.loads(Path(args.manifest).read_text())
    unknown = set(args.source or []) - {e['name'] for e in data['live']}
    if unknown: ap.error('unknown sources: '+', '.join(sorted(unknown)))
    output = dict(started=time.strftime('%Y-%m-%d %H:%M:%S %Z'), network=args.network_label,
                  seconds=args.seconds, rounds=args.rounds, concurrency=args.workers, decode_seconds=args.decode_seconds, decode_audio_requested=args.decode_audio, results=[], coverage=[])
    patterns = args.channel or None
    output['selection_policy'] = 'explicit channel regex' if patterns else 'cross-source CCTV18 + provincial31 + additional8; labels not identity; never CGTN/Phoenix substitute'
    ipv6, detail = base.ipv6_check(min(args.timeout, 3), quiet=True)
    output['ipv6'] = dict(ready=ipv6, detail=detail)
    def save():
        dest = Path(args.json_out); temp = dest.with_suffix(dest.suffix+'.tmp')
        temp.write_text(json.dumps(output, ensure_ascii=False, indent=2)); os.replace(temp,dest)
    def measure(job):
        entry, channel, round_no = job
        print('START',round_no,entry['name'],channel[0],channel[1],flush=True)
        result = audit(channel,args.seconds,args.timeout)
        result['qualification_scope'] = 'HLS supply plus supplementary decode' if args.decode_seconds else 'HLS supply only; no decode claim'
        if args.decode_seconds:
            _,decode_headers=base.media_headers(channel)
            decode_channel=(channel[0],result.get('media_url',channel[1]),decode_headers,'')
            result['decode'] = decode_sample(decode_channel,args.decode_seconds,args.timeout,args.decode_audio)
            result['decode']['media_url']=decode_channel[1]
            result['hls_supply_stable_sample'] = result.get('stable_sample', False)
            result['decode_clean_sample'] = clean_decode_sample(result['decode'], args.decode_seconds)
            result['stable_sample'] = result.get('stable_sample', False) and result['decode_clean_sample']
        result.update(source=entry['name'], source_url=entry['url'], round=round_no)
        return result
    with run_lock(args.lock):
        for round_no in range(args.rounds):
            jobs=[]
            for entry in data['live']:
                if args.source and entry['name'] not in args.source: continue
                response = base.fetch(entry['url'], args.timeout, use_range=False)
                if not response.ok:
                    output['results'].append(dict(source=entry['name'], round=round_no+1, error=response.error)); save(); continue
                channels = base.parse_playlist_channels(response.body.decode('utf-8-sig','replace'))
                picked, missing = select_representatives(channels, patterns, ipv6)
                output['coverage'].append(dict(source=entry['name'],round=round_no+1,selected=[c[0] for c in picked],missing_patterns=missing))
                if not picked:
                    output['results'].append(dict(source=entry['name'], round=round_no+1, error='no representative match/IPv6 skipped')); save()
                jobs.extend((entry, channel, round_no+1) for channel in picked)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for future in as_completed([pool.submit(measure,job) for job in jobs]):
                    result=future.result()
                    output['results'].append(result); save()
                    print('DONE',result['source'],result['channel'],result['status'],result['stable_sample'],json.dumps(result['summary'],ensure_ascii=False),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
