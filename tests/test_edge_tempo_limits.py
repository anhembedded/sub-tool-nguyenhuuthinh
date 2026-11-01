import math,tempfile,wave,unittest,json
from array import array
from pathlib import Path
from hongguo_lite.edge_fit import fit
from runtime_fixture import environment

class TempoLimitTests(unittest.TestCase):
    def test_measured_short_and_long_speech_respect_requested_limits(self):
        env=environment()
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            for duration,expected in [(.5,1.25),(2,2.0),(6,None)]:
                raw=base/f'raw-{duration}.wav'
                samples=array('h',(int(8000*math.sin(i*2*math.pi*220/48000)) for i in range(round(duration*48000))))
                with wave.open(str(raw),'wb') as w:
                    w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(samples.tobytes())
                cue=dict(cue_id=str(duration),canonical_text='Timing test',start_frame=0,end_frame_exclusive=30)
                row=fit(cue,{'audio':str(raw)},30,30,'30',base/f'fit-{duration}',env,min_tempo=1.25,max_tempo=5)
                if expected is None:
                    self.assertEqual(row['status'],'NEEDS_EDIT')
                else:
                    self.assertEqual(row['status'],'PASS');self.assertGreaterEqual(row['tempo'],expected)
                    self.assertLessEqual(row['tempo'],5);self.assertLessEqual(row['samples'],48000)
                    self.assertTrue(row['tempo_attempts'])
if __name__=='__main__':unittest.main()
