#!/usr/bin/env python3
"""Offline public-CCTV inventory from explicitly selected saved playlist directories."""
import argparse,json,time
from pathlib import Path
from urllib.parse import urlsplit
import check_sources as base
from cctv_policy import BASE_CHANNELS,EXTRA_CHANNELS,canonical_station
from check_live_stability import run_lock

def build_inventory(snapshots):
    items={}
    for snapshot,subscription_url,text in snapshots:
        for ch in base.parse_playlist_channels(text):
            station=canonical_station(ch[0])
            if not station: continue
            url,headers=base.media_headers(ch)
            key=(station,url,tuple(sorted(headers.items())))
            if key not in items:
                items[key]=dict(station=station,url=url,headers=headers,names=[],sources=[],host=urlsplit(url).netloc,bare_ipv6=base.is_ipv6_url(url),http=url.startswith(('http://','https://')))
            value=items[key]
            if ch[0] not in value['names']:value['names'].append(ch[0])
            source=dict(snapshot=str(snapshot),subscription_url=subscription_url)
            if source not in value['sources']:value['sources'].append(source)
    return list(items.values())

def summarize(rows):
    return [dict(station=station,unique_url_headers=len(channels),http_ipv4_domain=sum(x['http'] and not x['bare_ipv6'] for x in channels),bare_ipv6=sum(x['bare_ipv6'] for x in channels),hosts=len({x['host'] for x in channels})) for station in BASE_CHANNELS+EXTRA_CHANNELS for channels in [[x for x in rows if x['station']==station]]]

def source_mapping(paths):
    mapping={}
    for path in paths:
        data=json.loads(Path(path).read_text())
        if isinstance(data,dict) and 'live' not in data:
            mapping.update(data);continue
        for item in data if isinstance(data,list) else data['live']:
            key=item.get('snapshot') or item['name']+'.playlist';mapping[key]=item['url']
    return mapping

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--snapshot-dir',action='append',required=True,help='public snapshots only; repeatable; no recursive hidden scan')
    ap.add_argument('--source-map',action='append',default=['builtin.json'],help='manifest/list or snapshot-path:subscription-URL JSON')
    ap.add_argument('--out-dir',required=True);ap.add_argument('--lock',default='.cctv-inventory.lock');args=ap.parse_args()
    directories=[Path(x) for x in args.snapshot_dir]
    if not all(x.is_dir() for x in directories):ap.error('snapshot directory missing')
    files=sorted({p for d in directories for p in d.glob('*.playlist')});mapping=source_mapping(args.source_map)
    if not files:ap.error('no public playlist snapshots')
    with run_lock(args.lock):
        rows=build_inventory((str(p),mapping.get(str(p),mapping.get(p.name,'')),p.read_text(errors='replace')) for p in files)
        out=Path(args.out_dir);out.mkdir(parents=True,exist_ok=True)
        for name,value in [('inventory-all.json',rows),('inventory-summary.json',summarize(rows)),('inventory-run.json',dict(created_beijing=time.strftime('%Y-%m-%d %H:%M:%S %Z'),snapshot_count=len(files),candidate_pairs=len(rows),distinct_url_headers=len({(v['url'],tuple(sorted(v['headers'].items()))) for v in rows}),unknown_source_urls=sum(not s['subscription_url'] for v in rows for s in v['sources']),offline_only=True))]:
            dest=out/name;temp=dest.with_suffix('.json.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(dest)
        print('OFFLINE SNAPSHOTS',len(files),'CCTV candidate station/URL/header pairs',len(rows),'NOT network-tested or identity-certified')
if __name__=='__main__':main()
