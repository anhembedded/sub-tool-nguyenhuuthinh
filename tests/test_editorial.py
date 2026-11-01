"""Fresh-film handoff, actual approval and zero-name regressions; no network/GPU."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
from hongguo_lite.common import atomic, load, sha
import season_editorial as editorial
import season_name_ocr as ocr
import season_visual_review as review


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'fresh-film'
        self.root.mkdir()
        self.export = self.root / 'export-new'
        self.export.mkdir()
        self.ffmpeg = self.root / 'fixture-ffmpeg'
        self.ffmpeg.write_bytes(b'fixture-only')
        self.env = dict(ffmpeg=str(self.ffmpeg), gpu_lock=str(self.root / 'gpu.lock'),
                        ocr_det_model=str(self.root / 'det-model'), ocr_rec_model=str(self.root / 'rec-model'),
                        ocr_dll_directories=[])
        self.rows = []
        for ep, frames in [(1, 60), (2, 90)]:
            p = self.root / f'source-{ep}.mp4'
            p.write_bytes(f'new-film-source-{ep}'.encode())
            self.rows.append(dict(episode=ep, status='ready', path=str(p),
                 validation=dict(sha256=sha(p), fullDecodeExitCode=0, frameCount=frames, frameRate='30',
                      probe=dict(streams=[dict(codec_type='video', width=1920, height=1080)]))))
        atomic(self.root / 'manifest.json', dict(series_id='90000002', episodes=[1, 2],
               environment=self.env, latest_export=str(self.export)))
        atomic(self.root / 'download/download_report.json', dict(status='complete', seriesId='90000002', episodes=self.rows))
        atomic(self.root / 'runtime-lock.json', dict(files={}))
        (self.export / 'raw.zh.srt').write_text('1\n00:00:00,100 --> 00:00:00,800\n你好\n', encoding='utf-8')
        atomic(self.export / 'saved-draft.json', dict(raw='new-film'))
        atomic(self.export / 'episode001.json', dict(episode=1, cues=[
            dict(cue_id=1, source_text='你好', start_us=100000, end_us=800000),
            dict(cue_id=2, source_text='啊', start_us=1000000, end_us=1500000)]))
        atomic(self.export / 'episode002.json', dict(episode=2, cues=[
            dict(cue_id=3, source_text='再见', start_us=100000, end_us=900000)]))
        atomic(self.export / 'extraction-report.json', dict(status='READY_FOR_CONTEXT_TRANSLATION', cue_count=3,
            source_report_sha256=sha(self.root / 'download/download_report.json'),
            sources={str(r['episode']): r['validation']['sha256'] for r in self.rows},
            boundaries=[dict(episode=1, start_us=0, end_us=2000000), dict(episode=2, start_us=2000000, end_us=5000000)],
            anomalies=[], timeline_anomalies=[], missing_episodes=[], draft_sha256=sha(self.export / 'saved-draft.json'),
            raw_srt_sha256=sha(self.export / 'raw.zh.srt'), files={p.name: sha(p) for p in self.export.iterdir()}))

    def filled_draft(self, names=False):
        editorial.editorial_export(self.root)
        draft = load(self.root / 'editorial-draft.json')
        proof = dict(path=str(self.export / 'raw.zh.srt'), sha256=sha(self.export / 'raw.zh.srt'))
        draft.update(reviewer='Editor Test', evidence=[proof])
        for index, cue in enumerate(draft['cues']):
            cue.update(action='KEEP', classification='DIALOGUE', canonical_text=f'Line {index + 1}.')
        if names:
            draft['cues'][0].update(canonical_text='Hello, Alice.', proper_names=['Alice'])
            draft['glossary'] = {'Alice': {'source_variants': ['爱丽丝'], 'english_aliases': []}}
        atomic(self.root / 'editorial-draft.json', draft)
        return draft

    def imported(self, names=False):
        self.filled_draft(names)
        editorial.editorial_import(self.root, self.root / 'editorial-draft.json')

    def fake_ffmpeg(self, argv, **kwargs):
        count = int(argv[argv.index('-frames:v') + 1])
        for index in range(1, count + 1):
            p = Path(argv[-1] % index)
            image = Image.new('RGB', (1920, 1080), 'navy')
            ImageDraw.Draw(image).rectangle((20, 20, 1900, 120), fill='yellow')
            image.save(p)
        return SimpleNamespace(returncode=0)

    def geometry(self):
        with patch.object(editorial.subprocess, 'run', side_effect=self.fake_ffmpeg):
            result = editorial.geometry_prepare(self.root)
        draft = load(result['draft'])
        draft['reviewer'] = 'Geometry Reviewer'
        for entry in draft['episodes'].values():
            entry.update(reviewed=True, note='Viewed the extracted native source samples.',
                         blur_box=dict(x=20, y=20, width=1880, height=100))
        atomic(result['draft'], draft)
        editorial.geometry_import(self.root, result['draft'])
        return result

    def name_ocr(self):
        paddle = SimpleNamespace(is_compiled_with_cuda=lambda: True,
                  device=SimpleNamespace(get_device=lambda: 'gpu:0', cuda=SimpleNamespace(
                      device_count=lambda: 1, get_device_name=lambda: 'fixture GPU')))
        engine = SimpleNamespace(predict=lambda p: [SimpleNamespace(json={'rec_texts': ['爱丽丝'], 'rec_scores': [0.99]})])
        with patch.object(ocr.subprocess, 'run', side_effect=self.fake_ffmpeg), patch.dict(os.environ), \
             patch.dict(sys.modules, {'paddle': paddle, 'paddleocr': SimpleNamespace(PaddleOCR=lambda **kw: engine)}):
            ocr.run(self.root)
        report = Path(load(self.root / 'active-name-ocr.json')['report'])
        draft = load(report.parent / 'decisions-draft.json')
        draft['reviewer'] = 'Name Reviewer'
        for item in draft['decisions']:
            item.update(decision='MATCH_KEEP', readable=True, reason='Actually reviewed native source name.')
        atomic(report.parent / 'decisions-draft.json', draft)
        return report.parent / 'decisions-draft.json'

    def video_generation(self):
        run = self.root / 'video-generations' / 'fixture'
        run.mkdir(parents=True)
        atomic(run / 'video-complete.json', dict(status='AWAITING_ENCODED_NAME_VISUAL_REVIEW'))
        atomic(run / 'dependency-snapshot.json', {str(self.root / 'translation/episode001.json'): sha(self.root / 'translation/episode001.json')})
        atomic(self.root / 'active-video-generation.json', dict(identity='fixture-generation', run=str(run)))
        cues = [c for ep in [1, 2] for c in load(self.root / 'translation' / f'episode{ep:03d}.json')['cues'] if c['proper_names']]
        for cue in cues:
            video = run / 'episode.mp4'
            video.write_bytes(b'encoded-video-fixture')
            layout = run / 'layout.json'
            atomic(layout, dict(cues=[cue]))
            image = run / 'encoded-name-frame.png'
            Image.new('RGB', (1920, 1080), 'purple').save(image)
            item = dict(episode=cue['episode'], cue_id=cue['cue_id'], frame=10,
                        canonical_text=cue['canonical_text'], image=str(image), image_sha256=sha(image),
                        layout_errors=[], checks={k: 'PENDING' for k in review.CHECKS}, status='AWAITING_VISUAL_REVIEW')
            atomic(run / f"ep{cue['episode']:03d}-name-frames.json", dict(video=str(video), video_sha256=sha(video),
                   layout=str(layout), layout_sha256=sha(layout), items=[item]))
        return run

    def test_export_uses_native_scope_global_frames_and_empty_glossary(self):
        result = editorial.editorial_export(self.root)
        plan = load(self.root / 'input.json')
        self.assertEqual(result['source_cues'], 3)
        self.assertEqual(plan['total_frames'], 150)
        self.assertEqual(plan['cues'][2]['start_frame'], 63)
        self.assertEqual(plan['cues'][2]['source_cue_id'], 3)
        self.assertEqual(load(self.root / 'editorial-draft.json')['glossary'], {})
        with self.assertRaises(FileExistsError):
            editorial.editorial_export(self.root)

    def test_import_requires_reviewer_and_exact_raw_coverage(self):
        draft = self.filled_draft()
        draft['reviewer'] = ''
        atomic(self.root / 'editorial-draft.json', draft)
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            editorial.editorial_import(self.root, self.root / 'editorial-draft.json')
        draft['reviewer'] = 'Editor Test'
        draft['cues'].pop()
        atomic(self.root / 'editorial-draft.json', draft)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            editorial.editorial_import(self.root, self.root / 'editorial-draft.json')
        self.assertFalse((self.root / 'name-decisions.json').exists())

    def test_import_preserves_lineage_and_requires_exclusion_evidence(self):
        draft = self.filled_draft()
        draft['cues'][0]['source_text'] = 'rewritten raw source'
        atomic(self.root / 'editorial-draft.json', draft)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            editorial.editorial_import(self.root, self.root / 'editorial-draft.json')
        draft['cues'][0]['source_text'] = '你好'
        draft['cues'][1].update(action='EXCLUDE', classification='NONVERBAL', reason='Standalone grunt.')
        atomic(self.root / 'editorial-draft.json', draft)
        with self.assertRaisesRegex(ValueError, 'evidence'):
            editorial.editorial_import(self.root, self.root / 'editorial-draft.json')
        draft['cues'][1]['evidence'] = draft['evidence']
        atomic(self.root / 'editorial-draft.json', draft)
        editorial.editorial_import(self.root, self.root / 'editorial-draft.json')
        self.assertEqual(load(self.root / 'dialogue-dispositions.json')['omitted'], 1)
        self.assertEqual(len(load(self.root / 'input.json')['cues']), 3)
        self.assertEqual(len(load(self.root / 'translation/episode001.json')['cues']), 1)

    def test_export_rejects_changed_raw_or_non_native_boundaries(self):
        report_path = self.export / 'extraction-report.json'
        report = load(report_path)
        report['boundaries'][1]['start_us'] += 100000
        atomic(report_path, report)
        with self.assertRaisesRegex(ValueError, 'native frame clock'):
            editorial.editorial_export(self.root)
        self.assertFalse((self.root / 'input.json').exists())

    def test_another_film_export_cannot_initialize_editorial_lineage(self):
        report_path = self.export / 'extraction-report.json'
        report = load(report_path)
        report['sources']['1'] = 'hash-from-a-different-film'
        atomic(report_path, report)
        with self.assertRaisesRegex(ValueError, 'fresh film source ledger'):
            editorial.editorial_export(self.root)
        self.assertFalse((self.root / 'input.json').exists())

    def test_zero_names_needs_no_gpu_and_records_not_required(self):
        self.imported()
        with patch.dict(sys.modules, {'paddle': None, 'paddleocr': None}), patch.object(ocr, 'wait_gpu', side_effect=AssertionError('GPU must not initialize')):
            self.assertEqual(ocr.run(self.root)['status'], 'NOT_REQUIRED')
        self.assertEqual(editorial.names_import(self.root)['status'], 'NOT_REQUIRED')
        run = self.video_generation()
        self.assertEqual(review.prepare(self.root)['status'], 'NOT_REQUIRED')
        self.assertEqual(load(run / 'name-visual-review.json')['items'], [])

    def test_names_import_rejects_unreviewed_canonical_edits_even_without_names(self):
        self.imported()
        path = self.root / 'translation/episode001.json'
        doc = load(path)
        doc['cues'][0]['canonical_text'] = 'Changed without a reviewed import.'
        atomic(path, doc)
        with self.assertRaisesRegex(ValueError, 'Canonical changed after editorial import'):
            editorial.names_import(self.root)
        self.assertEqual(load(self.root / 'name-decisions.json')['status'], 'AWAITING_NAME_DECISIONS')

    def test_geometry_requires_actual_review_all_episodes_and_evidence(self):
        self.imported()
        with patch.object(editorial.subprocess, 'run', side_effect=self.fake_ffmpeg):
            result = editorial.geometry_prepare(self.root)
        draft = load(result['draft'])
        draft['reviewer'] = 'Geometry Reviewer'
        atomic(result['draft'], draft)
        with self.assertRaisesRegex(ValueError, 'actual-review'):
            editorial.geometry_import(self.root, result['draft'])
        for entry in draft['episodes'].values():
            entry.update(reviewed=True, note='Viewed real source samples.', blur_box=dict(x=20, y=20, width=1880, height=100))
        atomic(result['draft'], draft)
        editorial.geometry_import(self.root, result['draft'])
        self.assertEqual(set(load(self.root / 'geometry.json')), {'1', '2'})
        self.assertTrue(load(self.root / 'geometry.json')['2']['verified'])
        image = Path(draft['episodes']['1']['evidence'][0]['path'])
        image.write_bytes(b'corrupted evidence')
        with self.assertRaisesRegex(ValueError, 'Evidence changed'):
            editorial.geometry_import(self.root, result['draft'])

    def test_geometry_custom_native_frames_extend_defaults_and_bind_request(self):
        self.imported()
        request = self.root / 'geometry-extra.json'
        atomic(request, {'additional_frames': {'1': [15, 42]}})
        with patch.object(editorial.subprocess, 'run', side_effect=self.fake_ffmpeg):
            result = editorial.geometry_prepare(self.root, request)
        report = load(result['report'])
        by_episode = {row['episode']: row for row in report['samples']}
        self.assertEqual({p['frame'] for p in by_episode[1]['evidence']}, {13, 15, 37, 42})
        self.assertEqual({p['frame'] for p in by_episode[2]['evidence']}, {15})
        self.assertEqual(report['selection_input_sha256'], sha(request))
        self.assertEqual(sha(report['selection_snapshot']), sha(request))
        self.assertFalse(load(result['draft'])['episodes']['1']['verified'])

    def test_geometry_custom_frames_reject_wrong_scope_bounds_and_boolean(self):
        self.imported()
        request = self.root / 'geometry-extra.json'
        for invalid in [{'3': [1]}, {'1': [-1]}, {'1': [60]}, {'1': [True]}, {'1': [15, 15]}, {'1': '15'}]:
            with self.subTest(frames=invalid):
                atomic(request, {'additional_frames': invalid})
                with patch.object(editorial.subprocess, 'run') as ffmpeg:
                    with self.assertRaises(ValueError):
                        editorial.geometry_prepare(self.root, request)
                    ffmpeg.assert_not_called()
        self.assertFalse((self.root / 'geometry-evidence').exists())

    def test_name_ocr_uses_measured_roi_and_explicit_decisions(self):
        self.imported(names=True)
        self.geometry()
        decision_path = self.name_ocr()
        report = load(load(self.root / 'active-name-ocr.json')['report'])
        self.assertEqual(report['items'][0]['crop_box']['y'], 20)
        with Image.open(decision_path.parent / 'cc-00001-band.png') as image:
            self.assertEqual(image.size, (1880, 100))
        result = editorial.names_import(self.root, decision_path)
        self.assertEqual(result['status'], 'APPLIED_BEFORE_TTS')
        decisions = load(self.root / 'name-decisions.json')
        self.assertEqual(len(decisions['canonical_snapshot']), 2)
        self.assertTrue(all(c['translation_status'] == 'CONTEXTUAL_CANONICAL' for c in load(self.root / 'translation/episode001.json')['cues']))

    def test_name_decision_coverage_and_image_hash_are_enforced(self):
        self.imported(names=True)
        self.geometry()
        decision_path = self.name_ocr()
        doc = load(decision_path)
        doc['decisions'] = []
        atomic(decision_path, doc)
        with self.assertRaisesRegex(ValueError, 'exactly one decision'):
            editorial.names_import(self.root, decision_path)

    def test_name_mismatch_updates_english_and_glossary_without_rewriting_raw(self):
        self.imported(names=True)
        self.geometry()
        decision_path = self.name_ocr()
        doc = load(decision_path)
        doc['decisions'][0].update(decision='MISMATCH_USE_OCR', observed_name='爱丽莎',
                                   canonical_text='Hello, Alicia.', proper_names=['Alicia'])
        doc['glossary']['Alice']['source_variants'].append('爱丽莎')
        doc['glossary']['Alice']['english_aliases'].append('Alicia')
        atomic(decision_path, doc)
        editorial.names_import(self.root, decision_path)
        cue = load(self.root / 'translation/episode001.json')['cues'][0]
        self.assertEqual(cue['canonical_text'], 'Hello, Alicia.')
        self.assertEqual(cue['source_text'], '你好')
        self.assertEqual(cue['start_frame'], 3)
        self.assertEqual(load(self.root / 'glossary.json')['Alice']['english_aliases'], ['Alicia'])

    def test_review_needs_explicit_viewer_and_rejects_tampered_contacts(self):
        self.imported(names=True)
        self.geometry()
        decision_path = self.name_ocr()
        editorial.names_import(self.root, decision_path)
        run = self.video_generation()
        prepared = review.prepare(self.root)
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            review.record(self.root, prepared['batch'], [0], 'Viewed source and encoded caption.')
        with self.assertRaisesRegex(ValueError, 'nonnegative'):
            review.record(self.root, prepared['batch'], [-1], 'Actually viewed.', 'Visual Reviewer')
        approved = review.record(self.root, prepared['batch'], [0], 'Actually viewed the listed encoded contact.', 'Visual Reviewer')
        self.assertEqual(approved['status'], 'PASS')
        self.assertEqual(approved['items'][0]['reviewer'], 'Visual Reviewer')
        self.assertEqual(review.prepare(self.root)['unseen'], 0)
        contact = Path(load(Path(prepared['batch']) / 'batch.json')['contacts'][0]['path'])
        contact.write_bytes(b'tampered contact')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            review.record(self.root, prepared['batch'], [0], 'Viewed.', 'Visual Reviewer')


if __name__ == '__main__':
    unittest.main()
