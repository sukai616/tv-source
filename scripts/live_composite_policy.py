"""Cross-source public TV composition. Labels are inventory, NOT identity evidence."""
import json,re
from pathlib import Path
from cctv_policy import canonical_station as canonical_cctv
POLICY=json.loads((Path(__file__).resolve().parent.parent/'live/channel-targets.json').read_text())
CCTV=POLICY['cctv'];PROVINCIAL=POLICY['provincial'];ADDITIONAL=POLICY['additional'];TARGETS=CCTV+PROVINCIAL+ADDITIONAL

def canonical_station(name):
    cc=canonical_cctv(name)
    if cc:return cc if cc in CCTV else None
    name=re.sub(r'\([^)]*\)|\[[^]]*\]|#[^#]*','',str(name)).strip()
    name=re.sub(r'(?:FHD|HEVC|H264|H265|HD|SD|4K|8K|1080P|720P|576P|480P)','',name,flags=re.I)
    name=re.sub(r'[^\w\u4e00-\u9fff]','',name)
    name=POLICY['aliases'].get(name,name)
    for station in PROVINCIAL+ADDITIONAL:
        if name==station or name.startswith(station) and re.fullmatch(r'[0-9_]*',name[len(station):]):return station
    return None

def select_representatives(channels,patterns=None,ipv6_ready=False):
    import check_sources as base
    picked=[];seen=set();missing=[]
    for target in patterns or TARGETS:
        found=False
        for ch in channels:
            matched=bool(re.search(target,ch[0],re.I)) if patterns else canonical_station(ch[0])==target
            if not matched:continue
            url,headers=base.media_headers(ch);key=(url,tuple(sorted(headers.items())))
            if key in seen or base.is_ipv6_url(url) and not ipv6_ready:continue
            seen.add(key);picked.append(ch);found=True;break
        if not found:missing.append(target)
    return picked,missing


def validate_target_coverage(group):
    """Fail closed on unsupported count/quality claims, not on honest incompleteness."""
    coverage=group.get('target_coverage')
    if coverage is None:return
    actual={canonical_station(ch['name']) for ch in group['channels']}-{None}
    sets={}
    for level in ['strict','relative','basic_only']:
        values=coverage.get(level,[])
        if len(values)!=len(set(values)) or not set(values).issubset(actual):raise ValueError('coverage must be unique actual target stations')
        sets[level]=set(values)
    if sets['strict']&sets['relative'] or sets['strict']&sets['basic_only'] or sets['relative']&sets['basic_only']:raise ValueError('coverage tiers must be disjoint')
    for level,stations in sets.items():
        for station in stations:
            channels=[ch for ch in group['channels'] if canonical_station(ch['name'])==station]
            def supported(ch):
                e=ch.get('evidence',{})
                if e.get('identity_verified') is not True:return False
                if level=='strict':return e.get('sustained_seconds',0)>=180 and e.get('sustained_qualified') is True
                if level=='relative':return ch.get('relative_only') is True and e.get('sustained_seconds',0)>=180 and e.get('playable_sustained_sample') is True and e.get('sustained_qualified') is not True
                return ch.get('relative_only') is True and e.get('basic_qualified') is True and e.get('long_test_pending') is True and e.get('sustained_qualified') is not True
            if not any(supported(ch) for ch in channels):raise ValueError('target tier lacks visible identity and matching scoped evidence')
