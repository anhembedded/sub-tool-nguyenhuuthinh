import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from hongguo_lite.edge_fit import trim_outer_silence,tempo_filter

class OuterSilenceTests(unittest.TestCase):
    def test_active_samples_preserved_with_margins(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);samples=array('h',[0]*8000+[1000,-1000]*1000+[0]*14000)
            with wave.open(str(p/'raw.wav'),'wb') as w:w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(samples.tobytes())
            output,info=trim_outer_silence(p/'raw.wav',p/'trim.wav')
            with wave.open(str(output),'rb') as w:actual=array('h',w.readframes(w.getnframes()))
            self.assertEqual(len(actual),2000+2*3840)
            self.assertEqual(list(n for n in actual if n),list(n for n in samples if n))
            self.assertEqual(info['removed_leading_samples'],4160)
            self.assertEqual(info['removed_trailing_samples'],10160)
    def test_tempo_chain_keeps_continuous_ratio(self):
        self.assertEqual(tempo_filter(3.5),'atempo=2.0,atempo=1.75')
        self.assertEqual(tempo_filter(1.13),'atempo=1.13')

if __name__=='__main__':unittest.main()
