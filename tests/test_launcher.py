import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'tools'))
import season
import season_capcut
from hongguo_lite.common import atomic,config


class LauncherTests(unittest.TestCase):
    def test_config_expands_role_paths_and_arrays_after_relocation(self):
        with tempfile.TemporaryDirectory(prefix='workflow config ') as temp:
            root=Path(temp);p=root/'config.json'
            atomic(p,dict(env={'ffmpeg':'runtime/ffmpeg.exe','ocr_dll_directories':['runtime/bin','${PROJECT}/vendor']},preset='${PROJECT}/config/preset.json'))
            cfg=config(p)
            self.assertEqual(cfg['env']['ffmpeg'],str(root/'runtime/ffmpeg.exe'))
            self.assertEqual(cfg['env']['ocr_dll_directories'][0],str(root/'runtime/bin'))
            self.assertEqual(Path(cfg['preset']),PROJECT/'config/preset.json')

    def test_new_series_runs_with_same_suffix_have_different_drafts(self):
        m={'series_id':'42','episodes':[1,2,3]}
        self.assertNotEqual(season_capcut.draft_name(Path('20261025-v001'),m),season_capcut.draft_name(Path('20261026-v001'),m))

    def test_only_whole_series_scope_is_supported(self):
        for value in ['2-3','0-5','4-1','1-10000']:
            with self.assertRaises(ValueError):season_capcut.episodes(value)

    def test_resume_completed_run_cannot_launch_or_modify(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);atomic(root/'completion-status.json',{'status':'DRAFT_DELIVERED'})
            before=(root/'completion-status.json').read_bytes()
            with patch.object(sys,'argv',['season.py','resume','--run',str(root)]),patch.object(season.subprocess,'run') as run,redirect_stdout(io.StringIO()) as output:
                season.main()
            run.assert_not_called();self.assertIn('DRAFT_DELIVERED',output.getvalue())
            self.assertEqual(before,(root/'completion-status.json').read_bytes())
            self.assertEqual(sorted(p.name for p in root.iterdir()),['completion-status.json'])

    def test_completed_run_rejects_mutation_before_logging(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);atomic(root/'completion-status.json',{'status':'DRAFT_DELIVERED'})
            with patch.object(sys,'argv',['season.py','download','--run',str(root)]):
                with self.assertRaisesRegex(ValueError,'immutable'):season.main()
            self.assertFalse((root/'executions').exists())

    def test_stage_runtime_roles_are_independent_of_directory_names(self):
        files={'X:/private/node.exe':'n','X:/private/gpu/python.exe':'g','X:/private/ffmpeg.exe':'f'}
        roles={list(files)[0]:['source'],list(files)[1]:['ocr'],list(files)[2]:['media']}
        self.assertEqual(season.runtime_files_for_stage('speech',files,roles),{list(files)[2]:'f'})
        self.assertEqual(season.runtime_files_for_stage('ocr',files,roles),{list(files)[1]:'g',list(files)[2]:'f'})

    def test_revision_does_not_credit_stale_generation_or_block_new_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);gen=root/'generations/old';gen.mkdir(parents=True)
            changed=root/'canonical.json';changed.write_text('new canonical')
            atomic(gen/'dependency-snapshot.json',{str(changed):'old hash'})
            atomic(root/'active-generation.json',{'run':str(gen),'identity':'old'})
            self.assertEqual(season.verified_credit(root),0)

    def test_saved_caption_parser_preserves_text_and_does_not_infer(self):
        data={'materials':{'texts':[{'id':'t','content':json.dumps({'text':'你好'})}]},'tracks':[{'type':'text','segments':[{'material_id':'t','target_timerange':{'start':0,'duration':1000000}}]}]}
        self.assertEqual(season_capcut.caption_cues(data)[0][2],[(0,1000000,'你好')])
        data['tracks'][0]['segments'][0]['target_timerange']['start']=True
        with self.assertRaises(ValueError):season_capcut.caption_cues(data)


if __name__=='__main__':unittest.main()
