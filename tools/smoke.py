"""Run real media/GPU/service checks on synthetic content, never on a film."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import config,load,atomic,sha,utc
from hongguo_lite import source,media,speech,edge_fit


def execute(argv,out,name,timeout=7200):
    out.mkdir(parents=True,exist_ok=True)
    with (out/(name+'.stdout.log')).open('wb') as so,(out/(name+'.stderr.log')).open('wb') as se:
        p=subprocess.run(list(map(str,argv)),stdout=so,stderr=se,timeout=timeout,env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
    if p.returncode:raise RuntimeError(f'{name} failed ({p.returncode}); inspect {out/(name+".stderr.log")}')


def synthetic_media(root,env,preset):
    """Two unrelated synthetic series, with different episode/cue/seam counts."""
    import numpy as np
    import wave
    from PIL import Image,ImageDraw,ImageFont
    root.mkdir(parents=True)
    poster=root/'source.png';im=Image.new('RGB',(1920,1080),'#203040')
    ImageDraw.Draw(im).text((110,895),'SMOKE 123',font=ImageFont.truetype(env['font'],48),fill='white');im.save(poster)
    native=root/'synthetic-hevc.mp4'
    execute([env['ffmpeg'],'-v','error','-nostdin','-n','-loop','1','-i',poster,'-f','lavfi','-i','sine=frequency=330:sample_rate=48000',
             '-map','0:v:0','-map','1:a:0','-vf','fps=30','-frames:v','30','-t','1','-c:v','libx265','-preset','ultrafast',
             '-x265-params','pools=1:frame-threads=2:log-level=error','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2',native],root/'logs','fixture')
    native_source=source.probe(native,env)
    if (native_source['width'],native_source['height'],native_source['frames'],native_source['fps'])!=(1920,1080,30,'30'):raise ValueError('Synthetic native source failed')
    raw=root/'voice-native.wav'
    data=(np.sin(np.arange(9600)*2*np.pi*660/48000)*4000).astype('<i2')
    with wave.open(str(raw),'wb') as w:
        w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(data.tobytes())
    # Synthetic distinguishable SFX is independent from the separate real MVSEP smoke.
    sf=root/'sfx-float48.wav';_,core=media._core()
    import struct
    values=(np.sin(np.arange(48000)*2*np.pi*440/48000)*.025).astype('<f4')
    stereo=np.column_stack([values,values]).astype('<f4')
    payload=stereo.tobytes()
    sf.write_bytes(b'RIFF'+struct.pack('<I',36+len(payload))+b'WAVEfmt '+struct.pack('<IHHIIHH',16,3,2,48000,384000,8,32)+b'data'+struct.pack('<I',len(payload))+payload)
    checks=[]
    for total in [2,3]:
        film=root/f'synthetic-series-{total}';film.mkdir();episodes=[]
        for ep in range(1,total+1):
            out=film/f'ep{ep:03d}';out.mkdir();src={**native_source,'episode':ep}
            cues=[dict(cue_id=f'smoke-{total}-{ep}',canonical_text=f'Synthetic episode {ep}.',start_frame=3,end_frame_exclusive=15,proper_names=[])]
            row=edge_fit.fit(cues[0],{'audio':str(raw)},15,30,'30',out/'fit',env,max_tempo=5,min_tempo=1.25)
            if row['status']!='PASS':raise ValueError('Measured synthetic fit failed')
            voice_dir=out/'voice';voice_dir.mkdir();voice=speech.timeline([row],src,voice_dir)
            geometry={'verified':True,'source_sha256':src['sha256'],'blur_box':{'x':64,'y':880,'width':1792,'height':120},
                      'evidence':[{'path':str(poster),'sha256':sha(poster),'frame':0,'note':'Synthetic test geometry only; no film approval'}]}
            video=media.render_video(src,cues,geometry,preset,out/'video',env)
            mixed=media.mix(src,voice['path'],sf,out/'mix',env,preset)
            episodes.append(dict(source=src,video=video,audio_master=mixed,cues=cues))
        result=media.finalize(episodes,film/'final',env,preset)
        if not result['technical_PASS'] or result['frames']!=30*total or result['pcm_samples']!=48000*total or len(result['seams'])!=total-1 or result['canonical_cues']!=total:raise ValueError('Synthetic series native/coverage QA failed')
        checks.append(dict(episodes=total,frames=result['frames'],pcm_samples=result['pcm_samples'],cues=result['canonical_cues'],seams=len(result['seams']),video_sha256=result['sha256']))
    return {'status':'PASS','series':checks,'source':native_source,'evidence_image':str(poster)}


def paddle_smoke(root,env,src):
    root.mkdir(parents=True)
    image=root/'native-frame.png'
    execute([env['ffmpeg'],'-v','error','-nostdin','-n','-i',src['path'],'-frames:v','1',image],root/'logs','frame')
    output=root/'engine';output.mkdir()
    lock=env.get('ocr_engine_lock',env['separation_engine_lock'])
    request={'mode':'ocr','source':src['path'],'source_sha256':sha(src['path']),'film_id':'synthetic-smoke',
             'output_dir':str(output),'lock_path':lock,
             'config':{'detector_dir':env['ocr_det_model'],'recognizer_dir':env['ocr_rec_model'],'dll_dirs':env['ocr_dll_directories'],
                       'cupti_library':env['cupti_library'],'max_frames':1},
             'roi':{'film_id':'synthetic-smoke','source_sha256':sha(src['path']),'x':64,'y':880,'width':1792,'height':120,
                    'verification':{'status':'VERIFIED','evidence':[{'frame_index':0,'path':str(image),'sha256':sha(image),'basis':'Synthetic known geometry, not editorial acceptance'}]}}}
    atomic(root/'request.json',request)
    execute([env['paddle_python'],PROJECT/'vendor/core/tools/fresh_v3_guard.py','--lock',env['gpu_lock'],PROJECT/'vendor/core/tools/fresh_engine_worker.py','--request',root/'request.json'],root/'logs','paddle',600)
    result=load(output/'receipt.json')
    if result['technical_status']!='PASS' or result.get('gpu_activity',{}).get('kernel_records',0)<=0:raise ValueError('Paddle model GPU execution proof missing')
    return {'status':'PASS','receipt_sha256':sha(output/'receipt.json'),'kernel_records':result['gpu_activity']['kernel_records']}


def capcut_smoke(root,env,src):
    """Create an actual scratch draft, without opening or recognizing a film."""
    root.mkdir(parents=True)
    scratch_env={**env,'capcut_project_root':str(root/'scratch-capcut-projects')}
    report={'status':'complete','seriesId':'900000001','seriesTitle':'Synthetic CapCut helper smoke','seriesEpisodeCount':1,
            'episodes':[{'episode':1,'vid':'synthetic-1','path':src['path'],'status':'ready',
                'validation':{'fullDecodeExitCode':0,'sha256':src['sha256'],'frameCount':src['frames'],
                              'probe':{'streams':[src['video_stream'],src['audio_stream']]}}}]}
    atomic(root/'run/download/download_report.json',report)
    atomic(root/'request.json',{'run':str(root/'run'),'manifest':{'series_id':'900000001','episodes':[1],'environment':scratch_env}})
    script="import sys,pathlib,json; sys.path.insert(0,sys.argv[1]); import season_capcut as c; r=c.load(sys.argv[2]); c.prepare(pathlib.Path(r['run']),r['manifest'])"
    execute([env['capcut_python'],'-X','utf8','-c',script,PROJECT/'tools',root/'request.json'],root/'logs','capcut',180)
    bounds=load(root/'run/boundaries.json')
    if len(bounds['episodes'])!=1 or bounds['episodes'][0]['frames']!=src['frames']:raise ValueError('CapCut native draft scope mismatch')
    return {'status':'PASS','draft_sha256':sha(bounds['draft']),'native_frames':src['frames'],'recognition':'NOT_RUN_MANUAL_CAPCUT_STEP_REQUIRED'}


def mvsep_smoke(root,env,preset,src):
    result=media.separate({**src,'audio_scope':'native_video'},root,env,preset['separation'])
    response=load(result['receipt'])
    if response['included_stems']!=['sfx'] or response['excluded_stems']!=['music','speech'] or response.get('gpu_activity',{}).get('kernel_records',0)<=0:raise ValueError('SFX-only GPU receipt failed')
    return {'status':'PASS','receipt_sha256':sha(result['receipt']),'included_stems':response['included_stems'],'kernel_records':response['gpu_activity']['kernel_records'],'input_samples':response['input_samples'],'output_samples':response['output_samples']}


def ava_smoke(root,env):
    root.mkdir(parents=True)
    script="import asyncio,edge_tts,sys; asyncio.run(edge_tts.Communicate('This is a synthetic workflow readiness check.','en-US-AvaMultilingualNeural',rate='+0%',volume='+0%',pitch='+0Hz').save(sys.argv[1]))"
    mp3=root/'ava.mp3'
    execute([env['edge_python'],'-X','utf8','-c',script,mp3],root/'logs','ava',120)
    wav=root/'ava-48000.wav'
    execute([env['ffmpeg'],'-v','error','-nostdin','-n','-i',mp3,'-ar','48000','-ac','1','-c:a','pcm_s16le',wav],root/'logs','ava-pcm')
    core,_=media._core();info=core.pcm_info(wav,channels=1)
    if info['samples']<=0:raise ValueError('Ava returned no audio')
    fit_script="import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from hongguo_lite.edge_fit import fit; fit(dict(cue_id='synthetic-ava',canonical_text='This is a synthetic workflow readiness check.',start_frame=0,end_frame_exclusive=300),{'audio':sys.argv[2]},300,300,'30',Path(sys.argv[3]),{'ffmpeg':sys.argv[4]},min_tempo=1.25,max_tempo=5)"
    execute([env['edge_python'],'-X','utf8','-c',fit_script,PROJECT,wav,root/'measured-fit',env['ffmpeg']],root/'logs','ava-fit',180)
    fitted=load(root/'measured-fit/fit.json')
    if fitted.get('status')!='PASS':raise ValueError('Ava isolated environment could not fit its actual speech')
    return {'status':'PASS','voice':'en-US-AvaMultilingualNeural','rate':'+0%','samples':info['samples'],'mp3_sha256':sha(mp3),'pcm_sha256':sha(wav),'isolated_edge_fit':'PASS','fit_tempo':fitted['tempo']}


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--output',type=Path);p.add_argument('--online',action='store_true');args=p.parse_args()
    cfg=config(args.config);out=(args.output or Path(cfg['env']['controller_python']).parents[2]/'validation'/f'smoke-{time.time_ns()}').resolve()
    if out.exists() and any(out.iterdir()):raise FileExistsError('Use a fresh smoke output; prior evidence is preserved')
    out.mkdir(parents=True,exist_ok=True);report={'status':'RUNNING','config_sha256':sha(args.config),'online':args.online,'begin_utc':utc(),'checks':{},'synthetic_only':True,'film_acceptance':'NOT_PERFORMED'};start=time.monotonic()
    try:
        env=cfg['env'];preset=load(cfg.get('preset',PROJECT/'config/preset.json'))
        result=synthetic_media(out/'media',env,preset);report['checks']['media']=result
        src=result['source'];report['checks']['capcut']=capcut_smoke(out/'capcut',env,src)
        report['checks']['paddle']=paddle_smoke(out/'paddle',env,src)
        report['checks']['mvsep']=mvsep_smoke(out/'mvsep',env,preset,src)
        report['checks']['ava']=ava_smoke(out/'ava',env) if args.online else {'status':'NOT_REQUESTED'}
        report['status']='PASS'
    except Exception as exc:
        report.update(status='FAIL',error_type=type(exc).__name__,error=str(exc));raise
    finally:
        report.update(end_utc=utc(),seconds=time.monotonic()-start);atomic(out/'smoke.json',report);print(str(out/'smoke.json'),flush=True)


if __name__=='__main__':main()
