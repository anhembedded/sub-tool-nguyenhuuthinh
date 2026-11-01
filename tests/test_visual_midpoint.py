import json
import tempfile
import unittest
from pathlib import Path
from hongguo_lite.common import atomic,sha
from hongguo_lite.visual_midpoint import record_review,CHECKS

class MidpointReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ['video.mp4','layout.json','frame.png']:(self.root/name).write_bytes(b'synthetic-fixture')
        self.path=self.root/'review.json'
        atomic(self.path,dict(video=str(self.root/'video.mp4'),video_sha256=sha(self.root/'video.mp4'),layout=str(self.root/'layout.json'),layout_sha256=sha(self.root/'layout.json'),items=[dict(episode=2,cue_id='cue-1',timestamp_seconds=1.5,frame=45,image=str(self.root/'frame.png'),image_sha256=sha(self.root/'frame.png'),layout_errors=[])]))
    def decisions(self):return {'cue-1':dict(checks={k:True for k in CHECKS})}
    def test_every_cue_required(self):
        with self.assertRaises(ValueError):record_review(self.path,{},'test')
    def test_failure_carries_episode_cue_timestamp(self):
        d=self.decisions();d['cue-1']['checks']['chinese_covered']=False;d['cue-1']['note']='Expand blur box to cover source subtitle'
        immutable=sha(self.path)
        r=record_review(self.path,d,'synthetic test',output_path=self.root/'decisions.json')
        self.assertEqual(r['status'],'FAIL');self.assertEqual(r['errors'][0]['episode'],2)
        self.assertEqual(r['errors'][0]['timestamp_seconds'],1.5)
        self.assertEqual(sha(self.path),immutable)
    def test_changed_render_rejected(self):
        (self.root/'video.mp4').write_bytes(b'new-render')
        with self.assertRaises(ValueError):record_review(self.path,self.decisions(),'test')
    def test_non_boolean_cannot_approve(self):
        d=self.decisions();d['cue-1']['checks']['english_correct']='PASS'
        with self.assertRaises(ValueError):record_review(self.path,d,'test')

if __name__=='__main__':unittest.main()
