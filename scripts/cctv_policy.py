"""Public live baseline policy; labels are candidates, never identity verification."""
import re
BASE_CHANNELS = ['CCTV-'+str(i) for i in range(1,18)] + ['CCTV-5+']
OFFICIAL_DIRECTORY = 'https://tv.cctv.com/cctvnav/m/'
EXTRA_CHANNELS = ['CCTV-4-Europe','CCTV-4-America','UHD-4K','UHD-8K']

def canonical_station(name):
    text = re.sub(r'[-_ \t]','',str(name)).upper().replace(chr(0xff0b),'+')
    if re.search(r'CCTV4K(?![0-9])',text): return 'UHD-4K'
    if re.search(r'CCTV8K(?![0-9])',text): return 'UHD-8K'
    match = re.search(r'CCTV(1[0-7]|[1-9])(?![0-9])(PLUS|[+]|P)?',text)
    if not match: return None
    number=int(match[1])
    if number==5 and (match[2] or '\u4f53\u80b2\u8d5b\u4e8b' in text): return 'CCTV-5+'
    if number==4:
        if 'EUROPE' in text or '\u6b27\u6d32' in text: return 'CCTV-4-Europe'
        if 'AMERICA' in text or '\u7f8e\u6d32' in text: return 'CCTV-4-America'
    return 'CCTV-'+str(number)

def select_representatives(channels, patterns=None, ipv6_ready=False):
    import check_sources as base
    targets=patterns or BASE_CHANNELS
    picked=[];seen=set();missing=[]
    for target in targets:
        found=False
        for ch in channels:
            matched=bool(re.search(target,ch[0],re.I)) if patterns else canonical_station(ch[0])==target
            if not matched: continue
            url,headers=base.media_headers(ch)
            key=(url,tuple(sorted(headers.items())))
            if key in seen or base.is_ipv6_url(url) and not ipv6_ready: continue
            seen.add(key);picked.append(ch);found=True;break
        if not found:missing.append(target)
    return picked,missing
