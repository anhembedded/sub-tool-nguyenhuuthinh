import importlib.util
import unittest
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('season_capcut', Path(__file__).resolve().parents[1]/'tools/season_capcut.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.bounds = [dict(episode=1,start_us=0,end_us=1000000),dict(episode=2,start_us=1000000,end_us=2000000)]

    def test_boundary_exact_and_local_clock(self):
        parts, anomalies = runner.partition([(0,1000000,'one'),(1000000,2000000,'two')],self.bounds)
        self.assertEqual(anomalies, [])
        self.assertEqual(parts[2][0]['start_us'],0)
        self.assertEqual(parts[2][0]['end_us'],1000000)

    def test_crossing_preserved_once_for_review(self):
        parts, anomalies = runner.partition([(900000,1100000,'crossing')],self.bounds)
        self.assertEqual(sum(map(len,parts.values())),0)
        self.assertEqual(len(anomalies),1)
        self.assertEqual(anomalies[0]['text'],'crossing')

    def test_duplicate_overlap_short_and_outside(self):
        _, anomalies = runner.partition([(0,33333,'a'),(0,33333,'a'),(1900000,2100000,'outside')],self.bounds)
        self.assertIn('SHORT_CUE',anomalies[0]['reasons'])
        self.assertIn('DUPLICATE',anomalies[1]['reasons'])
        self.assertIn('OVERLAP',anomalies[1]['reasons'])
        self.assertIn('CROSSING_OR_OUTSIDE_BOUNDARY',anomalies[2]['reasons'])

    def test_modified_source_blocks_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root/'episode.mp4'
            source.write_bytes(b'original verified bytes')
            probe={'streams':[dict(codec_type='video',codec_name='hevc',width=1920,height=1080,pix_fmt='yuv420p',avg_frame_rate='30/1',r_frame_rate='30/1')]}
            row = dict(episode=1,vid='123',path=str(source),status='ready',validation=dict(fullDecodeExitCode=0,sha256=runner.sha(source),probe=probe))
            runner.atomic(root/'download/download_report.json',dict(status='complete',seriesId='42',seriesEpisodeCount=1,episodes=[row]))
            runner.sources(root,dict(series_id='42',episodes=[1]))
            source.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'Invalid source'):
                runner.sources(root,dict(series_id='42',episodes=[1]))

    def test_missing_or_wrong_whole_season_scope_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            runner.atomic(root/'download/download_report.json',dict(status='complete',seriesId='42',seriesEpisodeCount=64,episodes=[]))
            with self.assertRaisesRegex(ValueError,'Catalog episode count'):
                runner.sources(root,dict(series_id='42',episodes=list(range(1,66))))

    def test_native_timestamp_rounding(self):
        self.assertEqual(runner.stamp(1000000/30),'00:00:00,033')
        self.assertEqual(runner.episodes('1-65'),list(range(1,66)))


if __name__ == '__main__':
    unittest.main()
