#!/usr/bin/env python3
"""Bounded FFmpeg continuous-stream decode evidence, not HLS freshness/phone stalls."""
import argparse,json,time
from pathlib import Path
import check_live_stability as quality

def measure(channel,seconds=180,timeout=8):
    decoded=quality.decode_sample(channel,seconds,timeout)
    final=decoded.get('progress',[{}])[-1] if decoded.get('progress') else {}
    media=float(final.get('out_time_us','0'))/1000000
    elapsed=decoded.get('elapsed',0)
    result=dict(channel=channel[0],url=quality.base.media_headers(channel)[0],decode=decoded,
        decoded_media_seconds=media,wall_seconds=elapsed,
        media_wall_ratio=media/elapsed if elapsed else 0,
        frames=int(final.get('frame','0')),dup_frames=int(final.get('dup_frames','0')),
        drop_frames=int(final.get('drop_frames','0')),
        note='FFmpeg -re paced VIDEO ONLY decode; no first-frame, no phone stalls, no proof of correct program or real-time freshness')
    result['continuous_decode_sample']=bool(decoded.get('status')=='decoded' and media>=seconds*.98 and
        media/max(elapsed,.001)>=.95 and result['frames']>0 and not decoded.get('warnings') and
        result['dup_frames']==0 and result['drop_frames']==0)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True);parser.add_argument('--name',default='continuous stream')
    parser.add_argument('--seconds',type=float,default=180);parser.add_argument('--rounds',type=int,default=2)
    parser.add_argument('--timeout',type=float,default=8);parser.add_argument('--json-out',required=True)
    parser.add_argument('--lock',default='.live-quality.lock')
    args=parser.parse_args()
    if args.seconds<1 or args.rounds<1:parser.error('positive duration/rounds required')
    out=dict(started=time.strftime('%Y-%m-%d %H:%M:%S %Z'),network='user Mac; not phone',results=[])
    with quality.run_lock(args.lock):
        for i in range(args.rounds):
            print('START',i+1,args.name,flush=True)
            result=measure((args.name,args.url),args.seconds,args.timeout);result['round']=i+1
            out['results'].append(result);p=Path(args.json_out);tmp=p.with_suffix(p.suffix+'.tmp')
            tmp.write_text(json.dumps(out,ensure_ascii=False,indent=2));tmp.replace(p)
            print('DONE',result['continuous_decode_sample'],result['frames'],result['media_wall_ratio'],flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
