"""Season-scoped source/background and canonical render stages with verified Store."""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,digest,process_lock
from hongguo_lite.store import Store
from hongguo_lite import source,media,speech,visual_midpoint
from hongguo_lite.season_contracts import name_decisions, unsealed


def dependencies(root,env):
    files=[root/'manifest.json',root/'input.json',root/'preset.json',root/'runtime-lock.json',root/'native-clock-conform.json',root/'download/download_report.json',Path(__file__)]
    files += [Path(env[k]) for k in ['ffmpeg','ffprobe','font','logo','wordmark','separation_python','separation_engine_lock','separation_checkpoint']]
    files += [PROJECT/'hongguo_lite'/f for f in ['media.py','source.py','speech.py','visual_midpoint.py','edge_fit.py','store.py']]
    files += [PROJECT/'vendor/core/tools'/f for f in ['fresh_separation_v3_worker.py','fresh_engine_worker.py','fresh_v3_guard.py']]
    return {str(p):sha(p) for p in files}


def background(root,m,env,preset):
    unsealed(root)
    store=Store(root/'media-store');report=load(root/'download/download_report.json');report_path=root/'download/download_report.json'
    locks={k:sha(env[k]) for k in ['ffmpeg','ffprobe','separation_python','separation_engine_lock','separation_checkpoint']}
    code={str(p):sha(p) for p in [PROJECT/'hongguo_lite/media.py',PROJECT/'vendor/core/tools/fresh_separation_v3_worker.py',PROJECT/'vendor/core/tools/fresh_engine_worker.py',PROJECT/'vendor/core/tools/fresh_v3_guard.py']}
    for ep in m['episodes']:
        r=next(r for r in report['episodes'] if r['episode']==ep)
        row=dict(episode=ep,path=r['path'],sha256=r['validation']['sha256'],fresh_download=True,adapter_validation=dict(report=str(report_path),report_sha256=sha(report_path)))
        src=store.stage(f'ep{ep:03d}/source',dict(row=row,ffprobe=locks['ffprobe'],source_code=sha(PROJECT/'hongguo_lite/source.py')),lambda out:source.validate(row,out,env))
        scoped_source={**src,'audio_scope':preset['separation']['audio_scope']}
        bg=store.stage(f'ep{ep:03d}/separation',dict(source=src['sha256'],frames=src['frames'],audio_scope=scoped_source['audio_scope'],config=preset['separation'],locks=locks,code=code),lambda out:media.separate(scoped_source,out,env,preset['separation']))
        print(f'Background EP{ep:03d} verified',flush=True)
    atomic(root/'background-complete.json',dict(episodes=m['episodes'],statistics=store.stats))


def render(root,m,env,preset):
    unsealed(root)
    geometry=load(root/'geometry.json')
    name_decisions(root,[c for ep in m['episodes'] for c in load(root/'translation'/f'episode{ep:03d}.json')['cues']])
    if load(root/'speech-summary.json')['status']!='PASS':raise ValueError('Speech needs editing')
    files=dependencies(root,env)
    files.update({str(p):sha(p) for p in [root/'name-decisions.json',root/'dialogue-dispositions.json',root/'geometry.json',root/'config.json']+list((root/'translation').glob('episode*.json'))})
    identity=digest(files);run=root/'generations'/identity[:16];run.mkdir(parents=True,exist_ok=True)
    snapshot=run/'dependency-snapshot.json'
    if snapshot.exists() and load(snapshot)!=files:raise ValueError('Generation snapshot mismatch')
    atomic(snapshot,files);atomic(root/'active-generation.json',dict(identity=identity,run=str(run)))
    store=Store(run);sources=Store(root/'media-store');plan=load(root/'input.json');episodes=[]
    for ep in m['episodes']:
        cues=load(root/'translation'/f'episode{ep:03d}.json')['cues'];src=sources.result(f'ep{ep:03d}/source');bg=sources.result(f'ep{ep:03d}/separation')
        offset=round(next(b for b in plan['episode_boundaries'] if b['episode']==ep)['start_seconds']*30)
        summary=load(root/f'synthesis-summary-ep{ep:03d}.json');local=[];rows=[]
        if len(cues)!=len(summary['rows']):raise ValueError('Speech coverage mismatch')
        for c,row in zip(cues,summary['rows']):
            if c['cue_id']!=row['cue_id'] or c['canonical_text']!=row['canonical_text']:raise ValueError('Speech canonical binding mismatch')
            c=dict(c);c['start_frame']-=offset;c['end_frame_exclusive']-=offset;local.append(c)
            row=dict(row);row['start_frame']-=offset;row['end_frame_exclusive']-=offset;row['start_sample']-=offset*1600;row['end_sample']-=offset*1600;rows.append(row)
        geom=geometry[str(ep)]
        if not geom.get('verified') or geom['source_sha256']!=src['sha256']:raise ValueError('Geometry unverified or wrong source')
        voice=store.stage(f'ep{ep:03d}/voice',dict(rows=rows,source=src['sha256'],generation=identity),lambda out:speech.timeline(rows,src,out))
        if (root/'active-video-generation.json').exists():
            info=load(root/'active-video-generation.json');video_root=Path(info['run']);prepared=Store(video_root/'episode-stores'/f'ep{ep:03d}' if info.get('per_episode_stores') else video_root);snap=load(video_root/'dependency-snapshot.json')
            if any(sha(p)!=h for p,h in snap.items()):raise ValueError('Prepared video dependency changed')
            video=prepared.result(f'ep{ep:03d}/video')
        else:
            video=store.stage(f'ep{ep:03d}/video',dict(cues=local,geometry=geom,preset=preset,source=src['sha256'],generation=identity),lambda out:media.render_video(src,local,geom,preset,out/'video',env))
        mix=store.stage(f'ep{ep:03d}/mix',dict(voice=voice['sha256'],background=sha(bg['output']),preset=preset,generation=identity),lambda out:media.mix(src,voice['path'],bg['output'],out/'mix',env,preset))
        selected=[c for c in local if c['proper_names']]
        if selected:
            preview=store.stage(f'ep{ep:03d}/name-frames',dict(video=video['sha256'],layout=sha(video['layout']),cues=selected,generation=identity),lambda out:visual_midpoint.extract(video['output'],src,selected,video['layout'],out/'midpoint',env,selection_policy='proper-name-cues-only'))
            atomic(run/f'ep{ep:03d}-name-frames.json',preview)
        episodes.append(dict(source=src,video=video,audio_master=mix,cues=local))
        atomic(run/'episodes.json',episodes)
        print(f'Rendered EP{ep:03d}; name-frame review pending',flush=True)
    atomic(run/'build-complete.json',dict(episodes=m['episodes'],generation=identity,status='AWAITING_ENCODED_NAME_VISUAL_REVIEW'))


def video(root,m,env,preset):
    unsealed(root)
    geometry=load(root/'geometry.json');plan=load(root/'input.json')
    all_cues=[c for ep in m['episodes'] for c in load(root/'translation'/f'episode{ep:03d}.json')['cues']]
    name_decisions(root,all_cues)
    files=dependencies(root,env)
    files.update({str(p):sha(p) for p in [root/'name-decisions.json',root/'geometry.json',root/'source-corrections.json']+list((root/'translation').glob('episode*.json'))})
    identity=digest(files);run=root/'video-generations'/identity[:16];run.mkdir(parents=True,exist_ok=True)
    snapshot=run/'dependency-snapshot.json'
    if snapshot.exists() and load(snapshot)!=files:raise ValueError('Video snapshot mismatch')
    atomic(snapshot,files);atomic(root/'active-video-generation.json',dict(identity=identity,run=str(run),per_episode_stores=True))
    report=load(root/'download/download_report.json')
    def one_episode(ep):
        store=Store(run/'episode-stores'/f'ep{ep:03d}')
        row=next(r for r in report['episodes'] if r['episode']==ep)
        src=source.probe(row['path'],env)
        if src['sha256']!=row['validation']['sha256'] or row['validation']['fullDecodeExitCode']!=0:raise ValueError('Unverified source')
        src['episode']=ep
        geom=geometry[str(ep)]
        if not geom['verified'] or geom['source_sha256']!=src['sha256'] or any(sha(e['path'])!=e['sha256'] for e in geom['evidence']):raise ValueError('Geometry evidence changed')
        offset=round(next(b for b in plan['episode_boundaries'] if b['episode']==ep)['start_seconds']*30);local=[]
        for c in load(root/'translation'/f'episode{ep:03d}.json')['cues']:
            c=dict(c);c['start_frame']-=offset;c['end_frame_exclusive']-=offset;local.append(c)
        result=store.stage(f'ep{ep:03d}/video',dict(source=src['sha256'],geometry=geom,cues=local,preset=preset,generation=identity),lambda out:media.render_video(src,local,geom,preset,out/'video',env))
        atomic(run/f'ep{ep:03d}-source.json',src)
        selected=[c for c in local if c['proper_names']]
        if selected:
            frames=store.stage(f'ep{ep:03d}/name-frames',dict(video=result['sha256'],layout=sha(result['layout']),selected=selected,generation=identity),lambda out:visual_midpoint.extract(result['output'],src,selected,result['layout'],out/'midpoint',env,selection_policy='proper-name-cues-only'))
            atomic(run/f'ep{ep:03d}-name-frames.json',frames)
        print(f'Video EP{ep:03d} encoded; proper-name review pending',flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one_episode,m['episodes']))
    atomic(run/'video-complete.json',dict(episodes=m['episodes'],generation=identity,status='AWAITING_ENCODED_NAME_VISUAL_REVIEW'))
    if not any(c.get('proper_names') for c in all_cues):
        atomic(run/'name-visual-review.json',dict(status='NOT_REQUIRED',video_generation=identity,reviewed_count=0,expected_count=0,items=[]))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['background','video','render']);p.add_argument('--run',type=Path,required=True);a=p.parse_args();root=a.run.resolve();m=load(root/'manifest.json')
    with process_lock(root/(a.action+'.lock')):globals()[a.action](root,m,m['environment'],load(root/'preset.json'))
