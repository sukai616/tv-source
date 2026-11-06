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
import base64
import gzip
import hashlib
import io
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
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 点播配置常用 okhttp；部分源（如饭太硬）对浏览器 UA 回门户 HTML，对 okhttp 回「JPEG 外壳 + Base64 JSON」。
OKHTTP_UA = "okhttp/3.15"
MAX_CONFIG_BYTES = 32 * 1024 * 1024


# 播放地址里常带空格 / 中文 / 竖线，urllib 不认，统一先清洗 + 转义
INVALID_URL_CHARS = re.compile(r"[\s\t\n\r]")
SAFE_URL_CHARS = re.compile(r"[^A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]")


def clean_url(url):
    """清掉空白字符，并把非法字符转义成 %xx；主机名转成 punycode，避免 urllib latin-1 炸中文域名。"""
    if not isinstance(url, str):
        return ""
    url = INVALID_URL_CHARS.sub("", url).strip()
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.hostname:
            host = parts.hostname.encode("idna").decode("ascii")
            # 保留用户信息/端口
            netloc = "[%s]" % host if ":" in host else host
            if parts.port:
                netloc = "%s:%s" % (netloc, parts.port)
            if parts.username:
                user = parts.username
                if parts.password:
                    user = "%s:%s" % (user, parts.password)
                netloc = "%s@%s" % (user, netloc)
            url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
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
    def __init__(self, url, ok, status=None, ctype="", body=b"", error="", seconds=0.0,
                 final_url="", redirects=None, content_encoding=""):
        self.url = url
        self.ok = ok
        self.status = status
        self.ctype = ctype
        self.body = body
        self.error = error
        self.seconds = seconds
        self.final_url = final_url or url
        self.redirects = redirects or []
        self.content_encoding = content_encoding


class RecordingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.urls = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.urls.append(newurl)
        if len(self.urls) > 10:
            raise urllib.error.URLError("重定向超过 10 跳")
        return super(RecordingRedirectHandler, self).redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, timeout, limit=None, ua=None, use_range=True, headers=None, max_bytes=MAX_CONFIG_BYTES):
    """GET 并记录重定向、显式解压缩；limit 是读取上限，不要求服务器支持 Range。"""
    url = clean_url(url)
    started = time.time()
    redirect_handler = RecordingRedirectHandler()
    try:
        req_headers = {"User-Agent": ua or UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        if limit and use_range:
            req.add_header("Range", "bytes=0-%d" % (limit - 1))
        opener = urllib.request.build_opener(redirect_handler)
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(limit) if limit else resp.read(max_bytes + 1 if max_bytes else -1)
            if max_bytes and len(body) > max_bytes:
                raise ValueError("响应超过 %d 字节上限" % max_bytes)
            encoding = resp.headers.get("Content-Encoding", "").lower().strip()
            if "gzip" in encoding or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            elif "deflate" in encoding:
                body = zlib.decompress(body)
            return FetchResult(
                url, True, resp.status, resp.headers.get("Content-Type", ""), body,
                seconds=time.time() - started, final_url=resp.geturl(),
                redirects=redirect_handler.urls, content_encoding=encoding,
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
        return FetchResult(url, False, exc.code, exc.headers.get("Content-Type", ""), body,
                           "HTTP %s" % exc.code, time.time() - started, redirects=redirect_handler.urls)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return FetchResult(url, False, None, "", b"", "%s" % reason, time.time() - started,
                           redirects=redirect_handler.urls)
    except Exception as exc:
        return FetchResult(url, False, None, "", b"", "%s: %s" % (type(exc).__name__, exc),
                           time.time() - started, redirects=redirect_handler.urls)


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
                attrs = line
            elif line.startswith("#"):
                continue
            elif name is not None or line.lower().startswith("http"):
                url, extra = split_url_headers(line)
                extra.update(m3u_headers(attrs))
                channels.append((name or url, url, extra, attrs))
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
DEEP_PLAYLIST_LIMIT = 65536
DEEP_SEGMENT_LIMIT = 65536
DEEP_MAX_FOLLOW = 3  # 主列表→子列表→媒体列表→分片


def decode_config_text(text):
    """按 App ApiConfig.FindResult 的公开算法解配置；不解或绕过密码/鉴权。"""
    text = text.lstrip("\ufeff\x00 \t\r\n")
    # 仅提取 JS 变量/JSONP 中的 JSON，不执行 JS。
    if text[:1] not in ("{", "["):
        match = re.match(r"^[\w$.]+\s*(?:=\s*|\()\s*([\[{])", text)
        if match:
            start = match.start(1)
            end = text.rfind("}" if text[start] == "{" else "]")
            if end > start:
                text = text[start:end + 1]
    if text[:1] not in ("{", "["):
        match = re.search(r"[A-Za-z0-9]{8}\*\*", text)
        if match:
            try:
                text = base64.b64decode(text[match.end():]).decode("utf-8").strip()
            except Exception:
                return None, "标记后 Base64 解码失败"
    compact = re.sub(r"\s+", "", text)
    if compact.startswith("2423"):
        try:
            raw = bytes.fromhex(compact).decode("utf-8", "replace").lower()
            key = raw[raw.index("$#") + 2:raw.index("#$")].strip()[:16].ljust(16, "0")
            iv = raw[-13:].strip()[:16].ljust(16, "0")
            ciphertext = bytes.fromhex(compact[compact.index("2324") + 4:-26])
            # 标准库无 AES；调用 Mac 已有 openssl，算法与 App CBC/PKCS7 一致。
            proc = subprocess.run(
                ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.encode("utf-8").hex(),
                 "-iv", iv.encode("utf-8").hex()],
                input=ciphertext, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
            if proc.returncode != 0:
                return None, "AES-CBC 解密失败（%s）" % proc.stderr.decode("utf-8", "ignore").strip()
            text = proc.stdout.decode("utf-8")
        except Exception as exc:
            return None, "AES-CBC 配置无法解析：%s" % exc
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
    text = re.sub(r",\s*([}\]])", r"\1", "\n".join(lines))
    try:
        data = json.loads(text)
    except Exception as exc:
        return None, "JSON 解析失败：%s" % exc
    return data, ""


def decode_image_wrapped_config(body):
    """识别 JPEG/PNG/GIF/BMP 外壳及任意 Content-Type 的 8位标记**+Base64。"""
    if not body:
        return None
    tails = []
    if body[:2] == b"\xff\xd8":
        eoi = body.find(b"\xff\xd9")
        if eoi >= 0:
            tails.append(body[eoi + 2:])
    if body.startswith((b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")):
        tails.append(body)
    tails.append(body)
    for index, tail in enumerate(tails):
        text = tail.decode("latin1", "ignore")
        match = re.search(r"[A-Za-z0-9]{8}\*\*", text)
        if match:
            text = text[match.end():]
        elif index < len(tails) - 1:
            text = text.strip()  # 图片 EOI 之后无显式标记的历史格式
        else:
            continue  # 普通文本没有标记时不瞎猜 Base64
        payload = re.sub(r"[^A-Za-z0-9+/=]", "", text)
        if len(payload) < 64:
            continue
        try:
            decoded = base64.b64decode(payload + "=" * ((-len(payload)) % 4)).decode("utf-8")
            data, _ = decode_config_text(decoded)
            if isinstance(data, dict) and data.get("sites"):
                return data
        except Exception:
            pass
    return None


def collection_urls(data):
    if not isinstance(data, dict) or data.get("sites"):
        return []
    urls = []
    for key in ("urls", "storeHouse", "warehouse"):
        values = data.get(key)
        if isinstance(values, str):
            values = [{"name": key, "url": values}]
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    urls.append((item.get("name") or key, item["url"]))
    return urls


ADULT_SITE_RE = re.compile(
    r"(?:成人|伦理|情色|三级|18\+|r18|鸡坤|麻豆|精东|好色|黄色|蜜桃|猫咪|九色|抖阴|(?:^|[^a-z])av(?:$|[^a-z]))", re.I)


def inspect_vod_body(body, ctype):
    """真实解析配置；『出现 sites 字样』不再算通过。"""
    out = {"ok": False, "reason": "", "data": None, "sites": 0, "csp": 0, "http_sites": 0,
           "collections": [], "adult": [], "suspicious": False}
    if not body:
        out["reason"] = "空响应"
        return out
    low = body[:4096].lower()
    if b"<html" in low or b"<!doctype" in low or b"<head" in low[:512]:
        out["reason"] = "返回的是 HTML 页面"
        return out
    data, err = decode_config_text(body.decode("utf-8-sig", "replace"))
    if data is None:
        data = decode_image_wrapped_config(body)
        if data is None:
            out["reason"] = ("纯图片/图片外壳，未解出配置" if body[:2] == b"\xff\xd8" or "image/" in (ctype or "").lower()
                             else "%s（不猜测未知加密格式）" % err)
            return out
    if not isinstance(data, dict):
        out["reason"] = "配置顶层不是对象"
        return out
    out["data"] = data
    sites = data.get("sites")
    if isinstance(sites, list):
        valid = [s for s in sites if isinstance(s, dict) and s.get("key") and s.get("api")]
        out["sites"] = len(valid)
        out["csp"] = sum(1 for s in valid if str(s.get("api", "")).startswith("csp_"))
        out["http_sites"] = sum(1 for s in valid if str(s.get("api", "")).lower().startswith("http"))
        if not valid:
            out["reason"] = "sites 为空或条目缺 key/api"
            return out
        out["adult"] = [str(s.get("name") or s.get("key")) for s in valid
                        if ADULT_SITE_RE.search(str(s.get("name") or ""))]
        out["suspicious"] = bool(out["adult"])
        out["ok"] = True
        out["reason"] = "配置 %d 站（CSP %d / HTTP %d）%s" % (
            len(valid), out["csp"], out["http_sites"],
            "；疑似成人站需人工排除：" + "、".join(out["adult"][:8]) if out["adult"] else "")
        return out
    out["collections"] = collection_urls(data)
    if out["collections"]:
        out["ok"] = True
        out["reason"] = "多仓 %d 条（不是单仓，须递归验证）" % len(out["collections"])
    else:
        out["reason"] = "不是含非空 sites 的单仓，也不是可识别多仓"
    return out


def classify_vod_body(body, ctype):
    info = inspect_vod_body(body, ctype)
    return info["ok"], info["reason"]


def is_ts_payload(body):
    """TS 必须多个 188 字节包同步；一个 0x47 不够。"""
    return len(body) >= 564 and all(body[i * 188] == 0x47 for i in range(3))


def is_mp4_payload(body):
    return len(body) >= 12 and body[4:8] == b"ftyp"


def is_fmp4_payload(body):
    return len(body) >= 12 and (body[4:8] in (b"moof", b"mdat", b"styp") or is_mp4_payload(body))


def judge_body(body, ctype):
    """必须看到媒体/列表内容；Content-Type 单独声明为视频不能证明可播。"""
    head = (body or b"")[:65536]
    low = head.lower()
    if not head:
        return False, "空响应"
    if (b"<html" in low[:4096] or b"<!doctype" in low[:2048]
            or b"not found" in low[:256] or b"access denied" in low[:256]):
        return False, "返回的是 HTML/错误页"
    if b"#extm3u" in low[:65536]:
        if b"#ext-x-stream-inf" in low or b"#ext-x-media:" in low:
            return True, "m3u8 主播放列表"
        if b"#extinf" in low or b"#ext-x-targetduration" in low:
            return True, "m3u8 媒体列表"
        return False, "只有 EXTM3U 标记、没有媒体条目"
    if is_ts_payload(body):
        return True, "TS 流（至少 3 个同步包）"
    if is_mp4_payload(body):
        return True, "MP4 流（ftyp 魔数）"
    if is_fmp4_payload(body):
        return True, "fMP4 分片（box 魔数）"
    if head[:3] == b"FLV":
        return True, "FLV 流"
    if b"OggS" in head[:4] or b"ID3" in head[:3] or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True, "音频流（魔数）"
    return False, "内容无法识别，不能仅凭 Content-Type=%s 判可播" % ((ctype or "无"),)


def split_url_headers(value):
    """TVBox/M3U 的 $、| 后缀参数不能拼进实际请求 URL。"""
    headers = {}
    url = (value or "").strip()
    for separator in ("$", "|"):
        if separator not in url:
            continue
        url, suffix = url.split(separator, 1)
        for part in suffix.split("&"):
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            if key.lower() in ("user-agent", "ua", "referer", "origin", "cookie"):
                headers["User-Agent" if key.lower() in ("user-agent", "ua") else key.title()] = val
    return url.strip(), headers


def m3u_headers(extinf_line):
    headers = {}
    for key in ("http-user-agent", "http-referrer", "http-origin", "http-cookie"):
        match = re.search(r'\b%s="([^"]*)"' % re.escape(key), extinf_line or "", re.I)
        if match:
            name = {"http-user-agent": "User-Agent", "http-referrer": "Referer",
                    "http-origin": "Origin", "http-cookie": "Cookie"}[key]
            headers[name] = match.group(1)
    return headers


def playlist_header_text(text):
    """保留 #EXTVLCOPT / #EXTHTTP 请求头，供后续真实请求使用。"""
    if not isinstance(text, str):
        return ""
    lines = text.replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if line.strip().upper().startswith("#EXTINF"):
            return "\n".join(lines[max(0, index - 3):index + 1])
    return "\n".join(lines[:4])


def media_headers(channel):
    name, url = channel[0], channel[1]
    headers = dict(channel[2]) if len(channel) > 2 and isinstance(channel[2], dict) else {}
    if len(channel) > 3:
        text = channel[3]
        for line in text.replace("\r", "\n").split("\n"):
            if line.upper().startswith("#EXTVLCOPT:HTTP-USER-AGENT="):
                headers["User-Agent"] = line.split("=", 1)[1].strip()
            elif line.upper().startswith("#EXTVLCOPT:HTTP-REFERER="):
                headers["Referer"] = line.split("=", 1)[1].strip()
            elif line.upper().startswith("#EXTHTTP:"):
                try:
                    headers.update(json.loads(line.split(":", 1)[1]))
                except ValueError:
                    pass
        headers.update(m3u_headers(text))
    clean, extra = split_url_headers(url)
    for key, value in extra.items():
        headers.setdefault(key, value)
    return clean, headers


def looks_like_m3u8(body, ctype):
    """只有内容和 EXT 标记才是 HLS；不再被 Content-Type 的 HTML 冒充。"""
    head = (body or b"")[:65536].lower()
    return b"#extm3u" in head and (b"#extinf" in head or b"#ext-x-targetduration" in head or b"#ext-x-stream-inf" in head or b"#ext-x-media:" in head)


def extract_first_m3u8_ref(body, base_url):
    """从 m3u8 里取出第一条媒体分片或子播放列表 URL。

    返回 (abs_url, kind)；kind 为 'playlist' / 'segment'。失败返回 (None, 原因)。
    相对路径按 base_url 拼接。
    """
    text = (body or b"").decode("utf-8", "ignore")
    if "#EXT-X-ENDLIST" in text.upper():
        # 部分直播也带 ENDLIST；报告标为疑似轮播/VOD，不假装验证了实时性。
        pass
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


def deep_follow_m3u8(url, body, ctype, timeout, headers=None):
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
        next_headers = dict(headers or {})
        next_headers.setdefault("Referer", current_url)
        result = fetch(next_url, timeout, limit=limit, headers=next_headers, use_range=True)
        if not result.ok:
            return False, "跟进失败(%s): %s" % (kind, result.error or "请求失败"), time.time() - started
        current_url = result.final_url or next_url
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


def probe_channel(channel, timeout, deep=False, ffprobe=False):
    name, original_url = channel[0], channel[1]
    url, headers = media_headers(channel)
    limit = DEEP_PLAYLIST_LIMIT if deep else 65536
    # 小 Range 读 MP4 的 moov/ftyp，不能把整部电影下载完；服务器忽略 Range 时仍只读 limit。
    result = fetch(url, timeout, limit=limit, headers=headers, use_range=True)
    if not result.ok:
        return channel, False, result.error or "请求失败", result.seconds
    ok, why = judge_body(result.body, result.ctype)
    if not ok:
        return channel, False, why, result.seconds
    if deep and looks_like_m3u8(result.body, result.ctype):
        ok2, why2, extra = deep_follow_m3u8(
            result.final_url or url, result.body, result.ctype, timeout, headers=headers)
        if ffprobe and ok2:
            ok2, why2, extra2 = probe_decode(result.final_url or url, headers, timeout)
            extra += extra2
        return channel, ok2, why2, result.seconds + extra
    if ffprobe and ok:
        ok2, why2, extra = probe_decode(result.final_url or url, headers, timeout)
        return channel, ok2, why2, result.seconds + extra
    return channel, ok, why, result.seconds


def probe_decode(url, headers, timeout):
    """可选 ffprobe：真读一段流并识别视频/音频轨；不会把首个分片等同于持续稳定。"""
    started = time.time()
    command = ["ffprobe", "-v", "error", "-analyzeduration", "1000000", "-probesize", "1000000",
               "-rw_timeout", str(int(max(2, timeout) * 1000000)), "-show_entries",
               "stream=codec_type,codec_name,width,height", "-of", "json"]
    for key, value in headers.items():
        command += ["-headers", "%s: %s\r\n" % (key, value)]
    command.append(url)
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=max(5, timeout + 3))
        data = json.loads(proc.stdout.decode("utf-8", "ignore") or "{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = any(s.get("codec_type") == "audio" for s in streams)
        if proc.returncode == 0 and (video or audio):
            return True, "ffprobe：%s%s" % (
                ("视频 %sx%s" % (video.get("width", "?"), video.get("height", "?"))) if video else "音频",
                "+音轨" if video and audio else ""), time.time() - started
        return False, "ffprobe 无音视频轨：%s" % proc.stderr.decode("utf-8", "ignore").strip()[:180], time.time() - started
    except Exception as exc:
        return False, "ffprobe 失败：%s" % exc, time.time() - started


def probe_channel_name(name, urls, timeout, max_lines, deep=False, ffprobe=False):
    """频道口径：这个频道名的前 max_lines 条线路里，有没有一条能播。

    命中就提前返回，不再往下试——多线路聚合源一个频道挂 2~6 条线路，
    逐条探完既慢又没意义（用户点开这个台，播放器也是按顺序重试）。
    --phone 下默认 max_lines=1，只认排序后的第一条。
    """
    for url in urls[:max_lines]:
        _, ok, _, _ = probe_channel(url, timeout, deep=deep, ffprobe=ffprobe)
        if ok:
            return True
    return False


def test_live_source(entry, sample_size, timeout, workers, ipv6_ready=True,
                     max_lines=4, deep=False, full=False, ffprobe=False):
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
        "ffprobe": bool(ffprobe),
    }
    started = time.time()
    page = fetch(entry["url"], timeout)
    if not page.ok:
        out["fetch_error"] = page.error or "抓取失败"
        out["seconds"] = time.time() - started
        return out
    text = page.body.decode("utf-8", "ignore")
    # 把文本关联到频道：M3U 的 header 行属于下一条 EXTINF；TXT 用 URL 后缀。
    channels = []
    if text.lstrip()[:512].upper().find("#EXTM3U") >= 0:
        pending = []
        name = None
        for line in text.replace("\r", "\n").split("\n"):
            line = line.strip()
            if line.startswith("#"):
                pending.append(line)
                if line.upper().startswith("#EXTINF"):
                    name = line.split(",")[-1].strip() or None
                continue
            if line:
                url, extra = split_url_headers(line)
                if name is not None or url.lower().startswith("http"):
                    channels.append((name or url, url, extra, "\n".join(pending)))
                name, pending = None, []
    else:
        channels = [(n, u, split_url_headers(u)[1], "") for n, u in parse_playlist(text)]
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
    out["ipv6_channels"] = sum(1 for channel in picked if is_ipv6_url(channel[1]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda ch: probe_channel(ch, timeout, deep=deep, ffprobe=ffprobe), picked))
    # 同名频道复用相同 URL 的内容探测，再按 max-lines=1 取第一条；不能重复 ffprobe 同一媒体。

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

    # 频道口径：优先复用同一 URL 的实测结果。重复请求既有服务器限流风险，
    # 也会让第一轮已过、第二轮被临时拒绝的同一频道出现 0% 假异常。
    by_name = {}
    result_by_url = {}
    for channel, ok, _, _ in results:
        n, u = channel[0], channel[1]
        result_by_url[u] = ok
    for channel in testable:
        n, u = channel[0], channel[1]
        by_name.setdefault(n or u, []).append(u)
    out["named_total"] = len(by_name)
    out["lines_per_name"] = round(len(testable) / float(len(by_name)), 2) if by_name else 0.0
    out["multi_line"] = out["lines_per_name"] >= MULTI_LINE_RATIO
    if out["multi_line"]:
        n_chan = len(by_name) if full else sample_size
        chan_picked = sample_evenly(sorted(by_name), n_chan)

        def test_name(name):
            for url in by_name[name][:max_lines]:
                if url in result_by_url:
                    if result_by_url[url]:
                        return name, True
                    continue
                if probe_channel_name(name, [url], timeout, 1, deep=deep, ffprobe=ffprobe):
                    return name, True
            return name, False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            chan_res = list(pool.map(test_name, chan_picked))
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


def jar_bytes(body):
    match = re.search(rb"[A-Za-z0-9]{8}\*\*", body)
    if match:
        try:
            return base64.b64decode(body[match.end():])
        except Exception:
            return b""
    return body


def check_jar_url(url, timeout):
    raw_url, sep, expected_md5 = (url or "").partition(";md5;")
    raw_url = raw_url.replace("img+", "", 1) if raw_url.startswith("img+") else raw_url
    if not raw_url.lower().startswith(("http://", "https://")):
        return {"url": raw_url, "ok": True, "reason": "本地/自定义 spider，无法远程测"}
    result = fetch(raw_url, timeout, ua=OKHTTP_UA, use_range=False, max_bytes=48 * 1024 * 1024)
    if not result.ok:
        return {"url": raw_url, "ok": False, "reason": result.error}
    raw = jar_bytes(result.body)
    if expected_md5 and hashlib.md5(raw).hexdigest().lower() != expected_md5.strip().lower():
        return {"url": raw_url, "ok": False, "reason": "jar MD5 不匹配"}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if "classes.dex" not in names:
                return {"url": raw_url, "ok": False, "reason": "ZIP 内无 classes.dex"}
            if archive.testzip() is not None:
                return {"url": raw_url, "ok": False, "reason": "ZIP CRC 损坏"}
    except Exception:
        return {"url": raw_url, "ok": False, "reason": "不是可打开的 jar/ZIP（含图片内嵌格式）"}
    return {"url": raw_url, "ok": True, "reason": "jar/ZIP+classes.dex 有效，MD5 %s" % hashlib.md5(raw).hexdigest()[:12],
            "bytes": len(raw)}


def cms_list_url(api, vod_id=None):
    if not api.lower().startswith(("http://", "https://")):
        return ""
    separator = "&" if "?" in api else "?"
    return api + separator + ("ac=detail&ids=%s" % urllib.parse.quote(str(vod_id)) if vod_id else "ac=detail&pg=1")


def probe_cms_site(site, timeout, playable=False):
    name = str(site.get("name") or site.get("key"))
    result = fetch(cms_list_url(site["api"]), timeout, ua=OKHTTP_UA, use_range=False, max_bytes=4 * 1024 * 1024)
    out = {"name": name, "url": site["api"], "ok": False, "items": 0, "playable": None, "reason": ""}
    if not result.ok:
        out["reason"] = result.error
        return out
    try:
        data = json.loads(result.body.decode("utf-8-sig", "replace"))
        items = data.get("list") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            out["reason"] = "列表为空或不是采集 JSON"
            return out
        out["items"] = len(items)
        out["ok"] = True
        out["reason"] = "列表 %d 条" % len(items)
        if playable:
            vod_id = items[0].get("vod_id")
            if not vod_id:
                out["ok"] = False
                out["reason"] = "列表缺 vod_id"
                return out
            detail = fetch(cms_list_url(site["api"], vod_id), timeout, ua=OKHTTP_UA, use_range=False,
                           max_bytes=4 * 1024 * 1024)
            detail_data = json.loads(detail.body.decode("utf-8-sig", "replace")) if detail.ok else {}
            rows = detail_data.get("list") or []
            play_url = rows[0].get("vod_play_url", "") if rows else ""
            urls = re.findall(r"https?://[^$#\s]+", str(play_url))
            if not urls:
                out["ok"] = False
                out["reason"] = "详情无 HTTP 播放地址"
                return out
            ch, ok, why, _ = probe_channel((name, urls[0]), timeout, deep=True)
            out["playable"] = ok
            out["reason"] += "；详情播放地址%s（%s）" % ("有媒体迹象" if ok else "失败", why)
            # 有列表和详情但媒体需 App 专用解析时标 playable=False，人工降级，不把详情抹成空列表。
    except Exception as exc:
        out["reason"] = "采集 JSON 异常：%s" % exc
    return out


def test_vod_source(entry, timeout, sample=8, workers=8, playable=False):
    out = {"name": entry.get("name") or entry.get("url"), "url": entry.get("url"),
           "ok": False, "reason": "", "sites": 0, "csp": 0, "http_sites": 0,
           "adult": [], "redirects": [], "jars": [], "cms": [], "cms_total": 0, "cms_passed": 0,
           "site_keys": [], "site_apis": [], "playable_passed": 0, "playable_total": 0}
    result = fetch(entry["url"], timeout, ua=OKHTTP_UA, use_range=False)
    if not result.ok:
        out["reason"] = result.error
        return out
    info = inspect_vod_body(result.body, result.ctype)
    out.update({key: info[key] for key in ("sites", "csp", "http_sites", "adult")})
    out["redirects"] = result.redirects
    out["final_url"] = result.final_url
    out["encoding"] = result.content_encoding
    if not info["ok"]:
        out["reason"] = info["reason"]
        return out
    if info["collections"]:
        out["reason"] = info["reason"] + "；公开清单优先直接单仓，多仓不自动算可用"
        return out
    data = info["data"]
    out["site_keys"] = sorted(set(str(site.get("key")) for site in data["sites"] if isinstance(site, dict) and site.get("key")))
    out["site_apis"] = sorted(set(str(site.get("api")) for site in data["sites"] if isinstance(site, dict) and site.get("api")))
    jars = set()
    if data.get("spider"):
        jars.add(data["spider"])
    for site in data["sites"]:
        if isinstance(site, dict) and site.get("jar"):
            jars.add(site["jar"])
    resolved_jars = set()
    for jar_url in jars:
        raw_url = jar_url.replace("img+", "", 1)
        if raw_url.startswith(("http://", "https://")):
            resolved_jars.add(jar_url)
        elif not raw_url.startswith(("clan://", "file://", "assets://", "proxy://")):
            resolved_jars.add(urllib.parse.urljoin(result.final_url, jar_url))
    remote_jars = sorted(resolved_jars)
    with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
        out["jars"] = list(pool.map(lambda url: check_jar_url(url, timeout), remote_jars))
    bad_jars = [j for j in out["jars"] if not j["ok"]]
    sites = [site for site in data["sites"] if isinstance(site, dict)
             and site.get("key") and str(site.get("api", "")).lower().startswith("http")
             and (not site.get("type") or site.get("type") in (0, 1))]
    # 排除显式隐藏站，均匀抽样而非只测排最前的站。
    sites = [site for site in sites if not site.get("hide")]
    picked = sample_evenly(sites, sample)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out["cms"] = list(pool.map(lambda site: probe_cms_site(site, timeout, playable=playable), picked))
    out["cms_total"] = len(sites)
    out["cms_passed"] = sum(1 for c in out["cms"] if c["ok"])
    out["playable_total"] = sum(1 for c in out["cms"] if c["playable"] is not None)
    out["playable_passed"] = sum(1 for c in out["cms"] if c["playable"] is True)
    failures = []
    if bad_jars:
        failures.append("spider jar %d/%d 失败" % (len(bad_jars), len(out["jars"])))
    if out["cms"] and out["cms_passed"] == 0:
        failures.append("采集抽测 0/%d" % len(out["cms"]))
    elif out["cms"] and out["cms_passed"] * 2 < len(out["cms"]):
        failures.append("采集抽测不足 50%%：%d/%d" % (out["cms_passed"], len(out["cms"])))
    out["ok"] = not failures
    out["reason"] = info["reason"]
    if out["cms"]:
        out["reason"] += "；采集列表 %d/%d，播放迹象 %d/%d" % (out["cms_passed"], len(out["cms"]), out["playable_passed"], out["playable_total"])
    if out["jars"]:
        out["reason"] += "；jar %d/%d" % (len(out["jars"]) - len(bad_jars), len(out["jars"]))
    if failures:
        out["reason"] += "；不合格：" + "、".join(failures)
    return out


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
    parser.add_argument("--vod", action="store_true", help="测点播结构、spider jar、采集列表（默认抽 8 路）")
    parser.add_argument("--vod-sample", type=int, default=8, help="每源抽测多少个 HTTP 采集站，0 跳过采集")
    parser.add_argument("--vod-playable", action="store_true", help="采集列表之外再取详情并探测首个播放地址")
    parser.add_argument("--ffprobe", action="store_true", help="直播内容检查后调用 ffprobe 真读流，识别音视频轨")
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

        vod_results = []
        if args.vod:
            print("  点播质量探测（结构 + spider jar + 采集列表；不是仅 HTTP 200）：")
            for item in (data.get("vod") or []):
                out = test_vod_source(item, args.timeout, sample=args.vod_sample,
                                      workers=min(args.workers, 8), playable=args.vod_playable)
                vod_results.append(out)
                print("    %s %s → %s" % ("✓" if out["ok"] else "✗",
                      pad((item.get("name") or item.get("url"))[:24], 26), out["reason"]))
                for jar in out["jars"]:
                    if not jar["ok"]:
                        print("      · jar %s：%s" % (jar["url"], jar["reason"]))
                for cms in out["cms"]:
                    if not cms["ok"]:
                        print("      · 采集 %s：%s" % (cms["name"][:28], cms["reason"]))
            if any(not out["ok"] for out in vod_results):
                exit_code = 1

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
                        deep=args.deep, full=args.full, ffprobe=args.ffprobe,
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
            "vod": vod_results,
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
                "ffprobe": bool(args.ffprobe),
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
