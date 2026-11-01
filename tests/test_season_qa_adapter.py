import sys
import subprocess
import unittest
import tempfile
from pathlib import Path
import numpy as np

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'tools'))
from season_package import balanced
from hongguo_lite.media import _final_decode
from runtime_fixture import environment


class QAAdapterTests(unittest.TestCase):
    def test_integrated_full_decode_handles_long_selection_and_native_audio(self):
        ffmpeg=environment()['ffmpeg']
        wanted=list(range(0,300,2))+list(range(301,390))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);video=root/'test.mp4'
            subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=size=16x16:rate=30:duration=13','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=13','-c:v','libx264','-preset','ultrafast','-c:a','aac','-ac','2',str(video)],check=True,capture_output=True)
            receipt,images=_final_decode(video,390,'30',624000,wanted,root/'decode',{'ffmpeg':ffmpeg})
            self.assertEqual(receipt['status'],'PASS')
            self.assertEqual(receipt['video_frames'],390)
            self.assertEqual(receipt['all_frame_PTS'],'PASS')
            self.assertEqual([r['frame'] for r in receipt['sample_images']],wanted)
            self.assertEqual(len(images),239)
            self.assertGreater(receipt['audio_rms'],0)
            self.assertLess(receipt['aac_padding_samples'],1024)

    def test_balanced_long_selection_keeps_exact_frames(self):
        ffmpeg=environment()['ffmpeg']
        wanted=list(range(0,300,2))+list(range(301,390))
        expr=balanced([f'eq(n,{n})' for n in wanted])
        result=subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=size=16x16:rate=30:duration=13','-vf',f"select='{expr}'",'-fps_mode','vfr','-f','framemd5','-'],capture_output=True,text=True,check=True)
        rows=[line for line in result.stdout.splitlines() if line and not line.startswith('#')]
        self.assertEqual([int(row.split(',')[2]) for row in rows],wanted)

    def test_vector_reducer_matches_scalar_and_rejects_nonfinite(self):
        values=np.random.default_rng(42).uniform(-1,1,10000).astype(np.float32)
        scalar_peak=max(abs(float(v)) for v in values);scalar_energy=sum(float(v)*float(v) for v in values);measured=values.astype(np.float64)
        self.assertEqual(float(np.abs(measured).max()),scalar_peak)
        self.assertAlmostEqual(float(np.dot(measured,measured)),scalar_energy,places=9)
        for bad in [float('nan'),float('inf'),-float('inf')]:
            measured[3]=bad;self.assertFalse(np.isfinite(measured).all())


if __name__=='__main__':unittest.main()
