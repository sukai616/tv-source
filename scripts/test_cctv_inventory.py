import unittest
import build_cctv_inventory as c
class InventoryTests(unittest.TestCase):
    def test_exact_dedup_keeps_headers_names_and_provenance(self):
        rows=c.build_inventory([('one','https://source.test/one','CCTV1,http://media.test/live?a=1'),('two','https://source.test/two','CCTV-1HD,http://media.test/live?a=1'),('three','https://source.test/three','CCTV1,http://media.test/live?a=1|User-Agent=other')])
        self.assertEqual(len(rows),2);self.assertEqual(len(rows[0]['sources']),2);self.assertEqual(len(rows[0]['names']),2);self.assertEqual(rows[1]['headers'],{'User-Agent':'other'})
    def test_explicit_backups_and_variants_are_not_news_or_extra_baseline(self):
        rows=c.build_inventory([('one','https://s.test','CCTV5P,http://a.test/live?q=full#http://b.test/live?q=keep\nCCTV4-Europe,http://e.test/live\nCCTV8K,http://8.test/live\nCGTN,http://news.test/live')])
        self.assertEqual(len(rows),4);self.assertEqual([x['station'] for x in rows],['CCTV-5+','CCTV-5+','CCTV-4-Europe','UHD-8K']);self.assertEqual(rows[1]['url'],'http://b.test/live?q=keep')
        summary={x['station']:x for x in c.summarize(rows)};self.assertEqual(summary['CCTV-5+']['unique_url_headers'],2);self.assertEqual(summary['CCTV-4']['unique_url_headers'],0)
if __name__=='__main__':unittest.main()
