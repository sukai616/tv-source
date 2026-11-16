import unittest
from live_composite_policy import TARGETS,CCTV,PROVINCIAL,ADDITIONAL,canonical_station as c,select_representatives,validate_target_coverage
import build_live_inventory as inventory
class CompositePolicyTests(unittest.TestCase):
    def test_counts_unique(self):
        self.assertEqual((len(CCTV),len(PROVINCIAL),len(ADDITIONAL)),(18,31,8));self.assertEqual(len(set(TARGETS)),57)
    def test_cctv_excludes_international_uhd(self):
        self.assertEqual(c('CCTV5PLUS HD'),'CCTV-5+');self.assertIsNone(c('CCTV4Europe'));self.assertIsNone(c('CCTV8K'));self.assertIsNone(c('CGTN'));self.assertIsNone(c('Phoenix'))
    def test_every_target(self):
        for name in TARGETS:self.assertEqual(c(name),name)
    def test_default_selection_exact_url_dedup(self):
        a,b=PROVINCIAL[:2];picked,missing=select_representatives([(a,'https://example.com/same'),(b,'https://example.com/same')]);self.assertEqual(len(picked),1);self.assertIn(b,missing)
    def test_inventory_keeps_headers_and_sources(self):
        name=PROVINCIAL[0];rows=inventory.build_inventory([('public.playlist','https://example.com/list',name+',https://example.com/live\n')]);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['station'],name);self.assertEqual(rows[0]['sources'][0]['subscription_url'],'https://example.com/list')
    def test_coverage_cannot_turn_basic_or_warning_into_strict(self):
        station=PROVINCIAL[0];ch={'name':station,'relative_only':True,'evidence':{'identity_verified':True,'sustained_seconds':180,'playable_sustained_sample':True,'sustained_qualified':False}};group={'channels':[ch],'target_coverage':{'relative':[station]}}
        validate_target_coverage(group);group['target_coverage']={'strict':[station]}
        with self.assertRaises(ValueError):validate_target_coverage(group)
        group['target_coverage']={'relative':[station,station]}
        with self.assertRaises(ValueError):validate_target_coverage(group)
        group['target_coverage']={'relative':[station],'basic_only':[station]}
        with self.assertRaises(ValueError):validate_target_coverage(group)
    def test_live_default_worker_budget(self):
        from check_sources import default_workers
        self.assertEqual(default_workers(live=True),2)
        self.assertEqual(default_workers(full=True,live=True),2)
        self.assertEqual(default_workers(),16)
    def test_basic_only_requires_pending_and_identity(self):
        station=ADDITIONAL[0];ch={'name':station,'relative_only':True,'evidence':{'identity_verified':True,'basic_qualified':True,'long_test_pending':True}};group={'channels':[ch],'target_coverage':{'basic_only':[station]}};validate_target_coverage(group);ch['evidence']['identity_verified']=False
        with self.assertRaises(ValueError):validate_target_coverage(group)
if __name__=='__main__':unittest.main()
