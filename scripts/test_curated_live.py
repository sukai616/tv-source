#!/usr/bin/env python3
import unittest
from unittest.mock import patch
import refresh_curated_live as c
import check_continuous_stream as stream

class CuratedTests(unittest.TestCase):
    def fixture(self):return {'lists':[{'file':'news.m3u','title':'News','channels':[{'name':'Public station','url':'https://example.com/live?variant=hd','reviewed_public_station':True,'headers':{'Referer':'https://example.com/'},'provenance':['https://example.com/'],'evidence':{'seconds':180}}]}]}
    def test_render_preserves_full_url_headers(self):
        data=self.fixture();text=c.render(data)['news.m3u'];self.assertIn('?variant=hd',text);self.assertIn('#EXTHTTP:',text)
    def test_requires_manual_review(self):
        data=self.fixture();data['lists'][0]['channels'][0]['reviewed_public_station']=False
        with self.assertRaises(ValueError):c.render(data)
    def test_no_credentials(self):
        data=self.fixture();data['lists'][0]['channels'][0]['headers']['Cookie']='secret'
        with self.assertRaises(ValueError):c.render(data)
    def test_no_duplicate_or_path_escape(self):
        data=self.fixture();data['lists'][0]['channels']*=2
        with self.assertRaises(ValueError):c.render(data)
        data=self.fixture();data['lists'][0]['file']='../bad.m3u'
        with self.assertRaises(ValueError):c.render(data)
    def test_decode_success_requires_full_video_and_no_warning(self):
        decoded={'status':'decoded','elapsed':181,'progress':[{'out_time_us':'180000000','frame':'4500','dup_frames':'0','drop_frames':'0'}],'warnings':''}
        with patch.object(stream.quality,'decode_sample',return_value=decoded):self.assertTrue(stream.measure(('x','https://example.com/a'),180)['continuous_decode_sample'])
        decoded['warnings']='error'
        with patch.object(stream.quality,'decode_sample',return_value=decoded):self.assertFalse(stream.measure(('x','https://example.com/a'),180)['continuous_decode_sample'])
        decoded['status']='timeout'
        with patch.object(stream.quality,'decode_sample',return_value=decoded):self.assertFalse(stream.measure(('x','https://example.com/a'),180)['continuous_decode_sample'])
if __name__=='__main__':unittest.main()
