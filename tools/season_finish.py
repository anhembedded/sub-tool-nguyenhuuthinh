"""Verify revised video dependencies, build native voice/mix, wait for owned GPU stages."""
import argparse
import sys
import time
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,digest,process_lock
from hongguo_lite.store import Store
from hongguo_lite import speech,media
from hongguo_lite.season_contracts import episode_map, unsealed


def main(root):
    unsealed(root)
    m=load(root/'manifest.json');env=m['environment'];preset=load(root/'preset.json');tick=time.monotonic()
    video_active=load(root/'active-video-generation.json');video_run=Path(video_active['run'])
    if not (video_run/'video-complete.json').exists():raise ValueError('Video build incomplete')
    mapping=episode_map(video_run,m['episodes'])
    files=load(video_run/'dependency-snapshot.json')
    for path,h in files.items():
        if sha(path)!=h:raise ValueError('Revised video dependency mismatch')
    if load(root/'speech-summary.json')['status']!='PASS':raise ValueError('Measured speech still needs editing')
    additional=[root/'speech-summary.json',root/'dialogue-dispositions.json',root/'glossary.json',root/'canonical-summary.json',root/'native-pts-qa-v002/report.json',Path(__file__),PROJECT/'tools/season_speech.py',PROJECT/'tools/season_package.py']+list(root.glob('synthesis-summary-ep*.json'))
    files.update({str(p):sha(p) for p in additional});identity=digest(files);run=root/'generations'/identity[:16];run.mkdir(parents=True,exist_ok=True)
    snapshot=run/'dependency-snapshot.json'
    if snapshot.exists() and load(snapshot)!=files:raise ValueError('Final generation snapshot changed')
    atomic(snapshot,files);atomic(root/'active-generation.json',dict(identity=identity,run=str(run)));store=Store(run);plan=load(root/'input.json');episodes=[]
    for ep in m['episodes']:
        video_store=Store(mapping[str(ep)]);video=video_store.result(f'ep{ep:03d}/video');src=load(video_run/f'ep{ep:03d}-source.json');cues=load(root/'translation'/f'episode{ep:03d}.json')['cues'];summary=load(root/f'synthesis-summary-ep{ep:03d}.json');offset=round(next(b for b in plan['episode_boundaries'] if b['episode']==ep)['start_seconds']*30);rows=[];local=[]
        if len(cues)!=len(summary['rows']):raise ValueError('Speech/caption coverage differs')
        for cue,row in zip(cues,summary['rows']):
            if any(cue[k]!=row[k] for k in ['cue_id','canonical_text','start_frame','end_frame_exclusive']) or row['status']!='PASS' or sha(row['path'])!=row['sha256']:raise ValueError('Measured voice binding failed')
            if row['start_sample']!=cue['start_frame']*1600 or row['end_sample']!=row['start_sample']+row['samples']:raise ValueError('Measured speech sample timing differs')
            media._core()[0].pcm_info(row['path'],channels=1,samples=row['samples'])
            cue=dict(cue);cue['start_frame']-=offset;cue['end_frame_exclusive']-=offset;local.append(cue)
            row=dict(row);row['start_frame']-=offset;row['end_frame_exclusive']-=offset;row['start_sample']-=offset*1600;row['end_sample']-=offset*1600;rows.append(row)
        layout=load(video['layout'])
        if layout['source_sha256']!=src['sha256']:raise ValueError('Encoded raster source differs')
        keys=['cue_id','canonical_text','start_frame','end_frame_exclusive']
        if [{k:c[k] for k in keys} for c in local]!=[{k:c[k] for k in keys} for c in layout['cues']]:raise ValueError('Encoded raster canonical/timing differs')
        voice=store.stage(f'ep{ep:03d}/voice',dict(rows=rows,source=src['sha256'],speech_code=sha(PROJECT/'hongguo_lite/speech.py')),lambda out:speech.timeline(rows,src,out))
        warned=False
        while True:
            backgrounds=Store(root/'media-store');stage=backgrounds.state['stages'].get(f'ep{ep:03d}/separation',{})
            if stage.get('status')=='COMPLETE':break
            if stage.get('status') in ['FAILED','BLOCKED']:raise RuntimeError(f'Background EP{ep} failed; inspect preserved stage')
            if not warned:print(f'Awaiting owned background EP{ep:03d}',flush=True);warned=True
            if time.monotonic()-tick>14400:raise TimeoutError('Background stage timeout')
            time.sleep(2)
        background=backgrounds.result(f'ep{ep:03d}/separation');source_bg=backgrounds.result(f'ep{ep:03d}/source')
        if source_bg['sha256']!=src['sha256']:raise ValueError('Background/source hash differs')
        if background.get('included_stems')!=preset['separation']['included_stems']:raise ValueError('Separation stem selection differs')
        mix=store.stage(f'ep{ep:03d}/mix',dict(voice=voice['sha256'],background=sha(background['output']),included_stems=background['included_stems'],preset=preset['mix'],media_code=sha(PROJECT/'hongguo_lite/media.py')),lambda out:media.mix(src,voice['path'],background['output'],out/'mix',env,preset))
        episodes.append(dict(source=src,video=video,audio_master=mix,cues=local,voice=voice,separation=background));atomic(run/'episodes.json',episodes);print(f'Native voice and {preset["mix"]["background_db"]} dB mix EP{ep:03d} verified',flush=True)
    atomic(run/'build-complete.json',dict(episodes=m['episodes'],status='AWAITING_FINAL_NAME_QA_AND_ASSEMBLY',generation=identity))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args()
    with process_lock(a.run/'finish.lock'):main(a.run.resolve())
