#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""builtin.json 自检脚本（只用 Python 标准库，无需装依赖）。

三层检查，一层比一层贵：

1. 静态检查（默认就跑）：JSON 能不能解析、字段和 URL 格式、地址有没有重复、
   backup / recommended 规则是否自洽。
2. 源级检查（--sources）：清单里的地址本身还活着吗（点播 JSON、直播 m3u/txt 抓得到吗）。
3. 频道级检查（--live）：把直播源拉下来，解析出频道，**抽测若干频道真的能不能播**
   （HTTP 拿到 m3u8 播放列表 / TS 切片 / 视频流才算过，HTML 错误页、超时算挂）。

带 --live 时会先做一次 **IPv6 体检**（本机地址 + 真连一把双栈站点）：没有 IPv6 出口的话，
整条都是 IPv6 裸地址的源直接跳过，免得每次都被一条“10% 可播”的假阴性干扰结论。

用法：

    python3 scripts/check_sources.py                          # 只做静态检查
    python3 scripts/check_sources.py --ipv6                   # 只做 IPv6 体检
    python3 scripts/check_sources.py --live                   # 静态 + 源级 + 频道抽测（自动带 IPv6 体检）
    python3 scripts/check_sources.py --live --sources         # 只测哪些源还活着，不抽测频道
    python3 scripts/check_sources.py --live --git f2f4b5a     # 顺带把某个提交里的旧清单也测一遍
    python3 scripts/check_sources.py --live --sample 20       # 每个源抽 20 个频道
    python3 scripts/check_sources.py --live --force-ipv6      # 没 IPv6 也照测 IPv6 源
    python3 scripts/check_sources.py --live --json-out /tmp/report.json

退出码：0 = 通过；1 = 有问题。判定失败的情形：JSON 坏了、地址重复、源抓不到、
一条直播源都没通过、或者被标了 recommended 的源没达标（可播率 < --min-pass，默认 0.5）。
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
        return True, "MP4 流"
    if head[:3] == b"FLV":
        return True, "FLV 流"
    return False, "内容无法识别（Content-Type=%s）" % (ctype or "无")


def probe_channel(channel, timeout):
    name, url = channel
    result = fetch(url, timeout, limit=2048)
    if not result.ok:
        return channel, False, result.error or "请求失败", result.seconds
    ok, why = judge_body(result.body, result.ctype)
    return channel, ok, why, result.seconds


def test_live_source(entry, sample_size, timeout, workers, ipv6_ready=True):
    """抓直播源 → 解析频道 → 抽测。返回结果 dict。

    entry 里基本是 IPv6 裸地址、而本机又没有 IPv6 出口时，直接标 skipped 不白跑
    （测出来只会是“连不上”，对结论没有信息量）。
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
        "skipped": "",
        "channels": [],
        "seconds": 0.0,
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

    out["ipv6_total"] = sum(1 for (_, u) in channels if re.match(r"^https?://\[", u.strip()))
    out["ipv6_only"] = out["ipv6_total"] * 2 >= len(channels)
    if out["ipv6_only"] and not ipv6_ready:
        out["skipped"] = "本机没有 IPv6 出口（%d/%d 个频道是 IPv6 裸地址）" % (out["ipv6_total"], len(channels))
        out["seconds"] = time.time() - started
        return out

    picked = sample_evenly(channels, sample_size)
    out["ipv6_channels"] = sum(1 for (_, u) in picked if re.match(r"^https?://\[", u.strip()))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda ch: probe_channel(ch, timeout), picked))

    out["sampled"] = len(results)
    for (channel, ok, why, seconds) in results:
        if ok:
            out["passed"] += 1
        out["channels"].append(
            {"name": channel[0], "url": channel[1], "ok": ok, "reason": why, "seconds": round(seconds, 2)}
        )
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
        note = "本机没有 IPv6（只有链路本地 fe80::），IPv6 源将跳过（%s）" % detail
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


def report_live(label, results, min_pass):
    """打印每个直播源的结果，返回通过的那些。"""
    passed_sources = []
    for out in results:
        if out.get("skipped"):
            print("  ⊘ %s → 跳过：%s" % (pad_visible(out["name"]), out["skipped"]))
            continue
        if not out["fetch_ok"]:
            print("  ✗ %s → %s" % (pad_visible(out["name"]), out["fetch_error"]))
            continue
        rate = out["passed"] / float(out["sampled"]) if out["sampled"] else 0.0
        ok = rate >= min_pass
        if ok:
            passed_sources.append(out)
        head = "  %s %s" % ("✓" if ok else "✗", pad_visible(out["name"]))
        print(
            "%s 频道 %4d · 抽测 %2d · 可播 %2d · 通过率 %3d%% · %4.1fs" % (
                head, out["total"], out["sampled"], out["passed"], int(round(rate * 100)), out["seconds"]
            )
        )
        for ch in out["channels"]:
            if not ch["ok"]:
                print("        · %s：%s" % (ch["name"][:28], ch["reason"]))
        if not ok and out["ipv6_channels"] * 2 >= max(1, out["sampled"]):
            print("        （抽到的频道里 %d/%d 是 IPv6 地址，本机没有 IPv6 出口的话这条源测不准）"
                  % (out["ipv6_channels"], out["sampled"]))
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
    parser.add_argument("--live", action="store_true", help="抓直播源并抽测频道可播性（自动带 IPv6 体检）")
    parser.add_argument("--ipv6", action="store_true", help="只做 IPv6 体检：本机到底有没有 IPv6 出口")
    parser.add_argument("--force-ipv6", action="store_true", help="本机没有 IPv6 也照测 IPv6 源（不再跳过）")
    parser.add_argument("--sources", action="store_true", help="只测清单地址本身是否可达（不抽测频道）")
    parser.add_argument("--vod", action="store_true", help="顺带测点播地址是否可达")
    parser.add_argument("--sample", type=int, default=10, help="每个直播源抽测多少频道，默认 10")
    parser.add_argument("--workers", type=int, default=12, help="并发数，默认 12")
    parser.add_argument("--timeout", type=float, default=8.0, help="单请求超时秒数，默认 8")
    parser.add_argument("--min-pass", type=float, default=0.5, help="判定源通过的最低可播比例，默认 0.5")
    parser.add_argument("--json-out", default="", help="把完整结果写成 JSON")
    args = parser.parse_args(argv)

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
            print("  直播频道抽测（每源 %d 个频道，可播率 ≥ %d%% 算通过）：" % (args.sample, int(args.min_pass * 100)))
            # 逐个源串行抓取，源内部再并发探测频道——避免线程套线程把并发数放大成 workers^2
            results = [test_live_source(e, args.sample, args.timeout, args.workers,
                                        ipv6_ready=ipv6_ready) for e in live]
            passed = report_live(label, results, args.min_pass)
            skipped = [o for o in results if o.get("skipped")]
            print("  通过的直播源：%s" % ("、".join(o["name"] for o in passed) or "无"))
            if skipped:
                print("  跳过的直播源（本机测不了，不代表源有问题）：%s"
                      % "、".join(o["name"] for o in skipped))
            # 只要有推荐源没达标，或一条都没通过，就算失败（适合挂 CI / 推送前跑）；跳过的源不计入
            recommended = set(e["name"] for e in live if e.get("recommended"))
            passed_names = [p["name"] for p in passed]
            broken = [o["name"] for o in results
                      if o["name"] in recommended and o["name"] not in passed_names]
            if not passed and skipped:
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
        all_results[label] = {"static_issues": issues, "live": results}

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(all_results, fh, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % args.json_out)

    print("=" * 78)
    print("结论：%s" % ("全部通过" if exit_code == 0 else "存在问题，见上面 [错误] / ✗"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
