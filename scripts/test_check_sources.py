#!/usr/bin/env python3
import base64
import io
import json
import unittest
import zipfile
from unittest import mock

import check_sources as c


class CheckSourcesRegressionTest(unittest.TestCase):
    def setUp(self):
        self.config = json.dumps({
            "spider": "https://example.com/a.jar",
            "sites": [{"key": "a", "api": "https://example.com/api"}],
        }).encode()

    def test_subscription_preserves_headers(self):
        text='#EXTM3U\n#EXTINF:-1,A\n#EXTVLCOPT:http-user-agent=fixtureUA\n#EXTHTTP:{"Referer":"https://example.com/"}\nhttps://example.com/live.m3u8\n#EXTINF:-1,B\nhttps://example.com/b.m3u8'
        entries=c.parse_playlist_channels(text)
        self.assertEqual(c.media_headers(entries[0])[1],{'User-Agent':'fixtureUA','Referer':'https://example.com/'})
        self.assertEqual(c.media_headers(entries[1])[1],{})

    def test_explicit_alternatives_preserve_per_line_headers(self):
        text='CCTV-1,http://a.test/1.m3u8|User-Agent=first#https://b.test/1.m3u8?x=2|User-Agent=second'
        channels=c.parse_playlist_channels(text);self.assertEqual(len(channels),2)
        self.assertEqual(c.media_headers(channels[0]),('http://a.test/1.m3u8',{'User-Agent':'first'}))
        self.assertEqual(c.media_headers(channels[1]),('https://b.test/1.m3u8?x=2',{'User-Agent':'second'}))
        m3u='#EXTM3U\n#EXTINF:-1,CCTV-1\n#EXTHTTP:{"Referer":"https://ref.test/"}\nhttp://a.test/1|User-Agent=one#https://b.test/1|User-Agent=two\n'
        parsed=c.parse_playlist_channels(m3u);self.assertEqual(len(parsed),2)
        self.assertEqual(c.media_headers(parsed[1])[1],{'User-Agent':'two','Referer':'https://ref.test/'})

    def test_full_default_is_low_concurrency(self):
        self.assertEqual(c.default_workers(True),4)
        self.assertEqual(c.default_workers(False),16)

    def test_type0_xml_list_and_detail(self):
        body=b'<rss><list><video><id>42</id><name>fixture</name><dl><dd flag="m3u8"><![CDATA[ep$https://example.com/a.m3u8]]></dd></dl></video></list></rss>'
        data=c.parse_cms_body(body)
        self.assertEqual(data['response_format'],'xml')
        self.assertEqual(data['list'][0]['vod_id'],'42')
        self.assertIn('https://example.com/a.m3u8',data['list'][0]['vod_play_url'])
        with self.assertRaises(ValueError):c.parse_cms_body(b'<!DOCTYPE x [<!ENTITY x "x">]><rss/>')
        with self.assertRaises(ValueError):c.parse_cms_body(b'<html><video><id>1</id></video></html>')
        self.assertEqual(c.parse_cms_body(b'<rss><list><video/></list></rss>')["list"],[])

    def test_invalid_configs_do_not_pass_by_keyword(self):
        for body in (b'{"sites":[]}', b'{"sites":[broken', b'{"spider":"ok"}',
                     b'<html>{"sites":[{}]}</html>'):
            self.assertFalse(c.classify_vod_body(body, "application/json")[0], body)

    def test_image_wrappers_and_jsonp(self):
        encoded = base64.b64encode(self.config)
        for body in (b"\xff\xd8fake\xff\xd9abcdefgh**" + encoded,
                     b"\xff\xd8fake\xff\xd9" + encoded,
                     b"\x89PNG\r\n\x1a\nabcdefgh**" + encoded,
                     b"callback(" + self.config + b");"):
            self.assertTrue(c.classify_vod_body(body, "application/octet-stream")[0], body[:12])

    def test_content_type_is_not_media_proof(self):
        self.assertFalse(c.judge_body(b"text", "video/mp4")[0])
        self.assertFalse(c.judge_body(b"Ggarbage", "video/mp2t")[0])
        self.assertFalse(c.judge_body(b"#EXTM3U", "application/vnd.apple.mpegurl")[0])
        self.assertTrue(c.judge_body(b"G" + b"X" * 187 + b"G" + b"X" * 187 + b"G" + b"X" * 187, "")[0])
        self.assertTrue(c.judge_body(b"\x00\x00\x00\x18ftypisom" + b"X" * 12, "")[0])
        self.assertTrue(c.judge_body(b"\x00\x00\x00\x10moof" + b"X" * 20, "")[0])

    def test_headers_and_url_suffix(self):
        self.assertEqual(c.m3u_headers('#EXTINF:-1 http-user-agent="okhttp/3.15",CCTV1'),
                         {"User-Agent": "okhttp/3.15"})
        self.assertEqual(c.split_url_headers("https://a/b$User-Agent=okhttp"),
                         ("https://a/b", {"User-Agent": "okhttp"}))
        self.assertEqual(c.split_url_headers("https://a/b|Referer=https://a/")[0], "https://a/b")

    def test_real_jar_zip_not_html(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("classes.dex", b"dex\n035\0")
        response = c.FetchResult("x", True, 200, body=buffer.getvalue())
        with mock.patch.object(c, "fetch", return_value=response):
            self.assertTrue(c.check_jar_url("https://example.com/a.jar", 1)["ok"])
        response.body = b"<html>not a jar</html>"
        with mock.patch.object(c, "fetch", return_value=response):
            self.assertFalse(c.check_jar_url("https://example.com/a.jar", 1)["ok"])

    def test_live_pipeline_and_tuple_compat(self):
        playlist = b"#EXTM3U\n#EXTINF:-1,CCTV1\nhttps://example.com/live.m3u8\n"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nseg.ts\n"
        segment = b"G" + b"X" * 187 + b"G" + b"X" * 187 + b"G" + b"X" * 187

        def fake_fetch(url, timeout, **kwargs):
            body = segment if url.endswith(".ts") else media if url.endswith(".m3u8") else playlist
            return c.FetchResult(url, True, 200, body=body, final_url=url)

        with mock.patch.object(c, "fetch", side_effect=fake_fetch):
            result = c.test_live_source({"name": "fixture", "url": "https://example.com/list.m3u"},
                                        10, 2, 2, deep=True, full=True)
            self.assertEqual((result["total"], result["passed"], result["chan_passed"]), (1, 1, 1))
            self.assertTrue(c.probe_channel(("CCTV", "https://example.com/live.m3u8"), 2, deep=True)[1])

    def test_channel_metric_reuses_line_evidence(self):
        playlist = ("CCTV1,https://example.com/live.m3u8\n" * 2).encode()
        segment = b"G" + b"X" * 187 + b"G" + b"X" * 187 + b"G" + b"X" * 187
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nseg.ts\n"
        calls = []

        def fake_fetch(url, timeout, **kwargs):
            calls.append(url)
            body = segment if url.endswith(".ts") else media if url.endswith(".m3u8") else playlist
            return c.FetchResult(url, True, 200, body=body, final_url=url)

        with mock.patch.object(c, "fetch", side_effect=fake_fetch):
            result = c.test_live_source({"name": "fixture", "url": "https://example.com/list.txt"},
                                        10, 2, 2, deep=True, full=True)
            self.assertEqual(result["passed"], 2)
            self.assertEqual(result["chan_passed"], 1)
            self.assertEqual(calls.count("https://example.com/live.m3u8"), 2)  # 两条同 URL 各一次，不再第三轮重试

    def test_gzip_and_redirect_recorded(self):
        import gzip
        body = gzip.compress(self.config)
        self.assertEqual(c.classify_vod_body(gzip.decompress(body), "application/json")[0], True)
        self.assertEqual(c.clean_url("http://www.饭太硬.net/tv"), "http://www.xn--sss604efuw.net/tv")
        self.assertEqual(c.clean_url("http://[2409::1]:80/live"), "http://[2409::1]:80/live")


if __name__ == "__main__":
    unittest.main(verbosity=2)
