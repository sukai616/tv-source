import unittest
from cctv_policy import canonical_station as c,select_representatives,BASE_CHANNELS

class PolicyTests(unittest.TestCase):
    def test_baseline_eighteen(self):self.assertEqual(len(BASE_CHANNELS),18)
    def test_aliases(self):
        self.assertEqual(c('CCTV-1 HD'),'CCTV-1');self.assertEqual(c('CCTV10'),'CCTV-10')
        self.assertEqual(c('CCTV5PLUS'),'CCTV-5+');self.assertEqual(c('CCTV5P'),'CCTV-5+')
        self.assertEqual(c('CCTV-5+ HD'),'CCTV-5+')
    def test_variants_not_false_completeness(self):
        self.assertEqual(c('CCTV-4K'),'UHD-4K');self.assertEqual(c('CCTV8K'),'UHD-8K')
        self.assertEqual(c('CCTV4Europe'),'CCTV-4-Europe');self.assertEqual(c('CCTV4America'),'CCTV-4-America')
        self.assertIsNone(c('CGTN'));self.assertIsNone(c('Phoenix'))
    def test_default_does_not_fill_missing_with_other_news(self):
        ch=[('CGTN','https://example.com/a'),('CCTV4K','https://example.com/b'),('CCTV-5+','https://example.com/c'),('CCTV5','https://example.com/d')]
        picked,missing=select_representatives(ch)
        self.assertEqual({x[0] for x in picked},{'CCTV5','CCTV-5+'});self.assertEqual(len(missing),16)
    def test_same_url_two_labels_not_two_stations(self):
        picked,missing=select_representatives([('CCTV1','https://example.com/a'),('CCTV2','https://example.com/a')])
        self.assertEqual(len(picked),1);self.assertIn('CCTV-2',missing)
    def test_ipv6_skip_is_not_failure(self):
        picked,missing=select_representatives([('CCTV1','http://[2001:db8::1]/a')])
        self.assertFalse(picked);self.assertIn('CCTV-1',missing)
    def test_explicit_other_category_still_works(self):
        picked,missing=select_representatives([('Phoenix','https://example.com/a')],['Phoenix'])
        self.assertEqual(len(picked),1);self.assertFalse(missing)
if __name__=='__main__':unittest.main()
