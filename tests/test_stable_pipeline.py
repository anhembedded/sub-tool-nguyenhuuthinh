"""Regression checks for generic scope, recovery, SFX identity and sealed history."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from hongguo_lite.common import atomic,load,sha
from hongguo_lite import source,edge_fit,media
from hongguo_lite.store import Store
from hongguo_lite.season_contracts import validate_review,episode_map
import season_package as package
import season_delivery_audit as sealing
import season_revision as revision
import season_finish as finishing


def wav(path,samples=48000,channels=1):
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as f:
        f.setparams((channels,2,48000,0,'NONE','not compressed'));f.writeframes(bytes(samples*channels*2))
    return path


class StableContracts(unittest.TestCase):
    def test_unsupported_source_profiles_fail_before_production(self):
        profile=dict(codec_name='hevc',width=1920,height=1080,avg_frame_rate='30',r_frame_rate='30',pix_fmt='yuv420p',sample_aspect_ratio='1:1')
        self.assertEqual(source.validate_profile(profile),'30')
        for changed in [dict(width=1080,height=1920),dict(codec_name='h264'),dict(avg_frame_rate='25',r_frame_rate='25'),dict(r_frame_rate='60'),dict(color_transfer='smpte2084'),dict(pix_fmt='yuv420p10le')]:
            with self.subTest(changed=changed),self.assertRaises(ValueError):source.validate_profile({**profile,**changed})

    def test_source_cache_includes_live_mp4_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);mp4=root/'source.mp4';mp4.write_bytes(b'original')
            row=dict(path=str(mp4),sha256=sha(mp4),episode=2)
            store=Store(root/'store');inputs={'source':row['sha256']}
            with patch.object(source,'probe',return_value=dict(path=str(mp4),sha256=sha(mp4),frames=30)),patch.object(source,'command'):
                result=store.stage('source',inputs,lambda out:source.validate(row,out,{'ffmpeg':'fixture'}))
            self.assertIn(str(mp4),result['files'])
            mp4.write_bytes(b'corrupt!')
            self.assertIsNone(store.cached('source',inputs))
            with self.assertRaisesRegex(ValueError,'artifact hash changed'):store.result('source')

    def test_partial_fit_retry_preserves_failed_wav(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);raw=wav(root/'raw.wav',48000);out=root/'fit'
            cue=dict(cue_id='c',canonical_text='Hello',start_frame=0,end_frame_exclusive=30)
            def interrupted(argv,directory,**kwargs):
                Path(argv[-1]).write_bytes(b'partial');raise RuntimeError('simulated worker interruption')
            with patch.object(edge_fit,'command',side_effect=interrupted):
                with self.assertRaises(RuntimeError):edge_fit.fit(cue,{'audio':str(raw)},30,30,'30',out,{'ffmpeg':'fixture'},min_tempo=1.25,max_tempo=5)
            failed=next(out.glob('attempt-*/ratio-0.wav'));before=failed.read_bytes()
            def completed(argv,directory,**kwargs):wav(Path(argv[-1]),38000)
            with patch.object(edge_fit,'command',side_effect=completed):
                result=edge_fit.fit(cue,{'audio':str(raw)},30,30,'30',out,{'ffmpeg':'fixture'},min_tempo=1.25,max_tempo=5)
            self.assertEqual(result['status'],'PASS');self.assertEqual(failed.read_bytes(),before)
            self.assertNotEqual(Path(result['path']).parent,failed.parent)

    def test_zero_names_requires_not_required_and_named_scope_requires_real_review(self):
        episodes=[dict(cues=[dict(cue_id='a',proper_names=[])])]
        review=dict(status='NOT_REQUIRED',video_generation='v',items=[])
        validate_review(review,episodes,'v')
        with self.assertRaises(ValueError):validate_review({**review,'status':'PASS'},episodes,'v')
        episodes[0]['cues'][0]['proper_names']=['Ava']
        with self.assertRaises(ValueError):validate_review(review,episodes,'v')

    def test_malformed_review_checks_and_changed_contact_cannot_approve_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);image=root/'frame.png';contact=root/'contact.png'
            image.write_bytes(b'reviewed frame');contact.write_bytes(b'reviewed contact')
            keys=['chinese_covered','english_correct','no_clipping','max_two_lines','position_correct']
            item=dict(cue_id='named',status='PASS',checks={key:True for key in keys},reviewer='Actual reviewer',note='Viewed encoded name frame',
                      image=str(image),image_sha256=sha(image),contact=str(contact),contact_sha256=sha(contact))
            episodes=[dict(cues=[dict(cue_id='named',proper_names=['Ava'])])]
            review=dict(status='PASS',video_generation='v',items=[item])
            validate_review(review,episodes,'v')
            for malformed in [False,1,'false','PASS']:
                bad={**item,'checks':{**item['checks'],'no_clipping':malformed}}
                with self.subTest(check=malformed),self.assertRaises(ValueError):validate_review({**review,'items':[bad]},episodes,'v')
            missing={k:v for k,v in item['checks'].items() if k!='english_correct'}
            with self.assertRaises(ValueError):validate_review({**review,'items':[{**item,'checks':missing}]},episodes,'v')
            with self.assertRaises(ValueError):validate_review({**review,'items':[{**item,'reviewer':'  '}]},episodes,'v')
            contact.write_bytes(b'changed contact')
            with self.assertRaises(ValueError):validate_review(review,episodes,'v')

    def test_changed_glossary_invalidates_finished_generation_before_packaging(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);video=root/'video'
            atomic(root/'manifest.json',dict(episodes=[],environment={}))
            atomic(root/'preset.json',{});atomic(root/'active-video-generation.json',dict(identity='v',run=str(video)))
            atomic(video/'video-complete.json',{});atomic(video/'dependency-snapshot.json',{})
            atomic(root/'speech-summary.json',dict(status='PASS'))
            for name in ['dialogue-dispositions.json','glossary.json','canonical-summary.json','native-pts-qa-v002/report.json']:atomic(root/name,{})
            raw=root/'raw.srt';raw.write_text('\n',encoding='utf-8')
            atomic(root/'input.json',dict(source_srt=str(raw),source_srt_sha256=sha(raw)))
            finishing.main(root)
            generation=Path(load(root/'active-generation.json')['run'])
            self.assertEqual(load(generation/'dependency-snapshot.json')[str(root/'glossary.json')],sha(root/'glossary.json'))
            atomic(root/'glossary.json',dict(unreviewed='different film'))
            with self.assertRaisesRegex(ValueError,'Generation dependency changed'):package.preflight(root)

    def test_sfx_selection_excludes_music_and_speech_before_resample(self):
        sys.path.insert(0,str(ROOT/'vendor/core/tools'))
        spec=importlib.util.spec_from_file_location('stable_sep_worker',ROOT/'vendor/core/tools/fresh_separation_v3_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        stems=np.stack([np.full((2,9),100,dtype='float32'),np.full((2,9),.25,dtype='float32'),np.full((2,9),200,dtype='float32')])
        self.assertTrue(np.array_equal(worker.selected_background(stems,['sfx']),stems[1].T))
        with self.assertRaises(Exception):worker.selected_background(stems,['speech'])

    def test_wrong_background_selection_cannot_pass_receipt_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);model=root/'model.bin';model.write_bytes(b'checkpoint')
            output=root/'background.wav';output.write_bytes(b'float-payload')
            stems={}
            for name in ['music','sfx','speech']:
                p=root/(name+'.wav');p.write_bytes(name.encode());stems[name]=dict(path=str(p),sha256=sha(p))
            request=dict(source_sha256='source-pcm',config=dict(checkpoint=str(model),included_stems=['sfx']))
            receipt=dict(technical_status='PASS',input_samples=1600,output_samples=1600,included_stems=['sfx'],excluded_stems=['music','speech'],sources=['music','sfx','speech'],precision='FP32',engine='MVSEP-CDX23',shifts=1,overlap=.8,source_sha256='source-pcm',model_sha256=sha(model),output=str(output),output_sha256=sha(output),stems=stems)
            media.validate_separation_response(receipt,request,1600)
            with self.assertRaises(ValueError):media.validate_separation_response({**receipt,'included_stems':['music','sfx']},request,1600)
            Path(stems['sfx']['path']).write_bytes(b'changed')
            with self.assertRaises(ValueError):media.validate_separation_response(receipt,request,1600)

    def test_second_revision_retains_previous_episode_map_and_immutable_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/'video-base';base.mkdir()
            episodes=[2,3];atomic(root/'manifest.json',dict(episodes=episodes,environment={}))
            atomic(root/'preset.json',{});atomic(root/'input.json',dict(episode_boundaries=[]));atomic(root/'geometry.json',{})
            atomic(base/'dependency-snapshot.json',{});atomic(base/'video-complete.json',{})
            atomic(base/'name-visual-review.json',dict(items=[]))
            for ep in episodes:
                atomic(base/f'ep{ep:03d}-source.json',dict(episode=ep))
                atomic(root/'translation'/f'episode{ep:03d}.json',dict(cues=[]))
            first=dict(base_video=dict(identity='base',run=str(base)),changed_files={},episodes=[])
            atomic(root/'canonical-revision.json',first);revision.main(root)
            first_active=load(root/'active-video-generation.json');first_run=Path(first_active['run'])
            first_ledger_hash=sha(first_run/'revision.json');original_map=episode_map(first_run,episodes)
            second=dict(base_video=first_active,changed_files={},episodes=[])
            atomic(root/'canonical-revision.json',second);revision.main(root)
            second_run=Path(load(root/'active-video-generation.json')['run'])
            self.assertEqual(episode_map(second_run,episodes),original_map)
            self.assertEqual(sha(first_run/'revision.json'),first_ledger_hash)
            for path,h in load(first_run/'dependency-snapshot.json').items():self.assertEqual(sha(path),h)


class DeliveryRecovery(unittest.TestCase):
    def fixture(self,root):
        scope=[2,3];prod=root/'production';video=root/'video';deliveries=root/'deliveries'
        engine=root/'engine.json';atomic(engine,{'model':'fixture'})
        env=dict(deliveries_root=str(deliveries),separation_engine_lock=str(engine))
        atomic(root/'manifest.json',dict(series_id='film-other',series_title='Other film',episodes=scope,environment=env))
        atomic(root/'active-generation.json',dict(identity='production',run=str(prod)))
        atomic(root/'active-video-generation.json',dict(identity='video',run=str(video)))
        atomic(prod/'dependency-snapshot.json',{})
        atomic(prod/'build-complete.json',dict(episodes=scope,generation='production'))
        atomic(video/'name-visual-review.json',dict(status='NOT_REQUIRED',video_generation='video',items=[]))
        raw=root/'Chinese.srt';raw.write_text('\n',encoding='utf-8')
        atomic(root/'input.json',dict(source_srt=str(raw),source_srt_sha256=sha(raw)))
        preset=dict(mix=dict(background_db=-5),separation=dict(included_stems=['sfx']))
        for name in package.EVIDENCE:atomic(root/name,{} if name!='preset.json' else preset)
        episodes=[];pts=[];downloads=[]
        for ep,frames in zip(scope,[3,5]):
            src=root/f'ep{ep}.mp4';src.write_bytes(f'source-{ep}'.encode())
            source_row=dict(episode=ep,path=str(src),sha256=sha(src),frames=frames,fps='30')
            vstore=Store(video/'episode-stores'/f'ep{ep:03d}')
            def video_action(out):
                p=out/'video.mp4';p.write_bytes(b'encoded');layout=out/'layout.json';atomic(layout,dict(cues=[]))
                return dict(output=str(p),sha256=sha(p),layout=str(layout),files=[str(p),str(layout)])
            v=vstore.stage(f'ep{ep:03d}/video',{},video_action)
            def voice_action(out):
                p=wav(out/'voice.wav',frames*1600);return dict(path=str(p),sha256=sha(p),files=[str(p)])
            voice=Store(prod).stage(f'ep{ep:03d}/voice',{},voice_action)
            def mix_action(out):
                p=wav(out/'mix.wav',frames*1600,2);return dict(output=str(p),sha256=sha(p),files=[str(p)])
            mix=Store(prod).stage(f'ep{ep:03d}/mix',{},mix_action)
            def sep_action(out):
                p=wav(out/'sfx.wav',frames*1600,2);r=out/'response.json'
                atomic(r,dict(included_stems=['sfx'],output_sha256=sha(p)))
                return dict(output=str(p),sha256=sha(p),included_stems=['sfx'],receipt=str(r),files=[str(p),str(r)])
            sep=Store(root/'media-store').stage(f'ep{ep:03d}/separation',{},sep_action)
            episodes.append(dict(source=source_row,video=v,voice=voice,audio_master=mix,separation=sep,cues=[]))
            atomic(root/f'synthesis-summary-ep{ep:03d}.json',dict(rows=[]))
            atomic(root/'translation'/f'episode{ep:03d}.json',dict(cues=[]))
            pts.append(dict(episode=ep,status='PASS',source_sha256=sha(src),video_samples=frames))
            downloads.append(dict(episode=ep,validation=dict(sha256=sha(src),frameCount=frames,frameRate='30',fullDecodeExitCode=0)))
        atomic(prod/'episodes.json',episodes)
        atomic(root/'name-decisions.json',dict(status='NOT_REQUIRED',canonical_snapshot={str(root/'translation'/f'episode{ep:03d}.json'):sha(root/'translation'/f'episode{ep:03d}.json') for ep in scope}))
        atomic(root/'download/download_report.json',dict(episodes=downloads))
        atomic(root/'native-pts-qa-v002/report.json',dict(status='PASS',episodes=pts))
        def finalized(episodes,out,env,preset):
            out.mkdir(parents=True);mp4=out/'final.mp4';mp4.write_bytes(b'final-native-media')
            subtitle=out/'English.srt';subtitle.write_text('\n',encoding='utf-8');qa=out/'qa.json';atomic(qa,dict(status='PASS'))
            return dict(technical_PASS=True,frames=8,pcm_samples=12800,seams=[{}],output=str(mp4),sha256=sha(mp4),srt=str(subtitle),qa=str(qa),files=[str(mp4),str(subtitle),str(qa)])
        return deliveries,finalized

    def test_missing_evidence_fails_before_public_delivery_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);deliveries,_=self.fixture(root);(root/'glossary.json').unlink()
            with self.assertRaises(FileNotFoundError):package.main(root)
            self.assertFalse(deliveries.exists())

    def test_failed_copy_retries_and_sealed_payload_stays_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);deliveries,finalized=self.fixture(root);original_copy=package.shutil.copy2;calls=0
            def fail_copy(src,dst):
                nonlocal calls
                calls+=1
                if calls==2:raise OSError('simulated disk copy failure')
                return original_copy(src,dst)
            with patch.object(package.media,'finalize',side_effect=finalized),patch.object(package.shutil,'copy2',side_effect=fail_copy):
                with self.assertRaises(OSError):package.main(root)
            self.assertEqual([p for p in deliveries.iterdir() if p.name!='.staging'],[])
            with patch.object(package.media,'finalize',side_effect=finalized):report=package.main(root)
            delivery=Path(report['video']).parent
            self.assertEqual(report['episodes'],[2,3]);self.assertEqual(report['frames'],8)
            self.assertEqual(report['name_review_status'],'NOT_REQUIRED')
            self.assertTrue((delivery/'cue-lineage.json').is_file());self.assertTrue((delivery/'separation-engine-lock.json').is_file())
            committed={str(p):sha(p) for p in delivery.rglob('*') if p.is_file()}
            (root/'delivery.json').unlink()
            self.assertEqual(package.main(root),report)
            self.assertEqual(committed,{str(p):sha(p) for p in delivery.rglob('*') if p.is_file()})
            sealing.main(root)
            before={str(p):sha(p) for p in delivery.rglob('*') if p.is_file()}
            with self.assertRaises(ValueError):package.main(root)
            with self.assertRaises(ValueError):sealing.main(root)
            self.assertEqual(before,{str(p):sha(p) for p in delivery.rglob('*') if p.is_file()})


if __name__=='__main__':unittest.main()
