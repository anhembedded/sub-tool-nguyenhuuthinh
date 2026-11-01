from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from hongguo_lite.store import Store
from hongguo_lite.common import atomic, load, sha

class StoreRegression(unittest.TestCase):
    def test_partial_failure_never_reused_and_old_attempt_preserved(self):
        with TemporaryDirectory() as d:
            s=Store(d);calls=[]
            def crash(out):
                (out/'partial.wav').write_bytes(b'RIFFpartial');calls.append(out);raise RuntimeError('power-loss simulation')
            with self.assertRaises(RuntimeError):s.stage('voice',{'text':'A'},crash)
            self.assertTrue((calls[0]/'partial.wav').exists());self.assertFalse((calls[0]/'receipt.json').exists())
            def good(out):
                (out/'complete.bin').write_bytes(b'complete');return {'files':[str(out/'complete.bin')],'value':4}
            self.assertEqual(s.stage('voice',{'text':'A'},good)['value'],4)
            self.assertTrue((calls[0]/'failure.json').exists());self.assertEqual(s.state['stages']['voice']['status'],'COMPLETE')
            self.assertEqual(len(list((Path(d)/'artifacts/voice').glob('attempt-*'))),2)

    def test_tampered_content_rebuilds_even_when_result_exists(self):
        with TemporaryDirectory() as d:
            s=Store(d);calls=[]
            def make(out):
                p=out/'audio.wav';p.write_bytes(b'VALID-AUDIO');calls.append(p);return {'files':[str(p)],'audio':str(p)}
            first=s.stage('tts/a',{'text':'A'},make);s.stage('tts/a',{'text':'A'},make);self.assertEqual(len(calls),1)
            Path(first['audio']).write_bytes(b'BROKEN-DATA')
            s.stage('tts/a',{'text':'A'},make);self.assertEqual(len(calls),2)

    def test_input_version_and_config_are_cache_boundaries(self):
        with TemporaryDirectory() as d:
            s=Store(d);calls=[]
            def make(out):calls.append(out);return {'value':len(calls)}
            a=s.stage('stage',{'input':'x','engine':'v1','style':'a'},make)
            self.assertEqual(s.stage('stage',{'input':'x','engine':'v1','style':'a'},make),a)
            for field,value in [('input','y'),('engine','v2'),('style','b')]:
                sig={'input':'x','engine':'v1','style':'a'};sig[field]=value;s.stage('stage',sig,make)
            self.assertEqual(len(calls),4)

    def test_retry_is_bounded_and_preserves_failures(self):
        with TemporaryDirectory() as d:
            s=Store(d);calls=[]
            def fail(out):calls.append(out);raise RuntimeError('transient simulation')
            with self.assertRaises(RuntimeError):s.stage('download',{},fail,retries=1)
            self.assertEqual(len(calls),2)
            self.assertTrue(all((p/'failure.json').exists() for p in calls))

if __name__=='__main__':unittest.main()
