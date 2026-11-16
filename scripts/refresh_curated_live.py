#!/usr/bin/env python3
"""Render manually reviewed public station allowlists; NEVER import upstream lists wholesale."""
import argparse,json,re
from pathlib import Path
from urllib.parse import urlsplit
from check_live_stability import run_lock
from cctv_policy import BASE_CHANNELS, canonical_station
from live_composite_policy import validate_target_coverage

def render(data):
    outputs={};all_urls=set()
    for group in data['lists']:
        validate_target_coverage(group)
        filename=group['file']
        if not re.fullmatch(r'[a-z0-9-]+[.]m3u',filename):raise ValueError('unsafe output name')
        if filename in outputs:raise ValueError('duplicate filename')
        if any(x in group['title'] for x in '\r\n\"'):raise ValueError('invalid group title')
        lines=['#EXTM3U','# Manually reviewed public stations; see live/README.md and selected.json.']
        if not group['channels']:raise ValueError('empty list')
        claimed=set(group.get('cctv_baseline_qualified',[]))
        actual={canonical_station(ch['name']) for ch in group['channels']}
        if not claimed.issubset(set(BASE_CHANNELS)) or not claimed.issubset(actual):
            raise ValueError('CCTV completeness claim not supported by channel entries')
        relative=set(group.get('cctv_baseline_relative_only',[]))
        if relative & claimed or not relative.issubset(set(BASE_CHANNELS)) or not relative.issubset(actual):
            raise ValueError('relative CCTV coverage must be actual, baseline and disjoint from qualified')
        for ch in group['channels']:
            if canonical_station(ch['name']) in relative:
                evidence=ch.get('evidence',{})
                if ch.get('relative_only') is not True or not ch.get('group_title') or ch['group_title']==group['title'] or evidence.get('identity_verified') is not True or evidence.get('sustained_seconds',0)<180 or evidence.get('hls_supply_qualified') is not True or evidence.get('sustained_qualified') is True:
                    raise ValueError('relative CCTV requires identity/supply evidence and no strict qualification claim')
            if canonical_station(ch['name']) in claimed:
                evidence=ch.get('evidence',{})
                if not isinstance(evidence,dict) or evidence.get('identity_verified') is not True or evidence.get('sustained_seconds',0)<180 or evidence.get('sustained_qualified') is not True:
                    raise ValueError('CCTV qualification requires visible identity and qualified sustained evidence')

        for ch in group['channels']:
            if ch.get('reviewed_public_station') is not True:raise ValueError('explicit public station review required')
            url=ch['url'];parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or any(x in url for x in '\r\n '):raise ValueError('invalid public URL')
            if url in all_urls:raise ValueError('duplicate full media URL')
            all_urls.add(url)
            if not ch.get('provenance') or not ch.get('evidence'):raise ValueError('provenance and measured coverage required')
            name=ch['name']
            if any(x in name for x in '\r\n,'):raise ValueError('invalid channel name')
            channel_group=ch.get('group_title',group['title'])
            if not isinstance(channel_group,str) or any(x in channel_group for x in '\r\n"'):raise ValueError('invalid channel group title')
            lines.append('#EXTINF:-1 group-title="%s",%s'%(channel_group,name))
            headers=ch.get('headers',{})
            if set(headers)-{'User-Agent','Referer','Origin'}:raise ValueError('no credentials/cookies in public playlist')
            if any('\r' in v or '\n' in v for v in headers.values()):raise ValueError('invalid header')
            if headers:lines.append('#EXTHTTP:'+json.dumps(headers,ensure_ascii=False,separators=(',',':')))
            lines.append(url)
        outputs[filename]='\n'.join(lines)+'\n'
    return outputs

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--check',action='store_true');args=p.parse_args()
    root=Path(__file__).resolve().parent.parent
    with run_lock(root/'.curated-live.lock'):
        outputs=render(json.loads((root/'live/selected.json').read_text()))
        for name,text in outputs.items():
            dest=root/'live'/name
            if args.check:
                if not dest.exists() or dest.read_text()!=text:raise SystemExit('out of date: '+str(dest))
            else:
                temp=dest.with_suffix('.tmp');temp.write_text(text);temp.replace(dest)
        print('curated lists checked' if args.check else 'curated lists rendered',len(outputs))
if __name__=='__main__':main()
