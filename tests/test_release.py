import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'tools'))
import build_release
from hongguo_lite.common import atomic


class ReleaseTests(unittest.TestCase):
    def fixture(self,root):
        src=root/'source';src.mkdir();(src/'tools').mkdir();(src/'tools/entry.py').write_text('print(1)')
        evidence=root/'evidence';evidence.mkdir();return src,evidence

    def test_nested_release_destination_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(build_release,'INCLUDE',('tools',)):
            root=Path(temp);src,evidence=self.fixture(root);out=src/'tools/release/stable.zip'
            with self.assertRaisesRegex(ValueError,'overlaps'):build_release.package(src,out,evidence,'1.1.0')
            self.assertFalse(out.parent.exists())

    def test_no_stable_claim_for_missing_or_stale_acceptance(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(build_release,'INCLUDE',('tools',)):
            root=Path(temp);src,evidence=self.fixture(root);out=root/'release.zip'
            atomic(evidence/'acceptance.json',{'status':'PASS','source_digest':'stale','version':'1.1.0','checks':{}})
            with self.assertRaisesRegex(ValueError,'final source'):build_release.package(src,out,evidence,'1.1.0')
            self.assertFalse(out.exists())

    def test_verified_archive_relocates_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(build_release,'INCLUDE',('tools',)):
            root=Path(temp);src,evidence=self.fixture(root);out=root/'release.zip'
            atomic(evidence/'fixture.json',{'fixture_only':True})
            atomic(evidence/'acceptance.json',{'status':'PASS','source_digest':build_release.source_digest(src),'version':'1.1.0','checks':{key:{'status':'PASS','fixture_only':True,'evidence':{'path':'fixture.json','sha256':build_release.sha(evidence/'fixture.json')}} for key in build_release.CHECKS}})
            result=build_release.package(src,out,evidence,'1.1.0')
            self.assertEqual(result['extracted_hashes'],'PASS');self.assertTrue(out.with_suffix('.zip.sha256').is_file())
            with self.assertRaises(FileExistsError):build_release.package(src,out,evidence,'1.1.0')


if __name__=='__main__':unittest.main()
