"""Bounded Ava synthesis after editorial source/name acceptance, measured PCM fits."""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,digest,process_lock,utc
from hongguo_lite import edge_fit
from hongguo_lite.season_contracts import name_decisions, unsealed


async def synthesize(root):
    unsealed(root)
    import edge_tts
    import importlib.metadata
    plan=load(root/'input.json');config=load(root/'config.json');scope=load(root/'manifest.json')['episodes']
    if sha(plan['source_srt'])!=plan['source_srt_sha256']:raise ValueError('Raw source changed')
    cues=[]
    for ep in scope:
        doc=load(root/'translation'/f'episode{ep:03d}.json')
        for c in doc['cues']:
            if c['translation_status']!='CONTEXTUAL_CANONICAL':raise ValueError('Unresolved translation')
            cues.append(c)
    decisions_sha256=sha(root/'name-decisions.json')
    decisions=name_decisions(root, cues)
    if sha(root/'name-decisions.json')!=decisions_sha256:raise ValueError('Name decisions changed during speech binding')
    originals={c['cue_id']:c for c in plan['cues']}
    for c in cues:
        original=originals.get(c['cue_id'])
        if original is None or any(c[k]!=original[k] for k in ['source_cue_id','source_text','episode','start_frame','end_frame_exclusive']):raise ValueError('Source lineage/timing changed')
    if config['voice']!='en-US-AvaMultilingualNeural' or config['rate']!='+0%' or config['min_tempo']!=1.25 or config['max_tempo']!=5:raise ValueError('Wrong voice/fit policy')
    version=importlib.metadata.version('edge-tts');sem=asyncio.Semaphore(4);blocked=asyncio.Event();tick=time.monotonic();completed=0
    native={}
    async def one(c):
        nonlocal completed
        async with sem:
            if blocked.is_set():return
            identity=digest(dict(backend='edge-tts',version=version,voice=config['voice'],rate='+0%',text=c['canonical_text'],pcm='mono_s16_48000',python_sha256=sha(sys.executable),ffmpeg_sha256=sha(config['ffmpeg']),worker_sha256=sha(__file__)))
            out=root/'audio'/identity;out.mkdir(parents=True,exist_ok=True);receipt=out/'receipt.json';mp3=out/'edge-native.mp3';pcm=out/'audio-48000.wav'
            if receipt.exists():
                saved=load(receipt)
                if saved.get('identity')!=identity or sha(mp3)!=saved.get('native_sha256') or sha(pcm)!=saved.get('audio_sha256'):raise ValueError('Cached native speech corrupted')
            else:
                success=False
                for attempt in range(3):
                    attempt_mp3=out/f'attempt-{time.time_ns()}.mp3'
                    try:
                        await asyncio.wait_for(edge_tts.Communicate(c['canonical_text'],config['voice'],rate='+0%',volume='+0%',pitch='+0Hz').save(str(attempt_mp3)),90)
                        attempt_pcm=out/f'attempt-{time.time_ns()}.wav'
                        await asyncio.to_thread(subprocess.run,[config['ffmpeg'],'-v','error','-nostdin','-n','-i',str(attempt_mp3),'-ar','48000','-ac','1','-c:a','pcm_s16le',str(attempt_pcm)],check=True,capture_output=True)
                        import shutil
                        shutil.copyfile(attempt_mp3,mp3);shutil.copyfile(attempt_pcm,pcm)
                        atomic(receipt,dict(identity=identity,text=c['canonical_text'],voice=config['voice'],rate='+0%',edge_version=version,native_sha256=sha(mp3),audio_sha256=sha(pcm),created_utc=utc(),source_cue_id=c['cue_id']))
                        success=True;break
                    except Exception as exc:
                        atomic(out/f'failure-{time.time_ns()}.json',dict(cue_id=c['cue_id'],attempt=attempt,error_type=type(exc).__name__,status=getattr(exc,'status',None)))
                        if getattr(exc,'status',None) in [401,403,429]:blocked.set();raise RuntimeError('Edge service rejected request; no automatic backend substitution') from exc
                        if attempt==2:blocked.set();raise
                        await asyncio.sleep(2*(attempt+1))
                if not success:raise RuntimeError('Native synthesis failed')
            native[c['cue_id']]=dict(audio=str(pcm),sha256=sha(pcm),receipt=str(receipt),receipt_sha256=sha(receipt))
            completed+=1
            if completed%25==0:print(f'Ava native {completed}/{len(cues)}',flush=True)
    # Avoid creating duplicate writers for identical text.
    bytext={}
    for c in cues:bytext.setdefault(c['canonical_text'],c)
    results=await asyncio.gather(*(one(c) for c in bytext.values()),return_exceptions=True)
    errors=[r for r in results if isinstance(r,BaseException)]
    if errors:
        atomic(root/'speech-blocker.json',dict(status='BLOCKED',error_type=type(errors[0]).__name__,completed_native=completed,total=len(bytext),created_utc=utc()))
        raise errors[0]
    for c in cues:
        representative=bytext[c['canonical_text']];native[c['cue_id']]=native[representative['cue_id']]
    summary=[]
    for ep in scope:
        local=[c for c in cues if c['episode']==ep];boundary=next(b for b in plan['episode_boundaries'] if b['episode']==ep);last_frame=round(boundary['end_seconds']*30);rows=[]
        for i,c in enumerate(local):
            next_start=local[i+1]['start_frame'] if i+1<len(local) else last_frame
            signature=digest(dict(cue=c,native=native[c['cue_id']],next_start=next_start,config=config,fit_code=sha(PROJECT/'hongguo_lite/edge_fit.py')))
            out=root/'fitted'/signature;out.mkdir(parents=True,exist_ok=True);receipt=out/'fit.json'
            if receipt.exists():
                row=load(receipt)
                if row.get('status')=='PASS' and sha(row['path'])!=row['sha256']:raise ValueError('Fit cache corrupted')
            else:row=await asyncio.to_thread(edge_fit.fit,c,native[c['cue_id']],next_start,plan['total_frames'],'30',out,{'ffmpeg':config['ffmpeg']},max_tempo=5,min_tempo=1.25)
            rows.append(row)
        atomic(root/f'synthesis-summary-ep{ep:03d}.json',dict(processed=len(rows),rows=rows,needs_edit=[r['cue_id'] for r in rows if r['status']!='PASS']))
        summary+=rows
        print(f'Fit EP{ep:03d}: {sum(r["status"]=="PASS" for r in rows)}/{len(rows)}',flush=True)
    result=dict(status='NEEDS_EDIT' if any(r['status']!='PASS' for r in summary) else 'PASS',cues=len(cues),unique_native=len(bytext),seconds=time.monotonic()-tick,needs_edit=[r['cue_id'] for r in summary if r['status']!='PASS'])
    if result['status']=='PASS':
        if sha(root/'name-decisions.json')!=decisions_sha256 or any(sha(p)!=h for p,h in decisions['canonical_snapshot'].items()):
            raise ValueError('Canonical/name decisions changed during synthesis; preserve attempts and rerun speech')
        result.update(canonical_snapshot=decisions['canonical_snapshot'],name_decisions_sha256=decisions_sha256)
    atomic(root/'speech-summary.json',result)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args()
    with process_lock(a.run/'speech.lock'):asyncio.run(synthesize(a.run.resolve()))
