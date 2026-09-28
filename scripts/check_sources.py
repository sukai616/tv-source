#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""builtin.json 自检脚本（只用 Python 标准库，无需装依赖）。

三层检查，一层比一层贵：

1. 静态检查（默认就跑）：JSON 能不能解析、字段和 URL 格式、地址有没有重复、
   backup / recommended 规则是否自洽。
2. 源级检查（--sources）：清单里的地址本身还活着吗（点播 JSON、直播 m3u/txt 抓得到吗）。
3. 频道级检查（--live）：把直播源拉下来，解析出频道，测线路/频道真的能不能播
   （HTTP 拿到像 m3u8 / TS / 视频流的内容才算过；加 --deep 还会跟进 m3u8 到分片再判）。

带 --live 时同时给 **两个口径**，因为多线路聚合源（同一个频道名挂 2~6 条备用线路）
用单条线路口径会严重低估可用性：

- **线路口径**：抽 sample 个（或 --full 时全部）播放地址逐个探。
- **频道口径**：抽 sample 个（或 --full 时全部）频道名，每个最多试 --max-lines 条线路，
  有一条能播就算这个频道可用。只有平均线路数 ≥ 1.2 的源才真跑这一轮。

默认达标：**线路口径达标 或 频道口径达标**。`--phone` 模式下推荐源要求**两个口径都**达标，
门槛默认提到 0.7，并默认开启 deep、把 --max-lines 降为 1（更贴近播放器先试第一条）。

带 --live 时会先做 **IPv6 体检**：无出口只剔除 IPv6 裸地址频道，不全盘跳过。

用法：

    python3 scripts/check_sources.py                          # 只做静态检查
    python3 scripts/check_sources.py --live --sample 20       # 快速抽测
    python3 scripts/check_sources.py --live --full --phone --json-out /tmp/live-full.json
    python3 scripts/check_sources.py --live --deep            # 跟进 m3u8 到分片再判
    python3 scripts/check_sources.py --live --force-ipv6

退出码：0 = 通过；1 = 有问题（JSON 坏了、推荐源未达标、一条都没通过等）。
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 播放地址里常带空格 / 中文 / 竖线，urllib 不认，统一先清洗 + 转义
INVALID_URL_CHARS = re.compile(r"[\s\t\n\r]")
SAFE_URL_CHARS = re.compile(r"[^A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]")


def clean_url(url):
    """清掉空白字符，并把非法字符转义成 %xx。"""
    if not isinstance(url, str):
        return ""
    url = INVALID_URL_CHARS.sub("", url).strip()
    return SAFE_URL_CHARS.sub(lambda m: "".join("%%%02X" % b for b in m.group(0).encode("utf-8")), url)


def display_width(text):
    """中文字符算 2 列，用来对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #

class FetchResult(object):
    def __init__(self, url, ok, status=None, ctype="", body=b"", error="", seconds=0.0):
        self.url = url
        self.ok = ok
        self.status = status
        self.ctype = ctype
        self.body = body
        self.error = error
        self.seconds = seconds


def fetch(url, timeout, limit=None):
    """GET 一个地址。limit 给定时只读前 limit 字节（探测用，省流量）。"""
    url = clean_url(url)
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        if limit:
            req.add_header("Range", "bytes=0-%d" % (limit - 1))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(limit) if limit else resp.read()
            return FetchResult(
                url, True, resp.status, resp.headers.get("Content-Type", ""), body,
                seconds=time.time() - started,
            )
    except urllib.error.HTTPError as exc:
        return FetchResult(url, False, exc.code, "", b"", "HTTP %s" % exc.code, time.time() - started)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return FetchResult(url, False, None, "", b"", "%s" % reason, time.time() - started)
    except Exception as exc:  # 超时 / 编码 / 连接重置
        return FetchResult(url, False, None, "", b"", "%s: %s" % (type(exc).__name__, exc), time.time() - started)


# --------------------------------------------------------------------------- #
# 静态检查
# --------------------------------------------------------------------------- #

REQUIRED_KEYS = ("vod", "live")


def check_manifest(raw, source_label):
    """静态检查。返回 (data, issues)；issues 是 [(级别, 说明), ...]。"""
    issues = []

    def add(level, msg):
        issues.append((level, msg))

    try:
        data = json.loads(raw)
    except ValueError as exc:
        add("ERROR", "JSON 解析失败：%s" % exc)
        return None, issues

    if not isinstance(data, dict):
        add("ERROR", "顶层不是对象")
        return None, issues

    version = data.get("version")
    if not isinstance(version, int):
        add("WARN", "version 建议写成整数（现在是 %r）" % (version,))

    entries = []
    for key in REQUIRED_KEYS:
        if key not in data:
            add("INFO", "没有 %s 段（可以只写一个）" % key)
            continue
        if not isinstance(data[key], list):
            add("ERROR", "%s 不是数组" % key)
            continue
        for i, item in enumerate(data[key]):
            where = "%s[%d]" % (key, i)
            if not isinstance(item, dict):
                add("ERROR", "%s 不是对象" % where)
                continue
            name = (item.get("name") or "").strip()
            url = item.get("url")
            if not url or not isinstance(url, str):
                add("ERROR", "%s 没有 url（App 会跳过这条）" % where)
                continue
            if INVALID_URL_CHARS.search(url):
                add("ERROR", "%s url 里有空白字符" % where)
            if not url.split("://")[0] in ("http", "https"):
                add("ERROR", "%s url 不是 http(s)：%s" % (where, url))
            if not name:
                add("WARN", "%s 没有 name（界面退回显示地址）" % where)
            if item.get("recommended") and item.get("backup"):
                add("WARN", "%s 既是推荐位又是同源备用（备用不该占推荐位）" % where)
            entries.append({"kind": key, "index": i, "name": name, "url": url, "item": item})

    # 重复地址（整份清单内，含 vod 与 live 之间）
    seen = {}
    for e in entries:
        key = e["url"].strip().lower()
        if key in seen:
            add("ERROR", "地址重复：%s 和 %s 都是 %s" % (seen[key], e["name"] or "?", e["url"]))
        else:
            seen[key] = e["name"] or "?"
    # 同一仓库同一路径必须同一份文件，不同协议（http/https）也算重复
    seen_noscheme = {}
    for e in entries:
        key = re.sub(r"^https?://", "", e["url"].strip().lower())
        if key in seen_noscheme and seen_noscheme[key] != e["url"].strip().lower():
            add("WARN", "疑似同址不同协议：%s / %s" % (seen_noscheme[key], e["url"]))
        else:
            seen_noscheme[key] = e["url"].strip().lower()

    # backup 规则
    names = set(e["name"] for e in entries if e["name"])
    by_kind = {}
    for e in entries:
        by_kind.setdefault(e["kind"], []).append(e)
    for kind, group in by_kind.items():
        positions = {}
        for pos, e in enumerate(group):
            positions.setdefault(e["name"], pos)
        for pos, e in enumerate(group):
            target = e["item"].get("backup")
            if target is None:
                continue
            if target not in names:
                add("ERROR", "%s 的 backup 指向不存在的条目「%s」" % (e["name"] or "?", target))
                continue
            if kind not in by_kind or target not in positions:
                add("WARN", "%s 的 backup 主条目「%s」不在同一段里" % (e["name"] or "?", target))
                continue
            # 主条目要在前面，且中间只能夹「同一个主条目的备用」，这样 App 里会挨着排
            head = positions[target]
            if head >= pos:
                add("WARN", "%s 的 backup 主条目「%s」排在它后面（App 排序会挨不到一起）" % (e["name"] or "?", target))
                continue
            sandwiched = [g["name"] for g in group[head + 1:pos] if g["item"].get("backup") != target]
            if sandwiched:
                add("WARN", "%s 的 backup 主条目「%s」没有紧挨在它上面（中间隔着 %s）" % (
                    e["name"] or "?", target, "、".join(n or "?" for n in sandwiched)))

    return data, issues


# --------------------------------------------------------------------------- #
# 直播源解析与频道探测
# --------------------------------------------------------------------------- #

def parse_playlist(text):
    """支持 m3u 和 TVBox 风格的 txt，返回 [(频道名, 地址), ...]。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    channels = []
    if text.lstrip()[:512].upper().find("#EXTM3U") >= 0:
        name = None
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.upper().startswith("#EXTINF"):
                # #EXTINF:-1 tvg-name="CCTV-1" group-title="央视",CCTV-1
                name = line.split(",")[-1].strip() or None
            elif line.startswith("#"):
                continue
            elif name is not None or line.lower().startswith("http"):
                channels.append((name or line, line))
                name = None
    else:
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue  # #genre# 之类的分组行直接跳过
            parts = line.split(",", 1)
            if len(parts) == 2 and parts[1].strip().lower().startswith("http"):
                channels.append((parts[0].strip(), parts[1].strip()))
            elif line.lower().startswith("http"):
                channels.append((line, line))
    return channels


# 平均线路数达到这个值就认为源是「多线路聚合」的，额外跑一轮频道口径。
# 用 1.2 而不是 1.0：留点余量，偶尔一两个频道重名不值得多花一轮请求。
MULTI_LINE_RATIO = 1.2

IPV6_URL_RE = re.compile(r"^https?://\[", re.I)


def is_ipv6_url(url):
    """是不是 IPv6 裸地址（http://[2409:8087::1]:80/...）。"""
    return bool(IPV6_URL_RE.match((url or "").strip()))


def sample_evenly(items, count):
    """从列表里均匀抽 count 个（顺序稳定，不用随机种子）。"""
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    idx = sorted(set(int(round(i * (len(items) - 1) / float(count - 1))) for i in range(count)))
    return [items[i] for i in idx]


# deep 模式：跟进 m3u8 时的单次 body 上限与最大跟进层数（主列表→子列表→分片）
DEEP_PLAYLIST_LIMIT = 8192
DEEP_SEGMENT_LIMIT = 2048
DEEP_MAX_FOLLOW = 2  # 最多再请求 2 次（不含最初那次）


def judge_body(body, ctype):
    """判断响应内容是否像「真的能播」。返回 (通过?, 说明)。"""
    head = body[:4096]
    low = head.lower()
    if not head:
        return False, "空响应"
    if b"<html" in low[:512] or b"<!doctype" in low[:256]:
        return False, "返回的是 HTML 页面"
    if b"#extm3u" in low:
        if b"#ext-x-stream-inf" in low:
            return True, "m3u8 主播放列表"
        if b"#extinf" in low or b"#ext-x-targetduration" in low:
            return True, "m3u8 媒体列表"
        return True, "m3u8"
    ctype = (ctype or "").lower()
    for key, label in (
        ("mpegurl", "HLS(m3u8)"),
        ("video/", "视频流"),
        ("audio/", "音频流"),
        ("mp2t", "TS 流"),
        ("octet-stream", "二进制流"),
        ("x-flv", "FLV 流"),
        ("mp4", "MP4 流"),
    ):
        if key in ctype:
            return True, label
    if head[0:1] == b"\x47":
        return True, "TS 流（同步字节 0x47）"
    if b"ftyp" in head[:32]:
        return True, "MP4 / fMP4 流"
    if head[:3] == b"FLV":
        return True, "FLV 流"
    # fMP4 分片常见以 moof/mdat 起头
    if head[:4] in (b"moof", b"mdat", b"styp"):
        return True, "fMP4 分片"
    return False, "内容无法识别（Content-Type=%s）" % (ctype or "无")


def looks_like_m3u8(body, ctype):
    """响应是不是 HLS 播放列表（要不要跟进分片）。"""
    head = (body or b"")[:4096].lower()
    if b"#extm3u" in head:
        return True
    ctype = (ctype or "").lower()
    return "mpegurl" in ctype or ctype.endswith("m3u8")


def extract_first_m3u8_ref(body, base_url):
    """从 m3u8 里取出第一条媒体分片或子播放列表 URL。

    返回 (abs_url, kind)；kind 为 'playlist' / 'segment'。失败返回 (None, 原因)。
    相对路径按 base_url 拼接。
    """
    text = (body or b"").decode("utf-8", "ignore")
    expect = None  # playlist | segment
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            upper = line.upper()
            if upper.startswith("#EXT-X-STREAM-INF"):
                expect = "playlist"
            elif upper.startswith("#EXTINF"):
                expect = "segment"
            elif upper.startswith("#EXT-X-BYTERANGE"):
                continue
            elif upper.startswith("#EXT-X-MAP:"):
                m = re.search(r'URI=(?:"([^"]+)"|([^,\s]+))', line, re.I)
                if m:
                    uri = m.group(1) or m.group(2)
                    return urllib.parse.urljoin(base_url, uri), "segment"
            continue
        uri = line.split()[0]
        abs_url = urllib.parse.urljoin(base_url, uri)
        if expect:
            kind = expect
        elif ".m3u8" in uri.lower() or uri.lower().endswith(".m3u"):
            kind = "playlist"
        else:
            kind = "segment"
        return abs_url, kind
    return None, "m3u8 无可用 URI"


def deep_follow_m3u8(url, body, ctype, timeout):
    """从已拿到的 m3u8 响应出发，跟进到媒体分片并用内容特征判定。

    主播放列表再跟一层到媒体列表，再到分片；分片 HTML/403/超时算挂。
    返回 (ok, why, seconds_spent)。
    """
    started = time.time()
    current_url = url
    current_body = body
    current_ctype = ctype
    for hop in range(DEEP_MAX_FOLLOW):
        if not looks_like_m3u8(current_body, current_ctype):
            ok, why = judge_body(current_body, current_ctype)
            return ok, (("分片: " + why) if ok else why), time.time() - started
        next_url, kind = extract_first_m3u8_ref(current_body, current_url)
        if not next_url:
            return False, kind or "m3u8 无可用 URI", time.time() - started
        limit = DEEP_PLAYLIST_LIMIT if kind == "playlist" else DEEP_SEGMENT_LIMIT
        result = fetch(next_url, timeout, limit=limit)
        if not result.ok:
            return False, "跟进失败(%s): %s" % (kind, result.error or "请求失败"), time.time() - started
        current_url = result.url or next_url
        current_body = result.body
        current_ctype = result.ctype
        # 已经是分片（非 m3u8）→ 用内容特征收尾
        if kind == "segment" or not looks_like_m3u8(current_body, current_ctype):
            ok, why = judge_body(current_body, current_ctype)
            label = ("分片: " + why) if ok else why
            return ok, label, time.time() - started
        # 子播放列表：继续循环再跟一层
    # 深度用尽仍停在播放列表上
    ok, why = judge_body(current_body, current_ctype)
    if ok and looks_like_m3u8(current_body, current_ctype):
        return False, "m3u8 跟进深度用尽仍未到分片", time.time() - started
    return ok, why, time.time() - started


def probe_channel(channel, timeout, deep=False):
    name, url = channel
    limit = DEEP_PLAYLIST_LIMIT if deep else 2048
    result = fetch(url, timeout, limit=limit)
    if not result.ok:
        return channel, False, result.error or "请求失败", result.seconds
    ok, why = judge_body(result.body, result.ctype)
    if not ok:
        return channel, False, why, result.seconds
    if deep and looks_like_m3u8(result.body, result.ctype):
        ok2, why2, extra = deep_follow_m3u8(
            result.url or url, result.body, result.ctype, timeout)
        return channel, ok2, why2, result.seconds + extra
    return channel, ok, why, result.seconds


def probe_channel_name(name, urls, timeout, max_lines, deep=False):
    """频道口径：这个频道名的前 max_lines 条线路里，有没有一条能播。

    命中就提前返回，不再往下试——多线路聚合源一个频道挂 2~6 条线路，
    逐条探完既慢又没意义（用户点开这个台，播放器也是按顺序重试）。
    --phone 下默认 max_lines=1，只认排序后的第一条。
    """
    for url in urls[:max_lines]:
        _, ok, _, _ = probe_channel((name, url), timeout, deep=deep)
        if ok:
            return True
    return False


def test_live_source(entry, sample_size, timeout, workers, ipv6_ready=True,
                     max_lines=4, deep=False, full=False):
    """抓直播源 → 解析频道 → 按线路口径和频道口径各测一轮。返回结果 dict。

    sample_size：抽测数量；full=True 时测全部可测线路/频道名（忽略 sample_size）。
    deep=True 时 m3u8 会跟进到分片再判。
    """
    name = entry["name"] or entry["url"]
    out = {
        "name": name,
        "url": entry["url"],
        "fetch_ok": False,
        "fetch_error": "",
        "total": 0,
        "sampled": 0,
        "passed": 0,
        "ipv6_channels": 0,
        "ipv6_total": 0,
        "ipv6_only": False,
        "note": "",
        "skipped": "",
        "channels": [],
        "seconds": 0.0,
        "named_total": 0,
        "lines_per_name": 0.0,
        "multi_line": False,
        "chan_sampled": 0,
        "chan_passed": 0,
        "chan_rate": 0.0,
        "chan_channels": [],
        "deep": bool(deep),
        "full": bool(full),
    }
    started = time.time()
    page = fetch(entry["url"], timeout)
    if not page.ok:
        out["fetch_error"] = page.error or "抓取失败"
        out["seconds"] = time.time() - started
        return out
    text = page.body.decode("utf-8", "ignore")
    channels = parse_playlist(text)
    out["fetch_ok"] = True
    out["total"] = len(channels)
    if not channels:
        out["fetch_error"] = "抓到了 %d 字节，但解析不出频道（格式不是 m3u / txt？）" % len(page.body)
        out["seconds"] = time.time() - started
        return out

    # 没有 IPv6 出口时只剔除 IPv6 裸地址的那些频道，剩下的（域名 / IPv4）照测：
    # 很多带“IPv6”名头的源里其实混着不少域名线路，整条跳过会把能播的也丢掉。
    v6_channels = [c for c in channels if is_ipv6_url(c[1])]
    testable = channels if ipv6_ready else [c for c in channels if not is_ipv6_url(c[1])]
    out["ipv6_total"] = len(v6_channels)
    out["ipv6_only"] = not testable
    if not testable:
        out["skipped"] = "整条源 %d/%d 个频道都是 IPv6 裸地址，本机没有 IPv6 出口" % (len(v6_channels), len(channels))
        out["seconds"] = time.time() - started
        return out
    if not ipv6_ready and v6_channels:
        out["note"] = "本机没有 IPv6：已剔除 %d 个 IPv6 裸地址频道，只测剩下 %d 个" % (
            len(v6_channels), len(testable))

    n_pick = len(testable) if full else sample_size
    picked = sample_evenly(testable, n_pick)
    out["ipv6_channels"] = sum(1 for (_, u) in picked if is_ipv6_url(u))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda ch: probe_channel(ch, timeout, deep=deep), picked))

    out["sampled"] = len(results)
    # 全量时只留失败样例（最多 40），避免 JSON 爆炸；抽测模式保留全部明细
    fail_cap = 40 if full else 10 ** 9
    fail_kept = 0
    for (channel, ok, why, seconds) in results:
        if ok:
            out["passed"] += 1
        keep = (not full) or (not ok and fail_kept < fail_cap)
        if keep:
            out["channels"].append(
                {"name": channel[0], "url": channel[1], "ok": ok, "reason": why, "seconds": round(seconds, 2)}
            )
            if not ok:
                fail_kept += 1

    # 频道口径：同一个频道名挂多条线路时，只要有 1 条能播就算这个频道可用。
    # 单线路源两个口径等价，直接沿用线路口径的结果，不再多发一轮请求。
    by_name = {}
    for (n, u) in testable:
        by_name.setdefault(n or u, []).append(u)
    out["named_total"] = len(by_name)
    out["lines_per_name"] = round(len(testable) / float(len(by_name)), 2) if by_name else 0.0
    out["multi_line"] = out["lines_per_name"] >= MULTI_LINE_RATIO
    if out["multi_line"]:
        n_chan = len(by_name) if full else sample_size
        chan_picked = sample_evenly(sorted(by_name), n_chan)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            chan_res = list(pool.map(
                lambda n: (n, probe_channel_name(n, by_name[n], timeout, max_lines, deep=deep)),
                chan_picked))
        out["chan_sampled"] = len(chan_res)
        chan_fail_cap = 40 if full else 10 ** 9
        chan_fail_kept = 0
        for (n, ok) in chan_res:
            if ok:
                out["chan_passed"] += 1
            elif chan_fail_kept < chan_fail_cap:
                out["chan_channels"].append({"name": n, "lines": len(by_name[n])})
                chan_fail_kept += 1
    else:
        out["chan_sampled"] = out["sampled"]
        out["chan_passed"] = out["passed"]
    out["chan_rate"] = out["chan_passed"] / float(out["chan_sampled"]) if out["chan_sampled"] else 0.0

    out["seconds"] = time.time() - started
    return out


def pad_visible(text):
    return pad(text, 26)


# --------------------------------------------------------------------------- #
# IPv6 体检
# --------------------------------------------------------------------------- #

# 双栈站点：解析出 AAAA 后真连一把 443，比“看本机有没有地址”可信
IPV6_PROBE_TARGETS = (("www.taobao.com", 443), ("www.qq.com", 443), ("www.aliyun.com", 443))


def normalize_ipv6(token):
    """把一个疑似 IPv6 的串规整化；不是 IPv6 就返回 None。"""
    token = (token or "").strip().strip("[](),;<>")
    if "%" in token:
        token = token.split("%", 1)[0]  # 去掉 %en0 这类 scope
    if ":" not in token:
        return None
    try:
        return socket.inet_ntop(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, token))
    except (OSError, ValueError, AttributeError):
        return None


def classify_ipv6(addr):
    """global / ula / link / loopback。"""
    low = addr.lower()
    if addr == "::1":
        return "loopback"
    if re.match(r"^fe[89ab][0-9a-f]:", low):
        return "link"
    first = low.split(":", 1)[0]
    if len(first) <= 2 and int(first or "0", 16) & 0xFE == 0xFC:
        return "ula"
    if low.startswith("2001:db8"):
        return "doc"
    return "global"


def scan_local_ipv6():
    """从系统命令输出里捞本机 IPv6 地址。返回 kind -> [地址, ...]。"""
    found = {}
    for cmd in (["ip", "-6", "addr"], ["ifconfig", "-a"], ["ipconfig", "/all"]):
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            continue
        if proc.returncode != 0:
            continue
        for token in re.split(r"\s+", proc.stdout.decode("utf-8", "ignore")):
            addr = normalize_ipv6(token)
            if not addr:
                continue
            found.setdefault(classify_ipv6(addr), [])
            if addr not in found[classify_ipv6(addr)]:
                found[classify_ipv6(addr)].append(addr)
        if found:
            break
    return found


def is_real_ipv6(addr):
    """IPv4 映射地址（::ffff:1.2.3.4）走的还是 IPv4，不算 IPv6 出口。"""
    low = addr.lower()
    return not (low.startswith("::ffff:") or low.startswith("::") and "." in low)


def probe_ipv6_egress(timeout):
    """真连一把双栈站点：解析 AAAA → TCP 连 443。返回 (通不通, 说明)。

    要滤掉 IPv4 映射地址：没配 IPv6 的机器上 getaddrinfo 可能把 A 记录以
    ::ffff:113.137.54.210 的形式返回，直接连等于在走 IPv4，会误判成“有 IPv6”。
    """
    detail = "没有可用的探测目标"
    flags = getattr(socket, "AI_ADDRCONFIG", 0)
    for host, port in IPV6_PROBE_TARGETS:
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM, 0, flags)
        except (socket.gaierror, OSError) as exc:
            detail = "%s 解析不出 AAAA（%s）" % (host, exc)
            continue
        for info in infos:
            addr = info[4][0]
            if not is_real_ipv6(addr):
                detail = "%s 只解析到 IPv4 映射地址（%s），本机没走 IPv6" % (host, addr)
                continue
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect((addr, port))
                return True, "连通 %s [%s]:%d" % (host, addr, port)
            except OSError as exc:
                detail = "%s [%s]:%d 连不上（%s）" % (host, addr, port, exc)
            finally:
                sock.close()
    return False, detail


def ipv6_check(timeout=5.0, quiet=False):
    """IPv6 体检：本机地址 + 真连一把。返回 (有出口?, 说明)。"""
    addrs = scan_local_ipv6()
    ok, detail = probe_ipv6_egress(timeout)
    globals_ = addrs.get("global", [])
    if ok:
        note = "有 IPv6 出口（%s；本机地址 %s）" % (detail, globals_[0] if globals_ else "未识别")
    elif globals_:
        note = "本机有 IPv6 地址但连不出去（%s），IPv6 源测不准" % detail
    elif addrs.get("ula"):
        note = "只有内网 ULA 地址（fd00::/8），连不到运营商 IPTV，IPv6 源测不准（%s）" % detail
    else:
        note = "本机没有 IPv6（只有链路本地 fe80::），IPv6 裸地址频道将剔除（%s）" % detail
    if not quiet:
        print("  IPv6 体检：%s %s" % ("✓" if ok else "⊘", note))
    return ok, note


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def load_manifest_file(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def load_manifest_git(rev, path):
    spec = "%s:%s" % (rev, path)
    try:
        proc = subprocess.run(["git", "show", spec], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return None, "跑不动 git：%s" % exc
    if proc.returncode != 0:
        return None, "git show %s 失败：%s" % (spec, proc.stderr.decode("utf-8", "ignore").strip())
    return proc.stdout.decode("utf-8"), None


def report_static(label, issues, data):
    errors = [m for (lv, m) in issues if lv == "ERROR"]
    warns = [m for (lv, m) in issues if lv == "WARN"]
    infos = [m for (lv, m) in issues if lv == "INFO"]
    if not issues:
        print("  静态检查：通过（%d 条点播 / %d 条直播）" % (
            len(data.get("vod") or []), len(data.get("live") or [])))
    else:
        line = "  静态检查：%d 个错误、%d 个提醒" % (len(errors), len(warns))
        print(line)
        for msg in errors:
            print("    [错误] %s" % msg)
        for msg in warns:
            print("    [提醒] %s" % msg)
        for msg in infos:
            print("    [信息] %s" % msg)
    return not errors


def source_passes(out, min_pass, min_pass_channel, require_both=False):
    """一条源是否达标。require_both=True 时线路与频道口径都要过（--phone）。"""
    if out.get("skipped") or not out.get("fetch_ok"):
        return False
    rate = out["passed"] / float(out["sampled"]) if out["sampled"] else 0.0
    chan_rate = out.get("chan_rate", 0.0)
    if require_both:
        return (rate >= min_pass) and (chan_rate >= min_pass_channel)
    return (rate >= min_pass) or (chan_rate >= min_pass_channel)


def report_live_one(out, min_pass, min_pass_channel=None, require_both=False,
                    index=None, total_sources=None):
    """打印单个直播源结果，返回是否达标。"""
    if min_pass_channel is None:
        min_pass_channel = min_pass
    progress = ""
    if index is not None and total_sources:
        progress = " [%d/%d]" % (index, total_sources)
    if out.get("skipped"):
        print("  ⊘%s %s → 跳过：%s" % (progress, pad_visible(out["name"]), out["skipped"]))
        return False
    if not out["fetch_ok"]:
        print("  ✗%s %s → %s" % (progress, pad_visible(out["name"]), out["fetch_error"]))
        return False
    rate = out["passed"] / float(out["sampled"]) if out["sampled"] else 0.0
    chan_rate = out.get("chan_rate", 0.0)
    ok = source_passes(out, min_pass, min_pass_channel, require_both=require_both)
    mode = "全量" if out.get("full") else "抽测"
    head = "  %s%s %s" % ("✓" if ok else "✗", progress, pad_visible(out["name"]))
    print(
        "%s 频道 %4d · %s %4d · 可播 %4d · 通过率 %3d%% · %5.1fs%s" % (
            head, out["total"], mode, out["sampled"], out["passed"],
            int(round(rate * 100)), out["seconds"],
            " · deep" if out.get("deep") else "",
        )
    )
    if out.get("multi_line"):
        print("        多线路源：%d 个频道名平均 %.1f 条线路；频道口径%s %d 个，可用 %d（%d%%）"
              % (out["named_total"], out["lines_per_name"], mode,
                 out["chan_sampled"], out["chan_passed"], int(round(chan_rate * 100))))
        for ch in out.get("chan_channels", [])[:20]:
            print("        · %s：%d 条线路全挂" % (ch["name"][:28], ch["lines"]))
    if out.get("note"):
        print("        （%s）" % out["note"])
    # 多线路源已经按频道给过结论了，再逐条列线路失败只是噪音——除非它没过
    if (not out.get("multi_line")) or (not ok):
        shown = 0
        for ch in out["channels"]:
            if not ch["ok"]:
                print("        · %s：%s" % (ch["name"][:28], ch["reason"]))
                shown += 1
                if shown >= 20:
                    break
    if not out.get("note") and not ok and out["ipv6_channels"] * 2 >= max(1, out["sampled"]):
        print("        （测到的频道里 %d/%d 是 IPv6 地址，本机没有 IPv6 出口的话这条源测不准）"
              % (out["ipv6_channels"], out["sampled"]))
    return ok


def report_live(label, results, min_pass, min_pass_channel=None, require_both=False):
    """打印每个直播源的结果，返回通过的那些。

    默认达标：线路口径 ≥ min_pass **或** 频道口径 ≥ min_pass_channel。
    require_both=True（--phone）时两个口径都要过。
    """
    if min_pass_channel is None:
        min_pass_channel = min_pass
    passed_sources = []
    n = len(results)
    for i, out in enumerate(results, 1):
        if report_live_one(out, min_pass, min_pass_channel, require_both=require_both,
                           index=i, total_sources=n):
            passed_sources.append(out)
    return passed_sources


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="builtin.json 自检：JSON 合法性、重复地址、频道能不能真播")
    parser.add_argument("-f", "--file", action="append", default=[],
                        help="清单文件（默认 builtin.json，可重复传）")
    parser.add_argument("--git", action="append", default=[],
                        help="额外读取某个提交里的清单做同样测试，例如 --git f2f4b5a")
    parser.add_argument("--git-path", default="builtin.json", help="配合 --git 的清单路径")
    parser.add_argument("--live", action="store_true", help="抓直播源并测频道可播性（自动带 IPv6 体检）")
    parser.add_argument("--ipv6", action="store_true", help="只做 IPv6 体检：本机到底有没有 IPv6 出口")
    parser.add_argument("--force-ipv6", action="store_true", help="本机没有 IPv6 也照测 IPv6 源（不再跳过）")
    parser.add_argument("--sources", action="store_true", help="只测清单地址本身是否可达（不抽测频道）")
    parser.add_argument("--vod", action="store_true", help="顺带测点播地址是否可达")
    parser.add_argument("--sample", type=int, default=10,
                        help="每个直播源抽测多少（线路 / 频道名各抽这么多），默认 10；与 --full 并存时 --full 优先")
    parser.add_argument("--full", action="store_true",
                        help="全量：每个源测全部可测线路（及频道口径下全部频道名），不再 sample")
    parser.add_argument("--deep", action="store_true",
                        help="更深判定：若响应是 m3u8，跟进到媒体分片再用内容特征判定")
    parser.add_argument("--phone", "--strict", dest="phone", action="store_true",
                        help="贴近手机门槛：开启 deep；min-pass/min-pass-channel=0.7；"
                             "推荐源要求双口径都过；默认 max-lines=1")
    parser.add_argument("--channel-first-line", action="store_true",
                        help="频道口径只试排序后的第一条线路（等价于 --max-lines 1）")
    parser.add_argument("--workers", type=int, default=None,
                        help="并发数（默认 16；--full 时默认 24）")
    parser.add_argument("--timeout", type=float, default=8.0, help="单请求超时秒数，默认 8")
    parser.add_argument("--min-pass", type=float, default=None,
                        help="线路口径最低可播比例（默认 0.5；--phone 下 0.7）")
    parser.add_argument("--min-pass-channel", type=float, default=None,
                        help="频道口径最低可用比例，默认取 --min-pass")
    parser.add_argument("--max-lines", type=int, default=None,
                        help="频道口径下每个频道最多试几条线路（默认 4；--phone 下 1）")
    parser.add_argument("--json-out", default="", help="把完整结果写成 JSON（可中断后仍尽量落盘）")
    args = parser.parse_args(argv)

    phone = bool(args.phone)
    if phone or args.full:
        args.deep = True
    if args.channel_first_line:
        args.max_lines = 1
    # 预设默认值（用户显式传入的保留）
    if args.min_pass is None:
        args.min_pass = 0.7 if phone else 0.5
    if args.min_pass_channel is None:
        args.min_pass_channel = args.min_pass
    if args.max_lines is None:
        args.max_lines = 1 if phone else 4
    if args.workers is None:
        args.workers = 24 if args.full else 16
    require_both = phone  # --phone：推荐源/达标判定用 AND

    ipv6_ready = True
    if args.ipv6:
        print("IPv6 体检")
        ipv6_check(args.timeout)
        return 0

    files = args.file or ["builtin.json"]

    manifests = []
    for path in files:
        if not os.path.exists(path):
            print("找不到清单文件：%s" % path)
            return 1
        manifests.append((path, load_manifest_file(path), None))
    for rev in args.git:
        raw, err = load_manifest_git(rev, args.git_path)
        manifests.append(("git:%s" % rev, raw or "", err))

    exit_code = 0
    all_results = {}
    interrupted = False
    for (label, raw, err) in manifests:
        print("=" * 78)
        print("清单：%s" % label)
        if err:
            print("  读取失败：%s" % err)
            exit_code = 1
            continue
        data, issues = check_manifest(raw, label)
        static_ok = report_static(label, issues, data or {})
        if not static_ok or data is None:
            exit_code = 1
            continue

        live = [e for e in (data.get("live") or []) if isinstance(e, dict) and e.get("url")]

        if args.vod:
            print("  点播地址可达性：")
            for item in (data.get("vod") or []):
                url = clean_url(item.get("url") or "")
                res = fetch(url, args.timeout, limit=2048)
                print("    %s %s → %s" % ("✓" if res.ok else "✗",
                                          pad((item.get("name") or url)[:24], 26),
                                          "HTTP %s %.1fs" % (res.status, res.seconds) if res.ok else res.error))

        results = []
        if args.live:
            ipv6_ready, _ = ipv6_check(min(args.timeout, 5.0))
            if args.force_ipv6 and not ipv6_ready:
                print("  （--force-ipv6：IPv6 源不再跳过，测出来的失败是本机网络限制，不算源的锅）")
                ipv6_ready = True
            rule = "且" if require_both else "或"
            scope = "全量" if args.full else ("每源线路/频道各抽 %d 个" % args.sample)
            print("  直播频道测试（%s；workers=%d%s；线路 ≥ %d%% %s 频道 ≥ %d%% 算通过）：" % (
                scope, args.workers,
                "；deep 跟分片" if args.deep else "",
                int(args.min_pass * 100), rule, int(args.min_pass_channel * 100)))
            if phone:
                print("  （--phone：deep + 双口径都过 + max-lines=%d + 门槛 0.7）" % args.max_lines)
            # 逐个源串行；源内并发。每源完成即打印进度，Ctrl+C 可中断。
            n_live = len(live)
            passed = []
            try:
                for i, e in enumerate(live, 1):
                    out = test_live_source(
                        e, args.sample, args.timeout, args.workers,
                        ipv6_ready=ipv6_ready, max_lines=args.max_lines,
                        deep=args.deep, full=args.full,
                    )
                    results.append(out)
                    if report_live_one(out, args.min_pass, args.min_pass_channel,
                                       require_both=require_both,
                                       index=i, total_sources=n_live):
                        passed.append(out)
            except KeyboardInterrupt:
                interrupted = True
                print("\n  中断：已完成 %d/%d 个源，正在落盘…" % (len(results), n_live))
                exit_code = 1
            skipped = [o for o in results if o.get("skipped")]
            print("  通过的直播源：%s" % ("、".join(o["name"] for o in passed) or "无"))
            if skipped:
                print("  跳过的直播源（本机测不了，不代表源有问题）：%s"
                      % "、".join(o["name"] for o in skipped))
            if not interrupted:
                recommended = set(e.get("name") for e in live if e.get("recommended"))
                passed_names = set(p["name"] for p in passed)
                broken = [o["name"] for o in results
                          if o["name"] in recommended and o["name"] not in passed_names]
                # 推荐源未测到（仍在队列）不算 broken
                if not passed and skipped and len(skipped) == len(results):
                    print("  ⊘ 没测到通过的源（全都跳过了）")
                    exit_code = 1
                elif not passed:
                    print("  ✗ 一条都没通过")
                    exit_code = 1
                elif broken:
                    print("  ✗ 推荐源未达标：%s" % "、".join(broken))
                    exit_code = 1
        elif args.sources:
            print("  直播源可达性：")
            for item in live:
                res = fetch(clean_url(item["url"]), args.timeout, limit=2048)
                print("    %s %s → %s" % ("✓" if res.ok else "✗", pad((item.get("name") or "")[:24], 26),
                                          "HTTP %s %.1fs" % (res.status, res.seconds) if res.ok else res.error))
        all_results[label] = {
            "static_issues": issues,
            "live": results,
            "options": {
                "full": bool(args.full),
                "deep": bool(args.deep),
                "phone": phone,
                "sample": args.sample,
                "min_pass": args.min_pass,
                "min_pass_channel": args.min_pass_channel,
                "max_lines": args.max_lines,
                "workers": args.workers,
                "require_both": require_both,
            },
        }
        if interrupted:
            break

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(all_results, fh, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % args.json_out)

    print("=" * 78)
    if interrupted:
        print("结论：已中断（部分结果见上 / json-out）")
    else:
        print("结论：%s" % ("全部通过" if exit_code == 0 else "存在问题，见上面 [错误] / ✗"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
