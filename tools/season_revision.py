"""Render only declared canonical revisions; preserve verified unaffected episodes."""
import argparse
import shutil
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,digest,process_lock
from hongguo_lite.store import Store
from hongguo_lite import media,visual_midpoint
from hongguo_lite.season_contracts import episode_map, unsealed


def main(root):
    unsealed(root)
    revision=load(root/'canonical-revision.json');base=revision['base_video'];base_root=Path(base['run']);old=load(base_root/'dependency-snapshot.json');allowed=set(revision['changed_files'])
    # The editable request is a pointer. Previous generations retain their own
    # immutable ledger; changing the pointer must not invalidate a second edit.
    old.pop(str(root/'canonical-revision.json'),None)
    if not (base_root/'video-complete.json').exists():raise ValueError('Base video build incomplete')
    for path,h in old.items():
        if sha(path)!=h and path not in allowed:raise ValueError(f'Undeclared dependency change: {path}')
    for path,record in revision['changed_files'].items():
        if record['before_sha256']!=old[path] or record['after_sha256']!=sha(path):raise ValueError('Canonical revision ledger mismatch')
    files={p:sha(p) for p in old};files[str(Path(__file__))]=sha(__file__)
    identity=digest(dict(dependencies=files,revision=revision));run=root/'video-revisions'/identity[:16];run.mkdir(parents=True,exist_ok=True)
    ledger=run/'revision.json'
    if ledger.exists() and load(ledger)!=revision:raise ValueError('Immutable revision ledger changed')
    atomic(ledger,revision);files[str(ledger)]=sha(ledger)
    snapshot=run/'dependency-snapshot.json'
    if snapshot.exists() and load(snapshot)!=files:raise ValueError('Revision generation snapshot changed')
    atomic(snapshot,files)
    m=load(root/'manifest.json');env=m['environment'];preset=load(root/'preset.json');plan=load(root/'input.json');geometry=load(root/'geometry.json');mapping={};changed=revision['episodes']
    if len(set(changed))!=len(changed) or not set(changed)<=set(m['episodes']):raise ValueError('Revision episode scope invalid')
    previous_mapping=episode_map(base_root,m['episodes'])
    for ep in m['episodes']:
        location=run/'episode-stores'/f'ep{ep:03d}' if ep in changed else Path(previous_mapping[str(ep)]);mapping[str(ep)]=str(location)
        shutil.copyfile(base_root/f'ep{ep:03d}-source.json',run/f'ep{ep:03d}-source.json')
        if ep not in changed and (base_root/f'ep{ep:03d}-name-frames.json').exists():shutil.copyfile(base_root/f'ep{ep:03d}-name-frames.json',run/f'ep{ep:03d}-name-frames.json')
    atomic(run/'episode-map.json',mapping)
    def one(ep):
        src=load(run/f'ep{ep:03d}-source.json');offset=round(next(b for b in plan['episode_boundaries'] if b['episode']==ep)['start_seconds']*30);local=[]
        for c in load(root/'translation'/f'episode{ep:03d}.json')['cues']:
            c=dict(c);c['start_frame']-=offset;c['end_frame_exclusive']-=offset;local.append(c)
        store=Store(mapping[str(ep)]);geom=geometry[str(ep)]
        video=store.stage(f'ep{ep:03d}/video',dict(source=src['sha256'],cues=local,geometry=geom,preset=preset,dependencies=files),lambda out:media.render_video(src,local,geom,preset,out/'video',env))
        selected=[c for c in local if c['proper_names']]
        if selected:
            frames=store.stage(f'ep{ep:03d}/name-frames',dict(video=video['sha256'],layout=sha(video['layout']),selected=selected),lambda out:visual_midpoint.extract(video['output'],src,selected,video['layout'],out/'midpoint',env,selection_policy='proper-name-cues-only'));atomic(run/f'ep{ep:03d}-name-frames.json',frames)
        print(f'Canonical revision EP{ep:03d} encoded',flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(one,changed))
    # Retain actual reviews only when their complete encoded episode is unchanged.
    previous=load(base_root/'name-visual-review.json');items=[i for i in previous['items'] if i['episode'] not in changed]
    expected=sum(bool(c['proper_names']) for ep in m['episodes'] for c in load(root/'translation'/f'episode{ep:03d}.json')['cues'])
    atomic(run/'name-visual-review.json',dict(status='NOT_REQUIRED' if not expected else ('PASS' if len(items)==expected else 'PARTIAL'),video_generation=identity,reviewed_count=len(items),expected_count=expected,items=items))
    atomic(run/'video-complete.json',dict(status='AWAITING_REVISED_EPISODE_NAME_REVIEW',episodes=m['episodes'],changed_episodes=changed,base_generation=base['identity']))
    atomic(root/'active-video-generation.json',dict(identity=identity,run=str(run),per_episode_stores=True,episode_map=str(run/'episode-map.json')))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args()
    with process_lock(a.run/'revision.lock'):main(a.run.resolve())
