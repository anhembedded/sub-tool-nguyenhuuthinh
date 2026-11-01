"""Hash-bound native midpoint extraction and GPU name OCR; no approval inferred."""
import argparse
import os
import sys
import subprocess
import time
import contextlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load, atomic, sha, digest, process_lock, utc
from hongguo_lite.season_contracts import unsealed


@contextlib.contextmanager
def wait_gpu(path):
    tick=time.monotonic()
    while True:
        stack=contextlib.ExitStack()
        try:
            stack.enter_context(process_lock(path));break
        except RuntimeError:
            stack.close()
            if time.monotonic()-tick>7200:raise
            time.sleep(1)
    with stack:yield


def run(root, selection_path=None):
    root=Path(root).resolve();m=load(root/'manifest.json');env=m['environment']
    unsealed(root)
    selection_path=Path(selection_path) if selection_path else root/'proper-name-selection.json'
    plan=load(root/'input.json');selection=load(selection_path)
    current=[c for ep in m['episodes'] for c in load(root/'translation'/f'episode{ep:03d}.json')['cues'] if c['proper_names']]
    if selection['cues']!=current:raise ValueError('Name selection differs from current canonical cues; import editorial work first')
    if not current:
        identity=digest(dict(selection=sha(selection_path),input=sha(root/'input.json'),worker=sha(__file__)))
        out=root/'name-ocr'/identity[:16];out.mkdir(parents=True,exist_ok=True)
        atomic(out/'report.json',dict(status='NOT_REQUIRED',identity=identity,created_utc=utc(),items=[],files={},reason='No proper-name cues; GPU OCR was not initialized.'))
        atomic(root/'active-name-ocr.json',dict(identity=identity,report=str(out/'report.json'),report_sha256=sha(out/'report.json')))
        print('Name OCR NOT_REQUIRED: zero selected cues',flush=True)
        return load(out/'report.json')
    geometry=load(root/'geometry.json')
    source_rows=load(root/'download/download_report.json')['episodes'];sources={r['episode']:r for r in source_rows}
    identity=digest(dict(selection=sha(selection_path),input=sha(root/'input.json'),geometry=sha(root/'geometry.json'),sources={r['episode']:r['validation']['sha256'] for r in source_rows},worker=sha(__file__),det=env['ocr_det_model'],rec=env['ocr_rec_model'],device='gpu:0',precision='fp32',runtime_lock=sha(root/'runtime-lock.json'),ffmpeg_sha256=sha(env['ffmpeg'])))
    out=root/'name-ocr'/identity[:16];out.mkdir(parents=True,exist_ok=True);jobs=[];tick=time.monotonic()
    with wait_gpu(env['gpu_lock']):
        for ep in m['episodes']:
            selected=[c for c in selection['cues'] if c['episode']==ep]
            if not selected:continue
            row=sources[ep];source=Path(row['path'])
            if sha(source)!=row['validation']['sha256']:raise ValueError('Source hash changed')
            geom=geometry[str(ep)]
            if geom.get('verified') is not True or geom['source_sha256']!=row['validation']['sha256'] or any(sha(e['path'])!=e['sha256'] for e in geom['evidence']):raise ValueError('Measured OCR geometry evidence changed')
            from hongguo_lite.media import geometry_box
            stream=next(s for s in row['validation']['probe']['streams'] if s['codec_type']=='video')
            box=geometry_box(dict(sha256=row['validation']['sha256'],width=stream['width'],height=stream['height']),geom)
            boundary=next(b for b in plan['episode_boundaries'] if b['episode']==ep)
            offset=round(boundary['start_seconds']*30)
            byframe={}
            for c in selected:
                frame=(c['start_frame']+c['end_frame_exclusive'])//2-offset
                byframe.setdefault(frame,[]).append(c)
            frames=sorted(byframe);folder=out/f'ep{ep:03d}';folder.mkdir(exist_ok=True)
            cache=folder/'extraction.json'
            expected=dict(source_sha256=row['validation']['sha256'],frames=frames,worker_identity=identity)
            valid=False
            if cache.exists():
                old=load(cache)
                cached_images=old.get('images',[])
                valid=(old.get('inputs')==expected and len(cached_images)==len(frames)
                       and set(old.get('files',{}))==set(cached_images)
                       and all(Path(p).is_file() and sha(p)==h for p,h in old.get('files',{}).items()))
            if not valid:
                attempt=folder/f'attempt-{time.time_ns()}';attempt.mkdir()
                select='+'.join(f'eq(n\\,{n})' for n in frames)
                subprocess.run([env['ffmpeg'],'-v','error','-nostdin','-n','-threads','2','-i',str(source),'-vf',f'select={select}','-fps_mode','vfr','-frames:v',str(len(frames)),str(attempt/'frame-%04d.png')],check=True)
                images=sorted(attempt.glob('frame-*.png'))
                if len(images)!=len(frames):raise ValueError('Selected native frame coverage incomplete')
                atomic(cache,dict(inputs=expected,files={str(p):sha(p) for p in images},images=[str(p) for p in images]))
            images=load(cache)['images']
            for frame,image in zip(frames,images):
                for c in byframe[frame]:
                    jobs.append(dict(cue_id=c['cue_id'],episode=ep,frame=frame,image=image,image_sha256=sha(image),source_sha256=row['validation']['sha256'],source_text=c['source_text'],canonical_text=c['canonical_text'],proper_names=c['proper_names'],crop_box=box))
            print(f'Extracted EP{ep:03d}: {len(frames)} native name frames',flush=True)
        dirs=[Path(p) for p in env.get('ocr_dll_directories',[])]
        handles=[os.add_dll_directory(str(p)) for p in dirs if p.is_dir()] if os.name=='nt' else []
        os.environ['PATH']=os.pathsep.join(str(p) for p in dirs)+os.pathsep+os.environ['PATH']
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
        import paddle
        if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count()<1:raise RuntimeError('Required Paddle GPU unavailable; no CPU fallback')
        from paddleocr import PaddleOCR
        from PIL import Image
        engine=PaddleOCR(text_detection_model_name='PP-OCRv5_mobile_det',text_recognition_model_name='PP-OCRv5_mobile_rec',text_detection_model_dir=env['ocr_det_model'],text_recognition_model_dir=env['ocr_rec_model'],use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,device='gpu:0',precision='fp32',enable_mkldnn=False)
        for i,job in enumerate(jobs,1):
            result_path=out/(job['cue_id']+'.json')
            if result_path.exists():
                old=load(result_path)
                if old.get('input')==job and old.get('identity')==identity and sha(out/(job['cue_id']+'-band.png'))==old.get('crop_sha256'):continue
                raise ValueError('Existing OCR snapshot mismatch; create new generation')
            box=job['crop_box'];origin=[box['x'],box['y']]
            with Image.open(job['image']) as image:crop=image.convert('RGB').crop((box['x'],box['y'],box['x']+box['width'],box['y']+box['height']))
            crop_path=out/(job['cue_id']+'-band.png');crop.save(crop_path)
            result=[r.json for r in engine.predict(str(crop_path))]
            atomic(result_path,dict(identity=identity,input=job,crop_origin=origin,crop_sha256=sha(crop_path),ocr=result,review_status='AWAITING_EDITORIAL_NAME_DECISION'))
            if i%10==0:print(f'GPU OCR {i}/{len(jobs)}',flush=True)
        evidence_files=[out/(j['cue_id']+suffix) for j in jobs for suffix in ['.json','-band.png']]
        atomic(out/'report.json',dict(status='AWAITING_EDITORIAL_NAME_REVIEW',identity=identity,created_utc=utc(),items=jobs,seconds=time.monotonic()-tick,device=paddle.device.get_device(),gpu_name=paddle.device.cuda.get_device_name(),precision='fp32',files={str(p):sha(p) for p in evidence_files}))
        atomic(root/'active-name-ocr.json',dict(identity=identity,report=str(out/'report.json'),report_sha256=sha(out/'report.json')))
        atomic(out/'decisions-draft.json',dict(reviewer='',ocr_report_sha256=sha(out/'report.json'),evidence=[dict(path=str(out/'report.json'),sha256=sha(out/'report.json'))],glossary=load(root/'glossary.json'),decisions=[dict(cue_id=j['cue_id'],decision='PENDING',readable=None,observed_name='',canonical_text=j['canonical_text'],proper_names=j['proper_names'],reason='',evidence=[dict(path=j['image'],sha256=j['image_sha256'])]) for j in jobs]))
        print(f'OCR complete: {len(jobs)} cues; manual name verdicts pending',flush=True)
        print('Name decisions draft:',out/'decisions-draft.json',flush=True)
        return load(out/'report.json')


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--selection',type=Path)
    args=parser.parse_args()
    with process_lock(args.run/'name-ocr.lock'):run(args.run,args.selection)


if __name__=='__main__':main()
