"""Package measured native media through an atomic, retryable delivery stage."""
import argparse
from pathlib import Path
import shutil
import sys
import time

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from hongguo_lite.common import load,atomic,sha,utc,process_lock
from hongguo_lite import media
from hongguo_lite.store import Store
from hongguo_lite.season_contracts import episode_map,validate_review,unsealed,name_decisions

EVIDENCE=['name-decisions.json','glossary.json','dialogue-dispositions.json','geometry.json',
          'native-clock-conform.json','canonical-summary.json','speech-summary.json',
          'source-corrections.json','runtime-lock.json','preset.json']
balanced=media.balanced


def preflight(root):
    """Validate every required input before creating a delivery directory."""
    unsealed(root)
    m=load(root/'manifest.json');active=load(root/'active-generation.json');run=Path(active['run'])
    plan=load(root/'input.json')
    if sha(plan['source_srt'])!=plan['source_srt_sha256']:raise ValueError('Original captions changed')
    video_active=load(root/'active-video-generation.json');video_run=Path(video_active['run'])
    for path,h in load(run/'dependency-snapshot.json').items():
        if sha(path)!=h:raise ValueError('Generation dependency changed')
    complete=load(run/'build-complete.json')
    if complete['episodes']!=m['episodes'] or complete.get('generation')!=active['identity']:
        raise ValueError('Final generation build incomplete')
    episodes=load(run/'episodes.json');review=load(video_run/'name-visual-review.json')
    if [e['source']['episode'] for e in episodes]!=m['episodes']:raise ValueError('Final episode scope/order mismatch')
    name_decisions(root,[c for e in episodes for c in e['cues']])
    validate_review(review,episodes,video_active['identity'])
    pts_path=root/'native-pts-qa-v002/report.json';pts=load(pts_path)
    if pts['status']!='PASS' or [r['episode'] for r in pts['episodes']]!=m['episodes']:
        raise ValueError('Native sample timing QA failed')
    for e,row in zip(episodes,pts['episodes']):
        if row.get('status')!='PASS' or row['source_sha256']!=e['source']['sha256'] or row['video_samples']!=e['source']['frames']:
            raise ValueError('Source timing QA is stale')
    preset=load(root/'preset.json');env=m['environment'];mapping=episode_map(video_run,m['episodes'])
    evidence={name:root/name for name in EVIDENCE}
    evidence.update({'name-visual-review.json':video_run/'name-visual-review.json','native-pts-qa.json':pts_path,
                     'separation-engine-lock.json':Path(env['separation_engine_lock'])})
    for p in evidence.values():
        if not p.is_file():raise FileNotFoundError(p)
    points=[];offset=0;fits=[];lineage=[];production=Store(run);backgrounds=Store(root/'media-store')
    for e in episodes:
        ep=e['source']['episode'];rows=load(root/f'synthesis-summary-ep{ep:03d}.json')['rows']
        if len(rows)!=len(e['cues']):raise ValueError('Speech/canonical coverage differs')
        for cue,row in zip(e['cues'],rows):
            if (cue['cue_id']!=row['cue_id'] or cue['canonical_text']!=row['canonical_text']
                    or cue['start_frame']+offset!=row['start_frame'] or cue['end_frame_exclusive']+offset!=row['end_frame_exclusive']
                    or row['status']!='PASS' or not 1.25<=row['tempo']<=5
                    or row['samples']>row['available_samples'] or sha(row['path'])!=row['sha256']):
                raise ValueError('Speech timing/canonical/fit/hash failed')
            if row['start_sample']!=row['start_frame']*1600 or row['end_sample']!=row['start_sample']+row['samples']:raise ValueError('Speech sample timing differs')
            media._core()[0].pcm_info(row['path'],channels=1,samples=row['samples'])
        video=Store(mapping[str(ep)]).result(f'ep{ep:03d}/video')
        voice=production.result(f'ep{ep:03d}/voice');mix=production.result(f'ep{ep:03d}/mix')
        separation=backgrounds.result(f'ep{ep:03d}/separation')
        if video!=e['video'] or mix!=e['audio_master'] or voice!=e.get('voice') or separation!=e.get('separation'):
            raise ValueError('Episode media binding differs from verified stage receipts')
        if separation['included_stems']!=preset['separation']['included_stems']:raise ValueError('Separation stem selection changed')
        response=load(separation['receipt'])
        if response.get('included_stems')!=separation['included_stems'] or response.get('output_sha256')!=sha(separation['output']):
            raise ValueError('Separation receipt/stem binding differs')
        if sha(e['source']['path'])!=e['source']['sha256']:raise ValueError('Native source changed')
        for r in review['items']:
            if r['episode']!=ep:continue
            if r['video_sha256']!=video['sha256'] or sha(r['image'])!=r['image_sha256']:raise ValueError('Reviewed frame/video changed')
            points.append(dict(cue_id=r['cue_id'],frame=offset+r['frame'],expected_sha256=r['image_sha256']))
        lineage.append(dict(episode=ep,source_sha256=e['source']['sha256'],video_sha256=video['sha256'],
                            voice_sha256=voice['sha256'],master_sha256=mix['sha256'],
                            separation_receipt_sha256=sha(separation['receipt']),included_stems=separation['included_stems'],
                            cues=[dict(cue=cue,speech=row) for cue,row in zip(e['cues'],rows)]))
        fits+=rows;offset+=e['source']['frames']
    return m,active,run,video_run,episodes,review,preset,evidence,points,fits,lineage


def main(root):
    tick=time.monotonic()
    m,active,run,video_run,episodes,review,preset,evidence,points,fits,lineage=preflight(root)
    env=m['environment'];store=Store(run)
    scope=f"EP{m['episodes'][0]:03d}-{m['episodes'][-1]:03d}"
    directory=Path(env.get('deliveries_root',PROJECT/'deliveries'))/f"{m['series_id']}-{scope}-EN-Ava-DRAFT-{root.name}"
    if directory.exists():
        # Recover a crash between the atomic directory commit and the run
        # pointer update. Never replace bytes in the committed delivery.
        saved=load(directory/'delivery-report.json');existing=store.result('combined-final')
        if (saved.get('generation')!=active['identity'] or saved.get('episodes')!=m['episodes']
                or saved.get('video_sha256')!=existing['sha256'] or saved.get('srt_sha256')!=sha(existing['srt'])
                or Path(saved['video']).parent!=directory or Path(saved['srt']).parent!=directory
                or sha(saved['video'])!=saved['video_sha256'] or sha(saved['srt'])!=saved['srt_sha256']):
            raise FileExistsError('Existing delivery differs; preserve it and choose a new versioned run')
        for name,p in evidence.items():
            if sha(directory/name)!=sha(p):raise ValueError('Committed delivery evidence differs')
        if load(directory/'cue-lineage.json')!=dict(episodes=lineage,generation=active['identity']):raise ValueError('Committed lineage differs')
        if sha(directory/'technical-qa.json')!=sha(existing['qa']):raise ValueError('Committed technical QA differs')
        atomic(root/'delivery.json',saved)
        print(saved['video'],flush=True)
        return saved
    result=store.stage('combined-final',dict(generation=active['identity'],episodes=episodes,review_sha256=sha(video_run/'name-visual-review.json'),package_code=sha(__file__)),lambda out:media.finalize(episodes,out/'final',env,preset))
    expected_frames=sum(e['source']['frames'] for e in episodes)
    if (not result['technical_PASS'] or result['frames']!=expected_frames or result['pcm_samples']!=media.samples_at_frame(expected_frames,'30')
            or len(result['seams'])!=len(episodes)-1):raise ValueError('Final technical/native/seam QA failed')
    points=sorted(points,key=lambda r:r['frame']);images=[]
    if points:
        frames=run/'combined-name-frames';frames.mkdir(exist_ok=True);receipt=frames/'receipt.json'
        if receipt.exists():
            cached=load(receipt)
            if cached['video_sha256']!=result['sha256'] or cached['points']!=points or any(sha(p)!=h for p,h in cached['files'].items()):raise ValueError('Combined-frame snapshot changed')
            images=[Path(p) for p in cached['images']]
        else:
            attempt=frames/f'attempt-{time.time_ns()}';attempt.mkdir()
            expr=balanced([f'eq(n\\,{p["frame"]})' for p in points])
            media._run([env['ffmpeg'],'-v','error','-nostdin','-n','-threads','2','-i',result['output'],'-vf',f'select={expr}','-fps_mode','vfr',attempt/'frame-%05d.png'],attempt/'logs')
            images=sorted(attempt.glob('frame-*.png'))
            if len(images)!=len(points):raise ValueError('Combined name coverage incomplete')
            atomic(receipt,dict(video_sha256=result['sha256'],points=points,images=[str(p) for p in images],files={str(p):sha(p) for p in images}))
        if len(images)!=len(points):raise ValueError('Combined name coverage incomplete')
        for point,image in zip(points,images):
            if sha(image)!=point['expected_sha256']:raise ValueError('Combined pixels differ from reviewed episode')
    import srt
    subtitles=list(srt.parse(Path(result['srt']).read_text(encoding='utf-8')))
    if [s.content for s in subtitles]!=[c['canonical_text'] for e in episodes for c in e['cues']]:raise ValueError('Combined SRT text differs')
    evidence['technical-qa.json']=Path(result['qa'])
    for p in evidence.values():
        if not p.is_file():raise FileNotFoundError(p)
    staging=directory.parent/'.staging'/f'{directory.name}-{time.time_ns()}';staging.mkdir(parents=True)
    mp4_name=f"{m['series_id']}-{scope}-EN-Ava-DRAFT.mp4";srt_name=f"{m['series_id']}-English.srt"
    shutil.copy2(result['output'],staging/mp4_name);shutil.copy2(result['srt'],staging/srt_name)
    if sha(staging/mp4_name)!=result['sha256'] or sha(staging/srt_name)!=sha(result['srt']):raise ValueError('Delivery copy hash mismatch')
    for name,p in evidence.items():
        shutil.copy2(p,staging/name)
        if sha(staging/name)!=sha(p):raise ValueError('Delivery evidence copy changed')
    atomic(staging/'cue-lineage.json',dict(episodes=lineage,generation=active['identity']))
    report=dict(status='DRAFT',series_id=m['series_id'],series_title=m.get('series_title',''),episodes=m['episodes'],frames=result['frames'],duration_seconds=result['frames']/30,pcm_samples=result['pcm_samples'],cue_count=len(subtitles),name_cues=len(points),seams=len(result['seams']),background_db=preset['mix']['background_db'],included_stems=preset['separation']['included_stems'],voice='en-US-AvaMultilingualNeural',native_rate='+0%',measured_tempo_min=min((r['tempo'] for r in fits),default=None),measured_tempo_max=max((r['tempo'] for r in fits),default=None),artifact_ready=True,technical_pass=True,name_qa_pass=True,name_review_status=review['status'],listening_status='NOT_PERFORMED',semantic_status='NOT_PERFORMED',external_published=False,video=str(directory/mp4_name),video_sha256=result['sha256'],srt=str(directory/srt_name),srt_sha256=sha(result['srt']),name_pixels='ALL_EXACT_MATCH' if points else 'NOT_REQUIRED',completed_utc=utc(),assembly_qa_seconds=time.monotonic()-tick,generation=active['identity'])
    atomic(staging/'delivery-report.json',report)
    atomic(staging/'timing-report.json',dict(speech=load(root/'speech-summary.json'),assembly_qa_seconds=report['assembly_qa_seconds'],note='Parallel stage timings overlap; no manual listening or translation time inferred.'))
    (staging/'QA-report.md').write_text(f"# {m['series_id']} — {scope}\n\nDRAFT: {result['frames']} native CFR30 frames, {len(subtitles)} cues, {len(result['seams'])} PCM joins. Included background stems: {preset['separation']['included_stems']}; gain {preset['mix']['background_db']} dB. Name review: {review['status']}, {len(points)} exact encoded frames.\n\nListening and independent semantic review NOT_PERFORMED. External publication false.\n",encoding='utf-8')
    staging.rename(directory)
    atomic(root/'delivery.json',report)
    print(report['video'],flush=True)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);a=p.parse_args()
    with process_lock(a.run/'package.lock'):main(a.run.resolve())
