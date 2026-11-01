import unittest
from hongguo_lite.name_policy import resolve
class NamePolicyTests(unittest.TestCase):
    def test_matching_name(self):
        self.assertEqual(resolve('Lin Feng',['林风','林峰'],'林风',.99)['decision'],'MATCH_KEEP')
    def test_mismatch_uses_ocr(self):
        result=resolve('Fu Kui',['付奎'],'傅明',.95,'Fu Ming')
        self.assertEqual(result['english'],'Fu Ming');self.assertTrue(result['changed'])
    def test_unreadable_keeps_previous(self):
        self.assertEqual(resolve('Nana',['娜娜'])['english'],'Nana')
    def test_readable_conflict_cannot_silently_fallback(self):
        with self.assertRaises(ValueError):resolve('Fu Kui',['付奎'],'傅明',.95)
if __name__=='__main__':unittest.main()
